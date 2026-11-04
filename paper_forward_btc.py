r"""Isolated, public-data-only forward observation of the production PAPER runner.

Run: .venv\Scripts\python.exe paper_forward_btc.py --directory C:\TSA\delta-futures-paper-tests\btc-ema-5m
Stop: same command with --stop (only this instance's control directory).
"""
from __future__ import annotations
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parent
COMMIT='0eb93ad'
DEFAULT=ROOT.parent/'delta-futures-paper-tests'/'btc-ema-5m'


def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,indent=2,default=str,allow_nan=False),encoding='utf-8')
    os.replace(temporary,path)


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def frozen():
    raw=subprocess.check_output(['git','show',f'{COMMIT}:research_frequent_futures_protocol.json'],cwd=ROOT)
    protocol=json.loads(raw)
    candidate=next(c for c in protocol['candidates'] if c['id']=='trend_5m')
    assert candidate['rule']=='ema_pullback_scalp' and candidate['timeframe']=='5m'
    return protocol,candidate,hashlib.sha256(raw).hexdigest()


def validate_directory(directory):
    directory=Path(directory).resolve()
    if directory==ROOT or ROOT in directory.parents:
        raise ValueError('Use a dedicated directory outside the existing project/data/journals')
    return directory


def public_only(method,url,headers=None):
    parsed=urlsplit(str(url));path=parsed.path
    if (method.upper()!='GET' or parsed.scheme!='https' or parsed.hostname!='api.india.delta.exchange'
        or not any(path==p or path.startswith(p+'/') for p in ('/v2/history/candles','/v2/tickers','/v2/products'))
        or any(str(key).lower() in ('api-key','signature') for key in (headers or {}))):
        raise RuntimeError(f'Isolated PAPER process refuses non-public request: {method} {parsed.hostname}{path}')


def bootstrap(directory):
    # Do not load .env or inherit credentials/alert destinations. Overrides affect
    # only this process, never the running dashboard or the existing bot.
    from pydantic_settings import BaseSettings
    original=BaseSettings.__init__
    def init(self,*args,**kwargs):
        kwargs['_env_file']=None
        if type(self).__name__=='Settings':
            kwargs.update(MODE='paper',LIVE_TRADING_ENABLED=False,AUTO_BOT_LIVE_ENABLED=False,
                EXECUTION_MODE='paper',ENVIRONMENT='india_prod',DATA_DIR=str(directory),SYMBOLS=['BTCUSD'],
                TESTNET_API_KEY='',TESTNET_API_SECRET='',LIVE_API_KEY='',LIVE_API_SECRET='',
                TELEGRAM_BOT_TOKEN='',TELEGRAM_CHAT_ID='',SIZING_SLIPPAGE_BPS=5.,
                INITIAL_CAPITAL_INR=50000.,FIXED_USD_INR_RATE=85.,DEFAULT_LEVERAGE=10,
                RISK_PER_TRADE_PCT=1.,MAX_DAILY_LOSS_PCT=5.,TAKER_FEE=.0005,MAKER_FEE=.0002,GST_ON_FEES=.18)
        return original(self,*args,**kwargs)
    BaseSettings.__init__=init
    try:
        import config
    finally:
        BaseSettings.__init__=original
    settings=config.settings
    assert settings.MODE=='paper' and not settings.is_live
    assert not settings.LIVE_TRADING_ENABLED and not settings.AUTO_BOT_LIVE_ENABLED
    assert settings.data_path.resolve()==directory
    import requests
    import httpx
    import urllib3.util.connection as connection
    # This PC's public REST IPv6 routes stall; keep the workaround inside this
    # isolated process. The venue, endpoints and received candle data are unchanged.
    connection.HAS_IPV6=False
    request_send=requests.Session.send;httpx_send=httpx.Client.send
    def guarded_requests(self,request,*args,**kwargs):
        public_only(request.method,request.url,request.headers)
        return request_send(self,request,*args,**kwargs)
    def guarded_httpx(self,request,*args,**kwargs):
        public_only(request.method,request.url,request.headers)
        return httpx_send(self,request,*args,**kwargs)
    requests.Session.send=guarded_requests;httpx.Client.send=guarded_httpx
    import live_bot as lb
    import execution_engine as ee
    return settings,lb,ee


def prepare(directory,ee):
    import pandas as pd
    from backtester import BacktestConfig
    protocol,candidate,protocol_sha=frozen()
    execution=dict(protocol['execution']);execution.pop('slippage_bps_tested');execution['slippage_bps']=5
    cfg=BacktestConfig(**execution)
    entry=dict(symbol='BTCUSD',enabled=True,variant='research_trend_5m_forward_paper',
        custom_name='Isolated BTC EMA pullback 5m (0eb93ad)',strategy=candidate['rule'],rule_type=candidate['rule'],
        timeframe=candidate['timeframe'],params=candidate['params'],capital_inr=50000.,leverage=10.,risk_pct=1.,
        sl_type='atr_mult',sl_value=1.5,tp_type='atr_mult',tp_value=3.,trailing_stop=False,trail_mult=0.,
        use_4h_trend=False,use_cost_gate=False,fee_mode='taker',slippage_bps=5.,cooldown_bars=1,
        max_daily_loss_pct=5.,strict_lot_cap=True,trigger_source='trade',exit_on_opposite_signal=False,
        history_start=protocol['warmup_start_utc'],effective_config=asdict(cfg))
    profiles_path=directory/'bot_profiles.json'
    if profiles_path.exists():
        assert json.loads(profiles_path.read_text())=={'BTCUSD':entry},'Existing isolated profile differs; refuse to reset it'
    else:
        directory.mkdir(parents=True,exist_ok=True)
        (directory/'raw_1m').mkdir();(directory/'metadata').mkdir()
        for kind in ('TRADE','MARK','FUNDING'):
            name=f'BTCUSD_{kind}_1m.parquet'
            frame=pd.read_parquet(ROOT/'data/raw_1m'/name,filters=[('time','>=',int(pd.Timestamp(protocol['warmup_start_utc']).timestamp()))])
            frame.to_parquet(directory/'raw_1m'/name,index=False)
        shutil.copy2(ROOT/'data/metadata/BTCUSD.json',directory/'metadata/BTCUSD.json')
        write(profiles_path,{'BTCUSD':entry})
    profile=ee.bot_profiles()['BTCUSD']
    assert profile.params==candidate['params'] and profile.rule_type==candidate['rule'] and profile.timeframe=='5m'
    assert asdict(ee.validate_deployable(profile))==asdict(cfg)
    # Pin the exact dirty-worktree implementation that the research actually ran.
    evidence=ROOT.parent/'delta-futures-review-evidence/frequent-five-coin-20261103/manifest_before.json'
    research_manifest=json.loads(evidence.read_text())
    assert sha(ROOT/'strategies.py')==research_manifest['source_hashes']['strategies.py']
    write(directory/'frozen_settings.json',dict(research_commit=COMMIT,protocol_sha256=protocol_sha,
        candidate=candidate,profile=asdict(profile),effective_config=asdict(cfg),
        source_hashes={name:sha(ROOT/name) for name in ('strategies.py','execution_engine.py','live_bot.py','paper_forward_btc.py')},
        original_profiles_sha256=sha(ROOT/'data/bot_profiles.json'),
        original_saved_results_sha256=sha(ROOT/'data/saved_backtests.json'),
        original_presets_sha256=sha(ROOT/'data/strategies.json')))
    return profile,candidate


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--directory',type=Path,default=DEFAULT)
    ap.add_argument('--stop',action='store_true')
    ap.add_argument('--status',action='store_true')
    ap.add_argument('--prepare',action='store_true')
    args=ap.parse_args();directory=validate_directory(args.directory)
    if args.status:
        print((directory/'status.json').read_text());return
    if args.stop:
        cid=f'{int(time.time()*1000)}-{os.getpid()}-stop'
        write(directory/'control'/f'{cid}.cmd.json',dict(id=cid,cmd='stop',args={},at=time.time()))
        print('Stop requested for isolated BTC PAPER instance only:',directory);return
    settings,lb,ee=bootstrap(directory)
    profile,candidate=prepare(directory,ee)
    if args.prepare:
        print('PAPER only; profile/configuration verified:',directory);return
    import pandas as pd
    import numpy as np
    import strategies
    from notifier import TelegramNotifier
    started=time.time();seen=set()
    class ObservedRunner(lb.LiveBotRunner):
        def _try_hour_signal(self,symbol):
            requested=(self.pending_hour.get(symbol) or [None])[-1]
            super()._try_hour_signal(symbol)
            evaluation=self.last_eval.get(symbol)
            if evaluation is None or requested is None:return
            close=int(evaluation['bar_close'].timestamp())
            if close!=requested or close in seen:return
            seen.add(close)
            # Independent invocation of the research signal pipeline on exactly
            # the fully closed raw-minute prefix; never change the runner output.
            history=self.market.history(symbol)
            history=history[(history.index>=pd.Timestamp(profile.history_start))&(history.index<evaluation['bar_close'])]
            bars=lb.dm.resample_candles(history,'5m')
            reference=strategies.evaluate_custom_strategy(candidate['rule'],candidate['params'],bars)
            assert int(reference.signal.iloc[-1])==evaluation['signal']
            np.testing.assert_allclose(strategies.atr(bars,14).iloc[-1],evaluation['atr14'],rtol=1e-12)
            assert evaluation['bar_close']<=pd.Timestamp.now(tz='UTC')
            result=next((r['result'] for r in reversed(self.entries) if r['hour_close']==close),None)
            blocked=next((r for r in reversed(self.blocked_signals) if r['hour_close']==close),None)
            forward=close>started
            detail=dict(observed_at=datetime.now(timezone.utc).isoformat(),forward=forward,
                candle_open=str(evaluation['bar_open']),candle_close=str(evaluation['bar_close']),
                candle_close_ist=str(evaluation['bar_close'].tz_convert('Asia/Kolkata')),
                side=evaluation['signal_side'] or 'NONE',state=self.state,
                reason=(blocked or {}).get('reason') or ('No fresh condition edge' if not evaluation['fresh_signal'] else None),
                cond_long=evaluation['cond_long'],cond_short=evaluation['cond_short'],
                cost_gate_enabled=False,trend_4h_enabled=False,research_signal_parity=True,
                atr14=evaluation['atr14'],result=result,blocked=blocked,
                last_1m_open=str(evaluation['last_1m_open']))
            self.store.event('INFO','forward_evaluation' if forward else 'startup_historical_evaluation',symbol,detail,'paper')
            with (directory/'evaluations.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(detail,default=str)+'\n')
            write(directory/'latest_evaluation.json',detail)
            logging.info('FORWARD_EVALUATION %s',json.dumps(detail,default=str))
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    bot=ObservedRunner('paper',profiles={'BTCUSD':profile},store_path=directory/'paper_journal.db',
        lock_path=directory/'paper.lock',control_dir=directory/'control',
        notifier=TelegramNotifier(mode='paper',background=False))
    assert bot.mode=='paper' and bot.engine.mode=='paper' and bot.client is None and bot.engine.client is None
    guard=dict(pid=os.getpid(),mode='paper',symbol='BTCUSD',live_order_submission=False,
        live_trading_enabled=settings.LIVE_TRADING_ENABLED,auto_bot_live_enabled=settings.AUTO_BOT_LIVE_ENABLED,
        private_client_present=False,external_notifications=False,http_policy='GET public Delta India market-data endpoints only',
        started_utc=datetime.fromtimestamp(started,timezone.utc).isoformat(),directory=str(directory),journal=str(directory/'paper_journal.db'))
    write(directory/'safety_verified.json',guard)
    async def run():
        task=asyncio.create_task(bot.run_async())
        try:
            while not task.done():
                await asyncio.wait([task],timeout=5)
                # Never block the asyncio socket/control loop while a REST worker
                # owns the runner lock. An unavailable snapshot can wait five seconds.
                if not bot._work.acquire(blocking=False):continue
                try:
                    state=dict(guard,observed_utc=datetime.now(timezone.utc).isoformat(),state=bot.state,
                        latest_minute={s:pd.Timestamp(t,unit='s',tz='UTC').isoformat() for s,t in bot.last_minute.items()},
                        evaluations=len(seen),forward_evaluations=sum(t>started for t in seen),
                        entries=bot.entries,blocked_signals=bot.blocked_signals,
                        positions=[dict(p) for p in bot.store.positions('paper')],closed_trades=len(bot.store.trades('paper')),
                        readiness_issues=(bot.reconciliations[-1].get('issues',[]) if bot.reconciliations else ['Starting public-data connection']),
                        equity_inr=bot.engine.realized_equity_inr())
                    write(directory/'status.json',state)
                finally:
                    bot._work.release()
            await task
        finally:
            bot.close()
    asyncio.run(run())


if __name__=='__main__':main()
