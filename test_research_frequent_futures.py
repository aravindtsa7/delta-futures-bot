"""Fast isolated checks for the research protocol and its execution assumptions."""
import importlib
import socket

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(scope='module')
def research():
    originals={name:getattr(socket.socket,name) for name in ('connect','connect_ex','sendto')}
    module=importlib.import_module('research_frequent_futures')
    try:
        yield module
    finally:
        for name,original in originals.items():setattr(socket.socket,name,original)


def minutes(start='2026-08-01T07:50Z',periods=30):
    index=pd.date_range(start,periods=periods,freq='min',name='time')
    return pd.DataFrame(dict(open=100.,high=100.01,low=99.99,close=100.,volume=100.,
        mark_open=100.,mark_high=100.01,mark_low=99.99,mark_close=100.,funding_rate=.0001),index=index)


@pytest.mark.parametrize('field,value',[
    ('net_pnl_inr',0),('profit_factor',1.19),('max_drawdown_pct',12.01),('trades_per_day',1.99)])
def test_every_selection_gate_is_required(research,field,value):
    row=dict(net_pnl_inr=1,profit_factor=1.2,max_drawdown_pct=12,trades_per_day=2)
    assert research.passes(row)
    row[field]=value
    assert not research.passes(row)


def test_selection_uses_only_higher_cost_training_and_all_cases(research):
    def rows(candidate,net):
        return [dict(candidate=candidate,slippage_bps=5,net_pnl_inr=net,capital_inr=50000,
            profit_factor=1.3,max_drawdown_pct=3,trades_per_day=3) for _ in range(10)]
    data=rows('a',100)+rows('b',90)
    data[0]['net_pnl_inr']=-1
    # Favorable low-cost results cannot rescue a failing higher-cost case.
    data.append(dict(data[0],slippage_bps=2,net_pnl_inr=1000000))
    ranked=research.rank_training(data,['a','b'])
    assert ranked[0]['candidate']=='b'
    assert ranked[0]['eligible_cases']==10
    assert ranked[1]['eligible_cases']==9


@pytest.mark.parametrize('address',[('127.0.0.1',8080),('8.8.8.8',443)])
def test_research_cannot_connect_even_to_local_proxy(research,address):
    with socket.socket() as client,pytest.raises(AssertionError,match='network is forbidden'):
        client.connect(address)


@pytest.mark.parametrize('missing_row',[False,True])
def test_missing_funding_is_never_replaced_by_zero(research,monkeypatch,tmp_path,missing_row):
    monkeypatch.setattr(research,'ROOT',tmp_path)
    folder=tmp_path/'data/raw_1m';folder.mkdir(parents=True)
    for kind in ('TRADE','MARK','FUNDING'):(folder/f'TEST_{kind}_1m.parquet').touch()
    frame=minutes(periods=2).reset_index()
    frame['time']=pd.DatetimeIndex(frame.time).as_unit('s').asi8
    def read(path,**kwargs):
        out=frame.copy()
        if 'FUNDING' in str(path):
            if missing_row:return out.iloc[1:]
            out.loc[0,'close']=np.nan
        return out
    monkeypatch.setattr(pd,'read_parquet',read)
    with pytest.raises(ValueError,match='missing required minutes' if missing_row else 'missing/nonfinite'):
        research.load_minutes('TEST','2026-08-01T07:50Z','2026-08-01T07:52Z')


@pytest.mark.parametrize('candidate_index',range(6))
def test_each_candidate_is_invariant_to_future_minutes(research,candidate_index):
    frame=minutes(start='2026-08-01T00:00Z',periods=3*1440)
    rng=np.random.default_rng(20261103)
    close=100+np.cumsum(rng.normal(0,.05,len(frame)))
    frame['open']=np.r_[close[0],close[:-1]];frame['close']=close
    frame['high']=frame[['open','close']].max(axis=1)+.05
    frame['low']=frame[['open','close']].min(axis=1)-.05
    frame['volume']=rng.integers(50,250,len(frame))
    candidate=research.load(research.PROTOCOL)['candidates'][candidate_index]
    _,full,atr=research.make_signals(candidate,frame)
    _,prefix,prefix_atr=research.make_signals(candidate,frame.iloc[:2*1440])
    pd.testing.assert_series_equal(prefix,full.loc[prefix.index])
    pd.testing.assert_series_equal(prefix_atr,atr.loc[prefix_atr.index])


@pytest.mark.parametrize('slip',[2,5])
def test_production_next_bar_lots_costs_and_previous_minute_funding(research,slip):
    frame=minutes()
    # Settlement must use 07:59, never the 08:00 value.
    frame.loc[pd.Timestamp('2026-08-01T08:00Z'),'funding_rate']=.5
    signal=pd.Series([1],index=frame.index[:1],dtype='int64')
    atr=pd.Series([1.],index=signal.index)
    cfg=research.config(research.load(research.PROTOCOL),slip)
    product=research.bt.ProductSpec('TEST',1,1.,.01)
    result=research.bt.Backtester(cfg).run_portfolio([
        research.bt.Leg(product,frame,signal,'5m',atr)],frame.index[0],frame.index[-1]+pd.Timedelta(minutes=1))
    assert len(result.trades)==1
    tr=result.trades.iloc[0]
    assert tr.entry_time==pd.Timestamp('2026-08-01T07:55Z')
    assert tr.contracts==int(tr.contracts)>0
    assert tr.entry_price==pytest.approx(100*(1+slip/10000))
    assert tr.exit_price==pytest.approx(100*(1-slip/10000))
    assert tr.fees_usd==pytest.approx(tr.contracts*(tr.entry_price+tr.exit_price)*.0005*1.18)
    assert tr.funding_usd==pytest.approx(-tr.contracts*100*.0001)
    assert research.independent_accounting(result,cfg)>0
    assert research.independent_source_checks(result,cfg,{'TEST':frame},{'TEST':signal})>0
    assert not research.bt.audit_result(result)


def test_five_legs_use_one_wallet_not_five_capital_allocations(research):
    frame=minutes(periods=10)
    signal=pd.Series([1],index=frame.index[:1],dtype='int64')
    atr=pd.Series([.1],index=signal.index)
    cfg=research.config(research.load(research.PROTOCOL),5)
    legs=[research.bt.Leg(research.bt.ProductSpec(f'COIN{i}',i,1.,.01),frame,signal,'5m',atr) for i in range(5)]
    result=research.bt.Backtester(cfg).run_portfolio(legs,frame.index[0],frame.index[-1]+pd.Timedelta(minutes=1))
    trades=result.trades
    assert len(trades)>=2
    assert trades.margin_usd.sum()+trades.entry_fee_usd.sum()<=50000/85
    single=research.bt.Backtester(cfg).run_portfolio(legs[:1],frame.index[0],frame.index[-1]+pd.Timedelta(minutes=1))
    assert len(trades)<5 or trades.contracts.sum()<5*single.trades.contracts.sum()
    assert research.independent_accounting(result,cfg)>0
