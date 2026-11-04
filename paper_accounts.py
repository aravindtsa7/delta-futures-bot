"""Recoverable PAPER wallet operations and immutable, read-only session archives.

Call apply() while holding the runner's InstanceLock. The journal writer lock
and profile file lock fence other writers. A durable intent precedes any active
state change; incomplete operations block StateStore/runner startup until retried.
"""
from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid

from dataset_lock import dataset_lock


class AccountError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status, self.detail = status, detail


def directory(db):
    db = Path(db)
    return db.parent / 'paper_sessions' / db.stem


def pending_path(db):
    return Path(db).with_name(Path(db).name + '.paper-account-pending.json')


def assert_ready(db):
    if pending_path(db).exists():
        raise AccountError(409, 'Paper account operation is incomplete. Retry it in Paper Account Settings before starting or trading.')


def _read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def registry(db):
    return _read(directory(db)/'index.json', {'active_id':'legacy', 'revision':0, 'sessions':[], 'receipts':{}})


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(value, f, indent=2, sort_keys=True, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def connect(db):
    con = sqlite3.connect(f'file:{Path(db).as_posix()}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    try:
        yield con
    finally:
        con.close()


def _account(con):
    cfg = con.execute('SELECT * FROM bot_config_state WHERE id=1').fetchone()
    if cfg is None or cfg['mode'] != 'paper':
        raise AccountError(409, 'An initialized PAPER wallet is required.')
    fx = cfg['usd_inr_rate']
    closed = con.execute("SELECT coalesce(sum(net_pnl_inr),0) FROM trade_history WHERE mode='paper'").fetchone()[0]
    opened = con.execute("SELECT coalesce(sum(realized_funding_usd-entry_fee_usd),0) FROM open_positions WHERE mode='paper'").fetchone()[0]
    meta = dict(con.execute('SELECT key,value FROM system_meta'))
    baseline = float(meta['utc_day_start_equity_usd'])*fx if meta.get('utc_day_start_equity_usd') else cfg['capital_inr']
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    obligations = con.execute("SELECT count(*) FROM funding_settlements WHERE mode='paper' AND status!='BOOKED'").fetchone()[0] if 'funding_settlements' in tables else 0
    # Filled/closed/cancelled/rejected are historical evidence, not unresolved orders.
    unresolved = con.execute("SELECT count(*) FROM order_intents WHERE mode='paper' "
                             "AND state NOT IN ('FILLED','CANCELLED','REJECTED')").fetchone()[0]
    return dict(capital_inr=cfg['capital_inr'], daily_loss_limit_pct=cfg['daily_loss_limit_pct'],
                usd_inr_rate=fx, equity_inr=cfg['capital_inr']+closed+opened*fx,
                daily_baseline_inr=baseline, baseline_date_utc=meta.get('utc_day_anchor_date'),
                allowance_inr=baseline*cfg['daily_loss_limit_pct']/100,
                baseline_basis='Persisted UTC daily opening equity' if meta.get('utc_day_start_equity_usd') else 'Initial paper capital; daily baseline not yet initialized',
                halted=bool(cfg['halted']), halt_reason=cfg['halt_reason'], halt_until_us=cfg['halt_until_us'],
                trade_count=con.execute("SELECT count(*) FROM trade_history WHERE mode='paper'").fetchone()[0],
                positions=con.execute("SELECT count(*) FROM open_positions WHERE mode='paper'").fetchone()[0],
                unresolved_intents=unresolved, funding_obligations=obligations)


def describe(db):
    reg = registry(db)
    pending = _read(pending_path(db))
    result = dict(session_id=reg['active_id'], revision=reg['revision'], sessions=reg['sessions'],
                  pending={'operation':pending['operation'], 'body':pending['body']} if pending else None)
    if not Path(db).exists():
        return {**result, 'initialized':False}
    with connect(db) as con:
        return {**result, **_account(con), 'initialized':True}


def archive(db, session_id):
    reg = registry(db)
    if session_id in (None, reg['active_id']):
        return None
    row = next((s for s in reg['sessions'] if s['id']==session_id), None)
    if row is None:
        raise AccountError(404, 'Unknown paper session.')
    path = Path(row['file'])
    if not path.exists():
        raise AccountError(404, 'Archived paper journal is unavailable; restore its verified backup file.')
    return path


def _profiles(raw, capital, limit, fx, *, update_capital=True):
    import execution_engine as ee
    result = json.loads(json.dumps(raw))
    for sym, profile in result.items():
        cfg = ee.profile_config(ee._profile_from_dict(sym, profile))
        if profile.get('enabled', True) and cfg.usd_inr_rate != fx:
            raise AccountError(409, f'{sym}: profile FX differs from the paper wallet. FX is not changed by account settings.')
        effective = asdict(cfg)
        effective.update(max_daily_loss_pct=limit)
        profile.update(max_daily_loss_pct=limit, daily_loss_limit_pct=limit,
                       daily_loss_stop_pct=limit, effective_config=effective)
        if update_capital:
            effective['initial_capital_inr'] = capital
            profile['capital_inr'] = capital
            if 'initial_capital_inr' in profile:
                profile['initial_capital_inr'] = capital
        ee.profile_config(ee._profile_from_dict(sym, profile))
    return result


def _profile_snapshot(raw):
    import execution_engine as ee
    return json.dumps({sym:asdict(ee._profile_from_dict(sym, p)) for sym,p in raw.items()}, sort_keys=True)


def _completed_db_operation(db, request_id):
    if not Path(db).exists():
        return False
    with connect(db) as con:
        row = con.execute("SELECT value FROM system_meta WHERE key='paper_account_operation_id'").fetchone()
        return row is not None and row[0] == request_id


def _finish(db, profiles_path, plan):
    import backup_state
    body = plan['body']
    if not _completed_db_operation(db, body['request_id']):
        if plan['operation'] == 'new_session':
            backup_state._replace_verified(Path(plan['prepared_db']), Path(db))
        else:
            con = sqlite3.connect(str(db), isolation_level=None)
            try:
                con.execute('PRAGMA synchronous=FULL')
                con.execute('BEGIN IMMEDIATE')
                con.execute('UPDATE bot_config_state SET daily_loss_limit_pct=?,profiles_json=?,updated_at_us=? WHERE id=1',
                            (body['daily_loss_limit_pct'], _profile_snapshot(plan['profiles']), plan['at_us']))
                con.execute("INSERT INTO system_meta(key,value,updated_at_us) VALUES('paper_account_operation_id',?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at_us=excluded.updated_at_us",
                            (body['request_id'],plan['at_us']))
                con.execute("INSERT INTO events(at_us,mode,level,kind,detail) VALUES(?,'paper','INFO','paper_account_limit_updated',?)",
                            (plan['at_us'],json.dumps(plan['response'])))
                con.execute('COMMIT')
            except BaseException:
                if con.in_transaction: con.execute('ROLLBACK')
                raise
            finally:
                con.close()
    _atomic_json(profiles_path, plan['profiles'])
    _atomic_json(directory(db)/'index.json', plan['registry'])
    pending_path(db).unlink()
    if plan.get('prepared_db'):
        Path(plan['prepared_db']).unlink(missing_ok=True)
    return plan['response']


def apply(db, profiles_path, backup_dir, operation, body):
    """Caller holds InstanceLock; recover only the exact same authorized request."""
    import execution_engine as ee
    import backup_state
    db, profiles_path = Path(db), Path(profiles_path)
    limit = body['daily_loss_limit_pct']
    capital = body.get('capital_inr')
    if not math.isfinite(limit) or not 0 < limit <= 100:
        raise AccountError(422, 'Daily Loss Limit must be finite, greater than 0 and at most 100 percent.')
    if operation=='new_session' and (capital is None or not math.isfinite(capital) or capital<=0):
        raise AccountError(422, 'Paper Capital must be finite and greater than zero.')
    if operation=='new_session' and not math.isfinite(body['confirmed_equity_inr']):
        raise AccountError(422, 'Confirmed old equity must be finite.')
    if not db.exists() and operation != 'new_session':
        raise AccountError(409, 'An initialized PAPER wallet is required.')
    fingerprint = hashlib.sha256(json.dumps({'operation':operation,'body':body},sort_keys=True,allow_nan=False).encode()).hexdigest()
    db.parent.mkdir(parents=True,exist_ok=True)
    writer = ee._ProcessLock(db.with_name(db.name+'.lock'))
    try:
        with dataset_lock(profiles_path):
            pending = _read(pending_path(db))
            if pending:
                if pending['fingerprint'] != fingerprint:
                    raise AccountError(409, 'Another paper account operation is incomplete. Retry the pending operation first.')
                return _finish(db, profiles_path, pending)
            reg = registry(db)
            receipt = reg['receipts'].get(body['request_id'])
            if receipt:
                if receipt['fingerprint'] != fingerprint:
                    raise AccountError(409, 'Request ID was already used with different settings.')
                return receipt['response']
            if body['expected_session_id'] != reg['active_id'] or body['expected_revision'] != reg['revision']:
                raise AccountError(409, 'Paper account changed. Refresh the applied values and confirm again.')
            raw = _read(profiles_path, {})
            initializing = not db.exists()
            if initializing:
                enabled = [ee.profile_config(ee._profile_from_dict(sym,p)) for sym,p in raw.items() if p.get('enabled',True)]
                account = dict(positions=0,unresolved_intents=0,funding_obligations=0,equity_inr=0.,
                               usd_inr_rate=enabled[0].usd_inr_rate if enabled else ee.settings.FIXED_USD_INR_RATE,
                               capital_inr=0.,daily_baseline_inr=0.,halted=False,trade_count=0)
                cfg = None
            else:
                with connect(db) as con:
                    account = _account(con)
                    cfg = dict(con.execute('SELECT * FROM bot_config_state WHERE id=1').fetchone())
            if account['positions'] or account['unresolved_intents'] or account['funding_obligations']:
                raise AccountError(409, 'Stop the runner and resolve open positions, unresolved order intents and outstanding funding before changing the paper account.')
            if operation=='new_session' and (not math.isfinite(body['confirmed_equity_inr']) or
                                             abs(body['confirmed_equity_inr']-account['equity_inr'])>.005):
                raise AccountError(409, 'Old equity changed. Refresh and confirm the new session again.')
            target_capital = capital if operation=='new_session' else account['capital_inr']
            profiles = _profiles(raw,target_capital,limit,account['usd_inr_rate'],update_capital=operation=='new_session')
            backup = backup_state.backup(db, backup_dir) if not initializing else {'backup':None}
            profile_backup = Path(backup['backup']).with_suffix('.profiles.json') if backup['backup'] else Path(backup_dir)/('paper_initial_'+uuid.uuid4().hex+'.profiles.json')
            _atomic_json(profile_backup,raw)
            now = datetime.now(timezone.utc)
            at_us = int(now.timestamp()*1_000_000)
            new_id = 'paper-'+uuid.uuid4().hex if operation=='new_session' else reg['active_id']
            response = dict(ok=True, operation=operation, session_id=new_id, previous_session_id=reg['active_id'],
                            capital_inr=target_capital, daily_loss_limit_pct=limit, previous_equity_inr=account['equity_inr'],
                            baseline_inr=target_capital if operation=='new_session' else account['daily_baseline_inr'],
                            halted=False if operation=='new_session' else account['halted'],
                            stopped=True, backup=backup['backup'], revision=reg['revision']+1,
                            message='New paper session created; start it manually.' if operation=='new_session' else 'Daily limit saved. Existing halt, balance, baseline and history are preserved; this does not resume trading.')
            plan = dict(operation=operation,body=body,fingerprint=fingerprint,profiles=profiles,
                        at_us=at_us,response=response,registry=reg)
            if operation=='new_session':
                if not initializing:
                    reg['sessions'].append(dict(id=reg['active_id'],file=backup['backup'],closed_at=now.isoformat(),
                                                equity_inr=account['equity_inr'],trade_count=account['trade_count'],
                                                halted=account['halted'],sha256=backup['sha256']))
                prepared = directory(db)/(uuid.uuid4().hex+'.prepared.db')
                prepared.parent.mkdir(parents=True,exist_ok=True)
                store = ee.StateStore(prepared)
                try:
                    if initializing:
                        store.init_config('paper',{sym:ee._profile_from_dict(sym,p) for sym,p in profiles.items()})
                        cfg = dict(store.config())
                        with store.tx() as cx:
                            cx.execute('DELETE FROM bot_config_state')
                    cfg.update(capital_inr=target_capital,daily_loss_limit_pct=limit,profiles_json=_profile_snapshot(profiles),
                               halted=0,entries_paused=0,halt_reason=None,halt_until_us=None,updated_at_us=at_us)
                    with store.tx() as cx:
                        cx.execute('INSERT INTO bot_config_state ('+','.join(cfg)+') VALUES('+','.join('?' for _ in cfg)+')',tuple(cfg.values()))
                        store.meta_set(utc_day_anchor_date=now.strftime('%Y-%m-%d'),
                                       utc_day_start_equity_usd=target_capital/cfg['usd_inr_rate'],
                                       paper_account_operation_id=body['request_id'],paper_session_id=new_id)
                        store.event('INFO','paper_session_created',None,response,'paper')
                finally:
                    store.close()
                plan['prepared_db'] = str(prepared)
            reg['active_id'], reg['revision'] = new_id, reg['revision']+1
            reg['receipts'][body['request_id']] = dict(fingerprint=fingerprint,response=response)
            _atomic_json(pending_path(db),plan)
            return _finish(db,profiles_path,plan)
    finally:
        writer.release()
