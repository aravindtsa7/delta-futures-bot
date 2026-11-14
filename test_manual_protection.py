"""Offline regressions for live position adoption and percentage protection.

All exchange requests use MockTransport; all journals are temporary.
"""
import pytest
import httpx
from contextlib import closing
from pydantic import ValidationError
from fastapi import HTTPException
from fastapi.testclient import TestClient

from test_review_repairs import isolated  # noqa: F401 (network and filesystem isolation)
import app
import bot_ops
import execution_engine as ee
import manual_trade as m
from config import settings
from exchange_client import DeltaRestClientAdapter, ExchangeError
from step5_verify import FakeClock, FakeDelta, FAKE_KEY, FAKE_SECRET


@pytest.fixture
def exchange(monkeypatch, tmp_path):
    for key in bot_ops.PATHS:
        monkeypatch.setitem(bot_ops.PATHS, key, tmp_path / ('state.db' if key == 'db' else key))
    monkeypatch.setattr(bot_ops, 'runner', lambda: (False, None))
    fake = FakeDelta(FakeClock(1794500000000000))
    fake.positions[27] = {'size': 1, 'entry_price': 68733.5}
    fake.prices[27] = 68612.10422422
    clients = {ro: DeltaRestClientAdapter('testnet', FAKE_KEY, FAKE_SECRET,
               transport=httpx.MockTransport(fake.handler), read_only=ro) for ro in (False, True)}
    monkeypatch.setattr(m, 'CLIENT_FACTORY', lambda mode, read_only: clients[read_only])
    monkeypatch.setattr(settings, 'TESTNET_API_KEY', FAKE_KEY)
    monkeypatch.setattr(settings, 'TESTNET_API_SECRET', FAKE_SECRET)
    monkeypatch.setattr(m, '_cache', {})
    cat = {sym: {'symbol': sym, 'product_id': meta['id'], 'tick_size': float(meta['tick_size']),
                 'contract_value': float(meta['contract_value'])} for sym, meta in fake.meta.items()}
    monkeypatch.setattr(m, 'catalog', lambda: cat)
    monkeypatch.setattr(m, 'product_info', lambda sym: cat[sym])
    monkeypatch.setattr(m, 'ticker', lambda sym, ttl=1: {'mark_price': fake.prices[cat[sym]['product_id']]})
    monkeypatch.setattr(m, '_pub', lambda: clients[True])
    bot_ops.set_active_mode('testnet')
    yield fake
    for client in clients.values():
        client.close()


def binding(**kwargs):
    mode, gen = bot_ops.mode_state()
    return dict(expected_mode=mode, mode_generation=gen, **kwargs)


def test_adopts_external_and_refreshes_size_side_entry_even_with_same_size(exchange):
    with m._Session('testnet') as session:
        first = m._ensure_journal_position(session, 'BTCUSD')
        assert first['contracts'] == 1
        exchange.positions[27] = {'size': 7, 'entry_price': 69001.25}
        row = m._ensure_journal_position(session, 'BTCUSD')
        assert (row['contracts'], row['side'], row['entry_price'], row['raw_entry_price']) == (7, 'LONG', 69001.25, 69001.25)
        exchange.positions[27] = {'size': -7, 'entry_price': 68003.5}
        row = m._ensure_journal_position(session, 'BTCUSD')
        assert (row['contracts'], row['side'], row['entry_price']) == (7, 'SHORT', 68003.5)
        exchange.positions[27] = {'size': -7, 'entry_price': 67002.5}
        assert m._ensure_journal_position(session, 'BTCUSD')['entry_price'] == 67002.5
        exchange.positions[27]['size'] = 0
        assert m._ensure_journal_position(session, 'BTCUSD') is None
    assert all(call['method'] == 'GET' for call in exchange.calls)


def test_failed_position_read_never_uses_stale_journal(exchange, monkeypatch):
    with m._Session('testnet') as session:
        m._ensure_journal_position(session, 'BTCUSD')
        def unavailable(*args, **kwargs):
            raise ExchangeError('offline')
        monkeypatch.setattr(m, 'exchange_snapshot', unavailable)
        with pytest.raises(ExchangeError, match='offline'):
            m._ensure_journal_position(session, 'BTCUSD')


@pytest.mark.parametrize('side,size', [('LONG', 5), ('SHORT', -5)])
def test_percent_price_create_edit_clear_and_exact_live_size(exchange, side, size):
    exchange.positions[27] = {'size': size, 'entry_price': 68733.5}
    result = m.set_protection('BTCUSD', m.ProtectionRequest(**binding(sl_pct=2, tp_pct=6)))
    p = result['position']
    d = 1 if side == 'LONG' else -1
    assert p['sl_price'] == float(ee.snap_to_tick(68733.5 * (1 - d * .02), .5))
    assert p['tp_price'] == float(ee.snap_to_tick(68733.5 * (1 + d * .06), .5))
    posts = [c['body'] for c in exchange.calls if c['method'] == 'POST' and c['path'] == '/v2/orders']
    assert len(posts) == 2
    assert all(o['reduce_only'] and o['size'] == 5 and o['stop_trigger_method'] == 'mark_price' for o in posts)
    assert all(o['side'] == ('sell' if side == 'LONG' else 'buy') for o in posts)
    ids = (p['sl_order_id'], p['tp_order_id'])
    updated = m.set_protection('BTCUSD', m.ProtectionRequest(**binding(sl_pct=3, tp_pct=4, reference='mark')))['position']
    assert (updated['sl_order_id'], updated['tp_order_id']) == ids
    assert updated['tp_price'] == float(ee.snap_to_tick(exchange.prices[27] * (1 + d * .04), .5))
    assert not [c for c in exchange.calls if c['method'] == 'DELETE']
    m.set_protection('BTCUSD', m.ProtectionRequest(**binding(clear_sl=True, clear_tp=True)))
    assert not m.client_for('testnet', read_only=True).get_active_orders()


def test_entry_reference_warns_instead_of_silently_rebasing(exchange):
    exchange.prices[27] = 80000
    with pytest.raises(HTTPException) as error:
        m.set_protection('BTCUSD', m.ProtectionRequest(**binding(tp_pct=6)))
    assert error.value.status_code == 422 and "reference='mark'" in error.value.detail
    assert not [c for c in exchange.calls if c['method'] != 'GET']
    p = m.set_protection('BTCUSD', m.ProtectionRequest(**binding(tp_pct=6, reference='mark')))['position']
    assert p['tp_price'] == 84800


@pytest.mark.parametrize('values', [dict(tp_price=6, tp_pct=6), dict(clear_sl=True, sl_pct=2),
                                  dict(sl_pct=float('inf')), dict(sl_pct=100), dict(tp_price=float('nan'))])
def test_ambiguous_or_invalid_units_rejected(values):
    with pytest.raises(ValidationError):
        m.ProtectionRequest(**values)


def test_account_view_imports_all_symbols_and_does_not_show_stale_protection(exchange):
    exchange.positions[3136] = {'size': 17, 'entry_price': 3000}
    view = m.account_view('testnet')
    assert {(p['symbol'], p['contracts']) for p in view['positions']} == {('BTCUSD', 1), ('ETHUSD', 17)}
    with closing(ee.StateStore(bot_ops.mode_db('testnet'))) as store:
        assert store.position('testnet', 'ETHUSD')['contracts'] == 17
        store.update_position('testnet', 'BTCUSD', contracts=99, entry_price=1, sl_price=6)
    view = m.account_view('testnet')
    btc = next(p for p in view['positions'] if p['symbol'] == 'BTCUSD')
    assert (btc['contracts'], btc['entry_price'], btc['sl_price']) == (1, 68733.5, None)
    assert btc['tick_size'] == .5
    assert all(call['method'] == 'GET' for call in exchange.calls)


def test_both_protection_routes_and_state_import_external_positions(exchange):
    client = TestClient(app.app)
    reply = client.post('/api/manual/protect', json=dict(symbol='BTCUSD', **binding(sl_pct=2, tp_pct=6)))
    assert reply.status_code == 200, reply.text
    state = client.get('/api/manual/state?mode=testnet&include_candles=false')
    assert state.status_code == 200, state.text
    btc = state.json()['positions'][0]
    assert btc['sl_price'] == reply.json()['position']['sl_price']
    assert btc['tp_price'] == reply.json()['position']['tp_price']
    reply = client.put('/api/manual/position/BTCUSD/protection', json=binding(tp_price=72000))
    assert reply.status_code == 200, reply.text
    assert reply.json()['position']['tp_price'] == 72000


def test_edit_rejection_preserves_existing_stop(exchange, monkeypatch):
    p = m.set_protection('BTCUSD', m.ProtectionRequest(**binding(sl_pct=2)))['position']
    def refused(*args, **kwargs):
        raise ExchangeError('edit rejected')
    monkeypatch.setattr(m.client_for('testnet'), 'edit_order', refused)
    with pytest.raises(HTTPException):
        m.set_protection('BTCUSD', m.ProtectionRequest(**binding(sl_pct=3)))
    active = m.client_for('testnet', read_only=True).get_active_orders()
    assert len(active) == 1 and str(active[0]['id']) == p['sl_order_id']
    assert float(active[0]['stop_price']) == p['sl_price']


def test_partial_success_persisted(exchange, monkeypatch):
    client = m.client_for('testnet')
    place = client.place_order
    def fail_tp(**kwargs):
        if kwargs['stop_order_type'] == 'take_profit_order':
            raise ExchangeError('TP rejected')
        return place(**kwargs)
    monkeypatch.setattr(client, 'place_order', fail_tp)
    with pytest.raises(HTTPException):
        m.set_protection('BTCUSD', m.ProtectionRequest(**binding(sl_pct=2, tp_pct=4)))
    with closing(ee.StateStore(bot_ops.mode_db('testnet'))) as store:
        assert store.position('testnet', 'BTCUSD')['sl_order_id']
        assert store.position('testnet', 'BTCUSD')['tp_order_id'] is None


def test_missing_stop_readback_is_not_reported_as_success(exchange, monkeypatch):
    client = m.client_for('testnet')
    active = client.get_active_orders
    reads = []
    def disappear(*args, **kwargs):
        reads.append(True)
        return active(*args, **kwargs) if len(reads) == 1 else []
    monkeypatch.setattr(client, 'get_active_orders', disappear)
    with pytest.raises(HTTPException) as error:
        m.set_protection('BTCUSD', m.ProtectionRequest(**binding(sl_pct=2)))
    assert error.value.status_code == 409
    assert 'not confirmed by Delta readback' in error.value.detail


def test_percentages_that_snap_to_mark_or_nonpositive_prices_are_rejected(exchange):
    exchange.prices[27] = 68733.5
    with pytest.raises(HTTPException) as error:
        m.set_protection('BTCUSD', m.ProtectionRequest(**binding(sl_pct=.00001)))
    assert error.value.status_code == 422
    exchange.positions[27]['size'] = -1
    with pytest.raises(HTTPException) as error:
        m.set_protection('BTCUSD', m.ProtectionRequest(**binding(tp_pct=100)))
    assert error.value.status_code == 422
    assert all(call['method'] == 'GET' for call in exchange.calls)
