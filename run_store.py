"""Content-addressed, server-authored backtest evidence; never accept client metrics as verified."""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path

import pandas as pd

import data_manager as dm
from config import PROJECT_ROOT, settings


def root() -> Path:
    return settings.data_path / "backtest_runs"


def fingerprint(df: pd.DataFrame) -> dict:
    # Includes all warmup rows, index, column order and values consumed by the run.
    raw = pd.util.hash_pandas_object(df, index=True).to_numpy().tobytes()
    digest = hashlib.sha256(json.dumps(list(df.columns)).encode() + raw).hexdigest()
    return {"sha256": digest, "rows": len(df), "start": df.index[0].isoformat(),
            "last_open": df.index[-1].isoformat(), "columns": list(df.columns)}


def code_fingerprint() -> dict:
    names = ("backtester.py", "strategies.py", "data_manager.py", "trade_rules.py", "app.py", "config.py", "run_store.py")
    return {name: hashlib.sha256((PROJECT_ROOT / name).read_bytes()).hexdigest() for name in names}


def put(document: dict) -> str:
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    run_id = hashlib.sha256(raw).hexdigest()
    path = root() / (run_id + ".json.gz")
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        dm._atomic_write_bytes(path, gzip.compress(raw, mtime=0))
    return run_id


def get(run_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", run_id):
        raise ValueError("invalid backtest run ID")
    raw = gzip.decompress((root() / (run_id + ".json.gz")).read_bytes())
    if hashlib.sha256(raw).hexdigest() != run_id:
        raise ValueError("backtest evidence checksum mismatch")
    return json.loads(raw)
