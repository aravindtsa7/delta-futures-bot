"""Offline tests of frozen preset -> real JS form -> backtest/save/paper deploy.

All API writes, evidence and metadata are in temporary directories. No bot runs.
"""
from dataclasses import asdict
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from pydantic_settings import BaseSettings
import pytest

ROOT=Path(__file__).resolve().parent
NAME='BTCUSD 5m EMA Pullback (9/21) — Paper Signal Test'
SCRATCH=tempfile.TemporaryDirectory(prefix='btc-paper-preset-import-')
_init=BaseSettings.__init__


def _safe_settings(self,*args,**kwargs):
    kwargs['_env_file']=None
    if type(self).__name__=='Settings':
        kwargs.update(DATA_DIR=SCRATCH.name,MODE='research',EXECUTION_MODE='paper',
            LIVE_TRADING_ENABLED=False,AUTO_BOT_LIVE_ENABLED=False,
            LIVE_API_KEY='',LIVE_API_SECRET='',TESTNET_API_KEY='',TESTNET_API_SECRET='',
            TELEGRAM_BOT_TOKEN='',TELEGRAM_CHAT_ID='',FIXED_USD_INR_RATE=85,
            TAKER_FEE=.0005,MAKER_FEE=.0002,GST_ON_FEES=.18)
    return _init(self,*args,**kwargs)


BaseSettings.__init__=_safe_settings
try:
    import app
    import backtester as bt
    import bot_ops
    import data_manager as dm
    import execution_engine as ee
    import run_store
    import strategies
    from config import settings
finally:
    BaseSettings.__init__=_init

import httpx
import pandas as pd
import requests


class IsolatedClient:
    """In-process ASGI requests; no listening socket or Starlette adapter."""
    def request(self,method,path,**kwargs):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),base_url='http://testserver') as client:
                return await client.request(method,path,**kwargs)
        return asyncio.run(call())

    def get(self,path,**kwargs):return self.request('GET',path,**kwargs)
    def post(self,path,**kwargs):return self.request('POST',path,**kwargs)


def form_request():
    result=subprocess.run(['node',str(ROOT/'test_btc_paper_preset.js')],cwd=ROOT,
        capture_output=True,encoding='utf-8',check=True)
    return json.loads(result.stdout)


@pytest.fixture
def isolated(monkeypatch,tmp_path):
    def forbidden(*args,**kwargs):raise AssertionError('External HTTP forbidden in isolated preset tests')
    monkeypatch.setattr(requests.Session,'request',forbidden)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',forbidden)
    monkeypatch.setattr(settings,'DATA_DIR',str(tmp_path))
    monkeypatch.setattr(app,'STRATEGIES_FILE',tmp_path/'strategies.json')
    monkeypatch.setattr(app,'SAVED_BACKTESTS_FILE',tmp_path/'saved.json')
    monkeypatch.setattr(bot_ops,'SAVED_BACKTESTS_FILE',tmp_path/'saved.json')
    monkeypatch.setattr(ee,'BOT_PROFILES_FILE',tmp_path/'profiles.json')
    monkeypatch.setattr(bot_ops,'_signals',lambda:{})
    monkeypatch.setattr(app,'app_symbols',lambda:['BTCUSD','XRPUSD'])
    (tmp_path/'strategies.json').write_bytes((ROOT/'data/strategies.json').read_bytes())
    (tmp_path/'saved.json').write_text('[]')
    (tmp_path/'metadata').mkdir()
    for symbol in ('BTCUSD','XRPUSD'):
        (tmp_path/'metadata'/f'{symbol}.json').write_bytes((ROOT/f'data/metadata/{symbol}.json').read_bytes())
    protected=[ROOT/'data/bot_profiles.json',ROOT/'data/saved_backtests.json',ROOT/'data/strategies.json']
    hashes={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    app._data_cache.clear();app._frame_cache.clear()
    yield IsolatedClient(),tmp_path
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in hashes.items())


def test_btc_dropdown_exact_name_and_actual_js_form(isolated):
    client,_=isolated
    presets=client.get('/api/strategies?symbol=BTCUSD').json()['strategies']
    match=[s for s in presets if s['name']==NAME]
    assert len(match)==1 and match[0]['rule_type']=='ema_pullback_scalp'
    assert not any(s['name']==NAME for s in client.get('/api/strategies?symbol=ETHUSD').json()['strategies'])
    request=form_request()
    assert request['strategy_id']==match[0]['id'] and request['timeframe']=='5m'
    protocol=json.loads(subprocess.check_output(['git','show','0eb93ad:research_frequent_futures_protocol.json'],cwd=ROOT))
    frozen=next(c for c in protocol['candidates'] if c['id']=='trend_5m')
    assert {k:request['params'][k] for k in frozen['params']}==frozen['params']
    cfg=dict(protocol['execution']);cfg.pop('slippage_bps_tested');cfg['slippage_bps']=5
    req=app.BacktestRequest(**request)
    assert asdict(app.leg_config(req,app.resolve_legs(req)[0]))==asdict(bt.BacktestConfig(**cfg))


def test_real_backtest_save_and_paper_deploy_only_in_temp_files(isolated):
    client,path=isolated
    request=form_request();request.update(walk_forward_windows=0,include_chart=False)
    (path/'raw_1m').mkdir()
    # Deterministic synthetic candles make this regression portable without any
    # downloaded dataset. Only this temporary ASGI instance consumes the fixture.
    import numpy as np
    index=pd.date_range(request['history_start'],pd.Timestamp(request['end'])-pd.Timedelta(minutes=1),freq='min')
    rng=np.random.default_rng(4321)
    close=60000*np.exp(np.cumsum(rng.normal(0,.00035,len(index))))
    opening=np.r_[close[0],close[:-1]]
    frame=pd.DataFrame(dict(time=index.as_unit('s').asi8,open=opening,
        high=np.maximum(opening,close)*1.0004,low=np.minimum(opening,close)*.9996,
        close=close,volume=rng.lognormal(5,.5,len(index))))
    for kind in ('TRADE','MARK','FUNDING'):
        values=frame.copy()
        if kind=='FUNDING':values[['open','high','low','close']]=.01
        values.to_parquet(path/'raw_1m'/f'BTCUSD_{kind}_1m.parquet',index=False)
    response=client.post('/api/backtest/run',json=request)
    assert response.status_code==200,response.text
    result=response.json()
    assert result['audit']['passed'] and len(result['trades'])>0
    evidence=run_store.get(result['run_id'])
    expected=evidence['leg_configs']['BTCUSD']
    assert expected['atr_sl_min_pct']==.15 and expected['atr_sl_max_pct']==2
    assert expected['allow_min_one_lot'] is False and expected['breakeven_at_1r'] is False
    assert expected['exit_on_opposite_signal'] is False and expected['trailing_atr_mult']==0
    for tr in result['trades']:
        assert pd.Timestamp(tr['entry_time'])==pd.Timestamp(tr['signal_bar'])+pd.Timedelta(minutes=5)
    saved=client.post('/api/backtest/save_result',json={'run_id':result['run_id'],'custom_name':NAME})
    assert saved.status_code==200,saved.text
    record=saved.json()
    assert record['verified_run'] and record['effective_config']==expected
    assert record['rule_type']=='ema_pullback_scalp' and record['params']==request['params']
    previous={'symbol':'XRPUSD','enabled':False,'custom_name':'Preserve unrelated test profile','user_field':'unchanged'}
    ee.save_bot_profiles_raw({'XRPUSD':previous})
    deployed=client.post('/api/bot/deploy',json={'result_id':record['id']})
    assert deployed.status_code==200,deployed.text
    profiles=ee.load_bot_profiles_raw()
    assert profiles['XRPUSD']==previous
    assert profiles['BTCUSD']['effective_config']==expected
    assert profiles['BTCUSD']['params']==request['params']
    profile=ee.bot_profiles()['BTCUSD']
    history=dm.load_unified_1m_dataset('BTCUSD')
    evaluated=ee.evaluate_symbol(profile,history)
    bars=dm.resample_candles(history,'5m')
    reference=strategies.evaluate_custom_strategy('ema_pullback_scalp',request['params'],bars)
    assert evaluated['signal']==int(reference.signal.iloc[-1])
    assert ee.profile_config(profile)==bt.BacktestConfig(**expected)
    # A crossover alone is insufficient, and established-trend pullbacks can
    # signal without a new EMA crossover. Keep these two rule types distinct.
    above=reference.ema_fast>reference.ema_slow
    crosses=above!=above.shift(1,fill_value=bool(above.iloc[0]))
    cross_without_pullback=crosses&~(reference.cond_long|reference.cond_short)
    assert cross_without_pullback.any()
    assert reference.signal[cross_without_pullback].eq(0).all()
    assert ((reference.signal!=0)&~crosses).any()
