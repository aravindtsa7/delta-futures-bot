r"""Bounded offline discovery: last 30 complete local days, two independent halves.

Run: .venv\Scripts\python.exe test_liquidity_sweep.py
Only winning templates are written; raw data, saved results and journals are not.
Walk-forward halves are retrospective stability screens, not untouched holdouts.
"""
from __future__ import annotations
import concurrent.futures
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import time

STARTED = time.perf_counter()
ROOT = Path(__file__).resolve().parent
COINS = ('BTCUSD','ETHUSD','BNBUSD','DOGEUSD','XRPUSD','SUIUSD','SOLUSD','ADAUSD')
TFS = ('15m','30m','45m','1h')
MODES = ('liquidity_sweep_sfp','liquidity_sweep_mss')
PROFILES = ((50000.,10,1.5),(50000.,15,2.),(100000.,15,2.))

def forbidden(*a, **k):
    raise AssertionError('Discovery is strictly offline')
def block_network():
    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.socket.sendto = forbidden
# Never read dotenv or import the web app/trading clients.
from pydantic_settings import BaseSettings
_init = BaseSettings.__init__
def safe_init(self,*a,**k):
    k['_env_file'] = None
    return _init(self,*a,**k)
BaseSettings.__init__ = safe_init
import backtester as bt
import data_manager as dm
import numpy as np
import pandas as pd
import strategies as st
BaseSettings.__init__ = _init

def metrics(result):
    trades=result.trades
    wins=trades.loc[trades.net_pnl_inr>0,'net_pnl_inr']
    return dict(net=float(result.summary['net_pnl_inr']),pf=float(result.summary['profit_factor']),
        dd=float(result.summary['max_drawdown_pct']),trades=len(trades),
        win_rate=float(result.summary['win_rate_pct']),avg_win=float(wins.mean()) if len(wins) else 0.)

def causal_checks():
    """Literal rule checks, both sides, prefix stability and real engine timing."""
    index=pd.date_range('2026-01-01',periods=80,freq='min',tz='UTC',name='time')
    for mode in MODES:
        f=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=100.,
            mark_open=100.,mark_high=101.,mark_low=99.,mark_close=100.,funding_rate=0.),index=index)
        if mode.endswith('sfp'):
            f.loc[index[40],['open','high','low','close','volume']]=[99.8,101.,98.,100.5,300.]
            f.loc[index[60],['open','high','low','close','volume']]=[100.2,102.,99.,99.5,300.]
            p=dict(swing_lookback=5,rsi_max_long=100,rsi_min_short=0)
        else:
            f.loc[index[40],['open','high','low','close','volume']]=[99.5,102.,98.,101.5,300.]
            f.loc[index[60],['open','high','low','close','volume']]=[100.5,102.,98.,98.5,300.]
            p=dict(swing_lookback=5,sweep_window=3)
        out=st.evaluate_custom_strategy(mode,p,f)
        assert out.signal.iloc[40]==1 and out.signal.iloc[60]==-1,mode
        # Direct formula independently specifies the level/raid/displacement.
        ssl=f.low.shift(1).rolling(5).min();bsl=f.high.shift(1).rolling(5).max()
        rv=f.volume/f.volume.rolling(20).mean();span=f.high-f.low
        if mode.endswith('sfp'):
            a=st.atr(f,14)
            long=(f.low<ssl)&(ssl-f.low<=1.8*a)&(f.close>ssl)&(np.minimum(f.open,f.close)-f.low>=.38*span)&(rv>=1.2)
            short=(f.high>bsl)&(f.high-bsl<=1.8*a)&(f.close<bsl)&(f.high-np.maximum(f.open,f.close)>=.38*span)&(rv>=1.2)
        else:
            ssl=ssl.shift(1);bsl=bsl.shift(1)
            long=(f.low.rolling(3).min()<ssl)&(f.close>f.open)&(f.close>ssl)&(f.close>f.high.shift(1))&(f.close-f.open>=.48*span)&(rv>=1.2)
            short=(f.high.rolling(3).max()>bsl)&(f.close<f.open)&(f.close<bsl)&(f.close<f.low.shift(1))&(f.open-f.close>=.48*span)&(rv>=1.2)
        pd.testing.assert_series_equal(out.cond_long,long,check_names=False)
        pd.testing.assert_series_equal(out.cond_short,short,check_names=False)
        prefix=st.evaluate_custom_strategy(mode,p,f.iloc[:41])
        pd.testing.assert_series_equal(prefix.signal,out.signal.iloc[:41])
        assert not st.evaluate_custom_strategy(mode,dict(p,use_cost_gate=1,cost_gate_mult=1e8),f).signal.any()
        cfg=bt.BacktestConfig(initial_capital_inr=50000,leverage=10,risk_per_trade_pct=1.5,
            sl_mode='pct',stop_loss_pct=1,take_profit_pct=2,fee_mode='taker_maker_tp',
            slippage_bps=2,max_daily_loss_pct=5,allow_min_one_lot=False)
        result=bt.Backtester(cfg).run_portfolio([bt.Leg(bt.ProductSpec('TEST',1,1,.0001),f,out.signal,'1m')])
        assert len(result.trades)>0
        assert (result.trades.entry_time==result.trades.signal_bar+pd.Timedelta(minutes=1)).all()
        assert result.trades.iloc[0].entry_time==index[41]
    # UI registration checked without importing app and its trading dependencies.
    for file in ('static/index.html','static/app.js'):
        content=(ROOT/file).read_text(encoding='utf-8')
        assert all(mode in content for mode in MODES)

def evaluate_coin(symbol):
    block_network()
    rawdir=ROOT/'data/raw_1m'
    files=[rawdir/f'{symbol}_{kind}_1m.parquet' for kind in ('TRADE','MARK','FUNDING')]
    if not all(p.exists() for p in files):
        raise FileNotFoundError(f'{symbol}: local trade/mark/funding required; no download permitted')
    times=pd.read_parquet(files[0],columns=['time']).time
    end=pd.Timestamp(int(times.max())+60,unit='s',tz='UTC').floor('D')
    start=end-pd.Timedelta(days=30);mid=start+pd.Timedelta(days=15)
    warm=max(pd.Timestamp(int(times.min()),unit='s',tz='UTC'),start-pd.Timedelta(days=30))
    if pd.Timestamp(int(times.min()),unit='s',tz='UTC')>start:
        raise ValueError(f'{symbol}: fewer than 30 complete local days')
    cutoff=int(warm.timestamp())
    raw=[pd.read_parquet(p,filters=[('time','>=',cutoff),('time','<',int(end.timestamp()))]) for p in files]
    for f in raw:
        assert not f.time.duplicated().any() and f.time.is_monotonic_increasing
    frame=raw[0].set_index('time')[['open','high','low','close','volume']]
    frame=frame.join(raw[1].set_index('time')[['open','high','low','close']].add_prefix('mark_'))
    rate=raw[2].set_index('time').close.reindex(frame.index)
    frame['funding_rate']=rate.groupby(frame.index.to_numpy()//28800).ffill()/100
    frame.index=pd.to_datetime(frame.index,unit='s',utc=True);frame.index.name='time'
    # Signals need warmup; simulations consume only their bounded window.
    simulation=frame.loc[frame.index>=start]
    b4=dm.resample_candles(frame,'4h')
    meta=json.loads((ROOT/f'data/metadata/{symbol}.json').read_text(encoding='utf-8'))
    product=bt.ProductSpec(symbol,int(meta['id']),float(meta['contract_value']),float(meta['tick_size']))
    rows=[];tested=0
    for tf in TFS:
        bars=dm.resample_candles(frame,tf);a=st.atr(bars,14)
        delta=pd.Timedelta(minutes=dm.TIMEFRAME_MINUTES[tf])
        for mode in MODES:
            for lookback in ((20,40) if mode.endswith('sfp') else (24,40)):
                p=dict(swing_lookback=lookback,use_cost_gate=1,use_4h_trend=0,
                    cost_gate_mult=1.,rt_cost=(.0005+.0002)*1.18+.0004)
                if mode.endswith('sfp'):
                    p.update(wick_ratio=.38,rvol_min=1.2,max_pierce_atr=1.8,rsi_period=14,rsi_max_long=60,rsi_min_short=40)
                else:p.update(sweep_window=3,body_ratio=.48,rvol_min=1.2)
                out=st.evaluate_custom_strategy(mode,p,bars,bars_4h=b4)
                # Truncating future bars cannot change an earlier signal.
                cut=len(bars)//2
                pre=st.evaluate_custom_strategy(mode,p,bars.iloc[:cut],bars_4h=b4[b4.index+pd.Timedelta(hours=4)<=bars.index[cut]])
                pd.testing.assert_series_equal(pre.signal,out.signal.iloc[:cut])
                outputs=[]
                for cap,lev,risk in PROFILES:
                    cfg=bt.BacktestConfig(initial_capital_inr=cap,leverage=lev,risk_per_trade_pct=risk,
                        sl_mode='atr',sl_atr_mult=1.5,tp_mode='atr',tp_atr_mult=3.,atr_sl_min_pct=.8,atr_sl_max_pct=4.,
                        breakeven_at_1r=True,trailing_atr_mult=0.,cooldown_bars=1,max_daily_loss_pct=5.,
                        fee_mode='taker_maker_tp',maker_fee=.0002,taker_fee=.0005,gst_rate=.18,
                        usd_inr_rate=85.,slippage_bps=2.,allow_min_one_lot=False,trigger_source='trade')
                    leg=bt.Leg(product,simulation,out.signal,tf,a,mode)
                    results=[bt.Backtester(cfg).run_portfolio([leg],lo,hi) for lo,hi in ((start,end),(start,mid),(mid,end))]
                    for result in results:
                        if len(result.trades):
                            assert (result.trades.entry_time==result.trades.signal_bar+delta).all()
                            assert (result.trades.entry_time>result.trades.signal_bar).all()
                    m=metrics(results[0]);m['half1']=metrics(results[1])['net'];m['half2']=metrics(results[2])['net']
                    m['passed']=bool(m['half1']>0 and m['half2']>0 and m['pf']>=1.25 and m['dd']<=12.)
                    m.update(capital=cap,leverage=lev,risk_pct=risk)
                    outputs.append(m);tested+=3
                # Both account sizes must pass; never extrapolate scaled P&L.
                for small in outputs[:2]:
                    large=outputs[2]
                    if small['passed'] and large['passed']:
                        rows.append(dict(symbol=symbol,timeframe=tf,rule_type=mode,params=p,
                            small=small,large=large,start=str(start),end=str(end),warmup=str(warm),config=asdict(cfg)))
    # One best pair per mode/coin, ranking worst-half ROI rather than total P&L.
    winners=[]
    for mode in MODES:
        choices=[r for r in rows if r['rule_type']==mode]
        if choices:
            winners.append(max(choices,key=lambda r:min(r['small']['half1'],r['small']['half2'])/r['small']['capital']))
    return dict(symbol=symbol,winners=winners,passed_pairs=len(rows),simulation_runs=tested,
        window_start=str(start),window_end=str(end),warmup_start=str(warm))

def main():
    block_network()
    causal_checks()
    print('Local-only: last 30 complete days; 30-day warmup where available; two fresh-capital 15-day halves.',flush=True)
    print('Grid: 8 coins x 4 timeframes x 2 modes x 2 lookbacks x 3 capital/risk profiles x full/H1/H2.',flush=True)
    with concurrent.futures.ProcessPoolExecutor(max_workers=min(4,os.cpu_count() or 1)) as pool:
        results=list(pool.map(evaluate_coin,COINS))
    print('\nTop Liquidity Sweep Winners (each account independently passes H1>0, H2>0, PF>=1.25, DD<=12%)')
    print('Coin    Mode TF   WR%  AvgWin50k Net50k Net1L PF50k/1L DD50k/1L Trades50k/1L H1/H2_50k H1/H2_1L')
    presets=[]
    for result in results:
        if not result['winners']:print(result['symbol']+'  NO QUALIFYING WINNER')
        for row in result['winners']:
            x,y=row['small'],row['large'];mode=row['rule_type'].split('_')[-1]
            print(f"{row['symbol']:7} {mode.upper():3} {row['timeframe']:3} {x['win_rate']:5.1f} {x['avg_win']:8.0f} {x['net']:8.0f} {y['net']:8.0f} {x['pf']:.2f}/{y['pf']:.2f} {x['dd']:.2f}/{y['dd']:.2f} {x['trades']}/{y['trades']} {x['half1']:.0f}/{x['half2']:.0f} {y['half1']:.0f}/{y['half2']:.0f}")
            for m in (x,y):
                verification=dict(generator='test_liquidity_sweep.py',period_days=30,start=row['start'],end=row['end'],history_start=row['warmup'],
                    full=m,selection='retrospective two-half stability screen; not untouched validation',
                    max_daily_loss_pct=5.,cooldown_bars=1)
                presets.append(dict(id=f"liq_sweep_{row['symbol'].lower()}_{mode}_{row['timeframe']}_{int(m['capital'])}",
                    name=f"🎯 LiqSweep: {row['symbol']} {mode.upper()} {row['timeframe']} (₹{int(m['capital']/1000)}k · {m['leverage']}x)",
                    symbol=row['symbol'],timeframe=row['timeframe'],rule_type=row['rule_type'],params=dict(row['params'],breakeven_at_1r=1,atr_sl_min_pct=.8),
                    capital_inr=m['capital'],leverage=m['leverage'],risk_pct=m['risk_pct'],sl_type='atr_mult',sl_value=1.5,
                    tp_type='atr_mult',tp_value=3.,trail_mult=0.,fee_mode='taker_maker_tp',slippage_bps=2.,verification=verification))
    path=ROOT/'data/strategies.json';original=path.read_bytes();existing=json.loads(original)
    ids={p['id'] for p in presets}
    payload=presets+[p for p in existing if p.get('id') not in ids
        and p.get('verification',{}).get('generator')!='test_liquidity_sweep.py']
    elapsed=time.perf_counter()-STARTED
    if elapsed>=39:
        raise RuntimeError(f'Runtime budget exceeded ({elapsed:.2f}s); presets not written')
    if payload != existing:
        temp=path.with_suffix('.liquidity.tmp')
        temp.write_text(json.dumps(payload,indent=2,ensure_ascii=True,allow_nan=False)+'\n',encoding='utf-8')
        os.replace(temp,path)
    # Evidence outside the repository; no generated datasets/results enter Git.
    evidence=Path(os.environ.get('TEMP',str(ROOT.parent)))/'delta-liquidity-sweep-evidence.json'
    evidence.write_text(json.dumps(dict(elapsed_seconds=time.perf_counter()-STARTED,
        original_strategies_sha256=hashlib.sha256(original).hexdigest(),results=results,
        registered=len(presets)),indent=2,allow_nan=False),encoding='utf-8')
    print(f"\nPASS causal/formula/prefix/next-bar checks; {sum(r['simulation_runs'] for r in results)} simulations; {len(presets)} presets registered; {time.perf_counter()-STARTED:.2f}s. Evidence: {evidence}")

if __name__=='__main__':main()
