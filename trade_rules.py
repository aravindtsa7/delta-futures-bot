"""Shared causal trade configuration and next-candle stop management."""
from __future__ import annotations


def stop_config(sl_type: str, sl_value: float, tp_type: str, tp_value: float) -> dict:
    sl_type = {"fixed_pct": "pct", "atr_mult": "atr"}.get(sl_type, sl_type)
    tp_type = {"fixed_pct": "pct", "atr_mult": "atr", "rr_ratio": "rr"}.get(tp_type, tp_type)
    if sl_type not in ("pct", "atr") or tp_type not in ("pct", "atr", "rr"):
        raise ValueError("unsupported SL/TP type")
    if sl_value <= 0 or tp_value <= 0:
        raise ValueError("SL/TP values must be positive")
    return {"sl_mode": sl_type, "stop_loss_pct" if sl_type == "pct" else "sl_atr_mult": sl_value,
            "tp_mode": tp_type, {"pct": "take_profit_pct", "atr": "tp_atr_mult", "rr": "tp_rr_ratio"}[tp_type]: tp_value}


def advance_stop(cfg, product, direction, entry, initial_sl, sl, be_price, be_armed, extreme, high, low, trail_distance):
    """Evaluate only AFTER exits in the current candle; result applies next candle."""
    extreme = max(extreme, high) if direction == 1 else min(extreme, low)
    if cfg.breakeven_at_1r and not be_armed and initial_sl is not None and sl is not None:
        risk = abs(entry - initial_sl)
        reached = extreme >= entry + risk if direction == 1 else extreme <= entry - risk
        if reached:
            be_armed = True
            sl = max(sl, be_price) if direction == 1 else min(sl, be_price)
    candidates = []
    if cfg.trailing_sl_pct > 0:
        candidates.append(extreme * (1 - direction * cfg.trailing_sl_pct / 100))
    if trail_distance > 0:
        candidates.append(extreme - direction * trail_distance)
    for level in candidates:
        level = product.round_price(level)
        sl = level if sl is None else (max(sl, level) if direction == 1 else min(sl, level))
    return sl, extreme, be_armed
