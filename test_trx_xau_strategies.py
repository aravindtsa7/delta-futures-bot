r"""Bounded public-data sync + retrospective two-half discovery, no trading clients.

Run: .venv\Scripts\python.exe test_trx_xau_strategies.py
Gold defaults to XAUTUSD with the user's explicit approval after XAUUSD HTTP400.
30 complete local days, up to 30 prior warmup days. Missing data is never invented.
"""
from __future__ import annotations
import argparse
import concurrent.futures
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import time

STARTED=time.perf_counter()
ROOT=Path(__file__).resolve().parent
GENERATOR='test_trx_xau_strategies.py'
NEW=('session_orb_breakout','ema_macd_pullback')
from pydantic_settings import BaseSettings
_init=BaseSettings.__init__
def no_dotenv(self,*args,**kwargs):
    kwargs['_env_file']=None
    return _init(self,*args,**kwargs)
BaseSettings.__init__=no_dotenv
import backtester as bt
import data_manager as dm
import numpy as np
import pandas as pd
import strategies as st
BaseSettings.__init__=_init
def configure_runtime():
    # Imports during pytest collection must not redirect another test's data.
    dm.settings.DATA_DIR=str(ROOT/'data')
    dm.settings.HTTP_TIMEOUT_SECONDS=3
    dm.MAX_RETRIES=1

def serial(value):
    if isinstance(value,dict):return {k:serial(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [serial(x) for x in value]
    if isinstance(value,(np.integer,np.floating)):return serial(value.item())
    if isinstance(value,float) and not math.isfinite(value):return 'Infinity' if value>0 else None
    return value

def save(path,value):
    temp=path.with_suffix(path.suffix+'.trx-xau.tmp')
    temp.write_text(json.dumps(serial(value),indent=2,ensure_ascii=True,allow_nan=False)+'\n',encoding='utf-8')
    os.replace(temp,path)

def sync(symbol):
    """Use existing data first; public missing-range downloads only, time bounded."""
    configure_runtime()
    try:
        meta=ROOT/f'data/metadata/{symbol}.json'
        if not meta.exists():dm.fetch_and_save_metadata([symbol])
        product=json.loads(meta.read_text(encoding='utf-8'))
        if product.get('symbol')!=symbol:raise ValueError('metadata symbol mismatch')
        until=int(pd.Timestamp.now(tz='UTC').floor('min').timestamp())-60
        since=until-60*86400+60
        for kind in ('TRADE','MARK','FUNDING'):
            path=ROOT/f'data/raw_1m/{symbol}_{kind}_1m.parquet'
            if path.exists():
                ts=pd.read_parquet(path,columns=['time']).time
                expected=int((ts.max()-ts.min())//60+1)
                if ts.max()-ts.min()>=30*86400-60 and ts.is_unique and len(ts)==expected:
                    continue
            if time.perf_counter()-STARTED>15:raise TimeoutError('sync budget exhausted before complete history')
            def progress(*args,**kwargs):
                if time.perf_counter()-STARTED>18:raise TimeoutError('sync budget exhausted; no fabricated candles')
            dm.download_1m_range(symbol,kind,since,until,progress_cb=progress)
        return symbol,product,None
    except Exception as exc:
        return symbol,None,f'{type(exc).__name__}: {exc}'

def causal_checks():
    rng=np.random.default_rng(921)
    i=pd.date_range('2026-01-01',periods=2000,freq='min',tz='UTC',name='time')
    c=100*np.exp(np.cumsum(rng.normal(.00003,.0014,len(i))))
    o=np.r_[c[0],c[:-1]]
    f=pd.DataFrame(dict(open=o,high=np.maximum(o,c)*1.0005,low=np.minimum(o,c)*.9995,
        close=c,volume=rng.lognormal(5,.6,len(i)),mark_close=c,funding_rate=0.),index=i)
    for rule in NEW:
        p=dict(box_lookback=4,ema_trend=5,rvol_min=0,fast_ema=5,mid_ema=13,slow_ema=30,adx_min=0,pullback_tol=.01)
        out=st.evaluate_custom_strategy(rule,p,f)
        assert out.signal.any(),rule
        if rule==NEW[0]:
            hours=f.index.hour;session=(hours>=7)&(hours<19)
            expected_l=session&(f.close>f.high.shift(1).rolling(4).max())&(f.close>f.open)&(f.close>out.ema_trend)&out.rsi14.between(50,74)
            expected_s=session&(f.close<f.low.shift(1).rolling(4).min())&(f.close<f.open)&(f.close<out.ema_trend)&out.rsi14.between(26,50)
            expected_l=expected_l&(out.rvol>=0);expected_s=expected_s&(out.rvol>=0)
            alternate=f.copy();alternate.index=alternate.index.tz_convert('Asia/Calcutta')
            np.testing.assert_array_equal(st.evaluate_custom_strategy(rule,p,alternate).signal.to_numpy(),out.signal.to_numpy())
        else:
            h=out.macd_hist
            expected_l=(out.ema_fast>out.ema_mid)&(f.close>out.ema_slow)&(out.adx14>=0)&(f.low.rolling(2).min()<=out.ema_fast*1.01)&(f.close>out.ema_fast)&(f.close>f.open)&(h>h.shift(1))&out.rsi14.between(40,65)
            expected_s=(out.ema_fast<out.ema_mid)&(f.close<out.ema_slow)&(out.adx14>=0)&(f.high.rolling(2).max()>=out.ema_fast*.99)&(f.close<out.ema_fast)&(f.close<f.open)&(h<h.shift(1))&out.rsi14.between(35,60)
        pd.testing.assert_series_equal(out.cond_long,expected_l,check_names=False)
        pd.testing.assert_series_equal(out.cond_short,expected_s,check_names=False)
        pre=st.evaluate_custom_strategy(rule,p,f.iloc[:1000])
        pd.testing.assert_series_equal(pre.signal,out.signal.iloc[:1000])
        cfg=bt.BacktestConfig(initial_capital_inr=50000,leverage=10,risk_per_trade_pct=2,sl_mode='pct',
            stop_loss_pct=1,take_profit_pct=2,fee_mode='taker_maker_tp',slippage_bps=2,max_daily_loss_pct=5)
        result=bt.Backtester(cfg).run_portfolio([bt.Leg(bt.ProductSpec('TEST',1,1,.0001),f,out.signal,'1m')])
        assert len(result.trades)>0
        assert (result.trades.entry_time==result.trades.signal_bar+pd.Timedelta(minutes=1)).all()

def evaluate(task):
    symbol,meta,tf,rules=task
    rawdir=ROOT/'data/raw_1m'
    times=pd.read_parquet(rawdir/f'{symbol}_TRADE_1m.parquet',columns=['time']).time
    end=pd.Timestamp(int(times.max())+60,unit='s',tz='UTC').floor('D')
    start=end-pd.Timedelta(days=30);middle=start+pd.Timedelta(days=15)
    warm=max(pd.Timestamp(int(times.min()),unit='s',tz='UTC'),start-pd.Timedelta(days=30))
    if pd.Timestamp(int(times.min()),unit='s',tz='UTC')>start:raise ValueError(f'{symbol}: fewer than 30 complete days')
    parts=[pd.read_parquet(rawdir/f'{symbol}_{kind}_1m.parquet',filters=[('time','>=',int(warm.timestamp())),('time','<',int(end.timestamp()))]) for kind in ('TRADE','MARK','FUNDING')]
    for part in parts:assert part.time.is_unique and part.time.is_monotonic_increasing
    f=parts[0].set_index('time')[['open','high','low','close','volume']].join(parts[1].set_index('time')[['open','high','low','close']].add_prefix('mark_'))
    funding=parts[2].set_index('time').close.reindex(f.index)
    f['funding_rate']=funding.groupby(f.index.to_numpy()//28800).ffill()/100
    f.index=pd.to_datetime(f.index,unit='s',utc=True);f.index.name='time'
    simulation=f.loc[f.index>=start]
    product=bt.ProductSpec(symbol,int(meta['id']),float(meta['contract_value']),float(meta['tick_size']))
    b4=dm.resample_candles(f,'4h')
    rows=[];runs=0
    for tf in (tf,):
        bars=dm.resample_candles(f,tf);a=st.atr(bars,14)
        delta=pd.Timedelta(minutes=dm.TIMEFRAME_MINUTES[tf])
        for rule in rules:
            params=dict(cost_gate_mult=0,use_cost_gate=0,use_4h_trend=0)
            if rule in st.STRATEGIES:
                out=st.generate_signals(rule,bars,bars_4h=b4)
            else:out=st.evaluate_custom_strategy(rule,params,bars,bars_4h=b4)
            cut=len(bars)//2
            prior4=b4[b4.index+pd.Timedelta(hours=4)<=bars.index[cut]]
            pre=(st.generate_signals(rule,bars.iloc[:cut],bars_4h=prior4) if rule in st.STRATEGIES else
                 st.evaluate_custom_strategy(rule,params,bars.iloc[:cut],bars_4h=prior4))
            pd.testing.assert_series_equal(pre.signal,out.signal.iloc[:cut])
            for gate in ((False,True) if symbol.startswith('XAU') else (False,)):
                signal=out.signal
                if gate:
                    allowed=st.cost_gate(bars,.0005,.18,2.,multiplier=2.)['allow']
                    signal,_=st.apply_entry_filters(out,{'costgate':(allowed,allowed)})
                accounts=[]
                for capital in (50000.,100000.):
                    cfg=bt.BacktestConfig(initial_capital_inr=capital,leverage=10,risk_per_trade_pct=2,
                        sl_mode='atr',sl_atr_mult=1.5,tp_mode='atr',tp_atr_mult=3.,atr_sl_min_pct=.1 if symbol.startswith('XAU') else .8,
                        atr_sl_max_pct=4.,breakeven_at_1r=True,trailing_atr_mult=0.,cooldown_bars=1,max_daily_loss_pct=5.,
                        fee_mode='taker_maker_tp',maker_fee=.0002,taker_fee=.0005,gst_rate=.18,usd_inr_rate=85.,
                        slippage_bps=2.,allow_min_one_lot=False,trigger_source='trade')
                    leg=bt.Leg(product,simulation,signal,tf,a,rule)
                    results=[bt.Backtester(cfg).run_portfolio([leg],lo,hi) for lo,hi in ((start,end),(start,middle),(middle,end))]
                    for result in results:
                        if len(result.trades):assert (result.trades.entry_time==result.trades.signal_bar+delta).all()
                    full=results[0];s=full.summary;wins=full.trades.loc[full.trades.net_pnl_inr>0,'net_pnl_inr']
                    m=dict(capital=capital,net=float(s['net_pnl_inr']),pf=float(s['profit_factor']),dd=float(s['max_drawdown_pct']),
                        win_rate=float(s['win_rate_pct']),avg_win=float(wins.mean()) if len(wins) else 0.,trades=len(full.trades),
                        half1=float(results[1].summary['net_pnl_inr']),half2=float(results[2].summary['net_pnl_inr']))
                    m['passed']=bool(m['half1']>0 and m['half2']>0 and m['pf']>=1.2 and m['dd']<=12)
                    accounts.append(m);runs+=3
                rows.append(dict(symbol=symbol,rule_type=rule,timeframe=tf,cost_gate=gate,accounts=accounts,params=params,
                    start=str(start),end=str(end),history_start=str(warm),config=asdict(cfg)))
    return dict(symbol=symbol,rows=rows,runs=runs)

def preset(rule,symbol='ALL',tf='1h',params=None):
    return dict(id=f'universal_{rule}',name=f'Template: {rule}',symbol=symbol,timeframe=tf,rule_type=rule,params=params or {},
        capital_inr=50000,leverage=10,risk_pct=2,sl_type='atr_mult',sl_value=1.5,tp_type='atr_mult',tp_value=3.,
        trail_mult=0.,fee_mode='taker_maker_tp',slippage_bps=2.)

def main():
    configure_runtime()
    args=argparse.ArgumentParser();args.add_argument('--gold-symbol',choices=['XAUUSD','XAUTUSD'],default='XAUTUSD');args=args.parse_args()
    causal_checks()
    names=('TRXUSD',args.gold_symbol)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:availability=list(pool.map(sync,names))
    tasks=[];missing={}
    for symbol,meta,error in availability:
        if error:missing[symbol]=error;print(f'{symbol}: DATA NOT VERIFIED — {error}',flush=True)
        else:tasks.append((symbol,meta))
    print('30 complete local days, two independent 15-day halves; all custom rules + built-ins; fees/GST/2bps.',flush=True)
    results=[]
    grouped={}
    rules=list(dict.fromkeys(list(st.CUSTOM_RULE_TYPES)+list(st.STRATEGIES)))
    with concurrent.futures.ProcessPoolExecutor(max_workers=min(4,os.cpu_count() or 1)) as pool:
        futures={pool.submit(evaluate,(symbol,meta,tf,rules[offset:offset+6])):symbol
            for symbol,meta in tasks for tf in ('15m','30m','45m','1h') for offset in range(0,len(rules),6)}
        for future,symbol in futures.items():
            try:
                result=future.result()
                group=grouped.setdefault(symbol,dict(symbol=symbol,rows=[],runs=0))
                group['rows'].extend(result['rows']);group['runs']+=result['runs']
            except Exception as exc:missing[symbol]=f'{type(exc).__name__}: {exc}';print(symbol,'NOT VERIFIED:',missing[symbol])
    results=list(grouped.values())
    path=ROOT/'data/strategies.json';existing=json.loads(path.read_text(encoding='utf-8'))
    universal={p.get('rule_type') for p in existing if p.get('symbol')=='ALL'}
    additions=[preset(rule) for rule in dict.fromkeys(list(NEW)+list(st.CUSTOM_RULE_TYPES)) if rule not in universal]
    winners=[]
    print('\nSymbol Rule TF Gate WR% AvgWin50k Net50k/1L PF50k/1L DD50k/1L Trades H1/H2_50k H1/H2_1L')
    for result in results:
        eligible=[r for r in result['rows'] if all(m['passed'] for m in r['accounts'])]
        selected=sorted(eligible,key=lambda r:min(r['accounts'][0]['half1'],r['accounts'][0]['half2']),reverse=True)
        # Keep the best existing and each new archetype visible, if qualified.
        for rule in dict.fromkeys(r['rule_type'] for r in selected):
            row=next(r for r in selected if r['rule_type']==rule);x,y=row['accounts']
            print(f"{result['symbol']} {rule} {row['timeframe']} {int(row['cost_gate'])} {x['win_rate']:.1f} {x['avg_win']:.0f} {x['net']:.0f}/{y['net']:.0f} {x['pf']:.2f}/{y['pf']:.2f} {x['dd']:.2f}/{y['dd']:.2f} {x['trades']}/{y['trades']} {x['half1']:.0f}/{x['half2']:.0f} {y['half1']:.0f}/{y['half2']:.0f}")
            if rule not in st.CUSTOM_RULE_TYPES:continue
            winners.append(row)
            for m in row['accounts']:
                p=preset(rule,result['symbol'],row['timeframe'],dict(row['params'],breakeven_at_1r=1,atr_sl_min_pct=row['config']['atr_sl_min_pct']))
                p.update(id=f"trx_xau_{result['symbol'].lower()}_{rule}_{int(m['capital'])}",name=f"Winner: {result['symbol']} {rule} {row['timeframe']} (INR{int(m['capital'])})",capital_inr=m['capital'],
                    verification=dict(generator=GENERATOR,metrics=m,start=row['start'],end=row['end'],history_start=row['history_start'],
                        max_daily_loss_pct=5.,cooldown_bars=1,cost_gate=row['cost_gate'],selection='retrospective two-half stability screen; not untouched holdout'))
                additions.insert(0,p)
        for rule in NEW:
            choices=[r for r in result['rows'] if r['rule_type']==rule]
            best=max(choices,key=lambda r:r['accounts'][0]['net'])
            if not any(r['rule_type']==rule for r in selected):
                a=best['accounts'][0];print(f"{result['symbol']} {rule}: NO QUALIFYING PRESET; best net50k={a['net']:.2f}, PF={a['pf']:.2f}, DD={a['dd']:.2f}%, H1/H2={a['half1']:.2f}/{a['half2']:.2f}")
        if not selected:print(result['symbol'],'NO QUALIFYING WINNER')
    ids={p['id'] for p in additions}
    payload=additions+[p for p in existing if p.get('id') not in ids and p.get('verification',{}).get('generator')!=GENERATOR]
    if time.perf_counter()-STARTED>=44:raise RuntimeError('45-second budget reached; no presets written')
    save(path,payload)
    symbols_path=ROOT/'data/app_symbols.json';symbols=json.loads(symbols_path.read_text(encoding='utf-8'))
    if args.gold_symbol=='XAUTUSD' and not (ROOT/'data/metadata/XAUUSD.json').exists():
        symbols=[s for s in symbols if s!='XAUUSD']
    save(symbols_path,list(dict.fromkeys(symbols+list(names))))
    # Check API symbol filtering by executing just that function, no web-app imports.
    import ast
    from fastapi.responses import JSONResponse
    node=next(n for n in ast.parse((ROOT/'app.py').read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='list_strategies')
    node.decorator_list=[];namespace={'_load_custom_strategies':lambda:json.loads(path.read_text(encoding='utf-8')),'JSONResponse':JSONResponse,'clean':lambda v:v}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'app.py:list_strategies','exec'),namespace)
    for symbol in names+('NEWCOINUSD',):
        items=json.loads(namespace['list_strategies'](symbol).body)['strategies']
        assert set(st.CUSTOM_RULE_TYPES)<=set(p['rule_type'] for p in items if p.get('symbol')=='ALL')
        assert all(p.get('symbol') in (symbol,'ALL') for p in items)
    evidence=Path(os.environ.get('TEMP',str(ROOT.parent)))/'delta-trx-xau-discovery.json'
    save(evidence,dict(runtime=time.perf_counter()-STARTED,missing=missing,results=results,winners=winners))
    print(f"PASS causal/formula/prefix/next-bar and symbol-filter checks; {sum(r['runs'] for r in results)} simulations; {2*len(winners)} winning presets; {len(payload)} templates total; {time.perf_counter()-STARTED:.2f}s. Evidence: {evidence}")
    if missing:print('INCOMPLETE DATA:',json.dumps(missing))

if __name__=='__main__':main()
