"""Offline regression coverage for the 40ba8f4 deep-review findings.
Run: python -m pytest -q test_review_repairs.py
"""
import testenv  # noqa: F401
from dataclasses import asdict, replace
import json
from pathlib import Path
import socket
import shutil

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import app
import backtester as bt
import bot_ops
import data_manager as dm
import execution_engine as ee
import run_store
import strategies
from config import settings
from notifier import TelegramNotifier


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    # No fixture is permitted to reach an external server or overwrite user data.
    def no_network(*a, **k):
        raise AssertionError("network is forbidden in review regression tests")
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    shutil.copytree(Path(__file__).parent/'data'/'metadata', tmp_path/'metadata')
    monkeypatch.setattr(settings, 'DATA_DIR', str(tmp_path))
    monkeypatch.setattr(settings, 'TELEGRAM_BOT_TOKEN', '')
    for module, name in [(app, 'SAVED_BACKTESTS_FILE'), (bot_ops, 'SAVED_BACKTESTS_FILE')]:
        monkeypatch.setattr(module, name, tmp_path/'saved.json')
    monkeypatch.setattr(ee, 'BOT_PROFILES_FILE', tmp_path/'profiles.json')
    monkeypatch.setattr(bot_ops, '_signals', lambda: {})
    app._frame_cache.clear()


def frame(n=12, start='2026-01-01 00:45'):
    i=pd.date_range(start, periods=n, freq='min', tz='UTC', name='time')
    return pd.DataFrame(dict(open=100.,high=100.1,low=99.9,close=100.,volume=10.,
                             mark_open=100.,mark_high=100.1,mark_low=99.9,mark_close=100.,funding_rate=0.),index=i)


def spec(symbol='XRPUSD'):
    return bt.ProductSpec(symbol,1,1,.0001)


def config(**kw):
    return bt.BacktestConfig(**dict(dict(initial_capital_inr=100000,leverage=10,risk_per_trade_pct=2,
        sl_mode='pct',stop_loss_pct=1,take_profit_pct=3,max_daily_loss_pct=0,allow_min_one_lot=False),**kw))


def run_with_cfg(df,cfg,signal=None,tf='1m'):
    signal=signal if signal is not None else pd.Series([1],index=[df.index[0]-pd.Timedelta(minutes=1)])
    return bt.Backtester(cfg).run_portfolio([bt.Leg(spec(),df,signal,tf,atr=pd.Series(1.,index=signal.index))])


def test_unknown_limit_stays_blocked_then_recovers(monkeypatch,tmp_path):
    import step9_repair_verify as t
    monkeypatch.setattr(t,'T',tmp_path)
    clock=t.FakeClock(t.us('2026-10-20 07:00:05'));fake=t.FakeDelta9(clock)
    engine,store=t.tn_engine(fake,clock,'unknown')
    try:
        fake.timeout_after_processing_next_post=True
        r=engine.manual_exchange_entry('ETHUSD',1,5,10,order_type='limit',ref_price=3000,limit_price=2900)
        it=store.intent(r['intent_id']);coid=it['client_order_id']
        oid=next(o['id'] for o in fake.orders.values() if o.get('client_order_id')==coid)
        fake.hidden_coids.add(coid);clock.t+=ee.UNKNOWN_GRACE_US+1000000
        engine.reconcile_unknown_intents()
        assert store.intent(r['intent_id'])['state']=='OUTCOME_UNKNOWN'
        with pytest.raises(ee.EntryBlockedError):engine.manual_exchange_entry('ETHUSD',1,5,10,ref_price=3000)
        fake.hidden_coids.clear();fake.fill_qty(oid,5);engine.reconcile_unknown_intents();engine.reconcile_unknown_intents()
        assert fake.positions[t.ETH]['size']==store.position('testnet','ETHUSD')['contracts']==5
    finally:store.close()


@pytest.mark.parametrize('fee_mode',['taker','taker_maker_tp','maker','maker_maker'])
@pytest.mark.parametrize('direction',[1,-1])
@pytest.mark.parametrize('exit_kind',['BE','TP','SL','TSL'])
def test_same_candles_backtest_paper_and_restart(tmp_path,fee_mode,direction,exit_kind):
    cfg=config(fee_mode=fee_mode,slippage_bps=2,breakeven_at_1r=exit_kind=='BE',
               trailing_atr_mult=.7 if exit_kind=='TSL' else 0)
    prof=ee.StrategyProfile('XRPUSD',timeframe='45m',effective_config=asdict(cfg))
    df=frame(3)
    if exit_kind in ('BE','TSL'):
        df.iloc[0,df.columns.get_loc('high')]=101.2 if direction==1 else 100.1
        df.iloc[0,df.columns.get_loc('low')]=98.8 if direction==-1 else 99.9
    elif exit_kind=='TP':
        df.iloc[1,df.columns.get_loc('high')]=104 if direction==1 else 100.1
        df.iloc[1,df.columns.get_loc('low')]=96 if direction==-1 else 99.9
    else:
        df.iloc[1,df.columns.get_loc('low')]=98 if direction==1 else 99.9
        df.iloc[1,df.columns.get_loc('high')]=102 if direction==-1 else 100.1
    sig=pd.Series([direction],index=[df.index[0]-pd.Timedelta(minutes=45)])
    expected=run_with_cfg(df,cfg,sig,'45m').trades.iloc[0]
    assert expected.exit_reason==exit_kind
    now=int(df.index[0].value//1000);path=tmp_path/'paper.db';clock=lambda:now
    store=ee.StateStore(path,clock=clock)
    engine=ee.ExecutionEngine(store,'paper',profiles={'XRPUSD':prof},products={'XRPUSD':spec()},clock=clock,notifier=TelegramNotifier(None,None))
    try:
        entry=engine.on_signal('XRPUSD',direction,int(sig.index[0].value//1000),1.,100.)
        assert entry['action']=='filled'
        assert engine.cfg_row['capital_inr']==100000
        for j,(t,r) in enumerate(df.iterrows()):
            engine.on_bar('XRPUSD',int(t.value//1000),r.open,r.high,r.low,r.close)
            if j==0:
                # A restart with a different deployed strategy must not alter the existing trade.
                store.close();store=ee.StateStore(path,clock=clock)
                engine=ee.ExecutionEngine(store,'paper',profiles={},products={'XRPUSD':spec()},clock=clock,notifier=TelegramNotifier(None,None))
        actual=dict(store.trades('paper')[0])
        assert actual['exit_reason']==expected.exit_reason
        for field in ['contracts','entry_price','exit_price','net_pnl_usd']:
            assert actual[field]==pytest.approx(expected[field],rel=1e-10,abs=1e-8)
        assert actual['fees_gst_usd']==pytest.approx(expected.fees_usd)
        exit_us=actual['closed_at_us'];tf=45*60000000
        assert store.symbol_state('paper','XRPUSD')['cooldown_until_us']==(exit_us//tf+2)*tf
    finally:store.close()


def test_mixed_stop_units_and_actual_leverage():
    for sl_type,tp_type,atr,expected in [('fixed_pct','atr_mult',.5,(1,1)),('fixed_pct','atr_mult',2,(1,4)),
                                        ('atr_mult','fixed_pct',.5,(1,3)),('atr_mult','fixed_pct',2,(4,3))]:
        req=app.BacktestRequest(symbol='XRPUSD',sl_type=sl_type,sl_value=1 if sl_type=='fixed_pct'else 2,
                                tp_type=tp_type,tp_value=2 if tp_type=='atr_mult'else 3)
        leg={'symbol':'XRPUSD','timeframe':'1m','stop_mode':'pct','sl_type':sl_type,'sl_value':req.sl_value,
             'tp_type':tp_type,'tp_value':req.tp_value,'exit_mode':'fixed'}
        assert app.leg_config(req,leg,.1).stop_pcts(100,atr)==pytest.approx(expected)
        assert app.leg_config(req,leg,20).stop_pcts(100,atr)==pytest.approx(expected)
    with pytest.raises(ValueError):ee.sizing_config(100000,leverage=200,sl_type='fixed_pct',sl_value=.8)
    req=app.BacktestRequest(leverage=200)
    leg.update(sl_type='fixed_pct',sl_value=.8)
    with pytest.raises(HTTPException):app.leg_config(req,leg)


def test_portfolio_and_mark_gaps_rejected():
    f=frame();f.loc[f.index[2],'low']=98
    sig=pd.Series([1],index=[f.index[0]])
    a=bt.Leg(spec(),f,sig,'1m');b=bt.Leg(spec('OTHER'),f.drop(f.index[2]),sig*0,'1m')
    assert bt.Backtester(config()).run_portfolio([a]).trades.iloc[0].exit_reason=='SL'
    with pytest.raises(ValueError,match='missing 1 simulation minutes'):bt.Backtester(config()).run_portfolio([a,b])
    f.loc[f.index[2],'mark_low']=np.nan
    with pytest.raises(ValueError,match='trigger prices'):bt.Backtester(replace(config(),trigger_source='mark')).run_portfolio([a])


def test_missing_funding_rejected():
    f=frame(4,'2026-01-01 07:58');f.iloc[1,f.columns.get_loc('funding_rate')]=np.nan
    with pytest.raises(ValueError,match='missing funding'):run_with_cfg(f,config())


def test_45m_four_hour_source_is_true_and_prefix_stable():
    f=frame(960,'2026-01-01');f['close']=np.arange(960)+100.
    bars=dm.resample_candles(f,'45m');b4=dm.resample_candles(f,'4h')
    result=strategies._closed_4h_regime(bars,bars_4h=b4)
    at=pd.Timestamp('2026-01-01 03:45',tz='UTC')
    assert result.loc[at,'close_4h']==339
    with pytest.raises(ValueError,match='true closed 4h'):strategies._closed_4h_regime(bars)
    prior=strategies._closed_4h_regime(bars.iloc[:8],bars_4h=b4.iloc[:1])
    pd.testing.assert_series_equal(result['close_4h'].iloc[:8],prior['close_4h'])


def mock_run(monkeypatch,n=40,**overrides):
    f=frame(n,'2026-01-01');f['high']=102.;f['low']=99.9
    out=pd.DataFrame({'signal':np.where(np.arange(n)%3==0,1,0)},index=f.index)
    monkeypatch.setattr(app,'get_history',lambda *a:f)
    monkeypatch.setattr(app,'get_frame',lambda *a,**k:(f,out,pd.Series(1.,index=f.index)))
    monkeypatch.setattr(app,'app_symbols',lambda:['XRPUSD','ETHUSD'])
    monkeypatch.setattr(bt.ProductSpec,'from_metadata',classmethod(lambda cls,sym:spec(sym)))
    req=dict(symbol='XRPUSD',timeframe='1m',strategy='bollinger_volume_breakout',
             start=f.index[1].isoformat(),end=(f.index[-1]+pd.Timedelta(minutes=1)).isoformat(),
             sl_type='fixed_pct',sl_value=1,tp_type='fixed_pct',tp_value=1,exit_mode='fixed',
             capital_inr=100000,fee_mode='maker_maker',cooldown_bars=0,max_daily_loss_pct=0,
             walk_forward_windows=0,include_chart=False)
    req.update(overrides)
    result=json.loads(app.run_backtest(app.BacktestRequest(**req)).body)
    return result,f,req


def test_save_authority_full_ledger_and_deploy(monkeypatch):
    r,f,_=mock_run(monkeypatch,n=1806)
    assert len(r['trades'])>500
    saved=json.loads(app.save_backtest_result(app.SaveResultRequest(run_id=r['run_id'],custom_name='Verified',
        symbol='ETHUSD',capital_inr=5,params={'wrong':1},net_pnl_inr=999999999)).body)
    assert saved['symbol']=='XRPUSD' and saved['capital_inr']==100000
    assert saved['net_pnl_inr']==r['summary']['net_pnl_inr']
    assert saved['params']==r['config']['params'] and len(saved['trades'])==len(r['trades'])
    evidence=run_store.get(r['run_id'])
    assert len(evidence['equity_full'])==1805 and 'funding' in evidence
    assert evidence['data_fingerprints']['XRPUSD']['rows']==1806
    entry=bot_ops._build_deploy_entry(saved,bot_ops.DeployStrategyRequest(result_id=saved['id']))
    assert entry['effective_config']==saved['effective_config']
    assert entry['history_start']==f.index[0].isoformat()
    with pytest.raises(HTTPException):
        bot_ops._build_deploy_entry(saved,bot_ops.DeployStrategyRequest(result_id=saved['id'],risk_pct=99))
    with pytest.raises(HTTPException):bot_ops._build_deploy_entry({'id':'legacy','symbol':'XRPUSD'},bot_ops.DeployStrategyRequest(result_id='legacy'))
    artifact=run_store.root()/(r['run_id']+'.json.gz');artifact.write_bytes(b'corrupt')
    with pytest.raises(HTTPException):app.save_backtest_result(app.SaveResultRequest(run_id=r['run_id']))


def test_no_future_history_changes_prior_tp(monkeypatch):
    f=frame(3000,'2026-01-01');out=pd.DataFrame({'signal':np.where(np.arange(len(f))==99,1,0)},index=f.index)
    monkeypatch.setattr(app,'get_history',lambda *a:f)
    monkeypatch.setattr(app,'get_frame',lambda *a,**k:(f,out,strategies.atr(f,14)))
    monkeypatch.setattr(app,'app_symbols',lambda:['XRPUSD'])
    monkeypatch.setattr(bt.ProductSpec,'from_metadata',classmethod(lambda cls,sym:spec(sym)))
    req=app.BacktestRequest(symbol='XRPUSD',timeframe='1m',start=f.index[100].to_pydatetime(),end=f.index[200].to_pydatetime(),
        sl_type='fixed_pct',sl_value=1,tp_type='atr_mult',tp_value=2,exit_mode='fixed',walk_forward_windows=0,include_chart=False)
    a=json.loads(app.run_backtest(req).body)
    f.loc[f.index[500]:,'high']=104;f.loc[f.index[500]:,'low']=96
    b=json.loads(app.run_backtest(req).body)
    assert a['trades']==b['trades'] and a['summary']==b['summary']
    assert a['audit']['passed'] and b['audit']['passed']


def test_all_fee_modes_audit_match():
    f=frame();f.iloc[1,f.columns.get_loc('high')]=105
    for fee in ['taker','taker_maker_tp','maker','maker_maker']:
        r=run_with_cfg(f,config(fee_mode=fee,slippage_bps=3))
        assert bt.audit_result(r)==[]


def test_old_wallet_is_not_silently_reset(tmp_path):
    path=tmp_path/'wallet.db';store=ee.StateStore(path)
    try:
        original=ee.ExecutionEngine(store,'paper',profiles={},products={})
        profile=ee.StrategyProfile('XRPUSD',capital_inr=100000,sl_type='fixed_pct',sl_value=1,tp_type='fixed_pct',tp_value=2)
        engine=ee.ExecutionEngine(store,'paper',profiles={'XRPUSD':profile},products={'XRPUSD':spec()})
        r=engine.on_signal('XRPUSD',1,0,1,100)
        assert r['action']=='skip' and 'wallet' in r['reason']
        assert engine.cfg_row['capital_inr']==50000
    finally:store.close()


def test_step18_isolated_http_roundtrip(monkeypatch):
    r,_,_=mock_run(monkeypatch)
    client=TestClient(app.app)
    save=client.post('/api/backtest/save_result',json={'run_id':r['run_id'],'custom_name':'HTTP winner'})
    assert save.status_code==200
    rid=save.json()['id']
    dep=client.post('/api/bot/deploy',json={'result_id':rid})
    assert dep.status_code==200,dep.text
    assert dep.json()['profiles']['XRPUSD']['capital_inr']==100000
    assert client.put('/api/bot/deployed/XRPUSD/toggle',json={'enabled':False}).status_code==200
    assert client.delete('/api/bot/deployed/XRPUSD').status_code==200
    assert client.delete('/api/backtest/results/'+rid).status_code==200


@pytest.fixture(scope='module')
def synthetic_history():
    rng=np.random.default_rng(412);n=70000
    f=frame(n,'2026-01-01');c=100*np.exp(np.cumsum(rng.normal(0,.0008,n)));o=np.r_[c[0],c[:-1]]
    f['open']=o;f['close']=c;f['high']=np.maximum(o,c)*1.0008;f['low']=np.minimum(o,c)*.9992
    f['volume']=rng.lognormal(6,1,n)
    return f


@pytest.mark.parametrize('rule',list(strategies.STRATEGIES)+list(strategies.CUSTOM_RULE_TYPES))
def test_strategy_signal_prefix(rule,synthetic_history):
    f=synthetic_history;b=dm.resample_candles(f,'45m');b4=dm.resample_candles(f,'4h')
    short=f.iloc[:60000];bp=dm.resample_candles(short,'45m');b4p=dm.resample_candles(short,'4h')
    full=strategies.generate_signals(rule,b,bars_4h=b4)
    pre=strategies.generate_signals(rule,bp,bars_4h=b4p)
    for col in ['signal','exit_signal']:
        if col in pre:pd.testing.assert_series_equal(full.loc[pre.index,col],pre[col])


def test_replay_detects_changed_source(monkeypatch):
    r,f,_=mock_run(monkeypatch)
    evidence=run_store.get(r['run_id']);req=app.BacktestRequest(**evidence['request'])
    f.iloc[3,f.columns.get_loc('close')]=100.5
    with pytest.raises(HTTPException,match='source data changed'):app.run_backtest(req)


def test_presets_keep_causal_signals(synthetic_history):
    presets=json.loads((Path(__file__).parent/'data'/'strategies.json').read_text())
    f=synthetic_history;b4=dm.resample_candles(f,'4h');short=f.iloc[:60000];b4p=dm.resample_candles(short,'4h')
    frames={tf:(dm.resample_candles(f,tf),dm.resample_candles(short,tf))for tf in {p['timeframe']for p in presets}}
    for preset in presets:
        b,bp=frames[preset['timeframe']]
        full=strategies.evaluate_custom_strategy(preset['rule_type'],preset['params'],b,bars_4h=b4)
        pre=strategies.evaluate_custom_strategy(preset['rule_type'],preset['params'],bp,bars_4h=b4p)
        pd.testing.assert_series_equal(full.loc[pre.index,'signal'],pre['signal'],obj=preset['id'])


def test_bot_signal_filters_match_backtest(synthetic_history):
    f=synthetic_history
    prof=ee.StrategyProfile('XRPUSD',timeframe='45m',rule_type='vol_climax_reversal',
        params={'use_4h_trend':1,'cost_gate_mult':1.4},trend_filter_4h=True,cost_gate=True,slippage_bps=3)
    b=dm.resample_candles(f,'45m');b4=dm.resample_candles(f,'4h');cfg=ee.profile_config(prof)
    out=strategies.evaluate_custom_strategy(prof.rule_type,prof.params,b,bars_4h=b4)
    trend=strategies.trend_filter_4h(b,b4,45)
    gate=strategies.cost_gate(b,cfg.taker_fee,cfg.gst_rate,3)
    signals,_=strategies.apply_entry_filters(out,{'trend4h':(trend.allow_long,trend.allow_short),'costgate':(gate.allow,gate.allow)})
    actual=ee.evaluate_symbol(prof,f)
    assert actual['signal']==signals.iloc[-1]
    assert actual['signals_total']==int((signals!=0).sum())


def test_failed_be_edit_retries_from_persisted_extreme(tmp_path,monkeypatch):
    cfg=config(breakeven_at_1r=True)
    prof=ee.StrategyProfile('XRPUSD',timeframe='1m',effective_config=asdict(cfg))
    now=int(pd.Timestamp('2026-01-01 00:01',tz='UTC').value//1000)
    store=ee.StateStore(tmp_path/'retry.db',clock=lambda:now)
    engine=ee.ExecutionEngine(store,'paper',profiles={'XRPUSD':prof},products={'XRPUSD':spec()},clock=lambda:now,notifier=TelegramNotifier(None,None))
    try:
        engine.on_signal('XRPUSD',1,now-60000000,1,100)
        original=engine.update_stop
        monkeypatch.setattr(engine,'update_stop',lambda *a:{'sl_price':99.,'error':'rejected'})
        engine.on_bar('XRPUSD',now,100,101.1,100,100.5)
        assert not store.position('paper','XRPUSD')['be_armed']
        monkeypatch.setattr(engine,'update_stop',original)
        engine.on_bar('XRPUSD',now+60000000,100.5,100.8,100.4,100.5)
        assert store.position('paper','XRPUSD')['be_armed']
        assert store.position('paper','XRPUSD')['sl_price']>100
    finally:store.close()


@pytest.mark.parametrize('action',['pause','remove'])
def test_can_disable_with_conflicting_legacy_wallets(action):
    ee.save_bot_profiles_raw({sym:{'enabled':True,'capital_inr':cap}
        for sym,cap in [('XRPUSD',100000),('ETHUSD',50000),('BTCUSD',25000)]})
    if action=='pause':
        r=bot_ops.toggle_deployed_strategy('XRPUSD',bot_ops.ToggleDeployedRequest(enabled=False))
        assert not ee.load_bot_profiles_raw()['XRPUSD']['enabled']
    else:
        r=bot_ops.undeploy_strategy('XRPUSD')
        assert 'XRPUSD' not in ee.load_bot_profiles_raw()
    assert r['ok']


def test_profile_trail_keeps_unrounded_atr_distance():
    cfg=config(trailing_atr_mult=.731)
    profile=ee.StrategyProfile('XRPUSD',effective_config=asdict(cfg))
    product=bt.ProductSpec('XRPUSD',1,1,.1)
    sized=ee.calculate_order_lots(product,100,1.2345,'LONG',100000,profile=profile)
    assert sized.trail_amount==pytest.approx(.731*1.2345)


def test_step8b_exchange_regression_offline(monkeypatch, tmp_path):
    import step9_repair_verify as seed
    import step8b_verify as suite
    import manual_trade as manual
    seed._seed_market()
    far = 10**12
    sol = dict(manual._cache['tk:ETHUSD'][1], symbol='SOLUSD', best_bid=149.99,
               best_ask=150., mark_price=150., last_price=150., spot_price=150.)
    catalog = manual._cache['catalog'][1]
    meta = dm.load_metadata('SOLUSD')
    catalog['SOLUSD'] = dict(catalog['ETHUSD'], symbol='SOLUSD', product_id=int(meta['id']),
        contract_value=float(meta['contract_value']), tick_size=float(meta['tick_size']))
    manual._cache['tk:SOLUSD'] = (far, sol)
    manual._cache['ob:SOLUSD'] = (far, None)
    quotes = {s:dict(manual._cache['tk:'+s][1]) for s in ['BTCUSD','ETHUSD','SOLUSD','XRPUSD']}
    monkeypatch.setattr(manual, 'ticker', lambda symbol, ttl=1:dict(quotes[symbol]))
    monkeypatch.setattr(suite, 'T', tmp_path/'step8b')
    suite.CHECKS.clear()
    suite.test_mocked()
    assert len(suite.CHECKS) == 27
    assert all(row[2] for row in suite.CHECKS), [r for r in suite.CHECKS if not r[2]]
