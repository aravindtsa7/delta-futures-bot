"""Preregistered five-coin frequent-strategy research using production execution.

Reproduce with the existing Windows venv:
  .venv/Scripts/python.exe research_frequent_futures.py --output C:/TSA/delta-futures-review-evidence/frequent-five-coin-20261103

No bot, journals, saved library or presets are written. Artifacts live outside Git.
The protocol defines six candidates; selection writes a freeze before validation.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent
PROTOCOL=ROOT/'research_frequent_futures_protocol.json'

def no_network(*args,**kwargs):
    raise AssertionError('This experiment uses complete local inputs only; network is forbidden')
socket.socket.connect=no_network
socket.socket.connect_ex=no_network
socket.socket.sendto=no_network

# Numerical settings are explicit in the protocol. Do not load credentials.
from pydantic_settings import BaseSettings
_original_init=BaseSettings.__init__
def _without_dotenv(self,*args,**kwargs):
    kwargs['_env_file']=None
    return _original_init(self,*args,**kwargs)
BaseSettings.__init__=_without_dotenv
import backtester as bt
import data_manager as dm
import numpy as np
import pandas as pd
import strategies as st
BaseSettings.__init__=_original_init

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def clean(value):
    if isinstance(value,dict):return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)):return [clean(v) for v in value]
    if isinstance(value,(pd.Timestamp,Path)):return str(value)
    if isinstance(value,np.generic):return clean(value.item())
    if isinstance(value,float) and not math.isfinite(value):return 'Infinity' if value>0 else None
    return value
def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(clean(value),indent=2,allow_nan=False),encoding='utf-8')
def load(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def utc(value):return pd.Timestamp(value)

def inventory_inputs(protocol,output):
    inventory={}
    for sym in protocol['symbols']:
        item={}
        for kind in ('TRADE','MARK','FUNDING'):
            path=ROOT/f'data/raw_1m/{sym}_{kind}_1m.parquet'
            frame=pd.read_parquet(path);times=frame.time.to_numpy()
            unique=np.sort(np.unique(times));diff=np.diff(unique);gaps=np.flatnonzero(diff>60)
            item[kind]=dict(rows=len(frame),first=str(pd.Timestamp(times.min(),unit='s',tz='UTC')),
                last=str(pd.Timestamp(times.max(),unit='s',tz='UTC')),duplicate_rows=len(times)-len(unique),
                unordered=int((np.diff(times)<=0).sum()),missing_minutes=int(((diff[diff>60]//60)-1).sum()),
                gap_count=len(gaps),nulls=frame.isna().sum().to_dict(),sha256=digest(path),
                gaps=[dict(first_missing=str(pd.Timestamp(unique[i]+60,unit='s',tz='UTC')),
                    last_missing=str(pd.Timestamp(unique[i+1]-60,unit='s',tz='UTC'))) for i in gaps])
        meta=load(ROOT/f'data/metadata/{sym}.json')
        item['metadata']={key:meta.get(key) for key in ('id','symbol','contract_value','tick_size','state')}
        inventory[sym]=item
    write(output/'input_inventory.json',inventory)
    return inventory


def load_minutes(symbol,start,end):
    frames=[]
    for kind in ('TRADE','MARK','FUNDING'):
        path=ROOT/f'data/raw_1m/{symbol}_{kind}_1m.parquet'
        if not path.exists():raise ValueError(f'Missing required local input: {path}')
        f=pd.read_parquet(path,filters=[('time','>=',int(utc(start).timestamp())),('time','<',int(utc(end).timestamp()))])
        if f.empty or not f.time.is_unique or not f.time.is_monotonic_increasing:
            raise ValueError(f'{symbol} {kind}: empty/duplicate/unordered input')
        frames.append(f)
    trade,mark,funding=frames
    index=pd.date_range(utc(start),utc(end)-pd.Timedelta(minutes=1),freq='min')
    expected=index.as_unit('s').asi8
    for kind,f in zip(('TRADE','MARK','FUNDING'),frames):
        missing=np.setdiff1d(expected,f.time.to_numpy())
        if len(missing):raise ValueError(f'{symbol} {kind}: {len(missing)} missing required minutes; first {pd.Timestamp(missing[0],unit="s",tz="UTC")}')
    out=trade.set_index('time')[['open','high','low','close','volume']].join(
        mark.set_index('time')[['open','high','low','close']].add_prefix('mark_'))
    # Funding publishes percentage points. Only forward-fill within the same
    # 8h window; never backfill or substitute zero for missing history.
    rates=funding.set_index('time').close.reindex(out.index)
    out['funding_rate']=rates.groupby(out.index.to_numpy()//28800).ffill()/100.
    out.index=pd.to_datetime(out.index,unit='s',utc=True);out.index.name='time'
    required=['open','high','low','close','mark_open','mark_high','mark_low','mark_close','funding_rate']
    if not np.isfinite(out[required].to_numpy(float)).all():raise ValueError(f'{symbol}: missing/nonfinite market or funding inputs')
    for prefix in ('','mark_'):
        prices=out[[prefix+c for c in ('open','high','low','close')]]
        if (prices<=0).any().any():raise ValueError(f'{symbol}: nonpositive {prefix}price')
        if ((prices[prefix+'high']<prices.max(axis=1))|(prices[prefix+'low']>prices.min(axis=1))).any():
            raise ValueError(f'{symbol}: inconsistent {prefix}OHLC')
    return out

def make_signals(candidate,minutes):
    bars=dm.resample_candles(minutes,candidate['timeframe'])
    out=st.evaluate_custom_strategy(candidate['rule'],candidate['params'],bars)
    signal=out.signal
    if 'regime_adx_max' in candidate:
        allowed=st.adx(bars,14).adx<=candidate['regime_adx_max']
        # Apply the range regime BEFORE edge detection, avoiding suppressed
        # transitions when a previously blocked bar becomes eligible.
        signal,_=st.apply_entry_filters(out,{'range_regime':(allowed,allowed)})
    return bars,signal,st.atr(bars,14)

def config(protocol,slip):
    values=dict(protocol['execution']);values.pop('slippage_bps_tested')
    values['slippage_bps']=slip
    return bt.BacktestConfig(**values)

def factor(trades):
    net=trades.net_pnl_inr.to_numpy(float)
    wins=float(net[net>0].sum());loss=-float(net[net<0].sum())
    return wins/loss if loss else (math.inf if wins else math.nan)

def independent_accounting(result,cfg):
    """Separate arithmetic check; no production price/fee/stop helper calls."""
    checked=0
    for tr in result.trades.itertuples():
        direction=1 if tr.side=='LONG' else -1
        quantity=int(tr.contracts)*float(tr.contract_value)
        gross=direction*quantity*(tr.exit_price-tr.entry_price)
        entry_fee=quantity*tr.entry_price*.0005*1.18
        exit_fee=quantity*tr.exit_price*.0005*1.18
        expected={'gross_pnl_usd':gross,'entry_fee_usd':entry_fee,'exit_fee_usd':exit_fee,
            'fees_usd':entry_fee+exit_fee,'gst_usd':quantity*(tr.entry_price+tr.exit_price)*.0005*.18,
            'net_pnl_usd':gross-entry_fee-exit_fee+tr.funding_usd,
            'margin_usd':quantity*tr.entry_price/cfg.leverage}
        for key,value in expected.items():
            assert math.isclose(value,getattr(tr,key),rel_tol=1e-9,abs_tol=1e-8),(tr.trade_id,key,value,getattr(tr,key))
            checked+=1
        assert tr.contracts==int(tr.contracts) and tr.contracts>=1
    expected_final=cfg.initial_capital_inr+result.trades.net_pnl_inr.sum()
    assert math.isclose(float(result.equity.iloc[-1]),expected_final,rel_tol=1e-9,abs_tol=1e-7)
    return checked+1

def independent_source_checks(result,cfg,markets,signals):
    """Check source prices, causal direction and funding without engine helpers."""
    checks=0
    for tr in result.trades.itertuples():
        frame=markets[tr.symbol];direction=1 if tr.side=='LONG' else -1
        assert signals[tr.symbol].at[tr.signal_bar]==direction
        assert math.isclose(tr.raw_entry_price,frame.at[tr.entry_time,'open'],abs_tol=1e-10)
        assert math.isclose(tr.entry_price,tr.raw_entry_price*(1+direction*cfg.slippage_bps/10000),rel_tol=1e-12)
        assert math.isclose(tr.exit_price,tr.raw_exit_price*(1-direction*cfg.slippage_bps/10000),rel_tol=1e-12)
        assert tr.risk_pct_of_equity<=cfg.risk_per_trade_pct+1e-8
        assert tr.margin_usd+tr.entry_fee_usd<=tr.available_at_entry_usd+1e-8
        checks+=6
    events=result.funding
    for sym,group in events.groupby('symbol'):
        frame=markets[sym]
        previous=pd.DatetimeIndex(group.time)-pd.Timedelta(minutes=1)
        marks=frame.loc[previous,'mark_close'].to_numpy()
        rates=frame.loc[previous,'funding_rate'].to_numpy()
        direction=np.where(group.side=='LONG',1,-1)
        expected=-direction*group.contracts.to_numpy()*group.contract_value.to_numpy()*marks*rates
        np.testing.assert_allclose(group.mark_price,marks,rtol=1e-12)
        np.testing.assert_allclose(group.funding_rate,rates,rtol=1e-12)
        np.testing.assert_allclose(group.cashflow_usd,expected,rtol=1e-12,atol=1e-10)
        checks+=3*len(group)
    return checks

def metrics(result,signals,candidate,window,symbol,cfg,phase,wallet='independent'):
    start,end=utc(window['start']),utc(window['end']);days=float((end-start)/pd.Timedelta(days=1))
    trades=result.trades
    delta=pd.Timedelta(minutes=dm.TIMEFRAME_MINUTES[candidate['timeframe']])
    count=sum(start<=t+delta<end for t in signals[signals!=0].index)
    if len(trades):
        assert (trades.entry_time==trades.signal_bar+delta).all(),'Same-bar or delayed entries'
        assert (trades.entry_time>trades.signal_bar).all()
    audit=bt.audit_result(result)
    assert not audit,audit[:5]
    independent_checks=independent_accounting(result,cfg)
    return dict(phase=phase,wallet=wallet,candidate=candidate['id'],symbol=symbol,window=window['name'],
        start=str(start),end=str(end),days=days,slippage_bps=cfg.slippage_bps,
        capital_inr=cfg.initial_capital_inr,signals=int(count),trades=len(trades),trades_per_day=len(trades)/days,
        net_pnl_inr=float(trades.net_pnl_inr.sum()),gross_pnl_inr=float(trades.gross_pnl_usd.sum()*cfg.usd_inr_rate),
        fees_gst_inr=float(trades.fees_usd.sum()*cfg.usd_inr_rate),gst_inr=float(trades.gst_usd.sum()*cfg.usd_inr_rate),
        funding_inr=float(trades.funding_usd.sum()*cfg.usd_inr_rate),slippage_inr=float(trades.slippage_usd.sum()*cfg.usd_inr_rate),
        profit_factor=factor(trades),max_drawdown_pct=float(result.summary['max_drawdown_pct']),
        win_rate_pct=float((trades.net_pnl_inr>0).mean()*100) if len(trades) else 0.,
        mean_minutes_held=float(trades.minutes_held.mean()) if len(trades) else 0.,
        final_equity_inr=float(result.equity.iloc[-1]),engine_signals=result.summary['signals'],
        ambiguous_candles=int(result.summary['ambiguous_1m_bars']),daily_halts=int(result.summary['daily_halts']),
        skipped_insufficient_capital=int(result.summary['skipped_insufficient_capital']),
        skipped_lot_budget=int(result.summary['skipped_lot_budget']),skipped_cooldown=int(result.summary['skipped_cooldown']),
        skipped_in_position=int(result.summary['skipped_in_position']),funding_events=len(result.funding),
        accounting_checks=independent_checks,audit_errors=0)

def persist(directory,result,metric,keep_equity):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    write(directory/'metrics.json',metric)
    result.trades.to_csv(directory/'trades.csv',index=False)
    result.funding.to_csv(directory/'funding.csv',index=False)
    if keep_equity:result.equity.to_frame().to_parquet(directory/'equity.parquet')
    # Attribution from the continuous wallet; equity delta includes MTM, while
    # trade-net attribution books funding/costs into the month the trade closes.
    monthly=[];prior=float(metric['capital_inr'])
    for month,values in result.equity.groupby(result.equity.index.strftime('%Y-%m')):
        last=float(values.iloc[-1]);monthly.append(dict(month=month,equity_change_inr=last-prior,end_equity_inr=last));prior=last
    write(directory/'monthly_equity_changes.json',monthly)

def _product(symbol):
    meta=load(ROOT/f'data/metadata/{symbol}.json')
    assert meta['symbol']==symbol
    return bt.ProductSpec(symbol,int(meta['id']),float(meta['contract_value']),float(meta['tick_size']))

def worker(job):
    protocol=load(PROTOCOL);symbol=job['symbol'];phase=job['phase'];output=Path(job['output'])
    candidates=job['candidates'];windows=job['windows']
    start=job.get('warmup',protocol['warmup_start_utc']);end=max(utc(w['end']) for w in windows)
    minutes=load_minutes(symbol,start,end);product=_product(symbol);rows=[];checks=[]
    for candidate in candidates:
        bars,signals,a=make_signals(candidate,minutes)
        # At two cutoffs verify that adding future raw minutes cannot change
        # prior signals. ATR also stays identical (up to floating precision).
        for fraction in (.33,.66):
            at=int(len(bars)*fraction);cut=bars.index[at]
            prior=minutes.loc[minutes.index<cut]
            small,small_sig,small_atr=make_signals(candidate,prior)
            pd.testing.assert_series_equal(small_sig,signals.loc[small_sig.index])
            np.testing.assert_allclose(small_atr,a.loc[small_atr.index],rtol=1e-12,atol=1e-12,equal_nan=True)
            checks.append(dict(symbol=symbol,candidate=candidate['id'],cut=str(cut),bars=len(small),passed=True))
        for window in windows:
            for slip in protocol['execution']['slippage_bps_tested']:
                cfg=config(protocol,slip)
                leg=bt.Leg(product,minutes,signals,candidate['timeframe'],a,candidate['rule'])
                result=bt.Backtester(cfg).run_portfolio([leg],utc(window['start']),utc(window['end']))
                metric=metrics(result,signals,candidate,window,symbol,cfg,phase)
                metric['source_checks']=independent_source_checks(result,cfg,{symbol:minutes},{symbol:signals})
                directory=output/phase/candidate['id']/symbol/window['name']/f'{slip}bps'
                persist(directory,result,metric,keep_equity=phase!='selection')
                rows.append(metric)
    write(output/phase/f'prefix_{symbol}.json',checks)
    return rows

def numeric(value):
    if value=='Infinity':return math.inf
    return math.nan if value is None else float(value)
def passes(row,frequency=True):
    return (row['net_pnl_inr']>0 and numeric(row['profit_factor'])>=1.2 and row['max_drawdown_pct']<=12
            and (not frequency or row['trades_per_day']>=2))
def rank_training(rows,candidate_ids):
    ranked=[]
    for ident in sorted(candidate_ids):
        chosen=[r for r in rows if r['candidate']==ident and r['slippage_bps']==5]
        qualified=sum(passes(r) for r in chosen)
        rank=(qualified==10,qualified,min(r['net_pnl_inr']/r['capital_inr'] for r in chosen),sum(r['net_pnl_inr'] for r in chosen))
        ranked.append(dict(candidate=ident,rank=rank,eligible_cases=qualified,
            worst_return_pct=rank[2]*100,total_net_5bps=rank[3]))
    return sorted(ranked,key=lambda r:r['rank'],reverse=True)

def run_jobs(jobs,workers):
    rows=[]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(worker,job):job['symbol'] for job in jobs}
        for future in as_completed(futures):
            result=future.result();rows.extend(result)
            print(f"Completed {jobs[0]['phase']} {futures[future]}: {len(result)} runs",flush=True)
    return rows

def shared(protocol,candidate,windows,output):
    minutes={sym:load_minutes(sym,protocol['warmup_start_utc'],max(utc(w['end']) for w in windows)) for sym in protocol['symbols']}
    frames={sym:make_signals(candidate,f) for sym,f in minutes.items()}
    rows=[]
    for window in windows:
        for slip in protocol['execution']['slippage_bps_tested']:
            cfg=config(protocol,slip)
            legs=[bt.Leg(_product(sym),minutes[sym],frames[sym][1],candidate['timeframe'],frames[sym][2],candidate['rule']) for sym in protocol['symbols']]
            result=bt.Backtester(cfg).run_portfolio(legs,utc(window['start']),utc(window['end']))
            joined=pd.concat([frames[sym][1] for sym in protocol['symbols']])
            row=metrics(result,joined,candidate,window,'ALL',cfg,'shared','shared_50000')
            row['source_checks']=independent_source_checks(result,cfg,minutes,{sym:frames[sym][1] for sym in protocol['symbols']})
            directory=output/'shared'/window['name']/f'{slip}bps'
            persist(directory,result,row,True);rows.append(row)
            breakdown=[]
            for sym in protocol['symbols']:
                trades=result.trades[result.trades.symbol==sym]
                delta=pd.Timedelta(minutes=dm.TIMEFRAME_MINUTES[candidate['timeframe']])
                signals=frames[sym][1];count=sum(utc(window['start'])<=t+delta<utc(window['end']) for t in signals[signals!=0].index)
                breakdown.append(dict(symbol=sym,signals=int(count),trades=len(trades),trades_per_day=len(trades)/row['days'],
                    net_pnl_inr=float(trades.net_pnl_inr.sum()),profit_factor=factor(trades),fees_gst_inr=float(trades.fees_usd.sum()*85),
                    funding_inr=float(trades.funding_usd.sum()*85),max_drawdown_pct=None,
                    drawdown_note='Only the aggregate shared wallet has a capital/equity drawdown; per-coin cash allocations are not separate wallets.'))
            write(directory/'coin_breakdown.json',breakdown)
            print(f"Shared {window['name']} {slip}bps: trades={row['trades']}, net={row['net_pnl_inr']:.2f}",flush=True)
    return rows

def manifest(output):
    protocol=load(PROTOCOL)
    inputs={}
    for sym in protocol['symbols']:
        for kind in ('TRADE','MARK','FUNDING'):
            p=ROOT/f'data/raw_1m/{sym}_{kind}_1m.parquet';inputs[str(p.relative_to(ROOT))]=digest(p)
        p=ROOT/f'data/metadata/{sym}.json';inputs[str(p.relative_to(ROOT))]=digest(p)
    protected={str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'data/bot_profiles.json',ROOT/'data/saved_backtests.json',ROOT/'data/strategies.json')}
    value=dict(protocol_sha256=digest(PROTOCOL),base_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        branch=subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip(),
        git_status=subprocess.check_output(['git','status','--short'],cwd=ROOT,text=True),platform=platform.platform(),python=sys.version,
        dependencies={name:importlib.metadata.version(name) for name in ('numpy','pandas','pyarrow','pydantic','pydantic-settings','pytest')},
        source_hashes={name:digest(ROOT/name) for name in ('backtester.py','strategies.py','data_manager.py','trade_rules.py','research_frequent_futures.py')},
        inputs=inputs,protected=protected)
    write(output/'manifest_before.json',value)
    write(output/'preregistered_protocol.json',protocol)
    return value

def concentration(output,rows):
    # Continuous 61-day validation at higher costs is the dependence diagnostic.
    selected=[r for r in rows if r['window']=='validation_combined' and r['slippage_bps']==5]
    net_by_coin={r['symbol']:r['net_pnl_inr'] for r in selected}
    positive=sum(max(v,0) for v in net_by_coin.values())
    trades=[];months={}
    for row in selected:
        directory=output/'validation'/row['candidate']/row['symbol']/row['window']/'5bps'
        f=pd.read_csv(directory/'trades.csv');trades.extend(f.net_pnl_inr.tolist())
        for m in load(directory/'monthly_equity_changes.json'):months[m['month']]=months.get(m['month'],0)+m['equity_change_inr']
    positive_months=sum(max(v,0) for v in months.values())
    best=sorted([v for v in trades if v>0],reverse=True)[:5]
    return dict(net_by_coin=net_by_coin,monthly_equity_change=months,
        max_positive_coin_share=max([max(v,0)/positive for v in net_by_coin.values()],default=0) if positive else None,
        max_positive_month_share=max([max(v,0)/positive_months for v in months.values()],default=0) if positive_months else None,
        top_five_winners_inr=best,total_net_inr=sum(trades),net_without_five_winners_inr=sum(trades)-sum(best),
        caveat='Fixed-ledger sensitivity; deleting trades would change sizing, so this is not a counterfactual strategy replay.')

def detailed_tables(output,protocol,selected,training,validation,shared_rows,backcast):
    def table(headers,rows):
        def fmt(v):return f'{v:,.2f}' if isinstance(v,(float,np.floating)) else str(v)
        return ['| '+' | '.join(headers)+' |','|'+'|'.join(['---']*len(headers))+'|']+[
            '| '+' | '.join(fmt(v) for v in row)+' |' for row in rows]
    inventory=load(output/'input_inventory.json');rows=[]
    lines=['','## Local input coverage (UTC)','']
    for sym in protocol['symbols']:
        item=inventory[sym];streams=[item[k] for k in ('TRADE','MARK','FUNDING')]
        rows.append([sym,max(x['first'] for x in streams),min(x['last'] for x in streams),
            sum(x['missing_minutes'] for x in streams),item['metadata']['contract_value'],item['metadata']['tick_size']])
    lines+=table(['Coin','Common first','Common last','Internal missing minutes','Contract value','Tick'],rows)
    lines+=['','BTC and ETH funding each start one minute after trade/mark; XRP trade ends two minutes before mark/funding. These boundary differences are outside all test windows. No duplicate timestamps or internal minute gaps were found. All chosen evaluation/warmup windows have 100% required-minute coverage. Full stream dates, nulls and hashes: input_inventory.json. No download was needed.','',
        'Training totals sum two fresh-capital selection windows (ten INR50000 account allocations). They are not the P&L of one continuous INR250000 portfolio.','',
        '## Separate validation months (independent accounts)','']
    chosen=sorted([r for r in validation if r['window'] in ('validation_August','validation_September')],key=lambda r:(r['window'],r['symbol'],r['slippage_bps']))
    keys=('window','symbol','slippage_bps','signals','trades','trades_per_day','net_pnl_inr','profit_factor','max_drawdown_pct','fees_gst_inr')
    lines+=table(['Window','Coin','Slip','Signals','Trades','Trades/day','Net INR','PF','DD %','Fees+GST INR'],[[r[k] for k in keys] for r in chosen])
    lines+=['','## Continuous shared-wallet per-coin attribution',''];rows=[];sensitivity={}
    for slip in protocol['execution']['slippage_bps_tested']:
        folder=output/'shared/validation_combined'/f'{slip}bps'
        coins=load(folder/'coin_breakdown.json');trades=pd.read_csv(folder/'trades.csv')
        for c in coins:rows.append([c['symbol'],slip,c['signals'],c['trades'],c['trades_per_day'],c['net_pnl_inr'],numeric(c['profit_factor']),c['fees_gst_inr']])
        sensitivity[str(slip)]=dict(monthly_equity_changes=load(folder/'monthly_equity_changes.json'),
            net_without_top_five_winners=float(trades.net_pnl_inr.sum()-trades.net_pnl_inr.nlargest(5).sum()),
            by_side=trades.groupby('side').net_pnl_inr.agg(['size','sum']).to_dict('index'))
    write(output/'shared_sensitivity.json',sensitivity)
    lines+=table(['Coin','Slip','Signals','Trades','Trades/day','Net INR','PF','Fees+GST INR'],rows)
    lines+=['',f'Shared monthly equity, top-five-winner and long/short sensitivity: `{json.dumps(clean(sensitivity))}`.']
    lines+=['','## Older frozen backcast (2025-11-15 to 2026-05-15, no reselection)','']
    keys=('symbol','slippage_bps','signals','trades','trades_per_day','net_pnl_inr','profit_factor','max_drawdown_pct','fees_gst_inr')
    lines+=table(['Coin','Slip','Signals','Trades','Trades/day','Net INR','PF','DD %','Fees+GST INR'],[[r[k] for k in keys] for r in sorted(backcast,key=lambda r:(r['symbol'],r['slippage_bps']))])
    lines+=['','## Continuous shared-wallet cost and ambiguity details','']
    keys=('slippage_bps','gross_pnl_inr','slippage_inr','fees_gst_inr','gst_inr','funding_inr','net_pnl_inr','ambiguous_candles','daily_halts')
    lines+=table(['Slip','Gross after slip INR','Embedded slip INR','Fees incl GST INR','GST subset INR','Funding INR','Net INR','Ambiguous SL/TP candles','Daily halts'],[[r[k] for k in keys] for r in shared_rows if r['window']=='validation_combined'])
    manifest_data=load(output/'manifest_before.json')
    lines+=['','Slippage is embedded in gross P&L; do not subtract it twice. PF uses net trade P&L. Source checks independently compare entry opens, causal signal direction, adverse fill arithmetic, risk/margin bounds and funding against the preceding local minute. Prefix checks recompute signals without future minutes. Accounting and local causal replay passed; independent exchange-data matching and actual fills remain NOT VERIFIED.','',
        'Fee reference: [Delta India trading fees](https://www.delta.exchange/fees?tab=trading-fees).','',
        f"Base commit: `{manifest_data['base_commit']}`. Worktree had existing changes; executed source hashes are retained. Python: `{manifest_data['python'].splitlines()[0]}`. Dependencies: `{json.dumps(manifest_data['dependencies'])}`.",'',
        f"Protocol SHA256: `{manifest_data['protocol_sha256']}`. Every input SHA256 is in manifest_before.json. Test results: test_results.txt; arithmetic/source checks: verification.json."]
    return lines


def report(output,protocol,selected,training,validation,shared_rows,backcast,ranked):
    all_rows=training+validation+shared_rows+backcast
    pd.DataFrame(all_rows).to_csv(output/'all_metrics.csv',index=False)
    dep=concentration(output,validation);write(output/'concentration.json',dep)
    required=[r for r in validation if r['window'] in ('validation_August','validation_September','validation_combined')]
    independent_pass=all(passes(r) for r in required)
    shared_required=[r for r in shared_rows if r['window']!='examined_October']
    shared_pass=all(passes(r) and r['trades_per_day']>=10 for r in shared_required)
    shared_coin_pass=True
    for r in shared_required:
        coins=load(output/'shared'/r['window']/f"{int(r['slippage_bps'])}bps"/'coin_breakdown.json')
        shared_coin_pass &= all(x['trades_per_day']>=2 for x in coins)
    dependence_pass=(dep['max_positive_coin_share'] is not None and dep['max_positive_coin_share']<=.5
        and dep['max_positive_month_share']<=.75 and dep['net_without_five_winners_inr']>0)
    qualifies=independent_pass and shared_pass and shared_coin_pass and dependence_pass
    conclusion=dict(status='Historical screens pass; prospective holdout still required' if qualifies else 'No qualifying strategy',
        selected=selected,selection_rank=ranked[0],independent_validation_pass=independent_pass,
        shared_validation_pass=shared_pass,shared_per_coin_frequency_pass=shared_coin_pass,
        concentration_pass=dependence_pass,untouched_historical_holdout=False,
        preset_added=False,note='This research script never writes presets or deployment files. A failed screen must not be promoted.')
    write(output/'conclusion.json',conclusion)
    def fmt(v):return '—' if v is None or (isinstance(v,float) and math.isnan(v)) else (f'{v:,.2f}' if isinstance(v,(int,float)) else str(v))
    lines=['# Frequent five-coin futures research','',conclusion['status']+'.',
        f"Frozen diagnostic finalist: **{selected['id']}**, `{selected['rule']}`, {selected['timeframe']}. One common rule and parameter set on all five coins.",'',
        'The finalist is selected only on May15–July31, before August/September results are computed. October is previously examined and cannot select or qualify it. No provably untouched historical holdout exists; validation here is chronological, not a claim of completely unseen data.','',
        'All fills use the production backtester: closed-bar signals, next-bar-open entry, integer lots, 10x leverage, 1% risk, ATR SL1.5 / TP3, SL clamp0.15–2%, no BE/trail/opposite exits, one-bar cooldown, daily loss stop5%, INR50000 per independent account, FX85. Taker0.05% on both legs plus GST18%; no scalper rebate. Slippage2/5bps per side. Trade triggers; previous-minute mark/funding at00/08/16. No zero funding substitutions.','',
        '## Earlier-date comparison (five separate INR50000 accounts; sum capital INR250000)','','| Candidate | Training net2bps | Training net5bps | Eligible coin/window cases at5bps | Worst5bps return % |','|---|---:|---:|---:|---:|']
    for r in ranked:
        net2=sum(x['net_pnl_inr'] for x in training if x['candidate']==r['candidate'] and x['slippage_bps']==2)
        lines.append(f"| {r['candidate']} | {fmt(net2)} | {fmt(r['total_net_5bps'])} | {r['eligible_cases']}/10 | {fmt(r['worst_return_pct'])} |")
    lines+=['','Eligibility requires every coin/window net>0, PF≥1.20, DD≤12%, and ≥2 completed trades/day at5bps. Passing training is not forced: the best-ranked failed candidate can still be frozen for diagnostic validation.','',
        '## Continuous August–September validation: INR50000 per coin','','| Coin | Slip | Signals | Trades | Trades/day | Net INR | PF | Max DD % | Fees+GST INR | Funding INR |','|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in sorted([r for r in validation if r['window']=='validation_combined'],key=lambda r:(r['symbol'],r['slippage_bps'])):
        lines.append('| '+' | '.join(str(row[k]) if k in ('symbol','signals','trades','slippage_bps') else fmt(row[k]) for k in ('symbol','slippage_bps','signals','trades','trades_per_day','net_pnl_inr','profit_factor','max_drawdown_pct','fees_gst_inr','funding_inr'))+' |')
    lines+=['','## Actual one-wallet portfolio: INR50000 total, all five legs','','| Window | Slip | Signals | Trades | Trades/day total | Net INR | PF | Max DD % | Fees+GST INR | Funding INR |','|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in shared_rows:
        lines.append('| '+' | '.join(str(row[k]) if k in ('window','signals','trades','slippage_bps') else fmt(row[k]) for k in ('window','slippage_bps','signals','trades','trades_per_day','net_pnl_inr','profit_factor','max_drawdown_pct','fees_gst_inr','funding_inr'))+' |')
    lines+=['','Shared capital is not five times INR50000; simultaneous signals compete for the same margin. Fixed leg order: BTC, ETH, DOGE, XRP, TRX. Per-coin shared trade frequency/P&L/PF/fees are in shared/*/*/coin_breakdown.json; only the aggregate wallet has an independently meaningful equity drawdown.','',
        '## Dependence and limitations','',f"5bps standalone validation P&L by coin: `{json.dumps(clean(dep['net_by_coin']))}`.",
        f"5bps continuous validation monthly MTM equity changes: `{json.dumps(clean(dep['monthly_equity_change']))}`.",
        f"After subtracting the five largest winning trade P&Ls, net is INR{fmt(dep['net_without_five_winners_inr'])}. This is a fixed-ledger sensitivity, not a resimulation.",
        '','Backcast on four coins (2025Nov15–2026May15) is a frozen-rule robustness check, not reselection; TRX has no local data for those older dates. Per-month equity changes, all trades and all window/cost metrics are retained.','',
        'User-reported October Range Sweep & Reclaim baseline:834 signals,561 trades, approximately INR−18,929 across five INR50000 simulations. RSI Snapback was also reported losing. Exact original configurations were not supplied, so those are labelled baselines, not claimed reproductions.','',
        'OHLC cannot establish actual intrabar ordering or executable liquidity. Existing stop takes priority if SL/TP both touch. Fills, slippage and whole-order execution are assumptions; liquidation and partial fills are not independently exchange-verified. Current cached contract metadata and a fixed FX rate are used throughout historical dates.','',
        'No bot started, no orders/messages sent, no journals or profiles changed, no preset promoted. All computations use local data; the selected windows have no missing input history.','',
        'Reproduce: `.venv\\Scripts\\python.exe research_frequent_futures.py --output C:\\TSA\\delta-futures-review-evidence\\frequent-five-coin-20261103`','',
        'Inputs, source/dependency hashes and dirty-worktree context: manifest_before.json. Preregistered rules: preregistered_protocol.json. Selection frozen before validation: frozen_selection.json. Full comparison: all_metrics.csv.','']
    lines+=detailed_tables(output,protocol,selected,training,validation,shared_rows,backcast)
    (output/'REPORT.md').write_text('\n'.join(lines),encoding='utf-8')
    print('\n'+conclusion['status']+': '+selected['id'])
    for row in shared_rows:
        if row['window']=='validation_combined':print(f"Shared 61d {row['slippage_bps']}bps net INR{row['net_pnl_inr']:.2f}; PF {row['profit_factor']:.3f}; DD {row['max_drawdown_pct']:.2f}%; trades/day {row['trades_per_day']:.2f}")
    return conclusion

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT.parent/'delta-futures-review-evidence/frequent-five-coin-20261103')
    parser.add_argument('--workers',type=int,default=4);args=parser.parse_args()
    output=args.output.resolve()
    if output==ROOT or ROOT in output.parents:raise ValueError('Evidence directory must be outside the Git worktree')
    output.mkdir(parents=True,exist_ok=True);started=time.perf_counter()
    protocol=load(PROTOCOL);before=manifest(output)
    inventory_inputs(protocol,output)
    print('Preregistered protocol SHA256:',before['protocol_sha256'],flush=True)
    jobs=[dict(symbol=sym,phase='selection',output=str(output),candidates=protocol['candidates'],windows=protocol['selection_windows']) for sym in protocol['symbols']]
    training=run_jobs(jobs,args.workers)
    write(output/'selection_metrics.json',training)
    ranked=rank_training(training,[c['id'] for c in protocol['candidates']]);write(output/'selection_ranking.json',ranked)
    selected=next(c for c in protocol['candidates'] if c['id']==ranked[0]['candidate'])
    frozen=dict(candidate=selected,ranking=ranked,protocol_sha256=before['protocol_sha256'],input_hashes=before['inputs'],
        freeze_time_ns=time.time_ns(),selection_complete=True,validation_started=False,
        execution=protocol['execution'],training_qualified=bool(ranked[0]['rank'][0]))
    write(output/'frozen_selection.json',frozen)
    print('FROZEN before validation:',selected['id'],'training qualified:',frozen['training_qualified'],flush=True)
    freeze_hash=digest(output/'frozen_selection.json')
    windows=protocol['validation_windows']+[protocol['continuous_validation_window']]+protocol['examined_diagnostic_windows']
    validation_start_ns=time.time_ns()
    assert validation_start_ns>frozen['freeze_time_ns']
    validation=run_jobs([dict(symbol=sym,phase='validation',output=str(output),candidates=[selected],windows=windows) for sym in protocol['symbols']],args.workers)
    shared_rows=shared(protocol,selected,windows,output)
    back=protocol['extended_backcast'];window=dict(name='older_backcast',start=back['start'],end=back['end'])
    backcast=run_jobs([dict(symbol=sym,phase='backcast',output=str(output),candidates=[selected],windows=[window],warmup=back['warmup_start']) for sym in back['symbols']],args.workers)
    assert digest(output/'frozen_selection.json')==freeze_hash,'Parameters changed after freeze'
    conclusion=report(output,protocol,selected,training,validation,shared_rows,backcast,ranked)
    after={name:digest(ROOT/name) for name in before['protected']}
    assert after==before['protected'],'Protected library/profile changed'
    assert all(digest(ROOT/name)==value for name,value in before['inputs'].items()),'Historical inputs changed during experiment'
    write(output/'verification.json',dict(protected_unchanged=True,inputs_unchanged=True,freeze_unchanged=True,
        freeze_sha256=freeze_hash,validation_start_ns=validation_start_ns,freeze_time_ns=frozen['freeze_time_ns'],
        accounting_checks=sum(r['accounting_checks'] for r in training+validation+shared_rows+backcast),
        source_checks=sum(r['source_checks'] for r in training+validation+shared_rows+backcast),
        production_audit_errors=sum(r['audit_errors'] for r in training+validation+shared_rows+backcast),
        engine_runs=len(training)+len(validation)+len(shared_rows)+len(backcast),
        elapsed_seconds=time.perf_counter()-started,preset_added=False,conclusion=conclusion))
    print(f"Finished {len(training)+len(validation)+len(shared_rows)+len(backcast)} engine runs in {time.perf_counter()-started:.1f}s. Report: {output/'REPORT.md'}",flush=True)

if __name__=='__main__':main()
