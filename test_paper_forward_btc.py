"""Safety checks for the isolated forward-test launcher; no orders or downloads."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

import paper_forward_btc as forward


@pytest.mark.parametrize('method,url,headers',[
    ('POST','https://api.india.delta.exchange/v2/orders',{}),
    ('DELETE','https://api.india.delta.exchange/v2/orders/123',{}),
    ('GET','https://api.india.delta.exchange/v2/wallet/balances',{}),
    ('GET','https://api.telegram.org/bot123/sendMessage',{}),
    ('GET','http://api.india.delta.exchange/v2/tickers/BTCUSD',{}),
    ('GET','https://api.india.delta.exchange.evil.example/v2/products',{}),
    ('GET','https://api.india.delta.exchange/v2/tickers/BTCUSD',{'api-key':'not-a-key'}),
    ('GET','https://api.india.delta.exchange/v2/products',{'Signature':'not-a-signature'}),
])
def test_network_policy_blocks_orders_private_endpoints_and_alerts(method,url,headers):
    with pytest.raises(RuntimeError,match='refuses non-public request'):
        forward.public_only(method,url,headers)


@pytest.mark.parametrize('path',[
    '/v2/history/candles?symbol=BTCUSD&resolution=1m',
    '/v2/tickers/BTCUSD','/v2/products/27'])
def test_public_delta_market_data_is_permitted(path):
    forward.public_only('GET','https://api.india.delta.exchange'+path,{'User-Agent':'test'})


@pytest.mark.parametrize('directory',[forward.ROOT,forward.ROOT/'data',forward.ROOT/'data/bot_control'])
def test_original_data_and_control_paths_are_rejected(directory):
    with pytest.raises(ValueError,match='dedicated directory'):
        forward.validate_directory(directory)


def test_frozen_profile_is_btc_compatible_without_reselection():
    protocol,candidate,digest=forward.frozen()
    assert candidate['id']=='trend_5m'
    assert candidate['timeframe']=='5m'
    assert candidate['rule']=='ema_pullback_scalp'
    assert candidate['params']['fast_ema']==9
    assert candidate['params']['slow_ema']==21
    assert candidate['params']['cost_gate_mult']==0
    assert candidate['params']['use_4h_trend']==0
    assert protocol['execution']['initial_capital_inr']==50000
    assert protocol['execution']['risk_per_trade_pct']==1
    assert len(digest)==64


def test_child_bootstrap_forces_paper_without_reading_dotenv(tmp_path):
    code='''
import json
from pathlib import Path
from unittest.mock import patch
import paper_forward_btc as forward
with patch.dict('os.environ', {'MODE':'live','LIVE_TRADING_ENABLED':'true','AUTO_BOT_LIVE_ENABLED':'true'}):
    settings,lb,ee=forward.bootstrap(Path(__import__('sys').argv[1]))
    assert settings.MODE=='paper'
    assert not settings.LIVE_TRADING_ENABLED and not settings.AUTO_BOT_LIVE_ENABLED and not settings.is_live
    assert settings.LIVE_API_KEY==settings.LIVE_API_SECRET==settings.TESTNET_API_KEY==settings.TESTNET_API_SECRET==''
    assert settings.TELEGRAM_BOT_TOKEN==settings.TELEGRAM_CHAT_ID==''
    assert settings.data_path==Path(__import__('sys').argv[1])
    print(json.dumps({'mode':settings.MODE,'live_submission':settings.is_live}))
'''
    result=subprocess.run([sys.executable,'-c',code,str(tmp_path)],cwd=forward.ROOT,capture_output=True,text=True,check=True)
    assert json.loads(result.stdout)=={'mode':'paper','live_submission':False}
