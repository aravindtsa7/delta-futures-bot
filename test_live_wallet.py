"""Live/testnet wallet sizing regressions. Mock HTTP only; temporary journals only."""
from dataclasses import replace
from types import SimpleNamespace
import json

import httpx
import pytest

from test_review_repairs import isolated  # noqa: F401
import bot_ops as ops
import execution_engine as ee
import live_bot as lb
import manual_trade as manual
from backtester import Backtester, ProductSpec
from config import settings
from exchange_client import DeltaRestClientAdapter, ExchangeError
from exchange_wallet import wallet_from_balances
from step5_verify import FakeDelta, FakeClock, FAKE_KEY, FAKE_SECRET


@pytest.fixture(params=['testnet', 'live'])
def account(request, monkeypatch, tmp_path):
    mode = request.param
    for key in ops.PATHS:
        monkeypatch.setitem(ops.PATHS, key, tmp_path / ('state.db' if key == 'db' else key))
    monkeypatch.setattr(settings, 'MODE', 'live')
    monkeypatch.setattr(settings, 'LIVE_TRADING_ENABLED', True)
    monkeypatch.setattr(settings, 'AUTO_BOT_LIVE_ENABLED', True)
    clock = FakeClock(1794528000000000)  # 2026-11-13 00:00 UTC
    fake = FakeDelta(clock)
    funds = {'equity': 4026., 'available': 2893.}
    def handler(req):
        response = fake.handler(req)
        if req.url.path == '/v2/wallet/balances':
            body = response.json()
            body['result'][0].update(balance=str(funds['equity']/85), balance_inr=str(funds['equity']),
                                     available_balance=str(funds['available']/85), available_balance_inr=str(funds['available']),
                                     blocked_margin=str((funds['equity']-funds['available'])/85))
            return httpx.Response(200, json=body)
        return response
    client = DeltaRestClientAdapter(mode, FAKE_KEY, FAKE_SECRET, transport=httpx.MockTransport(handler))
    profile = ee.StrategyProfile('BTCUSD', capital_inr=100000, risk_pct=2, leverage=10,
                                 sl_type='fixed_pct', sl_value=1, tp_type='fixed_pct', tp_value=2,
                                 fee_mode='maker_maker', strict_lot_cap=False, cooldown_bars=0)
    product = ProductSpec('BTCUSD',27,.001,.5)
    store = ee.StateStore(ops.mode_db(mode), clock=clock)
    engine = ee.ExecutionEngine(store,mode,client,profiles={'BTCUSD':profile},products={'BTCUSD':product},clock=clock)
    engine.account_policy = {'blocked':{}}
    yield engine, funds, fake, clock, profile, product, monkeypatch
    store.close()
    client.close()


def test_normalization_uses_inr_and_cross_blocked_margin():
    w=wallet_from_balances([dict(asset_symbol='USD',balance='50',balance_inr='4026',available_balance='30',
                                 available_balance_inr='2893',position_margin='0',cross_position_margin='11',
                                 cross_order_margin='2',blocked_margin='13.32941176470588')])
    assert w['wallet_equity_inr']==4026 and w['available_margin_inr']==2893
    assert w['used_margin_inr']==pytest.approx(1133)
    with pytest.raises(ExchangeError): wallet_from_balances([])
    with pytest.raises(ExchangeError): wallet_from_balances([dict(asset_symbol='USD',balance='nan',available_balance='30')])


def test_engine_syncs_real_wallet_on_start_and_before_sizing(account):
    engine,funds,fake,clock,profile,product,_=account
    assert engine.cfg_row['capital_inr']==0  # no backtest capital imported
    assert engine.account_equity_inr()==(4026,2893)
    assert engine.realized_equity_inr()==4026
    assert float(engine.store.meta_get('daily_start_equity_inr'))==4026
    row=engine.store.one('SELECT * FROM exchange_wallet')
    assert row['wallet_equity_inr']==4026 and row['used_margin_inr']==pytest.approx(1133)
    funds.update(equity=5032.5,available=3000)
    assert engine.account_equity_inr()==(5032.5,3000)
    assert float(engine.store.meta_get('daily_start_equity_inr'))==4026
    assert all(c['method']=='GET' for c in fake.calls)


def test_live_sizes_ignore_backtest_capital_and_scale_real_risk(account):
    engine,funds,fake,clock,profile,product,_=account
    def size(equity, available, p=profile):
        return ee.calculate_order_lots(product,10000,100,'LONG',equity,available,profile=p,mode=engine.mode)
    small,large=size(4026,2893),size(8052,5786)
    assert small.contracts>0 and large.contracts==small.contracts*2
    assert small.risk_budget_usd==pytest.approx(4026/85*.02)
    assert size(4026,2893,replace(profile,capital_inr=50000))==small
    cfg=ee.runtime_sizing_config(profile,engine.mode,4026)
    assert cfg.initial_capital_inr==4026 and cfg.fee_mode=='taker' and not cfg.allow_min_one_lot
    reserve=small.notional_inr*(1/cfg.leverage+2*cfg.with_gst(cfg.taker_fee))*(1+cfg.slip)
    assert reserve<=2893*cfg.margin_buffer
    assert small.risk_usd*85<=4026*.02
    assert size(4026,0).contracts==0
    assert size(4026,2893,replace(profile,risk_pct=.00001)).contracts==0
    with pytest.raises(ValueError,match='observed wallet'):
        ee.calculate_order_lots(product,10000,100,profile=profile,mode=engine.mode)


def test_signal_uses_real_equity_and_taker_snapshot_without_dispatch(account):
    engine,funds,fake,clock,profile,product,monkeypatch=account
    seen=[]
    def capture(intent):
        seen.append(dict(intent))
        return {'action':'simulated','contracts':intent['size']}
    monkeypatch.setattr(engine,'_dispatch_entry',capture)
    result=engine.on_signal('BTCUSD',1,clock()-3600_000_000,100,10000)
    assert result['action']=='simulated'
    cfg=json.loads(seen[0]['execution_config'])['config']
    assert cfg['initial_capital_inr']==4026 and cfg['fee_mode']=='taker'
    expected=ee.calculate_order_lots(product,10000,100,'LONG',4026,2893,profile=profile,mode=engine.mode)
    assert result['contracts']==expected.contracts
    assert not [c for c in fake.calls if c['method']!='GET']


def test_margin_change_before_dispatch_refuses_locally(account):
    engine,funds,fake,clock,profile,product,monkeypatch=account
    dispatch=engine._dispatch_entry
    def shrink(intent):
        funds['available']=0
        return dispatch(intent)
    monkeypatch.setattr(engine,'_dispatch_entry',shrink)
    result=engine.on_signal('BTCUSD',1,clock()-3600_000_000,100,10000)
    assert result['action']=='skip' and 'wallet changed' in result['reason']
    assert engine.store.intent(result['intent_id'])['state']=='REJECTED'
    assert not [c for c in fake.calls if c['method']!='GET']


def test_utc_anchor_survives_restart_and_rolls_on_observation_not_old_candle(account):
    engine,funds,fake,clock,profile,product,_=account
    funds['equity']=3900
    # Recreating the engine against the same store simulates its restart init path.
    restarted=ee.ExecutionEngine(engine.store,engine.mode,engine.client,profiles={'BTCUSD':profile},products={'BTCUSD':product},clock=clock)
    assert float(engine.store.meta_get('daily_start_equity_inr'))==4026
    runner=object.__new__(lb.LiveBotRunner)
    runner.mode,runner.engine,runner.store=engine.mode,restarted,engine.store
    runner.clock=lambda:clock()/1_000_000
    runner.day=None
    runner._roll_day(int(runner.clock())-86400)
    assert runner.day_start_equity==4026
    clock.t += 86400_000_000
    runner._roll_day(int(runner.clock())-86400)
    assert runner.day_start_equity==3900
    assert runner.equity_inr()==3900
    funds['equity']=3800
    runner._roll_day(int(runner.clock()))
    assert runner.day_start_equity==3900


def test_daily_loss_uses_real_wallet_without_journal_capital(account):
    engine,funds,fake,clock,profile,product,_=account
    assert not engine.check_daily_loss(50000)  # stale caller capital cannot override Delta's daily anchor
    funds['equity']=3800
    # Deliberately poison legacy static capital: the real 4026 -> 3800 drop still trips 5%.
    engine.store.conn.execute('UPDATE bot_config_state SET capital_inr=100000')
    assert engine.check_daily_loss(4026)
    assert engine.halted()[0]


def test_status_uses_delta_positions_wallet_and_fails_closed(account):
    engine,funds,fake,clock,profile,product,monkeypatch=account
    fake.positions[27]={'size':7,'entry_price':68000}
    monkeypatch.setattr(manual,'CLIENT_FACTORY',lambda mode,read_only:engine.client)
    monkeypatch.setattr(manual,'_cache',{})
    monkeypatch.setattr(manual,'catalog',lambda:{'BTCUSD':dict(contract_value=.001,tick_size=.5)})
    monkeypatch.setattr(manual,'ticker',lambda sym,ttl=1:dict(mark_price=69000))
    monkeypatch.setattr(ops,'runner',lambda:(True,{'mode':engine.mode,'pid':123}))
    status=ops.read_status(mode=engine.mode,with_market=False,with_signals=False)
    assert status['wallet']['realized_equity_inr']==4026
    assert status['wallet']['available_margin_inr']==2893
    assert status['positions'][0]['contracts']==7 and status['positions'][0]['entry_price']==68000
    assert status['exchange']['connected']
    def fail(*args,**kwargs): raise ExchangeError('wallet offline')
    monkeypatch.setattr(manual,'exchange_snapshot',fail)
    status=ops.read_status(mode=engine.mode,with_market=False,with_signals=False)
    assert not status['exchange']['connected']
    assert status['wallet']['total_equity_inr'] is None
    assert status['wallet']['available_margin_inr'] is None
    assert status['positions']==[]


def test_live_account_policy_preserves_cross_mode(account):
    engine,funds,fake,clock,profile,product,monkeypatch=account
    if engine.mode!='live': return
    monkeypatch.setattr(engine,'read_margin_mode',lambda:'cross')
    engine.prepare_account({'BTCUSD':0})
    assert engine.account_policy['margin_mode']['margin_mode']=='cross'
    assert not [c for c in fake.calls if c['path']=='/v2/users/margin_mode' and c['method']=='PUT']


def test_start_stop_live_mode_uses_mock_process_only(account):
    engine,funds,fake,clock,profile,product,monkeypatch=account
    if engine.mode!='live': return
    held=[False]
    launched=[]
    monkeypatch.setattr(ops,'_proc',None)
    monkeypatch.setattr(ops,'_proc_bot_pid',None)
    monkeypatch.setattr(ops,'_proc_log',None)
    monkeypatch.setattr(ops,'runner',lambda:(held[0],{'pid':987654,'mode':'live'} if held[0] else None))
    monkeypatch.setattr(ops,'process_state',lambda:dict(state='running' if held[0] else 'stopped',error=None))
    monkeypatch.setattr(ops,'notifier',lambda mode:SimpleNamespace(notify_bot_lifecycle=lambda *args:None))
    def launch(cmd,**kwargs):
        launched.append(cmd);held[0]=True
        return SimpleNamespace(pid=987654,poll=lambda:None,wait=lambda timeout:0)
    monkeypatch.setattr(ops.subprocess,'Popen',launch)
    result=ops.bot_start(ops.StartRequest(mode='live'))
    assert result['mode']=='live' and result['started']
    assert launched[0][launched[0].index('--mode')+1]=='live'
    assert str(ops.mode_db('live')) in launched[0]
    def stop(*args,**kwargs):
        held[0]=False
        return {'stopped':True}
    monkeypatch.setattr(ops,'_send',stop)
    assert ops.bot_stop(ops.BotActionRequest(mode='live'))['stopped']
    assert not held[0]
    assert all(c['method']=='GET' for c in fake.calls)
