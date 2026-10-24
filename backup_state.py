"""Online backup / verify / restore of the bot's SQLite journal (Step 7).

Uses sqlite3.Connection.backup(), which copies a consistent snapshot INCLUDING pages that still live in
the -wal file, while the bot keeps writing. Never copy bot_state.db alone: committed rows can sit in
bot_state.db-wal until a checkpoint.

    python backup_state.py backup [--db data/bot_state.db] [--out data/backups]
    python backup_state.py verify data/backups/bot_state_20261020T110000Z.db
    python backup_state.py restore data/backups/bot_state_20261020T110000Z.db --target data/bot_state.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd

from config import settings

DEFAULT_DB = settings.data_path / settings.BOT_DB_FILE
BACKUP_DIR = settings.data_path / "backups"
LOCK_PATH = settings.data_path / "live_bot.lock"
EXPECTED_TABLES = ("bot_config_state", "order_intents", "open_positions", "trade_history")


class RestoreRefusedError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_online(src_path: Path, dst_path: Path) -> None:
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(dst_path))
    try:
        src.backup(dst, pages=256, sleep=0.01)
        dst.execute("PRAGMA journal_mode=DELETE")  # a backup is one self-contained file
    finally:
        dst.close()
        src.close()


def verify(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {"ok": False, "file": str(path), "error": "file not found"}
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        integrity = [r[0] for r in con.execute("PRAGMA integrity_check")]
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in EXPECTED_TABLES if t not in tables]
        counts = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in EXPECTED_TABLES if t in tables}
        mode = con.execute("SELECT mode FROM bot_config_state WHERE id=1").fetchone() if "bot_config_state" in tables else None
    finally:
        con.close()
    ok = integrity == ["ok"] and not missing
    return {"ok": ok, "file": str(path), "integrity": integrity[:5], "missing_tables": missing, "row_counts": counts,
            "mode": mode[0] if mode else None, "bytes": path.stat().st_size}


def backup(db_path: Path | str = DEFAULT_DB, out_dir: Path | str = BACKUP_DIR) -> dict[str, Any]:
    db_path, out_dir = Path(db_path), Path(out_dir)
    if not db_path.exists():
        raise FileNotFoundError(f"{db_path} does not exist")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = pd.Timestamp.now(tz="UTC").strftime("%Y%m%dT%H%M%SZ")
    target = out_dir / f"{db_path.stem}_{stamp}.db"
    n = 1
    while target.exists():
        target = out_dir / f"{db_path.stem}_{stamp}_{n}.db"
        n += 1
    tmp = target.with_suffix(".db.tmp")
    t0 = time.perf_counter()
    _copy_online(db_path, tmp)
    v = verify(tmp)
    if not v["ok"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed verification: {v}")
    os.replace(tmp, target)
    wal = db_path.with_name(db_path.name + "-wal")
    manifest = {
        "backup": str(target), "source": str(db_path), "created_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "method": "sqlite3.Connection.backup (online, WAL-consistent)", "sqlite_version": sqlite3.sqlite_version,
        "source_wal_bytes_at_backup": wal.stat().st_size if wal.exists() else 0, "sha256": _sha256(target),
        "seconds": round(time.perf_counter() - t0, 3), "verify": {**v, "file": str(target)},
    }
    target.with_suffix(".json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest["manifest"] = str(target.with_suffix(".json"))
    return manifest


def _held(path: Path) -> bool:
    from live_bot import InstanceLock
    return InstanceLock.is_held(path)


def restore(backup_file: Path | str, target: Path | str = DEFAULT_DB, lock_path: Path | str = LOCK_PATH) -> dict[str, Any]:
    """Atomically replace ``target`` with a verified copy of ``backup_file``. Refused while a runner is active.
    The bot instance lock AND the journal's writer lock are acquired first and held until the replacement is
    verified, so no runner or writer can start in between (Source #6)."""
    from execution_engine import StateStoreLockedError, _ProcessLock
    from live_bot import BotAlreadyRunningError, InstanceLock
    backup_file, target, lock_path = Path(backup_file), Path(target), Path(lock_path)
    try:
        bot_lock = InstanceLock(lock_path, "restore")
    except BotAlreadyRunningError as exc:
        raise RestoreRefusedError(f"{lock_path} is held by an active bot runner; stop it before restoring") from exc
    writer_lock = target.with_name(target.name + ".lock")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            journal_lock = _ProcessLock(writer_lock)
        except StateStoreLockedError as exc:
            raise RestoreRefusedError(f"{writer_lock} is held: another process has {target.name} open as writer") from exc
        try:
            return _replace_verified(backup_file, target)
        finally:
            journal_lock.release()
    finally:
        bot_lock.release()


def _replace_verified(backup_file: Path, target: Path) -> dict[str, Any]:
    v = verify(backup_file)
    if not v["ok"]:
        raise RestoreRefusedError(f"backup failed verification: {v}")
    tmp = target.with_name(target.name + ".restore.tmp")
    tmp.unlink(missing_ok=True)
    _copy_online(backup_file, tmp)
    vt = verify(tmp)
    if not vt["ok"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"restored copy failed verification: {vt}")
    previous = None
    if target.exists():
        previous = target.with_name(f"{target.stem}.pre_restore_{pd.Timestamp.now(tz='UTC'):%Y%m%dT%H%M%SZ}.db")
        _copy_online(target, previous)
    for suffix in ("-wal", "-shm"):  # a stale WAL next to the new file would be replayed into it
        target.with_name(target.name + suffix).unlink(missing_ok=True)
    os.replace(tmp, target)
    after = verify(target)
    if not after["ok"]:
        raise RuntimeError(f"target failed verification after restore: {after}")
    return {"restored": str(target), "from": str(backup_file), "previous_saved_as": str(previous) if previous else None,
            "verify": after}


def _held_writer(path: Path) -> bool:
    """The StateStore writer lock (byte 0 of '<db>.lock')."""
    if not path.exists():
        return False
    fd = os.open(str(path), os.O_RDWR)
    try:
        if sys.platform == "win32":
            import msvcrt
            os.lseek(fd, 0, 0)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return False
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--db", default=str(DEFAULT_DB))
    b.add_argument("--out", default=str(BACKUP_DIR))
    v = sub.add_parser("verify")
    v.add_argument("backup_file")
    r = sub.add_parser("restore")
    r.add_argument("backup_file")
    r.add_argument("--target", default=str(DEFAULT_DB))
    r.add_argument("--lock", default=str(LOCK_PATH))
    args = ap.parse_args()
    try:
        if args.cmd == "backup":
            out = backup(args.db, args.out)
        elif args.cmd == "verify":
            out = verify(args.backup_file)
        else:
            out = restore(args.backup_file, args.target, args.lock)
    except (RestoreRefusedError, FileNotFoundError, RuntimeError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 2
    print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok", True) and out.get("verify", {}).get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
