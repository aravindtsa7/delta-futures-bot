"""Shared Delta India wallet normalization for manual trading and the auto-bot."""
import math
from typing import Any

from config import settings
from exchange_client import ExchangeError


def wallet_from_balances(bals: list[dict[str, Any]]) -> dict[str, Any]:
    """Wallet balance is the risk base; available balance separately caps new margin.

    Delta's blocked_margin includes cross/isolated positions, orders and commissions.
    Unrealized position PnL is separate from this realized wallet balance.
    """
    usd = next((b for b in bals if b.get("asset_symbol") == "USD"), None)
    if usd is None:
        raise ExchangeError("no USD wallet balance returned by Delta")
    def number(key, default=None):
        value = usd.get(key)
        if value in (None, ""):
            if default is None:
                raise ExchangeError(f"Delta wallet is missing {key}")
            return default
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise ExchangeError(f"invalid Delta wallet {key}") from exc
        if not math.isfinite(result):
            raise ExchangeError(f"invalid Delta wallet {key}")
        return result
    rate = settings.FIXED_USD_INR_RATE
    balance, available = number("balance"), number("available_balance")
    components = sum(number(k, 0.0) for k in ("position_margin", "order_margin", "commission",
                     "cross_position_margin", "cross_order_margin", "cross_commission", "cross_locked_collateral"))
    used = number("blocked_margin", max(components, number("portfolio_margin", 0.0)))
    return {"wallet_equity_usd": balance, "available_margin_usd": available, "used_margin_usd": used,
            "wallet_equity_inr": number("balance_inr", balance * rate),
            "available_margin_inr": number("available_balance_inr", available * rate),
            "used_margin_inr": number("blocked_margin_inr", used * rate),
            "inr_source": "delta *_inr fields" if usd.get("balance_inr") not in (None, "") else f"USD x {rate:g}",
            "assets": [{k: b.get(k) for k in ("asset_symbol", "balance", "available_balance", "balance_inr")}
                       for b in bals if float(b.get("balance") or 0) or b.get("asset_symbol") in ("USD", "INR")]}
