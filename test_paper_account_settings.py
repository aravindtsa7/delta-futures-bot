"""Account/session API tests using only disposable journals and profiles."""
from dataclasses import asdict, replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import uuid
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from test_review_repairs import isolated
import bot_ops as ops
import execution_engine as ee
import live_bot as lb
import paper_accounts as pa


@pytest.fixture
def account(monkeypatch,tmp_path):
    db=tmp_path/'account.db'
    for k in ops.PATHS:
        monkeypatch.setitem(ops.PATHS,k,db if k=='db' else tmp_path/k)
    clock=[1793716500_000000]
    p=replace(ee.FROZEN_PROFILES['BTCUSD'],history_start=None,cost_gate=False,trend_filter_4h=False,
              rule_type='ema_pullback_scalp',params={'fast_ema':9,'slow_ema':21})
    raw={'BTCUSD':asdict(p),'DOGEUSD':dict(asdict(p),symbol='DOGEUSD',enabled=False,leverage=12,risk_pct=7,user_note='keep')}
    ee.save_bot_profiles_raw(raw)
    store=ee.StateStore(db,clock=lambda:clock[0]);engine=ee.ExecutionEngine(store,'paper',profiles={'BTCUSD':p},clock=lambda:clock[0])
    for _ in range(5):
        engine.manual_fill('BTCUSD',1,2,10000.,10,sl=9900.,tp=11000.)
        clock[0]+=60_000000;engine._paper_close('BTCUSD',9990.,'DAILY',clock[0])
    store.meta_set(utc_day_anchor_date='2026-11-03',utc_day_start_equity_usd=50000/85)
    store.set_halt(True,'audited fixture daily halt',clock[0]+100*86400_000000)
    with store.tx() as c:
        c.execute("INSERT INTO signal_evaluations(mode,symbol,bar_open_us,bar_close_us,evaluated_at_us,strategy,config_id,config_json,raw_signal,decision,detail) "
                  "VALUES('paper','BTCUSD',1,2,3,'ema_pullback_scalp','fixture','{}',1,'skip','{}')")
        c.execute("INSERT INTO funding_events(mode,symbol,at_us,rate,mark_price,contracts,cashflow_usd) VALUES('paper','BTCUSD',4,0,10000,2,0)")
    before={t:[dict(r) for r in store.q('select * from '+t)] for t in ['bot_config_state','trade_history','signal_evaluations','funding_events','system_meta']}
    store.close()
    app=FastAPI();app.include_router(ops.router)
    with TestClient(app) as client:
        yield client,db,raw,before


def request(client,kind='daily_limit',**changes):
    current=client.get('/api/bot/paper/sessions').json()
    body=dict(request_id=uuid.uuid4().hex,expected_session_id=current['session_id'],expected_revision=current['revision'],daily_loss_limit_pct=8.)
    if kind=='new_session':body.update(capital_inr=100000.,confirmed_equity_inr=current['equity_inr'])
    body.update(changes)
    return body


def send(client,body,kind='daily_limit'):
    return client.request('POST' if kind=='new_session' else 'PUT',
                          '/api/bot/paper/sessions' if kind=='new_session' else '/api/bot/paper/account/daily-loss',json=body)


def rows(db,table):
    with pa.connect(db) as c:return [dict(r) for r in c.execute('select * from '+table)]


def test_limit_update_preserves_halt_balance_baseline_history_and_other_settings(account):
    c,db,raw,before=account;old=pa.describe(db)
    body=request(c);r=send(c,body);assert r.status_code==200,r.text
    new=pa.describe(db)
    for key in ['equity_inr','daily_baseline_inr','capital_inr','usd_inr_rate','halted','halt_reason','halt_until_us','trade_count']:
        assert new[key]==old[key]
    assert new['daily_loss_limit_pct']==8 and new['allowance_inr']==4000 and new['revision']==1
    for t in ['trade_history','signal_evaluations','funding_events']:assert rows(db,t)==before[t]
    updated=ee.load_bot_profiles_raw()
    for sym in raw:
        for key in ['params','leverage','risk_pct','enabled']:assert updated[sym][key]==raw[sym][key]
        assert updated[sym]['daily_loss_limit_pct']==updated[sym]['daily_loss_stop_pct']==8
        assert updated[sym]['effective_config']['max_daily_loss_pct']==8
    # Read through newly opened connections: no in-memory settings cache is authoritative.
    assert c.get('/api/bot/status').json()['paper_account']['daily_loss_limit_pct']==8
    assert send(c,body).json()==r.json() and pa.describe(db)['revision']==1


def test_nonzero_signal_cannot_bypass_preserved_halt_after_limit_update(account):
    c,db,_,_=account;assert send(c,request(c,daily_loss_limit_pct=100)).status_code==200
    store=ee.StateStore(db)
    try:
        e=ee.ExecutionEngine(store,'paper',profiles=ee.bot_profiles())
        r=e.on_signal('BTCUSD',1,1793720000_000000,100.,10000.)
        assert r['action']=='skip' and r['reason'].startswith('halted:')
        assert not store.q('select * from order_intents') and store.config()['halted']==1
    finally:store.close()


def test_new_session_archives_entire_journal_and_starts_stopped(account):
    c,db,_,before=account;old=pa.describe(db);body=request(c,'new_session');r=send(c,body,'new_session')
    assert r.status_code==200,r.text
    new=pa.describe(db);assert new['session_id']!='legacy' and new['capital_inr']==new['equity_inr']==100000
    assert new['allowance_inr']==8000 and new['daily_baseline_inr']==100000 and not new['halted']
    assert new['trade_count']==0 and not ops.runner()[0]
    archive=Path(new['sessions'][0]['file']);assert hashlib.sha256(archive.read_bytes()).hexdigest()==new['sessions'][0]['sha256']
    for t in before:assert rows(archive,t)==before[t]
    status=c.get('/api/bot/status?paper_session_id=legacy').json()
    assert status['historical_session'] and status['readiness_state']=='ARCHIVED'
    assert len(status['trades'])==5 and status['paper_account']['halted']
    assert len(status['evaluation_history'])==1 and len(status['funding_history'])==1
    assert status['wallet']['total_equity_inr']==pytest.approx(old['equity_inr'])
    assert c.get('/api/bot/status?paper_session_id=../../bad').status_code==404
    assert send(c,body,'new_session').json()==r.json() and len(pa.describe(db)['sessions'])==1
    changed=dict(body,capital_inr=200000);assert send(c,changed,'new_session').status_code==409
    changed=dict(body,request_id=uuid.uuid4().hex);assert send(c,changed,'new_session').status_code==409


@pytest.mark.parametrize('field,value,kind',[
    ('daily_loss_limit_pct',0,'daily_limit'),('daily_loss_limit_pct',-1,'daily_limit'),
    ('daily_loss_limit_pct',100.01,'daily_limit'),('daily_loss_limit_pct','NaN','daily_limit'),
    ('daily_loss_limit_pct','Infinity','daily_limit'),('capital_inr',0,'new_session'),
    ('capital_inr',-10,'new_session'),('capital_inr','NaN','new_session'),
    ('capital_inr','Infinity','new_session'),('confirmed_equity_inr','NaN','new_session')])
def test_invalid_input_cannot_mutate_wallet(account,field,value,kind):
    c,db,_,before=account;r=send(c,request(c,kind,**{field:value}),kind)
    assert r.status_code==422,r.text
    assert rows(db,'bot_config_state')==before['bot_config_state']
    assert not pa.pending_path(db).exists() and not pa.describe(db)['sessions']


@pytest.mark.parametrize('kind',['daily_limit','new_session'])
@pytest.mark.parametrize('obligation',['position','intent','funding'])
def test_operations_refuse_outstanding_obligations(account,kind,obligation):
    c,db,_,_=account;store=ee.StateStore(db)
    try:
        e=ee.ExecutionEngine(store,'paper',profiles=ee.bot_profiles())
        if obligation=='position':e.manual_fill('BTCUSD',1,1,10000.,10)
        elif obligation=='intent':
            store.create_intent(intent_id='pending',client_order_id='pending',mode='paper',symbol='BTCUSD',product_id=27,
                                role='ENTRY',side='buy',size=1,order_type='market_order')
        else:
            store.conn.execute("INSERT INTO funding_settlements(mode,symbol,intent_id,at_us,side,contracts,contract_value) VALUES('paper','BTCUSD','old',1,'LONG',1,.001)")
    finally:store.close()
    r=send(c,request(c,kind),kind);assert r.status_code==409,r.text
    assert 'resolve' in r.text and not pa.pending_path(db).exists()


@pytest.mark.parametrize('kind',['daily_limit','new_session'])
def test_running_runner_blocks_both_operations(account,kind):
    c,db,_,_=account;body=request(c,kind);lock=lb.InstanceLock(ops.PATHS['lock'],'paper')
    try:assert send(c,body,kind).status_code==409
    finally:lock.release()


@pytest.mark.parametrize('kind',['daily_limit','new_session'])
@pytest.mark.parametrize('failed_file',['profiles','index'])
def test_partial_failure_blocks_startup_and_recovers_exactly_once(account,monkeypatch,kind,failed_file):
    c,db,_,before=account;body=request(c,kind);original=pa._atomic_json
    def fail(path,data):
        if (failed_file=='profiles' and Path(path)==ee.BOT_PROFILES_FILE) or (failed_file=='index' and Path(path).name=='index.json'):
            raise OSError('injected partial failure')
        return original(path,data)
    monkeypatch.setattr(pa,'_atomic_json',fail)
    assert send(c,body,kind).status_code==503
    assert pa.pending_path(db).exists() and pa.describe(db)['pending']['body']==body
    assert c.post('/api/bot/start',json={'mode':'paper'}).status_code==409
    with pytest.raises(pa.AccountError):ee.StateStore(db)
    with pytest.raises(pa.AccountError):
        lb.LiveBotRunner('paper',store_path=db,lock_path=ops.PATHS['lock'],control_dir=ops.PATHS['control'])
    monkeypatch.setattr(pa,'_atomic_json',original)
    result=send(c,body,kind);assert result.status_code==200,result.text
    assert not pa.pending_path(db).exists()
    assert send(c,body,kind).json()==result.json()
    if kind=='new_session':
        a=pa.describe(db);assert len(a['sessions'])==1 and rows(Path(a['sessions'][0]['file']),'trade_history')==before['trade_history']
    else:assert rows(db,'trade_history')==before['trade_history'] and pa.describe(db)['halted']


def test_concurrent_changes_and_runner_start_are_fenced(account,monkeypatch):
    c,db,_,_=account;body=request(c);entered=threading.Event();release=threading.Event();original=pa._atomic_json
    def slow(path,data):
        if Path(path)==pa.pending_path(db):entered.set();assert release.wait(5)
        return original(path,data)
    monkeypatch.setattr(pa,'_atomic_json',slow)
    with ThreadPoolExecutor(1) as pool:
        result=pool.submit(send,c,body)
        assert entered.wait(5)
        assert c.post('/api/bot/start',json={'mode':'paper'}).status_code==409
        with pytest.raises(lb.BotAlreadyRunningError):lb.InstanceLock(ops.PATHS['lock'],'paper')
        release.set();assert result.result().status_code==200
    body1=request(c,daily_loss_limit_pct=9);body2=dict(body1,request_id=uuid.uuid4().hex,daily_loss_limit_pct=10)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda b:send(c,b),[body1,body2]))
    assert sorted(r.status_code for r in results)==[200,409]
    assert pa.describe(db)['daily_loss_limit_pct'] in (9,10)


def test_concurrent_new_session_retries_create_one_archive(account):
    c,db,_,_=account;body=request(c,'new_session')
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:send(c,body,'new_session'),range(2)))
    assert all(r.status_code==200 for r in results)
    assert results[0].json()==results[1].json()
    assert len(pa.describe(db)['sessions'])==1


def test_other_journal_writer_and_changed_confirmation_block_mutations(account):
    c,db,_,before=account;body=request(c,'new_session')
    store=ee.StateStore(db)
    try:assert send(c,body,'new_session').status_code==409
    finally:store.close()
    body['confirmed_equity_inr']+=1
    assert send(c,body,'new_session').status_code==409
    assert rows(db,'bot_config_state')==before['bot_config_state']


def test_archived_evaluations_are_pageable(account):
    c,db,_,_=account;store=ee.StateStore(db)
    try:
        with store.tx() as cx:
            for i in range(101):
                cx.execute("INSERT INTO signal_evaluations(mode,symbol,bar_open_us,bar_close_us,evaluated_at_us,strategy,config_id,config_json,raw_signal,decision,detail) "
                           "VALUES('paper','BTCUSD',1,?,3,'ema_pullback_scalp','fixture','{}',0,'no_signal','{}')",(i+10,))
    finally:store.close()
    assert send(c,request(c,'new_session'),'new_session').status_code==200
    p1=c.get('/api/bot/status?paper_session_id=legacy').json()
    p2=c.get('/api/bot/status?paper_session_id=legacy&evaluation_offset=100').json()
    assert len(p1['evaluation_history'])==100 and p1['more_evaluations']
    assert len(p2['evaluation_history'])==2 and not p2['more_evaluations']
    assert {e['id'] for e in p1['evaluation_history']}.isdisjoint(e['id'] for e in p2['evaluation_history'])


@pytest.mark.parametrize('kind',['daily_limit','new_session'])
def test_account_and_session_catalog_persist_across_process_restart(account,kind):
    c,db,_,_=account;assert send(c,request(c,kind),kind).status_code==200
    result=subprocess.run([sys.executable,'-B','-c',
        'import json,sys,paper_accounts; print(json.dumps(paper_accounts.describe(sys.argv[1])))',str(db)],
        capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)==pa.describe(db)


def test_journal_writer_rechecks_recovery_after_acquiring_lock(account,monkeypatch):
    _,db,_,_=account;original=ee._ProcessLock
    def interleaved(path):
        lock=original(path)
        pa._atomic_json(pa.pending_path(db),{'injected':'between checks'})
        return lock
    monkeypatch.setattr(ee,'_ProcessLock',interleaved)
    try:
        with pytest.raises(pa.AccountError):ee.StateStore(db)
    finally:pa.pending_path(db).unlink()
    monkeypatch.setattr(ee,'_ProcessLock',original)
    store=ee.StateStore(db);store.close()  # refused constructor released its fence


def test_selected_paper_status_does_not_fall_back_to_manual_live_mode(account):
    c,_,_,_=account
    ops.set_active_mode('live')
    data=c.get('/api/bot/status?mode=paper').json()
    assert data['mode']=='paper' and data['paper_account']['halted']
    assert len(data['trades'])==5 and not data['runner']['running']
    assert c.get('/api/bot/process').json()['state']=='stopped'


def test_account_read_does_not_need_runner_status_or_market_data(account,monkeypatch):
    c,_,_,_=account
    def unavailable(*a,**k):raise OSError('runtime unavailable')
    monkeypatch.setattr(ops,'runner',unavailable)
    monkeypatch.setattr(ops,'_signals',unavailable)
    data=c.get('/api/bot/paper/sessions').json()
    assert data['paper_account']['halted'] and data['paper_account']['trade_count']==5
    assert c.get('/api/bot/process').json()['state']=='unknown'
    assert send(c,request(c)).status_code==409


def test_halted_account_can_have_running_or_stopping_process(account):
    c,_,_,_=account;lock=lb.InstanceLock(ops.PATHS['lock'],'paper')
    try:
        assert c.get('/api/bot/process').json()['state']=='running'
        assert c.get('/api/bot/paper/sessions').json()['paper_account']['halted']
        ops._stopping.set()
        assert c.get('/api/bot/process').json()['state']=='stopping'
    finally:
        ops._stopping.clear();lock.release()
    assert c.get('/api/bot/process').json()['state']=='stopped'
    assert c.get('/api/bot/paper/sessions').json()['paper_account']['halted']


def test_lock_release_does_not_prove_process_exit(account,monkeypatch):
    c,_,_,_=account
    monkeypatch.setattr(lb.InstanceLock,'read_info',staticmethod(lambda _:dict(pid=12345678,mode='paper')))
    monkeypatch.setattr(ops,'_pid_alive',lambda _:True)
    assert c.get('/api/bot/process').json()['state']=='stopping'
    assert send(c,request(c)).status_code==409
    result=c.post('/api/bot/stop').json()
    assert not result['stopped'] and result['process']['state']=='stopping'
    monkeypatch.setattr(ops,'_pid_alive',lambda _:False)
    assert c.get('/api/bot/process').json()['state']=='stopped'


def test_first_launch_initializes_account_stopped_without_market_sync(account,monkeypatch,tmp_path):
    c,_,_,_=account;db=tmp_path/'first.db';monkeypatch.setitem(ops.PATHS,'db',db)
    before=c.get('/api/bot/paper/sessions').json()
    assert not before['initialized'] and not before['paper_account']['initialized']
    body=dict(request_id=uuid.uuid4().hex,expected_session_id=before['session_id'],expected_revision=before['revision'],
              daily_loss_limit_pct=7.5,capital_inr=75000,confirmed_equity_inr=0.)
    response=send(c,body,'new_session');assert response.status_code==200,response.text
    new=pa.describe(db)
    assert new['initialized'] and new['capital_inr']==new['equity_inr']==75000
    assert new['daily_loss_limit_pct']==7.5 and not new['halted'] and not new['sessions']
    assert c.get('/api/bot/process').json()['state']=='stopped'
    assert send(c,body,'new_session').json()==response.json()


def test_account_operation_lock_is_not_reported_as_a_running_bot(account):
    c,_,_,_=account;lock=lb.InstanceLock(ops.PATHS['lock'],'paper-account-settings')
    try:
        result=c.get('/api/bot/process').json()
        assert result['state']=='unknown' and result['running'] is None
        assert 'account update' in result['error']
    finally:lock.release()
    assert c.get('/api/bot/process').json()['state']=='stopped'


def test_daily_limit_does_not_normalize_unrelated_profile_capital(account):
    c,_,raw,_=account
    raw['DOGEUSD']['capital_inr']=75000  # paused profile; preserve unrelated sizing settings
    ee.save_bot_profiles_raw(raw)
    assert send(c,request(c)).status_code==200
    changed=ee.load_bot_profiles_raw()['DOGEUSD']
    assert changed['capital_inr']==75000 and changed['effective_config']['initial_capital_inr']==75000
    assert changed['effective_config']['max_daily_loss_pct']==8 and changed['risk_pct']==7


@pytest.mark.parametrize('failed_file',['profiles','index'])
def test_first_initialization_failure_is_recoverable_without_duplicate_sessions(account,monkeypatch,tmp_path,failed_file):
    c,_,_,_=account;db=tmp_path/'initial.db';monkeypatch.setitem(ops.PATHS,'db',db)
    body=dict(request_id=uuid.uuid4().hex,expected_session_id='legacy',expected_revision=0,
              daily_loss_limit_pct=8.,capital_inr=60000,confirmed_equity_inr=0.)
    original=pa._atomic_json
    def fail(path,data):
        if (failed_file=='profiles' and Path(path)==ee.BOT_PROFILES_FILE) or (failed_file=='index' and Path(path).name=='index.json'):
            raise OSError('injected initialization failure')
        return original(path,data)
    monkeypatch.setattr(pa,'_atomic_json',fail)
    assert send(c,body,'new_session').status_code==503 and pa.pending_path(db).exists()
    assert c.post('/api/bot/start',json={'mode':'paper'}).status_code==409
    monkeypatch.setattr(pa,'_atomic_json',original)
    result=send(c,body,'new_session');assert result.status_code==200,result.text
    assert send(c,body,'new_session').json()==result.json()
    current=pa.describe(db)
    assert current['capital_inr']==60000 and current['trade_count']==0 and not current['sessions']


def test_paper_deployment_inherits_account_without_changing_saved_run_or_strategy(account,monkeypatch):
    c,db,_,_=account;assert send(c,request(c)).status_code==200
    source=ops._build_deploy_entry(None,ops.DeployStrategyRequest(symbol='XRPUSD'))
    source['capital_inr']=250000;source['effective_config']['initial_capital_inr']=250000
    source['effective_config']['max_daily_loss_pct']=25
    monkeypatch.setattr(ops,'_build_deploy_entry',lambda *a:json.loads(json.dumps(source)))
    r=c.post('/api/bot/deploy',json={'symbol':'XRPUSD','mode':'paper'});assert r.status_code==200,r.text
    added=ee.load_bot_profiles_raw()['XRPUSD']
    assert added['capital_inr']==50000 and added['effective_config']['max_daily_loss_pct']==8
    for key in ['params','leverage','risk_pct','fee_mode']:assert added[key]==source[key]
    assert source['effective_config']['initial_capital_inr']==250000
    ee.save_bot_profiles_raw({})
    r=c.post('/api/bot/deploy',json={'symbol':'XRPUSD','mode':'testnet'});assert r.status_code==200,r.text
    assert ee.load_bot_profiles_raw()['XRPUSD']['effective_config']['initial_capital_inr']==250000


def test_manual_trade_mode_cannot_redirect_paper_actions_or_status_to_live_journal(account):
    c, db, _, _ = account
    ops.set_active_mode('live')
    live_db = ops.mode_db('live')
    live_store = ee.StateStore(live_db)
    try:
        live_store.init_config('live', ee.FROZEN_PROFILES)
    finally:
        live_store.close()

    # Even without ?mode=paper, default Bot Operations status when stopped is paper, never manual_mode ('live').
    for url in ('/api/bot/status?mode=paper', '/api/bot/status'):
        st = c.get(url).json()
        assert st['mode'] == 'paper' and st['journal_mode'] == 'paper'
        assert st['paper_account']['halted'] and len(st['trades']) == 5
        assert st['wallet']['total_equity_inr'] == pytest.approx(46987.45, abs=0.05)

    # Pause & Resume in Paper mode must mutate only the Paper journal, never the Live journal.
    r_pause = c.post('/api/bot/pause', json={'mode': 'paper'})
    assert r_pause.status_code == 200 and r_pause.json()['mode'] == 'paper' and r_pause.json()['entries_paused'] is True
    assert rows(db, 'bot_config_state')[0]['entries_paused'] == 1
    assert rows(live_db, 'bot_config_state')[0]['entries_paused'] == 0

    r_resume = c.post('/api/bot/resume', json={'mode': 'paper'})
    assert r_resume.status_code == 200 and r_resume.json()['mode'] == 'paper' and r_resume.json()['entries_paused'] is False
    assert rows(db, 'bot_config_state')[0]['entries_paused'] == 0
    assert rows(live_db, 'bot_config_state')[0]['entries_paused'] == 0

    # Backup & Test Telegram in Paper mode must target Paper, never Live.
    r_backup = c.post('/api/bot/backup', json={'mode': 'paper'})
    assert r_backup.status_code == 200 and r_backup.json()['mode'] == 'paper'
    r_tg = c.post('/api/bot/test-telegram', json={'mode': 'paper'})
    assert r_tg.status_code == 200 and r_tg.json()['mode'] == 'paper'
    assert '[PAPER]' in (r_tg.json().get('text') or '')

    # Archived session must be refused on all Bot Operations actions.
    for ep in ('/api/bot/pause', '/api/bot/resume', '/api/bot/flatten', '/api/bot/backup',
               '/api/bot/test-telegram', '/api/bot/step', '/api/bot/start', '/api/bot/stop'):
        resp = c.post(ep, json={'mode': 'paper', 'paper_session_id': 'legacy'})
        assert resp.status_code == 409, f"{ep} did not refuse archived session: {resp.text}"
