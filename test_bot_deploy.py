"""Fast, isolated API checks; no bot loop, orders or real profile writes."""
import hashlib
import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor

_scratch = tempfile.TemporaryDirectory(prefix='delta-deploy-import-')
os.environ.update(DELTA_DATA_DIR=_scratch.name, DELTA_MODE='research',
    DELTA_LIVE_TRADING_ENABLED='false', DELTA_EXECUTION_MODE='paper',
    DELTA_LIVE_API_KEY='', DELTA_LIVE_API_SECRET='', DELTA_API_KEY='', DELTA_API_SECRET='',
    DELTA_TELEGRAM_BOT_TOKEN='')
from pydantic_settings import BaseSettings
_init=BaseSettings.__init__
def _no_dotenv(self,*args,**kwargs):
    kwargs['_env_file']=None
    return _init(self,*args,**kwargs)
BaseSettings.__init__=_no_dotenv
import bot_ops as ops
import execution_engine as ee
BaseSettings.__init__=_init

import httpx
import pytest
import requests
from fastapi import FastAPI

ROOT=Path(__file__).resolve().parent


class IsolatedClient:
    def __init__(self,app):self.app=app

    def request(self,method,path,**kwargs):
        async def call():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://testserver') as client:
                return await client.request(method,path,**kwargs)
        return asyncio.run(call())

    def post(self,path,**kwargs):return self.request('POST',path,**kwargs)


@pytest.fixture
def deployment(monkeypatch,tmp_path):
    def forbidden(*args,**kwargs):raise AssertionError('external HTTP is forbidden')
    monkeypatch.setattr(requests.Session,'request',forbidden)
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',forbidden)
    monkeypatch.setattr(httpx.AsyncHTTPTransport,'handle_async_request',forbidden)
    monkeypatch.setattr(ops,'_signals',lambda:{})
    monkeypatch.setattr(ee,'BOT_PROFILES_FILE',tmp_path/'profiles.json')
    monkeypatch.setattr(ops,'SAVED_BACKTESTS_FILE',tmp_path/'saved.json')
    monkeypatch.setattr(ops.run_store,'root',lambda:tmp_path/'runs')
    # Test-owned records/evidence must not depend on a changing user library.
    saved=[dict(id=f'fixture-{sym}',symbol=sym,custom_name=f'{sym} fixture',timeframe='15m',
        rule_type='ema_pullback_scalp',params={'fast_ema':9,'slow_ema':21},capital_inr=50000,
        leverage=10,risk_pct=1,sl_type='atr_mult',sl_value=1.5,tp_type='atr_mult',tp_value=3,
        trailing_stop=False,trail_mult=0,fee_mode='taker',slippage_bps=2)
        for sym in ('SUIUSD','DOGEUSD','ETHUSD','XRPUSD')]
    first=saved[0];cfg=asdict(ee.profile_config(ee._profile_from_dict(first['symbol'],first)))
    evidence={'result':{'audit':{'passed':True},'legs':[dict(symbol=first['symbol'],timeframe='15m',strategy='ema_pullback_scalp')],
        'config':first,'filters':{'trend_filter_4h':False,'cost_gate':False}},
        'leg_configs':{first['symbol']:cfg},'history_starts':{first['symbol']:'2026-01-01T00:00:00Z'}}
    first.update(run_id=ops.run_store.put(evidence),verified_run=True)
    (tmp_path/'saved.json').write_text(json.dumps(saved),encoding='utf-8')
    originals={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in
        [ROOT/'data/saved_backtests.json',ROOT/'data/bot_profiles.json']}
    app=FastAPI();app.include_router(ops.router)
    yield IsolatedClient(app),saved,tmp_path
    assert all(hashlib.sha256(p.read_bytes()).hexdigest()==digest for p,digest in originals.items())

def test_four_saved_strategies_single_and_batch(deployment):
    client,saved,path=deployment
    assert {x['symbol'] for x in saved}=={'SUIUSD','DOGEUSD','ETHUSD','XRPUSD'}
    # Each POST adds a symbol; a same-symbol POST replaces only that symbol.
    for count,row in enumerate(saved,1):
        response=client.post('/api/bot/deploy',json={'result_id':row['id'],'enabled':True})
        assert response.status_code==200,response.text
        assert response.json()['active_count']==count
    assert client.post('/api/bot/deploy',json={'result_id':saved[0]['id']}).json()['active_count']==4
    raw=ee.load_bot_profiles_raw()
    assert raw['XRPUSD']['run_evidence_status']=='legacy_defaults'
    (path/'profiles.json').unlink()
    response=client.post('/api/bot/deploy',json={'result_ids':[x['id'] for x in saved]})
    assert response.status_code==200,response.text
    assert response.json()['active_count']==response.json()['total_count']==4
    assert set(response.json()['profiles'])=={x['symbol'] for x in saved}

def test_wallet_aliases_normalize_without_losing_other_profiles(deployment):
    client,saved,path=deployment
    legacy=dict(saved[-1]);legacy.pop('run_id',None);legacy.pop('effective_config',None)
    legacy.update(id='legacy-wallet',symbol='XRPUSD',capital_inr=75000,fx=90,daily_loss_stop_pct=0)
    (path/'saved.json').write_text(json.dumps([legacy]),encoding='utf-8')
    existing=ops._build_deploy_entry(None,ops.DeployStrategyRequest(symbol='BTCUSD'))
    existing['keep_user_field']='untouched'
    paused=dict(existing,symbol='ADAUSD',enabled=False,capital_inr=12345)
    ee.save_bot_profiles_raw({'BTCUSD':existing,'ADAUSD':paused})
    response=client.post('/api/bot/deploy',json={'result_id':'legacy-wallet'})
    assert response.status_code==200,response.text
    raw=ee.load_bot_profiles_raw()
    assert set(raw)=={'BTCUSD','ADAUSD','XRPUSD'} and raw['ADAUSD']==paused
    assert raw['BTCUSD']['keep_user_field']=='untouched'
    for symbol in ('BTCUSD','XRPUSD'):
        cfg=ee.profile_config(ee._profile_from_dict(symbol,raw[symbol]))
        assert (cfg.initial_capital_inr,cfg.usd_inr_rate,cfg.max_daily_loss_pct)==(75000,90,0)

def test_concurrent_merges_and_failed_batch_is_atomic(deployment):
    _,saved,path=deployment
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda row:ops.deploy_strategy_to_bot(ops.DeployStrategyRequest(result_id=row['id'])),saved))
    assert all(r['ok'] for r in results) and len(ee.load_bot_profiles_raw())==4
    before=(path/'profiles.json').read_bytes()
    with pytest.raises(ops.HTTPException):
        ops.deploy_strategy_to_bot(ops.DeployStrategyRequest(result_ids=[saved[0]['id'],'missing']))
    assert (path/'profiles.json').read_bytes()==before

def test_verified_execution_overrides_still_rejected(deployment):
    client,saved,_=deployment
    verified=next(x for x in saved if x.get('verified_run'))
    response=client.post('/api/bot/deploy',json={'result_id':verified['id'],'risk_pct':99})
    assert response.status_code==422
