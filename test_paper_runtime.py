"""Offline PAPER runtime regressions; every journal/dataset write is temporary."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

from test_review_repairs import isolated  # shared socketpair-safe offline guard
import bot_ops as ops
import data_manager as dm
import execution_engine as ee
import live_bot as lb
import ws_manager as wm


class Market:
    def __init__(self, clock):
        self.clock = clock
        self.signals = {}
        self.evaluated = []
        self.funding = (None, None)

    def sync(self, symbols=None):
        return {s: {'last_1m':int(self.clock[0])//60*60-60,'gaps':{},'fetched':0}
                for s in symbols or ['BTCUSD']}

    def evaluate(self, sym, close=None):
        close = close or int(self.clock[0])//300*300
        self.evaluated.append(close)
        sig = self.signals.get(close, 0)
        return dict(symbol=sym,bar_open=pd.Timestamp(close-300,unit='s',tz='UTC'),
                    bar_close=pd.Timestamp(close,unit='s',tz='UTC'),signal=sig,
                    fresh_signal=bool(sig),signal_side={1:'LONG',-1:'SHORT'}.get(sig),
                    atr14=100.,close=10000.,cond_long=sig==1,cond_short=sig==-1)

    def minutes_after(self, *args): return []
    def ticker(self, sym): return dict(best_ask=10000.,best_bid=9999.,mark_price=10000.)
    def funding_inputs(self, *args): return self.funding


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    for key in ops.PATHS:
        monkeypatch.setitem(ops.PATHS,key,tmp_path/('state.db' if key=='db' else key))
    clock = [1793664060.]  # fixed synthetic test clock, never the running market
    profile = replace(ee.FROZEN_PROFILES['BTCUSD'],timeframe='5m',history_start=None,
                      cost_gate=False,trend_filter_4h=False,rule_type='ema_pullback_scalp',
                      params={'fast_ema':9,'slow_ema':21,'use_4h_trend':0,'cost_gate_mult':0})
    market = Market(clock)
    bot = lb.LiveBotRunner('paper',profiles={'BTCUSD':profile},market=market,clock=lambda:clock[0],
                          store_path=tmp_path/'state.db',lock_path=tmp_path/'bot.lock',control_dir=tmp_path/'control')
    bot.state = 'OBSERVED_READY'
    yield bot,market,clock,tmp_path
    bot.close()


@pytest.mark.parametrize('field,value',[('initial_capital_inr',75000),('usd_inr_rate',90),('max_daily_loss_pct',25)])
def test_paper_deploy_inherits_wallet_but_rejects_fx_mismatch(runtime,field,value,monkeypatch):
    bot,_,_,path=runtime
    original = ops._build_deploy_entry(None,ops.DeployStrategyRequest(symbol='BTCUSD'))
    original['effective_config'] = dict(__import__('dataclasses').asdict(ee.profile_config(bot.profiles['BTCUSD'])))
    ee.save_bot_profiles_raw({'BTCUSD':original})
    before=ee.BOT_PROFILES_FILE.read_bytes()
    bad=json.loads(json.dumps(original));bad['effective_config'][field]=value
    monkeypatch.setattr(ops,'_build_deploy_entry',lambda *a:bad)
    if field=='usd_inr_rate':
        with pytest.raises(ops.HTTPException) as error:
            ops.deploy_strategy_to_bot(ops.DeployStrategyRequest(symbol='BTCUSD'))
        assert error.value.status_code==409
        assert error.value.detail['code']=='shared_wallet_mismatch'
        assert ee.BOT_PROFILES_FILE.read_bytes()==before
    else:
        assert ops.deploy_strategy_to_bot(ops.DeployStrategyRequest(symbol='BTCUSD'))['ok']
        cfg=ee.profile_config(ee._profile_from_dict('BTCUSD',ee.load_bot_profiles_raw()['BTCUSD']))
        assert cfg.initial_capital_inr==50000 and cfg.max_daily_loss_pct==5
    assert bot.store.config()['daily_loss_limit_pct']==5


def test_durable_refusal_is_not_an_order_and_dedup_survives_restart(runtime):
    bot,market,clock,path=runtime
    close=int(clock[0])//300*300;market.signals[close]=1
    bot.profiles['BTCUSD']=replace(bot.profiles['BTCUSD'],max_daily_loss_pct=25)
    bot._queue_close('BTCUSD',close);bot._try_hour_signal('BTCUSD')
    row=bot.store.one('select * from signal_evaluations')
    assert row['raw_signal']==1 and row['decision']=='skip' and row['intent_id'] is None
    assert 'shared wallet' in row['skip_reason']
    assert row['evaluated_at_us']>=row['bar_close_us'] and len(row['config_id'])==64
    assert json.loads(row['config_json'])['params']['fast_ema']==9
    assert not bot.store.q("select * from order_intents where mode='paper'")
    bot._queue_close('BTCUSD',close);bot._try_hour_signal('BTCUSD')
    assert bot.store.one('select count(*) n from signal_evaluations')['n']==1
    bot.close()
    other=lb.LiveBotRunner('paper',profiles=bot.profiles,market=market,clock=lambda:clock[0],
                          store_path=path/'state.db',lock_path=path/'bot.lock',control_dir=path/'control')
    try:
        other.state='OBSERVED_READY';other._queue_close('BTCUSD',close);other._try_hour_signal('BTCUSD')
        assert not other.store.q("select * from order_intents where mode='paper'")
        assert other.store.one('select count(*) n from signal_evaluations')['n']==1
    finally:other.close()


def test_success_and_no_signal_have_distinct_durable_decisions(runtime):
    bot,market,clock,_=runtime;close=int(clock[0])//300*300
    bot._queue_close('BTCUSD',close);bot._try_hour_signal('BTCUSD')
    assert bot.store.one('select * from signal_evaluations')['decision']=='no_signal'
    clock[0]+=300;close+=300;market.signals[close]=1
    bot._queue_close('BTCUSD',close);bot._try_hour_signal('BTCUSD')
    row=bot.store.one('select * from signal_evaluations order by id desc')
    assert row['intent_id'] and row['decision'] not in ('skip','error','missed')
    bot._queue_close('BTCUSD',close);bot._try_hour_signal('BTCUSD')
    assert len(bot.store.q("select * from order_intents where mode='paper'"))==1
    # Changing config cannot bypass the engine's consumed-signal protection.
    assert bot.engine.on_signal('BTCUSD',1,(close-300)*1_000_000,100.,10000.)['action']=='skip'


def test_raw_signal_survives_entry_filter_refusal(runtime,monkeypatch):
    bot,market,clock,_=runtime;close=int(clock[0])//300*300
    evaluation=market.evaluate('BTCUSD',close)
    evaluation.update(raw_signal=1,entry_filters={'cost_gate_pass':False})
    monkeypatch.setattr(market,'evaluate',lambda *a:evaluation)
    bot._queue_close('BTCUSD',close);bot._try_hour_signal('BTCUSD')
    row=bot.store.one('select * from signal_evaluations')
    assert row['raw_signal']==1 and row['decision']=='skip'
    assert 'entry filters' in row['skip_reason'] and row['intent_id'] is None


def test_fixing_profile_cannot_replay_consumed_refused_signal(runtime):
    bot,market,clock,_=runtime;close=int(clock[0])//300*300;market.signals[close]=1
    original=bot.profiles['BTCUSD']
    bot.profiles['BTCUSD']=replace(original,max_daily_loss_pct=25)
    bot._queue_close('BTCUSD',close);bot._try_hour_signal('BTCUSD')
    bot.profiles['BTCUSD']=original
    bot._queue_close('BTCUSD',close);bot._try_hour_signal('BTCUSD')
    rows=bot.store.q('select * from signal_evaluations order by id')
    assert len(rows)==2 and all(r['decision']=='skip' for r in rows)
    assert rows[-1]['skip_reason']=='signal bar already processed'
    assert not bot.store.q('select * from order_intents')


def test_catchup_order_dedup_and_recovery_never_enters(runtime):
    bot,market,clock,_=runtime;latest=int(clock[0])//300*300
    first=latest-900
    bot._queue_close('BTCUSD',first,recovery=True);bot._try_hour_signal('BTCUSD')
    for t in range(first+300,latest+1,300):market.signals[t]=1
    bot._catch_up(market.sync());bot._catch_up(market.sync())
    assert bot.pending_hour['BTCUSD']==list(range(first+300,latest+1,300))
    bot._try_hour_signal('BTCUSD')
    rows=bot.store.q('select * from signal_evaluations order by id')
    assert [r['bar_close_us']//1_000_000 for r in rows]==list(range(first,latest+1,300))
    assert all(r['decision']=='missed' for r in rows)
    assert not bot.store.q("select * from order_intents where mode='paper'")


def test_stale_and_unavailable_candles_recorded_without_current_price_order(runtime,monkeypatch):
    bot,market,clock,_=runtime;close=int(clock[0])//300*300-600;market.signals[close]=1
    monkeypatch.setattr(bot,'_ref_price',lambda *a:pytest.fail('historical order attempted'))
    bot._queue_close('BTCUSD',close);bot._try_hour_signal('BTCUSD')
    assert bot.store.one('select * from signal_evaluations')['skip_reason']=='entry window expired'
    monkeypatch.setattr(market,'evaluate',lambda *a:None)
    bot._queue_close('BTCUSD',close+300);bot._try_hour_signal('BTCUSD')
    row=bot.store.one('select * from signal_evaluations order by id desc')
    assert row['raw_signal'] is None and row['decision']=='not_evaluated'


def test_historical_evaluation_excludes_later_bars(runtime,monkeypatch):
    bot,_,clock,_=runtime
    idx=pd.date_range('2026-10-20',periods=3000,freq='min',tz='UTC')
    h=pd.DataFrame({'open':10000.,'high':10010.,'low':9990.,'close':10001.,'volume':100.},index=idx)
    m=lb.RestMarketData(['BTCUSD'],bot.profiles);m._hist['BTCUSD']=h
    monkeypatch.setattr(m,'sync',lambda *a:pytest.fail('covered historical candle needs no sync'))
    target=int(idx[2400].timestamp())
    s=m.evaluate('BTCUSD',target)
    assert int(s['bar_close'].timestamp())==target
    assert s['last_1m_open']<pd.Timestamp(target,unit='s',tz='UTC')


def test_readiness_uses_time_after_slow_sync(runtime,monkeypatch):
    bot,market,clock,_=runtime
    old=market.sync()
    def slow(*args):clock[0]+=600;return old
    monkeypatch.setattr(market,'sync',slow)
    result=bot.run_reconciliation('synthetic slow synchronization')
    assert result['at']==int(clock[0])
    assert bot.state=='DEGRADED' and any('history ends' in x for x in result['issues'])


def test_watchdog_receipt_advances_during_slow_handler_and_stale_generation_does_not(runtime):
    bot,_,clock,_=runtime
    async def run():
        mgr=bot.ws;mgr._queue=asyncio.Queue();mgr.clock=lambda:clock[0];mgr.generation=1;mgr._last_rx=clock[0]
        incoming=asyncio.Queue();entered=threading.Event();release=threading.Event();handled=[]
        class Conn:
            async def recv(self):return await incoming.get()
        def handler(msg,*args):
            if not handled:
                entered.set();assert release.wait(5)
            handled.append(msg)
        mgr.handle_message=handler
        reader=asyncio.create_task(mgr._reader(1,'public',Conn(),mgr._queue))
        await incoming.put(json.dumps({'type':'heartbeat'}))
        pump=asyncio.create_task(mgr._pump(lambda:len(handled)==2,100.,'test'))
        try:
            for _ in range(500):
                if entered.is_set():break
                await asyncio.sleep(.001)
            assert entered.is_set()
            clock[0]+=40
            await incoming.put(json.dumps({'type':'heartbeat'}))
            for _ in range(100):
                if mgr._last_rx==clock[0]:break
                await asyncio.sleep(.001)
            assert mgr._last_rx==clock[0] and mgr._queue.qsize()==1
            release.set();await asyncio.wait_for(pump,3)
            old_rx=mgr._last_rx;mgr.generation=2;clock[0]+=1
            await incoming.put(json.dumps({'type':'heartbeat'}));await asyncio.sleep(.01)
            assert mgr._last_rx==old_rx
            clock[0]+=36
            with pytest.raises(wm.WatchdogTimeout):await mgr._pump(lambda:False,100.,'silence')
        finally:
            release.set();reader.cancel();pump.cancel();await asyncio.gather(reader,pump,return_exceptions=True)
    asyncio.run(run())


def test_delayed_writer_cannot_remove_newer_candles(runtime):
    _,_,_,_=runtime
    def frame(times):return pd.DataFrame({'time':times,'open':1.,'high':2.,'low':.5,'close':1.,'volume':1.})
    dm._commit_candles('BTCUSD','TRADE',frame([0,60]))
    ready=threading.Event();new_done=threading.Event()
    def old():
        stale=dm.load_raw_1m('BTCUSD','TRADE');ready.set();assert new_done.wait(5)
        return dm._commit_candles('BTCUSD','TRADE',dm._merge(stale,frame([120])))
    with ThreadPoolExecutor(2) as pool:
        future=pool.submit(old);assert ready.wait(5)
        dm._commit_candles('BTCUSD','TRADE',frame([120,180]));new_done.set();future.result()
    assert dm.load_raw_1m('BTCUSD','TRADE').time.tolist()==[0,60,120,180]


def test_dataset_lock_serializes_separate_processes(tmp_path):
    path=tmp_path/'counter';path.write_text('0')
    code='''from dataset_lock import dataset_lock
from pathlib import Path
import sys,time
p=Path(sys.argv[1])
for _ in range(10):
 with dataset_lock(p):
  n=int(p.read_text());time.sleep(.005);p.write_text(str(n+1))
'''
    children=[subprocess.Popen([sys.executable,'-B','-c',code,str(path)],stdout=subprocess.PIPE,stderr=subprocess.PIPE) for _ in range(2)]
    for child in children:
        out,err=child.communicate(timeout=15);assert child.returncode==0,(out,err)
    assert path.read_text()=='20'


@pytest.mark.parametrize('missing',[(None,10000.),(.001,None),(float('nan'),10000.)])
def test_funding_pending_keeps_protection_and_recovers_after_close_once(runtime,missing):
    bot,market,clock,_=runtime;e=bot.engine
    boundary=int(clock[0])//28800*28800+28800;clock[0]=boundary-60
    e.manual_fill('BTCUSD',1,3,10000.,10,sl=9900.,tp=10200.)
    p=bot.store.position('paper','BTCUSD');quantity=p['contracts']*p['contract_value']
    clock[0]=boundary
    e.on_bar('BTCUSD',boundary*1_000_000,10000.,10050.,9800.,9900.,*missing)
    assert bot.store.position('paper','BTCUSD') is None  # stop remained active
    assert len(e.pending_funding())==1
    trade=dict(bot.store.trades('paper')[0]);equity=e.realized_equity_inr()
    assert e.on_signal('BTCUSD',1,boundary*1_000_000,100.,10000.)['reason'].startswith('unresolved settlement')
    assert e.retry_funding(lambda *a:missing)==0
    assert e.retry_funding(lambda *a:(.001,10000.))==1
    assert e.retry_funding(lambda *a:(.001,10000.))==0
    updated=bot.store.trades('paper')[0];cash=-quantity*10000*.001
    assert updated['net_pnl_usd']==pytest.approx(trade['net_pnl_usd']+cash)
    assert updated['equity_after_inr']==pytest.approx(trade['equity_after_inr']+cash*85)
    assert e.realized_equity_inr()==pytest.approx(equity+cash*85)
    assert len(bot.store.q('select * from funding_events'))==1 and not e.pending_funding()


def test_open_funding_recovery_and_repeated_bar_are_idempotent(runtime):
    bot,_,clock,_=runtime;e=bot.engine;boundary=int(clock[0])//28800*28800+28800;clock[0]=boundary-60
    e.manual_fill('BTCUSD',-1,3,10000.,10,sl=11000.,tp=9000.)
    p=bot.store.position('paper','BTCUSD');cash=p['contracts']*p['contract_value']*10000*.001
    clock[0]=boundary
    for inputs in [(None,None),(.001,10000.),(.001,10000.)]:
        e.on_bar('BTCUSD',boundary*1_000_000,10000.,10010.,9990.,10000.,*inputs)
    assert bot.store.position('paper','BTCUSD')['realized_funding_usd']==pytest.approx(cash)
    assert len(bot.store.q('select * from funding_events'))==1


def test_status_separates_runner_evidence_and_recalculation(runtime,monkeypatch):
    bot,market,clock,_=runtime;close=int(clock[0])//300*300
    bot._queue_close('BTCUSD',close);bot._try_hour_signal('BTCUSD')
    monkeypatch.setattr(ops,'runner',lambda:(False,None))
    s=ops.read_status(with_market=False)
    assert s['signals']=={}
    assert s['runner_evaluations']['BTCUSD']['source']=='runner journal'
    assert s['runner_evaluations']['BTCUSD']['decision']=='no_signal'
    assert not s['wallet_provisional']


def test_interrupted_evaluation_recovers_as_evidence_not_order(runtime):
    bot,market,clock,path=runtime;close=int(clock[0])//300*300;market.signals[close]=1
    eid,created=bot._evaluation_record('BTCUSD',close,market.evaluate('BTCUSD',close))
    assert created
    bot.close()
    store=ee.StateStore(path/'state.db')
    try:
        ee.ExecutionEngine(store,'paper',profiles=bot.profiles)
        row=store.one('select * from signal_evaluations where id=?',(eid,))
        assert row['decision']=='interrupted' and row['intent_id'] is None
        assert not store.q('select * from order_intents')
    finally:store.close()


def test_pending_accounting_visible_and_restart_recovery_idempotent(runtime,monkeypatch):
    bot,market,clock,path=runtime;e=bot.engine
    boundary=int(clock[0])//28800*28800+28800;clock[0]=boundary-60
    e.manual_fill('BTCUSD',1,3,10000.,10,sl=9000.,tp=11000.)
    p=bot.store.position('paper','BTCUSD');expected=-p['contracts']*p['contract_value']*10000*.001
    clock[0]=boundary;e.on_bar('BTCUSD',boundary*1_000_000,10000.,10010.,9990.,10000.)
    monkeypatch.setattr(ops,'runner',lambda:(False,None))
    status=ops.read_status(with_market=False)
    assert status['wallet_provisional'] and status['accounting_status']=='PENDING'
    assert len(status['pending_funding'])==1
    bot.close();store=ee.StateStore(path/'state.db')
    try:
        engine=ee.ExecutionEngine(store,'paper',profiles=bot.profiles)
        assert engine.retry_funding(lambda *a:(.001,10000.))==1
        assert engine.retry_funding(lambda *a:(.001,10000.))==0
        assert store.position('paper','BTCUSD')['realized_funding_usd']==pytest.approx(expected)
        assert len(store.q('select * from funding_events'))==1
    finally:store.close()


def test_funding_recovery_allocates_partial_close_and_open_remainder(runtime):
    bot,_,clock,_=runtime;e=bot.engine
    boundary=int(clock[0])//28800*28800+28800;clock[0]=boundary-60
    e.manual_fill('BTCUSD',1,3,10000.,10,sl=9000.,tp=11000.)
    cv=bot.store.position('paper','BTCUSD')['contract_value']
    clock[0]=boundary;e.on_bar('BTCUSD',boundary*1_000_000,10000.,10010.,9990.,10000.)
    clock[0]+=60;e._paper_close('BTCUSD',10000.,'TEST_PARTIAL',int(clock[0]*1_000_000),contracts=1)
    before=e.realized_equity_inr()
    assert e.retry_funding(lambda *a:(.001,10000.))==1
    assert bot.store.trades('paper')[0]['funding_usd']==pytest.approx(-cv*10)
    assert bot.store.position('paper','BTCUSD')['realized_funding_usd']==pytest.approx(-2*cv*10)
    assert e.realized_equity_inr()==pytest.approx(before-3*cv*10*85)


def test_quote_receipt_age_not_refreshed_by_delayed_dispatch(runtime):
    bot,_,clock,_=runtime;mgr=bot.ws;mgr.generation=1;mgr.clock=lambda:clock[0]
    receipt=clock[0]-45
    mgr.handle_message({'type':'ob_l1','symbol':'BTCUSD','best_bid':9999.,'best_ask':10000.},1,'public',receipt)
    assert mgr.last_quote['BTCUSD']['rx']==receipt


def test_gap_across_settlement_records_obligation_without_substituting_newer_inputs(runtime):
    bot,_,clock,_=runtime;e=bot.engine;boundary=int(clock[0])//28800*28800+28800;clock[0]=boundary-60
    e.manual_fill('BTCUSD',1,2,10000.,10,sl=9000.,tp=11000.)
    clock[0]=boundary+120
    e.on_bar('BTCUSD',(boundary+120)*1_000_000,10000.,10010.,9990.,10000.,.007,12345.)
    row=e.pending_funding()[0]
    assert row['at_us']==boundary*1_000_000 and row['rate'] is None and row['mark_price'] is None
    assert not bot.store.q('select * from funding_events')


def test_overlapping_actual_downloads_reload_inside_commit_lock(runtime,monkeypatch):
    base=1793664000
    def frame(times):return pd.DataFrame({'time':times,'open':1.,'high':2.,'low':.5,'close':1.,'volume':1.})
    dm._commit_candles('BTCUSD','TRADE',frame(list(range(base,base+86400,60))))
    waiting=threading.Event();release=threading.Event()
    def fetch(sym,st,a,b):
        if threading.current_thread().name.startswith('older'):
            waiting.set();assert release.wait(5)
        return frame(list(range(int(a),int(b)+1,60))),1
    monkeypatch.setattr(dm,'_fetch_range',fetch)
    monkeypatch.setattr(dm,'_repair_gaps',lambda sym,st,df:(df,0))
    monkeypatch.setattr(dm,'_last_closed_minute',lambda:base+86400 if threading.current_thread().name.startswith('older') else base+86460)
    with ThreadPoolExecutor(1,thread_name_prefix='older') as pool:
        future=pool.submit(dm.download_1m_series,'BTCUSD','TRADE',1)
        assert waiting.wait(5)
        dm.download_1m_series('BTCUSD','TRADE',1)
        release.set();future.result()
    raw=dm.load_raw_1m('BTCUSD','TRADE')
    assert len(raw)==1442 and raw.time.max()==base+86460


def test_late_previous_day_funding_corrects_daily_anchor_once(runtime):
    bot,_,clock,_=runtime;e=bot.engine;boundary=int(clock[0])//28800*28800+28800;clock[0]=boundary-60
    e.manual_fill('BTCUSD',1,2,10000.,10,sl=9000.,tp=11000.)
    cv=bot.store.position('paper','BTCUSD')['contract_value'];clock[0]=boundary
    e.on_bar('BTCUSD',boundary*1_000_000,10000.,10010.,9990.,10000.)
    tomorrow=(boundary//86400+1)*86400
    bot.store.meta_set(utc_day_anchor_date=pd.Timestamp(tomorrow,unit='s',tz='UTC').strftime('%Y-%m-%d'),
                       utc_day_start_equity_usd=50000/85)
    cash=-2*cv*10000*.001
    assert e.retry_funding(lambda *a:(.001,10000.))==1
    assert float(bot.store.meta_get('utc_day_start_equity_usd'))==pytest.approx(50000/85+cash)
    assert e.retry_funding(lambda *a:(.001,10000.))==0
    assert float(bot.store.meta_get('utc_day_start_equity_usd'))==pytest.approx(50000/85+cash)


def test_legacy_journal_upgrade_preserves_wallet_and_trades(runtime):
    bot,_,clock,path=runtime
    bot.engine.manual_fill('BTCUSD',1,2,10000.,10,sl=9900.,tp=11000.)
    clock[0]+=60
    bot.engine.on_bar('BTCUSD',int(clock[0]*1_000_000),10000.,10010.,9800.,9900.)
    trades=[dict(t) for t in bot.store.trades('paper')];cfg=dict(bot.store.config())
    bot.store.conn.executescript('DROP TABLE signal_evaluations; DROP TABLE funding_settlements;')
    bot.close();store=ee.StateStore(path/'state.db')
    try:
        assert dict(store.config())==cfg and [dict(t) for t in store.trades('paper')]==trades
        assert store.q('select * from signal_evaluations')==[]
        assert store.q('select * from funding_settlements')==[]
    finally:store.close()


def healthy_transport(bot, clock):
    bot.ws.phase='READY'
    bot.ws._conns={'public':object()}
    bot.ws._reconnect_reason=None
    bot.ws._last_rx=clock[0]


@pytest.mark.parametrize('ws_current',[False,True])
def test_stale_startup_recovers_after_poll_even_without_fallback_rows(runtime,monkeypatch,ws_current):
    bot,market,clock,_=runtime
    fresh_sync=market.sync
    def stale(symbols=None):
        cov=fresh_sync(symbols)
        for row in cov.values():row['last_1m']-=600
        return cov
    monkeypatch.setattr(market,'sync',stale)
    assert bot.run_reconciliation('startup')['state']=='DEGRADED'
    clock[0]+=61;healthy_transport(bot,clock)
    calls=[]
    def fresh(symbols=None):calls.append(list(symbols or bot.symbols));return fresh_sync(symbols)
    monkeypatch.setattr(market,'sync',fresh)
    if ws_current:
        bot.last_minute['BTCUSD']=int(clock[0])//60*60-60
    bot._poll_once()
    assert bot.state=='OBSERVED_READY' and not bot.data_blocked
    assert calls and calls[-1]==bot.symbols
    assert bot.reconciliations[-1]['reason']=='automatic PAPER readiness revalidation'
    assert not bot.reconciliations[-1]['issues']
    assert not bot.store.q('select * from order_intents')


@pytest.mark.parametrize('state',['stale','missing','empty'])
def test_all_deployed_symbols_must_pass_before_recovery(runtime,monkeypatch,state):
    bot,market,clock,_=runtime
    bot.profiles['SUIUSD']=replace(bot.profiles['BTCUSD'],symbol='SUIUSD')
    bot.symbols.append('SUIUSD')
    fresh_sync=market.sync
    def incomplete(symbols=None):
        cov=fresh_sync(symbols)
        if state=='stale':cov['SUIUSD']['last_1m']-=600
        elif state=='missing':cov.pop('SUIUSD',None)
        else:cov['SUIUSD']={'status':'no_data','gaps':{}}
        return cov
    monkeypatch.setattr(market,'sync',incomplete)
    result=bot.run_reconciliation('startup')
    assert bot.state=='DEGRADED' and any('SUIUSD' in issue for issue in result['issues'])
    clock[0]+=61;healthy_transport(bot,clock);bot._revalidate_paper_readiness()
    assert bot.state=='DEGRADED'
    assert any('SUIUSD' in issue for issue in bot.reconciliations[-1]['issues'])
    monkeypatch.setattr(market,'sync',fresh_sync)
    clock[0]+=61;healthy_transport(bot,clock);bot._revalidate_paper_readiness()
    assert bot.state=='OBSERVED_READY' and not bot.reconciliations[-1]['issues']


def test_fresh_data_does_not_clear_non_data_readiness_blocker(runtime,monkeypatch):
    bot,_,clock,_=runtime
    monkeypatch.setattr(bot.engine,'reconcile_unknown_intents',lambda:[{'result':'STILL_UNKNOWN'}])
    bot.run_reconciliation('startup')
    clock[0]+=61;healthy_transport(bot,clock);bot._revalidate_paper_readiness()
    assert bot.state=='DEGRADED'
    assert 'OUTCOME_UNKNOWN intent unresolved' in bot.reconciliations[-1]['issues']


@pytest.mark.parametrize('blocker',['connecting','disconnected','invalidated','silent'])
def test_readiness_retry_cannot_bypass_transport_recovery(runtime,monkeypatch,blocker):
    bot,_,clock,_=runtime;bot.state='DEGRADED';bot.reconciliations.append({'issues':['old']})
    healthy_transport(bot,clock)
    if blocker=='connecting':bot.ws.phase='CONNECTING'
    elif blocker=='disconnected':bot.ws._conns={}
    elif blocker=='invalidated':bot.ws._reconnect_reason='sequence gap'
    else:bot.ws._last_rx=clock[0]-36
    monkeypatch.setattr(bot,'run_reconciliation',lambda *a:pytest.fail('transport recovery bypassed'))
    bot._revalidate_paper_readiness()
    assert bot.state=='DEGRADED'


def test_failed_revalidation_is_visible_throttled_and_retryable(runtime,monkeypatch):
    bot,market,clock,_=runtime;bot.state='DEGRADED';bot.reconciliations.append({'issues':['old']})
    fresh=market.sync;calls=[]
    def failed(*args):calls.append(1);raise RuntimeError('synthetic sync failure')
    monkeypatch.setattr(market,'sync',failed);healthy_transport(bot,clock)
    bot._revalidate_paper_readiness()
    assert bot.state=='DEGRADED' and 'synthetic sync failure' in bot.reconciliations[-1]['issues'][0]
    bot._revalidate_paper_readiness();assert len(calls)==1
    monkeypatch.setattr(market,'sync',fresh);clock[0]+=61;healthy_transport(bot,clock)
    bot._revalidate_paper_readiness();assert bot.state=='OBSERVED_READY'


def test_completed_candle_revalidates_stale_readiness_without_reconnect(runtime,monkeypatch):
    bot,market,clock,_=runtime;fresh=market.sync
    def stale(symbols=None):
        cov=fresh(symbols)
        for row in cov.values():row['last_1m']-=600
        return cov
    monkeypatch.setattr(market,'sync',stale);bot.run_reconciliation('startup')
    assert bot.state=='DEGRADED'
    monkeypatch.setattr(market,'sync',fresh);clock[0]+=61;healthy_transport(bot,clock)
    minute=int(clock[0])//60*60-60
    bot.on_minute_close('BTCUSD',minute,10000.,10010.,9990.,10000.)
    assert bot.state=='OBSERVED_READY'
    assert bot.reconciliations[-1]['reason']=='automatic PAPER readiness revalidation'
