"""Mode-aware hot daily-limit edits; all exchange HTTP and processes are mocked."""
from dataclasses import asdict, replace
import json
import threading
from types import SimpleNamespace
import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_review_repairs import isolated  # noqa: F401
import bot_ops as ops
import execution_engine as ee
import live_bot as lb
import manual_trade as manual
import paper_accounts as pa
from exchange_client import DeltaRestClientAdapter
from step5_verify import FakeDelta, FakeClock, FAKE_KEY, FAKE_SECRET
from config import settings


@pytest.fixture(params=['paper','live','testnet'])
def daily(request,monkeypatch,tmp_path):
    mode=request.param
    monkeypatch.setattr(settings,'MODE','live')
    monkeypatch.setattr(settings,'LIVE_TRADING_ENABLED',True)
    for k in ops.PATHS: monkeypatch.setitem(ops.PATHS,k,tmp_path/('state.db' if k=='db' else k))
    monkeypatch.setattr(ops,'_signals',lambda:{})
    monkeypatch.setattr(manual,'_cache',{})
    clock=FakeClock(1794528000000000)
    fake=FakeDelta(clock);fake.wallet_usd=4026/85
    clients={ro:DeltaRestClientAdapter(mode if mode!='paper' else 'testnet',FAKE_KEY,FAKE_SECRET,
             transport=httpx.MockTransport(fake.handler),read_only=ro) for ro in (False,True)}
    monkeypatch.setattr(manual,'CLIENT_FACTORY',lambda mode,read_only:clients[read_only])
    monkeypatch.setattr(manual,'catalog',lambda:{s:dict(contract_value=float(p['contract_value']),tick_size=float(p['tick_size'])) for s,p in fake.meta.items()})
    monkeypatch.setattr(manual,'ticker',lambda sym,ttl=1:dict(mark_price=fake.prices[27]))
    profile=replace(ee.FROZEN_PROFILES['BTCUSD'],max_daily_loss_pct=5,fee_mode='taker',history_start=None)
    raw={'BTCUSD':asdict(profile),'ETHUSD':dict(asdict(profile),symbol='ETHUSD',enabled=False)}
    ee.save_bot_profiles_raw(raw)
    store=ee.StateStore(ops.mode_db(mode),clock=clock)
    engine=ee.ExecutionEngine(store,mode,clients[False] if mode!='paper' else None,profiles={'BTCUSD':profile},clock=clock)
    if mode=='paper': store.meta_set(utc_day_anchor_date='2026-11-13',utc_day_start_equity_usd=50000/85)
    engine.account_policy={'blocked':{}}
    app=FastAPI();app.include_router(ops.router)
    client=TestClient(app)
    yield SimpleNamespace(mode=mode,store=store,engine=engine,fake=fake,clock=clock,client=client,raw=raw,monkeypatch=monkeypatch)
    store.close();client.close()
    for c in clients.values():c.close()


def update(ctx,pct,route='/api/bot/settings',**extra):
    current=pa.describe(ctx.store.path,ctx.mode)
    body=dict(mode=ctx.mode,request_id=uuid.uuid4().hex,expected_session_id=current['session_id'],
              expected_revision=current['revision'],daily_loss_limit_pct=pct,**extra)
    response=ctx.client.put(route,json=body)
    assert response.status_code==200,response.text
    return response.json(),body


def test_stopped_roundtrip_preserves_capital_positions_intents_and_baseline(daily):
    ctx=daily
    if ctx.mode=='paper':ctx.engine.manual_fill('BTCUSD',1,3,68733.5,10)
    else:
        ctx.fake.positions[27]={'size':3,'entry_price':68733.5}
        snapshot=manual.exchange_snapshot(ctx.mode,ttl=0)
        manual._ensure_journal_position(SimpleNamespace(mode=ctx.mode,store=ctx.store,engine=ctx.engine),'BTCUSD',snapshot)
    ctx.store.create_intent(intent_id='resting',client_order_id='resting',mode=ctx.mode,symbol='ETHUSD',product_id=3136,
                            role='ENTRY',side='buy',size=1,order_type='limit_order',entry_price=2900)
    before=dict(ctx.store.config());position=dict(ctx.store.position(ctx.mode,'BTCUSD'))
    anchor=ctx.store.meta_get('utc_day_start_equity_usd')
    ctx.store.close()
    for value in (5,10,50,5):
        result,_=update(ctx,value)
        assert result['daily_loss_limit_pct']==value
        settings=ctx.client.get('/api/bot/settings',params={'mode':ctx.mode})
        assert settings.status_code==200,settings.text
        a=settings.json()['account_settings']
        assert a['daily_loss_limit_pct']==value
        assert a['allowance_inr']==pytest.approx(a['daily_baseline_inr']*value/100)
        with pa.connect(ctx.store.path) as c:
            cfg=dict(c.execute('select * from bot_config_state').fetchone())
            assert cfg['capital_inr']==before['capital_inr']
            assert dict(c.execute('select * from open_positions where symbol=?',('BTCUSD',)).fetchone())==position
            assert c.execute('select count(*) from order_intents').fetchone()[0]==1
            assert dict(c.execute('select key,value from system_meta'))['utc_day_start_equity_usd']==anchor
        for sym,profile in ee.load_bot_profiles_raw().items():
            assert profile['daily_loss_limit_pct']==profile['effective_config']['max_daily_loss_pct']==value
            for key in ('capital_inr','leverage','risk_pct','params','enabled'):
                assert profile[key]==ctx.raw[sym][key]
    assert not pa.pending_path(ctx.store.path).exists()
    assert all(c['method']=='GET' for c in ctx.fake.calls)


def test_running_hot_edit_reloads_engine_and_clears_only_daily_halt(daily):
    ctx=daily
    ctx.store.set_entries_paused(True)
    if ctx.mode=='paper':
        ctx.store.conn.execute('update bot_config_state set capital_inr=47000')
    else:
        ctx.fake.wallet_usd=3750/85
        ctx.engine.sync_exchange_wallet()
    ctx.store.set_halt(True,'daily_loss_limit hit')
    runner=object.__new__(lb.LiveBotRunner)
    runner._work=threading.RLock();runner.mode=ctx.mode;runner.store=ctx.store;runner.engine=ctx.engine
    runner.profiles=ctx.engine.profiles;runner.market=SimpleNamespace(profiles=runner.profiles)
    held=lb.InstanceLock(ops.PATHS['lock'],ctx.mode)
    ctx.monkeypatch.setattr(ops,'_send',lambda cmd,timeout,args:runner.handle_command(cmd,args))
    try:
        result,_=update(ctx,10)
        assert result['daily_halt_cleared'] and not result['halted']
        cfg=ctx.store.config()
        assert cfg['halted']==0 and cfg['halt_reason'] is None and cfg['halt_until_us'] is None
        assert cfg['entries_paused']==1
        assert ctx.engine.cfg_row['daily_loss_limit_pct']==10
        assert ee.profile_config(runner.profiles['BTCUSD']).max_daily_loss_pct==10
        assert not ctx.engine.check_daily_loss(50000 if ctx.mode=='paper' else 4026)
        update(ctx,50)
        assert ctx.engine.cfg_row['daily_loss_limit_pct']==50
        update(ctx,5)
        assert ctx.engine.check_daily_loss(50000 if ctx.mode=='paper' else 4026)
        result,_=update(ctx,50)
        assert result['daily_halt_cleared']
        ctx.store.set_halt(True,'4 consecutive losing trades')
        result,_=update(ctx,90)
        assert not result['daily_halt_cleared'] and ctx.store.config()['halted']==1
        assert ctx.store.config()['halt_reason']=='4 consecutive losing trades'
        # Capital reset still requires stopping the runner and a paper-only target.
        assert ctx.client.post('/api/bot/paper/sessions',json={**result['account_settings'],
            'request_id':uuid.uuid4().hex,'expected_session_id':result['session_id'],'expected_revision':result['revision'],
            'capital_inr':100000,'confirmed_equity_inr':47000}).status_code in (409,422)
    finally:held.release()
    assert all(c['method']=='GET' for c in ctx.fake.calls)


@pytest.mark.parametrize('limit',[0.5,1,4,5,10,25,50,90,95])
def test_valid_range(daily,limit):
    daily.store.close()
    response,_=update(daily,limit)
    assert response['daily_loss_limit_pct']==limit


@pytest.mark.parametrize('limit',[0,-1,0.49,95.01,100,'NaN','Infinity'])
def test_outside_range_never_mutates(daily,limit):
    ctx=daily;before=dict(ctx.store.config());raw=ee.BOT_PROFILES_FILE.read_bytes()
    ctx.store.close()
    c=pa.describe(ctx.store.path,ctx.mode)
    response=ctx.client.put('/api/bot/settings',json=dict(mode=ctx.mode,request_id=uuid.uuid4().hex,
        expected_session_id=c['session_id'],expected_revision=c['revision'],daily_loss_limit_pct=limit))
    assert response.status_code==422
    with pa.connect(ctx.store.path) as connection:
        assert dict(connection.execute('select * from bot_config_state').fetchone())==before
    assert ee.BOT_PROFILES_FILE.read_bytes()==raw


def test_retry_stale_revision_and_aliases(daily):
    ctx=daily;ctx.store.close()
    first,body=update(ctx,10,route='/api/bot/paper/settings')
    assert ctx.client.put('/api/bot/settings',json=body).json()==first
    stale={**body,'request_id':uuid.uuid4().hex,'daily_loss_limit_pct':50}
    assert ctx.client.put('/api/bot/settings',json=stale).status_code==409
    assert ctx.client.get('/api/bot/paper/settings',params={'mode':ctx.mode}).json()['daily_loss_limit_pct']==10


def test_running_writer_recovers_partial_profile_failure_exactly_once(daily):
    ctx=daily
    runner=object.__new__(lb.LiveBotRunner)
    runner._work=threading.RLock();runner.mode=ctx.mode;runner.store=ctx.store;runner.engine=ctx.engine
    runner.profiles=ctx.engine.profiles;runner.market=SimpleNamespace(profiles=runner.profiles)
    held=lb.InstanceLock(ops.PATHS['lock'],ctx.mode)
    ctx.monkeypatch.setattr(ops,'_send',lambda cmd,timeout,args:runner.handle_command(cmd,args))
    a=pa.describe(ctx.store.path,ctx.mode)
    body=dict(mode=ctx.mode,request_id=uuid.uuid4().hex,expected_session_id=a['session_id'],
              expected_revision=a['revision'],daily_loss_limit_pct=10)
    original=pa._atomic_json
    def fail_profiles(path,data):
        if path==ee.BOT_PROFILES_FILE:raise OSError('injected profile write failure')
        return original(path,data)
    try:
        ctx.monkeypatch.setattr(pa,'_atomic_json',fail_profiles)
        assert ctx.client.put('/api/bot/settings',json=body).status_code==503
        assert pa.pending_path(ctx.store.path).exists()
        assert ctx.store.config()['daily_loss_limit_pct']==10
        ctx.monkeypatch.setattr(pa,'_atomic_json',original)
        response=ctx.client.put('/api/bot/settings',json=body)
        assert response.status_code==200,response.text
        assert ctx.client.put('/api/bot/settings',json=body).json()==response.json()
        assert not pa.pending_path(ctx.store.path).exists()
        assert ctx.store.one("select count(*) n from events where kind='account_daily_limit_updated'")['n']==1
        assert ctx.engine.cfg_row['daily_loss_limit_pct']==10
        assert ee.profile_config(runner.market.profiles['BTCUSD']).max_daily_loss_pct==10
    finally:held.release()
    assert all(c['method']=='GET' for c in ctx.fake.calls)
