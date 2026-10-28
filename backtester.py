"""Delta Exchange India futures simulator (1m intrabar exits, one position per symbol).

A single-symbol backtest is a portfolio with one leg; multi-symbol runs share one INR wallet.

Execution model (per 1m candle, in this order)
  0. Funding settles at 00/08/16 UTC for positions opened strictly before the timestamp:
     ``cashflow = -direction * contracts * contract_value * mark * funding_rate`` using the last
     1m mark close / funding rate before settlement. It is booked into the isolated position
     margin, so received funding stays locked until the position closes.
  1. Signals: a signal on HTF bar ``[t, t+tf)`` acts at the OPEN of the 1m candle at ``t+tf``.
       - flat -> enter (unless in cooldown or the UTC day is halted by the daily-loss breaker)
       - in position, opposite signal (or the strategy's ``exit_signal`` for that side) and
         ``exit_on_opposite_signal`` -> exit at that open (reason OPP / XSIG)
       - otherwise ignored (``skipped_in_position``)
     Cooldown: after an exit inside HTF bar B, the earliest entry is at end(B) + cooldown_bars*tf.
  2. Intrabar exits (entry candle included, since the fill is at its open):
       - SL triggers on low/high (mark_low/mark_high when ``trigger_source="mark"``). If the
         trigger series opens beyond the stop (gap), the fill is the worse of the 1m trade open
         and the stop; otherwise the stop price.
       - TP fills exactly at the TP limit price.
       - SL and TP both touched in one candle -> SL first (``ambiguous_1m_bars`` += 1).
       - Breakeven / trailing stops are updated only AFTER the candle is evaluated, so they take
         effect from the next 1m candle (no intrabar ordering assumption). The ATR trail distance
         is ``trailing_atr_mult * ATR(14)`` of the signal bar, fixed for the life of the trade.
  3. Mark-to-market on 1m close; if equity <= UTC-day-start equity * (1 - max_daily_loss_pct),
     all positions are closed at that close and new entries are blocked until the next UTC day.

Fees: ``notional * rate * (1 + GST)`` per leg. Entry is taker (maker in ``fee_mode="maker"``);
TP exits are maker in ``taker_maker_tp``/``maker``; every other exit (SL/BE/TSL/OPP/DAILY/END) is taker.

Slippage (``slippage_bps`` per fill, default 0): every fill is moved adversely from its raw price -
buys at ``raw * (1 + s)``, sells at ``raw * (1 - s)``. SL/TP levels are set from the executed entry.
With ``trigger_source="mark"`` and ``mark_fill_mode="traded"`` stops/targets trigger on 1m mark
high/low but fill from the 1m TRADED range: the level clamped to [trade low, trade high], or the
trade open if the mark had already gapped through the level at the open. ``mark_fill_mode="trigger"``
(default, Step 3/4 behaviour) fills at the level itself.

Lot sizing: ``allow_min_one_lot=False`` never rounds up to 1 contract when the risk budget cannot
afford it (counted as ``skipped_lot_budget``).

Legs may carry their own BacktestConfig (stops, exits, timeframe, strategy); wallet-level
settings (capital, FX rate, GST, daily-loss stop) always come from the portfolio config.

All accounting is in USD (settling asset); reporting in INR at the fixed rate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Literal, Sequence

import numpy as np
import pandas as pd

import data_manager as dm
import strategies
from config import settings

FeeMode = Literal["taker", "taker_maker_tp", "maker", "maker_maker"]
TriggerSource = Literal["trade", "mark"]
StopMode = Literal["pct", "atr"]
MarkFillMode = Literal["trigger", "traded"]

MARKET_EXITS = {"SL", "BE", "TSL", "OPP", "XSIG", "DAILY", "END"}
WALLET_FIELDS = ("initial_capital_inr", "usd_inr_rate", "gst_rate", "max_daily_loss_pct")

# Canonical trade-ledger schema (the keys _Sim._close writes), so a zero-trade run still has every column (F11).
TRADE_COLUMNS: dict[str, str] = {
    "trade_id": "int64", "symbol": "object", "side": "object", "signal_bar": "datetime64[ns, UTC]",
    "entry_time": "datetime64[ns, UTC]", "exit_time": "datetime64[ns, UTC]", "minutes_held": "int64", "contracts": "int64",
    "contract_value": "float64", "entry_price": "float64", "exit_price": "float64", "raw_entry_price": "float64",
    "raw_exit_price": "float64", "slippage_bps": "float64", "slippage_usd": "float64", "risk_usd": "float64",
    "risk_pct_of_equity": "float64", "sl_pct": "float64", "atr_at_signal": "float64", "initial_sl": "float64",
    "final_sl": "float64", "be_price": "float64", "tp_price": "float64", "exit_reason": "object",
    "notional_entry_usd": "float64", "notional_exit_usd": "float64", "margin_usd": "float64",
    "equity_at_entry_usd": "float64", "available_at_entry_usd": "float64", "risk_contracts": "int64",
    "margin_cap_contracts": "int64", "min_lot_floor": "int64", "gross_pnl_usd": "float64", "entry_fee_rate": "float64",
    "exit_fee_rate": "float64", "entry_fee_usd": "float64", "exit_fee_usd": "float64", "fees_usd": "float64",
    "gst_usd": "float64", "funding_usd": "float64", "funding_events": "int64", "net_pnl_usd": "float64",
    "net_pnl_inr": "float64", "equity_after_inr": "float64", "ambiguous_1m": "bool",
}
FUNDING_COLUMNS = ["trade_id", "symbol", "time", "side", "contracts", "contract_value", "mark_price", "notional_usd",
                   "funding_rate", "funding_rate_pct", "cashflow_usd", "cashflow_inr"]


def empty_trades() -> pd.DataFrame:
    """A zero-row trade ledger with the full canonical schema."""
    return pd.DataFrame({c: pd.Series(dtype=t) for c, t in TRADE_COLUMNS.items()})


# --------------------------------------------------------------------------- #
# Config / product
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_capital_inr: float = settings.INITIAL_CAPITAL_INR
    leverage: float = settings.DEFAULT_LEVERAGE
    risk_per_trade_pct: float = settings.RISK_PER_TRADE_PCT
    # Stops
    sl_mode: StopMode = "pct"
    stop_loss_pct: float = settings.STOP_LOSS_PCT
    take_profit_pct: float = settings.TAKE_PROFIT_PCT
    sl_atr_mult: float = 1.5
    tp_atr_mult: float = 3.5
    atr_sl_min_pct: float = 0.8
    atr_sl_max_pct: float = 4.0
    trailing_sl_pct: float = 0.0
    trailing_atr_mult: float = 0.0
    breakeven_at_1r: bool = False
    # Signal handling / risk controls
    exit_on_opposite_signal: bool = False
    cooldown_bars: int = 1
    max_daily_loss_pct: float = settings.MAX_DAILY_LOSS_PCT  # 0 disables
    # Costs / execution
    fee_mode: FeeMode = "taker"
    trigger_source: TriggerSource = "trade"
    usd_inr_rate: float = settings.FIXED_USD_INR_RATE
    gst_rate: float = settings.GST_ON_FEES
    margin_buffer: float = 0.95
    min_lot_max_loss_pct: float = 5.0
    allow_min_one_lot: bool = True
    slippage_bps: float = 0.0
    mark_fill_mode: MarkFillMode = "trigger"

    def __post_init__(self) -> None:
        if not 0 <= self.slippage_bps <= 100:
            raise ValueError("slippage_bps must be in [0, 100]")
        if self.mark_fill_mode not in ("trigger", "traded"):
            raise ValueError("mark_fill_mode must be 'trigger' or 'traded'")
        if self.fee_mode not in ("taker", "taker_maker_tp", "maker", "maker_maker"):
            raise ValueError("fee_mode must be 'taker', 'taker_maker_tp', 'maker' or 'maker_maker'")
        if self.trigger_source not in ("trade", "mark"):
            raise ValueError("trigger_source must be 'trade' or 'mark'")
        if self.sl_mode not in ("pct", "atr"):
            raise ValueError("sl_mode must be 'pct' or 'atr'")
        if self.leverage < 1:
            raise ValueError("leverage must be >= 1")
        max_stop = self.stop_loss_pct if self.sl_mode == "pct" else self.atr_sl_max_pct
        if not 0 < max_stop < 100 / self.leverage:
            # Liquidation is not simulated, so the widest stop must sit inside the liquidation distance.
            raise ValueError(f"stop distance must be in (0, {100 / self.leverage:g})% at {self.leverage:g}x")
        if self.sl_mode == "atr" and not (0 < self.atr_sl_min_pct <= self.atr_sl_max_pct and self.sl_atr_mult > 0 < self.tp_atr_mult):
            raise ValueError("invalid ATR stop settings")
        if self.take_profit_pct <= 0 or self.risk_per_trade_pct <= 0 or self.cooldown_bars < 0:
            raise ValueError("take_profit_pct/risk_per_trade_pct must be > 0; cooldown_bars >= 0")
        if self.trailing_sl_pct < 0 or self.trailing_atr_mult < 0:
            raise ValueError("trailing_sl_pct/trailing_atr_mult must be >= 0")

    # -- fees ---------------------------------------------------------------- #
    @property
    def entry_fee_rate(self) -> float:
        return settings.MAKER_FEE if self.fee_mode in ("maker", "maker_maker") else settings.TAKER_FEE

    def exit_fee_rate(self, reason: str) -> float:
        if reason == "TP" and self.fee_mode in ("taker_maker_tp", "maker", "maker_maker"):
            return settings.MAKER_FEE
        return settings.TAKER_FEE

    def with_gst(self, rate: float) -> float:
        return rate * (1.0 + self.gst_rate)

    @property
    def slip(self) -> float:
        """Adverse slippage per fill as a fraction."""
        return self.slippage_bps / 10_000.0

    def exec_price(self, raw: float, side: int) -> float:
        """Executed price for a fill; side +1 = buy, -1 = sell."""
        return raw * (1.0 + side * self.slip)

    # -- labels -------------------------------------------------------------- #
    @property
    def stop_label(self) -> str:
        if self.sl_mode == "pct":
            return f"pct {self.stop_loss_pct:g}/{self.take_profit_pct:g}"
        return f"atr {self.sl_atr_mult:g}x/{self.tp_atr_mult:g}x"

    def stop_pcts(self, entry: float, atr_value: float) -> tuple[float, float] | None:
        """(sl_pct, tp_pct) distances in percent; None if the ATR is not available yet."""
        if self.sl_mode == "pct":
            return self.stop_loss_pct, self.take_profit_pct
        if not (atr_value > 0):
            return None
        raw = self.sl_atr_mult * atr_value / entry * 100.0
        sl_pct = min(max(raw, self.atr_sl_min_pct), self.atr_sl_max_pct)
        # Keep the configured reward:risk when the SL distance is clamped.
        return sl_pct, sl_pct * self.tp_atr_mult / self.sl_atr_mult


@dataclass(frozen=True, slots=True)
class ProductSpec:
    symbol: str
    product_id: int
    contract_value: float
    tick_size: float

    @classmethod
    def from_metadata(cls, symbol: str) -> "ProductSpec":
        m = dm.load_metadata(symbol)
        return cls(symbol, int(m["id"]), float(m["contract_value"]), float(m["tick_size"]))

    @property
    def tick_decimals(self) -> int:
        return max(0, -Decimal(str(self.tick_size)).normalize().as_tuple().exponent)

    def _to_tick(self, ticks: float) -> float:
        return round(ticks * self.tick_size, self.tick_decimals)

    def round_price(self, price: float) -> float:
        return self._to_tick(round(price / self.tick_size))

    def ceil_price(self, price: float) -> float:
        return self._to_tick(math.ceil(round(price / self.tick_size, 9)))

    def floor_price(self, price: float) -> float:
        return self._to_tick(math.floor(round(price / self.tick_size, 9)))


@dataclass(slots=True)
class Leg:
    """One symbol in a run: 1m data + HTF signals (+ HTF ATR, strategy exit signals, own config)."""
    product: ProductSpec
    df_1m: pd.DataFrame
    signals: pd.Series
    timeframe: str
    atr: pd.Series | None = None
    strategy: str = ""
    exits: pd.Series | None = None
    config: BacktestConfig | None = None  # per-leg trade management; None -> portfolio config


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class BacktestResult:
    symbols: list[str]
    strategy: str
    timeframe: str
    config: BacktestConfig
    products: dict[str, ProductSpec]
    summary: dict[str, Any]
    trades: pd.DataFrame
    funding: pd.DataFrame
    equity: pd.Series  # INR, mark-to-market on 1m trade close
    leg_configs: dict[str, BacktestConfig] = field(default_factory=dict)
    timeframes: dict[str, str] = field(default_factory=dict)
    strategies: dict[str, str] = field(default_factory=dict)

    @property
    def symbol(self) -> str:
        return "+".join(self.symbols)

    @property
    def product(self) -> ProductSpec:
        return self.products[self.symbols[0]]


@dataclass(slots=True)
class _Position:
    trade_id: int
    direction: int
    contracts: int
    entry_price: float
    entry_time: int
    entry_index: int
    signal_time: int
    sl: float
    initial_sl: float
    tp: float
    sl_pct: float
    atr_at_signal: float
    margin: float
    entry_fee: float
    equity_at_entry: float
    available_at_entry: float
    risk_contracts: int
    margin_cap_contracts: int
    min_lot_floor: bool
    be_price: float
    extreme: float
    raw_entry_price: float = 0.0
    risk_usd: float = 0.0
    trail_dist: float = 0.0
    be_armed: bool = False
    funding: float = 0.0
    funding_events: int = 0


@dataclass(slots=True)
class _LegState:
    leg: Leg
    cfg: BacktestConfig
    bt: "Backtester"
    exit_map: dict[int, int]
    k: int
    cv: float
    tf_sec: int
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    to: np.ndarray
    th: np.ndarray
    tl: np.ndarray
    mark_close: np.ndarray
    funding_rate: np.ndarray
    entry_map: dict[int, tuple[int, int]]
    atr_map: dict[int, float]
    pos: _Position | None = None
    next_allowed: int = -(2**62)


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #
class Backtester:
    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    # -- sizing / levels ----------------------------------------------------- #
    def size_position(
        self, equity_usd: float, entry: float, sl: float, product: ProductSpec, available_usd: float | None = None,
    ) -> tuple[int, int, int, bool]:
        """Return (contracts, risk_contracts, margin_cap_contracts, min_lot_floor_used).

        Risk budget is a % of realized equity; the margin cap uses the free (available) balance.
        Loss per contract at SL includes the entry fee, the (taker) SL exit fee, incl. GST, and
        adverse slippage on both fills.
        """
        cfg = self.config
        available = equity_usd if available_usd is None else available_usd
        cv = product.contract_value
        fe, fx = cfg.with_gst(cfg.entry_fee_rate), cfg.with_gst(settings.TAKER_FEE)
        loss_per_contract = self.loss_per_contract(entry, sl, product)
        risk_usd = equity_usd * cfg.risk_per_trade_pct / 100.0
        risk_contracts = math.floor(risk_usd / loss_per_contract) if loss_per_contract > 0 else 0

        margin_per_contract = cv * entry / cfg.leverage
        roundtrip_fee_per_contract = cv * entry * (fe + fx)
        margin_cap = max(0, math.floor(available * cfg.margin_buffer / (margin_per_contract + roundtrip_fee_per_contract)))

        contracts = min(risk_contracts, margin_cap)
        floor_used = False
        if (cfg.allow_min_one_lot and risk_contracts == 0 and margin_cap >= 1
                and loss_per_contract <= equity_usd * cfg.min_lot_max_loss_pct / 100.0):
            contracts, floor_used = 1, True
        return max(contracts, 0), risk_contracts, margin_cap, floor_used

    def loss_per_contract(self, entry: float, sl: float, product: ProductSpec) -> float:
        """USD lost per contract if the stop fills at ``sl``: price move + fees (incl. GST) + slippage."""
        cfg = self.config
        cv = product.contract_value
        fe, fx = cfg.with_gst(cfg.entry_fee_rate), cfg.with_gst(settings.TAKER_FEE)
        s = cfg.slip
        return cv * abs(entry - sl) + cv * entry * fe + cv * sl * fx + cv * (entry + sl) * s

    def levels(self, entry: float, direction: int, sl_pct: float, tp_pct: float, product: ProductSpec) -> tuple[float, float]:
        sl = product.round_price(entry * (1 - direction * sl_pct / 100.0))
        tp = product.round_price(entry * (1 + direction * tp_pct / 100.0))
        return sl, tp

    def breakeven_price(self, entry: float, direction: int, product: ProductSpec) -> float:
        """Stop price at which entry fee + taker exit fee (incl. GST) are exactly covered."""
        cfg = self.config
        fe, fx = cfg.with_gst(cfg.entry_fee_rate), cfg.with_gst(settings.TAKER_FEE)
        if direction == 1:
            return product.ceil_price(entry * (1 + fe) / (1 - fx))
        return product.floor_price(entry * (1 - fe) / (1 + fx))

    # -- public entry points ------------------------------------------------- #
    def run(
        self,
        df_1m: pd.DataFrame,
        signals: pd.Series,
        timeframe: str,
        product: ProductSpec,
        strategy_name: str = "",
        atr: pd.Series | None = None,
        start: pd.Timestamp | None = None,
        end: pd.Timestamp | None = None,
    ) -> BacktestResult:
        return self.run_portfolio([Leg(product, df_1m, signals, timeframe, atr, strategy_name)], start, end)

    def run_portfolio(
        self, legs: Sequence[Leg], start: pd.Timestamp | None = None, end: pd.Timestamp | None = None,
    ) -> BacktestResult:
        """Simulate legs sharing one wallet over the common 1m index, optionally within [start, end)."""
        cfg = self.config
        rate = cfg.usd_inr_rate
        index = legs[0].df_1m.index
        for leg in legs[1:]:
            index = index.intersection(leg.df_1m.index)
        if start is not None:
            index = index[index >= start]
        if end is not None:
            index = index[index < end]
        if len(index) == 0:
            raise ValueError("empty simulation window")
        t = index.as_unit("s").asi8

        states: list[_LegState] = []
        for k, leg in enumerate(legs):
            leg_cfg = leg.config or cfg
            bad = [f for f in WALLET_FIELDS if getattr(leg_cfg, f) != getattr(cfg, f)]
            if bad:
                raise ValueError(f"{leg.product.symbol}: leg config must match portfolio config on {bad}")
            df = leg.df_1m.reindex(index)
            tf_sec = dm.TIMEFRAME_MINUTES[leg.timeframe] * 60
            exit_map: dict[int, int] = {}
            if leg.exits is not None:
                xz = leg.exits[leg.exits != 0]
                exit_map = {int(s) + tf_sec: int(d) for s, d in zip(xz.index.as_unit("s").asi8, xz.to_numpy())}
            if leg_cfg.trigger_source == "mark":
                to, th, tl = (df[x].to_numpy("float64") for x in ("mark_open", "mark_high", "mark_low"))
            else:
                to, th, tl = (df[x].to_numpy("float64") for x in ("open", "high", "low"))
            nz = leg.signals[leg.signals != 0]
            sig_t = nz.index.as_unit("s").asi8
            entry_map = {int(s) + tf_sec: (int(d), int(s)) for s, d in zip(sig_t, nz.to_numpy())}
            atr_map: dict[int, float] = {}
            if leg.atr is not None:
                a = leg.atr.reindex(nz.index)
                atr_map = dict(zip(sig_t.tolist(), a.to_numpy("float64").tolist()))
            states.append(_LegState(
                leg=leg, cfg=leg_cfg, bt=Backtester(leg_cfg), exit_map=exit_map,
                k=k, cv=leg.product.contract_value, tf_sec=tf_sec,
                o=df["open"].to_numpy("float64"), h=df["high"].to_numpy("float64"), l=df["low"].to_numpy("float64"),
                c=df["close"].to_numpy("float64"), to=to, th=th, tl=tl,
                mark_close=df["mark_close"].to_numpy("float64"), funding_rate=df["funding_rate"].to_numpy("float64"),
                entry_map=entry_map, atr_map=atr_map,
            ))

        entry_keys = np.fromiter({k for s in states for k in s.entry_map}, dtype="int64")
        entry_idx = np.flatnonzero(np.isin(t, entry_keys))

        sim = _Sim(self, states, t, rate)
        sim.loop(entry_idx)

        trades = pd.DataFrame(sim.trades) if sim.trades else empty_trades()
        funding = pd.DataFrame(sim.fund_rows) if sim.fund_rows else pd.DataFrame(columns=FUNDING_COLUMNS)
        equity = pd.Series(sim.equity, index=index, name="equity_inr")
        n_signals = sum(len(s.entry_map) for s in states)
        summary = _summarise(cfg, trades, funding, equity, sim.counters, n_signals, len(legs))
        return BacktestResult(
            symbols=[leg.product.symbol for leg in legs],
            strategy=legs[0].strategy,
            timeframe=legs[0].timeframe,
            config=cfg,
            products={leg.product.symbol: leg.product for leg in legs},
            summary=summary,
            trades=trades,
            funding=funding,
            equity=equity,
            leg_configs={s.leg.product.symbol: s.cfg for s in states},
            timeframes={leg.product.symbol: leg.timeframe for leg in legs},
            strategies={leg.product.symbol: leg.strategy for leg in legs},
        )


class _Sim:
    """Mutable state for one simulation (kept out of Backtester so runs are independent)."""

    def __init__(self, bt: Backtester, states: list[_LegState], t: np.ndarray, rate: float) -> None:
        self.bt, self.cfg, self.states, self.t, self.rate = bt, bt.config, states, t, rate
        self.wallet = self.cfg.initial_capital_inr / rate
        self.equity = np.empty(len(t))
        self.trades: list[dict[str, Any]] = []
        self.fund_rows: list[dict[str, Any]] = []
        self.trades_opened = 0
        self.counters = dict(
            ambiguous_1m_bars=0, min_lot_floor_used=0, skipped_in_position=0, skipped_cooldown=0,
            skipped_daily_halt=0, skipped_insufficient_capital=0, skipped_lot_budget=0, skipped_no_atr=0,
            funding_missing_rate=0, daily_halts=0,
        )

    # -- helpers ------------------------------------------------------------- #
    def _realized_equity(self) -> float:
        return self.wallet + sum(s.pos.margin for s in self.states if s.pos is not None)

    def _mtm(self, i: int) -> float:
        eq = self.wallet
        for s in self.states:
            p = s.pos
            if p is not None:
                eq += p.margin + p.direction * p.contracts * s.cv * (s.c[i] - p.entry_price)
        return eq

    def _open(self, s: _LegState, i: int, direction: int, sig_t: int) -> None:
        bt, cfg, product = s.bt, s.cfg, s.leg.product
        raw_px = s.o[i]
        px = raw_px if cfg.fee_mode == "maker_maker" else cfg.exec_price(raw_px, direction)  # entry buys for longs, sells for shorts
        atr_v = s.atr_map.get(sig_t, float("nan"))
        pcts = cfg.stop_pcts(px, atr_v)
        if pcts is None or (cfg.trailing_atr_mult > 0 and not atr_v > 0):
            self.counters["skipped_no_atr"] += 1
            return
        sl_pct, tp_pct = pcts
        sl, tp = bt.levels(px, direction, sl_pct, tp_pct, product)
        equity, available = self._realized_equity(), self.wallet
        n, n_risk, n_cap, floor_used = bt.size_position(equity, px, sl, product, available)
        if n < 1:
            self.counters["skipped_lot_budget" if n_risk == 0 and n_cap >= 1 else "skipped_insufficient_capital"] += 1
            return
        qty = n * s.cv
        margin = qty * px / cfg.leverage
        entry_fee = qty * px * cfg.with_gst(cfg.entry_fee_rate)
        self.wallet -= margin + entry_fee
        self.counters["min_lot_floor_used"] += floor_used
        self.trades_opened += 1
        s.pos = _Position(
            trade_id=self.trades_opened,
            direction=direction, contracts=n, entry_price=px, entry_time=int(self.t[i]), entry_index=i,
            signal_time=sig_t, sl=sl, initial_sl=sl, tp=tp, sl_pct=sl_pct, atr_at_signal=atr_v,
            margin=margin, entry_fee=entry_fee, equity_at_entry=equity, available_at_entry=available,
            risk_contracts=n_risk, margin_cap_contracts=n_cap, min_lot_floor=floor_used,
            be_price=bt.breakeven_price(px, direction, product), extreme=px,
            raw_entry_price=raw_px, risk_usd=n * bt.loss_per_contract(px, sl, product),
            trail_dist=cfg.trailing_atr_mult * atr_v if cfg.trailing_atr_mult > 0 else 0.0,
        )

    def _close(self, s: _LegState, i: int, raw_price: float, reason: str, ambiguous: bool = False) -> None:
        cfg, rate, p = s.cfg, self.rate, s.pos
        qty = p.contracts * s.cv
        price = raw_price if (reason == "TP" and cfg.fee_mode == "maker_maker") else cfg.exec_price(raw_price, -p.direction)  # exit sells longs, buys back shorts
        slippage = qty * (abs(p.entry_price - p.raw_entry_price) + abs(raw_price - price))
        exit_rate = cfg.exit_fee_rate(reason)
        gross = p.direction * qty * (price - p.entry_price)
        exit_fee = qty * price * cfg.with_gst(exit_rate)
        self.wallet += p.margin + gross - exit_fee
        s.pos = None
        ti = int(self.t[i])
        s.next_allowed = (ti // s.tf_sec + 1 + cfg.cooldown_bars) * s.tf_sec
        net = gross - p.entry_fee - exit_fee + p.funding
        n_in, n_out = qty * p.entry_price, qty * price
        self.trades.append({
            "trade_id": p.trade_id,
            "symbol": s.leg.product.symbol,
            "side": "LONG" if p.direction == 1 else "SHORT",
            "signal_bar": pd.Timestamp(p.signal_time, unit="s", tz="UTC"),
            "entry_time": pd.Timestamp(p.entry_time, unit="s", tz="UTC"),
            "exit_time": pd.Timestamp(ti, unit="s", tz="UTC"),
            "minutes_held": (ti - p.entry_time) // 60 + 1,
            "contracts": p.contracts,
            "contract_value": s.cv,
            "entry_price": p.entry_price,
            "exit_price": price,
            "raw_entry_price": p.raw_entry_price,
            "raw_exit_price": raw_price,
            "slippage_bps": cfg.slippage_bps,
            "slippage_usd": slippage,
            "risk_usd": p.risk_usd,
            "risk_pct_of_equity": p.risk_usd / p.equity_at_entry * 100.0,
            "sl_pct": p.sl_pct,
            "atr_at_signal": p.atr_at_signal,
            "initial_sl": p.initial_sl,
            "final_sl": p.sl,
            "be_price": p.be_price,
            "tp_price": p.tp,
            "exit_reason": reason,
            "notional_entry_usd": n_in,
            "notional_exit_usd": n_out,
            "margin_usd": n_in / cfg.leverage,
            "equity_at_entry_usd": p.equity_at_entry,
            "available_at_entry_usd": p.available_at_entry,
            "risk_contracts": p.risk_contracts,
            "margin_cap_contracts": p.margin_cap_contracts,
            "min_lot_floor": p.min_lot_floor,
            "gross_pnl_usd": gross,
            "entry_fee_rate": cfg.entry_fee_rate,
            "exit_fee_rate": exit_rate,
            "entry_fee_usd": p.entry_fee,
            "exit_fee_usd": exit_fee,
            "fees_usd": p.entry_fee + exit_fee,
            "gst_usd": (n_in * cfg.entry_fee_rate + n_out * exit_rate) * cfg.gst_rate,
            "funding_usd": p.funding,
            "funding_events": p.funding_events,
            "net_pnl_usd": net,
            "net_pnl_inr": net * rate,
            "equity_after_inr": self._realized_equity() * rate,
            "ambiguous_1m": ambiguous,
        })

    def _settle_funding(self, s: _LegState, i: int) -> None:
        p, ti = s.pos, int(self.t[i])
        j = i - 1
        fr, mk = (s.funding_rate[j], s.mark_close[j]) if j >= 0 else (np.nan, np.nan)
        if j < 0 or self.t[j] != ti - 60 or np.isnan(fr) or np.isnan(mk):
            self.counters["funding_missing_rate"] += 1
            return
        notional = p.contracts * s.cv * mk
        cash = -p.direction * notional * fr
        p.margin += cash
        p.funding += cash
        p.funding_events += 1
        self.fund_rows.append({
            "trade_id": p.trade_id, "symbol": s.leg.product.symbol, "time": pd.Timestamp(ti, unit="s", tz="UTC"),
            "side": "LONG" if p.direction == 1 else "SHORT", "contracts": p.contracts, "contract_value": s.cv,
            "mark_price": mk, "notional_usd": notional, "funding_rate": fr, "funding_rate_pct": fr * 100.0,
            "cashflow_usd": cash, "cashflow_inr": cash * self.rate,
        })

    @staticmethod
    def _traded_fill(s: _LegState, i: int, level: float, gapped: bool) -> float:
        """Mark-triggered fill from the traded 1m range (mark price itself is not tradable)."""
        if gapped:
            return float(s.o[i])
        return float(min(max(level, s.l[i]), s.h[i]))

    def _intrabar(self, s: _LegState, i: int) -> None:
        p, cfg = s.pos, s.cfg
        d = p.direction
        if d == 1:
            sl_hit, tp_hit, gapped = s.tl[i] <= p.sl, s.th[i] >= p.tp, s.to[i] <= p.sl
        else:
            sl_hit, tp_hit, gapped = s.th[i] >= p.sl, s.tl[i] <= p.tp, s.to[i] >= p.sl
        traded_fill = cfg.trigger_source == "mark" and cfg.mark_fill_mode == "traded"
        if sl_hit:
            ambiguous = bool(tp_hit)
            self.counters["ambiguous_1m_bars"] += ambiguous
            if traded_fill:
                fill = self._traded_fill(s, i, p.sl, gapped and i != p.entry_index)
            elif gapped and i != p.entry_index:
                fill = min(s.o[i], p.sl) if d == 1 else max(s.o[i], p.sl)
            else:
                fill = p.sl
            if p.be_armed and p.sl == p.be_price:
                reason = "BE"
            elif p.sl != p.initial_sl:
                reason = "TSL"
            else:
                reason = "SL"
            self._close(s, i, fill, reason, ambiguous)
            return
        if tp_hit:
            if traded_fill:
                tp_gapped = (s.to[i] >= p.tp if d == 1 else s.to[i] <= p.tp) and i != p.entry_index
                self._close(s, i, self._traded_fill(s, i, p.tp, tp_gapped), "TP")
            else:
                self._close(s, i, p.tp, "TP")
            return

        # Stop adjustments from this (already evaluated) candle apply from the next candle on.
        product = s.leg.product
        if cfg.breakeven_at_1r and not p.be_armed:
            r = abs(p.entry_price - p.initial_sl)
            reached = s.th[i] >= p.entry_price + r if d == 1 else s.tl[i] <= p.entry_price - r
            if reached:
                p.be_armed = True
                p.sl = max(p.sl, p.be_price) if d == 1 else min(p.sl, p.be_price)
        if cfg.trailing_sl_pct > 0 or p.trail_dist > 0:
            p.extreme = max(p.extreme, s.th[i]) if d == 1 else min(p.extreme, s.tl[i])
            candidates = []
            if cfg.trailing_sl_pct > 0:
                candidates.append(p.extreme * (1 - d * cfg.trailing_sl_pct / 100.0))
            if p.trail_dist > 0:
                candidates.append(p.extreme - d * p.trail_dist)
            for level in candidates:
                level = product.round_price(level)
                p.sl = max(p.sl, level) if d == 1 else min(p.sl, level)

    # -- main loop ----------------------------------------------------------- #
    def loop(self, entry_idx: np.ndarray) -> None:
        cfg, t, states, rate = self.cfg, self.t, self.states, self.rate
        n = len(t)
        daily_limit = cfg.max_daily_loss_pct / 100.0 if cfg.max_daily_loss_pct else 0.0
        cur_day, day_start_eq, halted_day = None, self.wallet * rate, None
        i = 0
        while i < n:
            if all(s.pos is None for s in states):
                # Nothing open: equity is flat until the next candle where a signal acts.
                k = np.searchsorted(entry_idx, i)
                nxt = int(entry_idx[k]) if k < len(entry_idx) else n
                if nxt > i:
                    self.equity[i:nxt] = self.wallet * rate
                    i = nxt
                    if i >= n:
                        break
            ti = int(t[i])
            day = ti // 86_400
            if day != cur_day:
                cur_day = day
                day_start_eq = self.equity[i - 1] if i > 0 else self.wallet * rate

            # 0) funding
            if ti % dm.FUNDING_INTERVAL_SECONDS == 0:
                for s in states:
                    if s.pos is not None and s.pos.entry_time < ti:
                        self._settle_funding(s, i)

            # 1) signals acting at this open
            for s in states:
                if s.pos is not None and s.cfg.exit_on_opposite_signal and s.exit_map.get(ti) == s.pos.direction:
                    self._close(s, i, s.o[i], "XSIG")
                e = s.entry_map.get(ti)
                if e is None:
                    continue
                direction, sig_t = e
                if s.pos is not None:
                    if s.cfg.exit_on_opposite_signal and direction == -s.pos.direction:
                        self._close(s, i, s.o[i], "OPP")
                    else:
                        self.counters["skipped_in_position"] += 1
                elif halted_day == day:
                    self.counters["skipped_daily_halt"] += 1
                elif ti < s.next_allowed:
                    self.counters["skipped_cooldown"] += 1
                else:
                    self._open(s, i, direction, sig_t)

            # 2) intrabar exits
            for s in states:
                if s.pos is not None:
                    self._intrabar(s, i)

            # 3) mark-to-market + daily-loss breaker
            eq = self._mtm(i) * rate
            if daily_limit and halted_day != day and eq <= day_start_eq * (1 - daily_limit):
                halted_day = day
                self.counters["daily_halts"] += 1
                for s in states:
                    if s.pos is not None:
                        self._close(s, i, s.c[i], "DAILY")
                eq = self._mtm(i) * rate
            self.equity[i] = eq
            i += 1

        for s in states:
            if s.pos is not None:
                self._close(s, n - 1, s.c[n - 1], "END")
        self.equity[n - 1] = self.wallet * rate
        self.trades.sort(key=lambda r: (r["entry_time"], r["trade_id"]))


def _summarise(cfg: BacktestConfig, trades: pd.DataFrame, funding: pd.DataFrame, equity: pd.Series,
               counters: dict[str, int], n_signals: int, n_legs: int = 1) -> dict[str, Any]:
    rate = cfg.usd_inr_rate
    n = len(trades)
    extra: dict[str, Any] = {"slippage_inr": 0.0, "risk_pct_min": float("nan"), "risk_pct_median": float("nan"),
                             "risk_pct_max": float("nan"), "exposure_pct": 0.0}
    if n:
        extra.update(
            slippage_inr=float(trades["slippage_usd"].sum() * rate),
            risk_pct_min=float(trades["risk_pct_of_equity"].min()),
            risk_pct_median=float(trades["risk_pct_of_equity"].median()),
            risk_pct_max=float(trades["risk_pct_of_equity"].max()),
            exposure_pct=float(trades["minutes_held"].sum() / (len(equity) * n_legs) * 100) if len(equity) else 0.0,
        )
    if n:
        net = trades["net_pnl_usd"]
        wins, losses = net[net > 0].sum(), net[net < 0].sum()
        if losses < 0:
            profit_factor = float(wins / -losses)
        else:
            profit_factor = math.inf if wins > 0 else float("nan")
        gross, fees, fund = trades["gross_pnl_usd"].sum(), trades["fees_usd"].sum(), trades["funding_usd"].sum()
        win_rate = float((net > 0).mean() * 100)
        longs = int((trades["side"] == "LONG").sum())
        exits = trades["exit_reason"].value_counts().to_dict()
    else:
        profit_factor, gross, fees, fund, win_rate, longs, exits = float("nan"), 0.0, 0.0, 0.0, 0.0, 0, {}
    net_total = gross - fees + fund
    peak = equity.cummax()
    max_dd = float(((peak - equity) / peak).max() * 100) if len(equity) else 0.0
    return {
        "total_trades": n,
        "long_trades": longs,
        "short_trades": n - longs,
        "win_rate_pct": win_rate,
        "gross_pnl_inr": gross * rate,
        "fees_gst_inr": fees * rate,
        "funding_inr": fund * rate,
        "net_pnl_inr": net_total * rate,
        "net_return_pct": net_total * rate / cfg.initial_capital_inr * 100,
        "profit_factor": profit_factor,
        "max_drawdown_pct": max_dd,
        "final_equity_inr": float(equity.iloc[-1]),
        "signals": n_signals,
        "funding_events": len(funding),
        "exit_reasons": exits,
        **extra,
        **counters,
    }


# --------------------------------------------------------------------------- #
# Audit: recompute every ledger number from first principles
# --------------------------------------------------------------------------- #
def audit_result(r: BacktestResult, tol: float = 1e-9) -> list[str]:
    errors: list[str] = []

    def close(a: float, b: float) -> bool:
        return math.isclose(a, b, rel_tol=tol, abs_tol=tol)

    fund_by_trade = r.funding.groupby("trade_id")["cashflow_usd"].sum() if len(r.funding) else pd.Series(dtype=float)
    last_exit: dict[str, pd.Timestamp] = {}
    rows = () if r.trades.empty else r.trades.sort_values("entry_time").itertuples()  # zero trades: identity only
    for tr in rows:
        p = r.products[tr.symbol]
        cfg = r.leg_configs.get(tr.symbol, r.config)
        bt = Backtester(cfg)
        d = 1 if tr.side == "LONG" else -1
        tf_sec = dm.TIMEFRAME_MINUTES[r.timeframes.get(tr.symbol, r.timeframe)] * 60
        pcts = cfg.stop_pcts(tr.entry_price, tr.atr_at_signal)
        sl, tp = bt.levels(tr.entry_price, d, *pcts, p) if pcts else (np.nan, np.nan)
        n, *_ = bt.size_position(tr.equity_at_entry_usd, tr.entry_price, tr.initial_sl, p, tr.available_at_entry_usd)
        qty = tr.contracts * p.contract_value
        gross = d * qty * (tr.exit_price - tr.entry_price)
        fe = qty * tr.entry_price * cfg.with_gst(cfg.entry_fee_rate)
        xe = qty * tr.exit_price * cfg.with_gst(cfg.exit_fee_rate(tr.exit_reason))
        fund = float(fund_by_trade.get(tr.trade_id, 0.0))
        traded_fill = cfg.trigger_source == "mark" and cfg.mark_fill_mode == "traded"
        slip = qty * (abs(tr.entry_price - tr.raw_entry_price) + abs(tr.raw_exit_price - tr.exit_price))
        checks = {
            "entry_slippage": close(tr.entry_price, cfg.exec_price(tr.raw_entry_price, d)),
            "exit_slippage": close(tr.exit_price, cfg.exec_price(tr.raw_exit_price, -d)),
            "slippage_usd": close(slip, tr.slippage_usd),
            "risk_usd": close(tr.risk_usd, tr.contracts * bt.loss_per_contract(tr.entry_price, tr.initial_sl, p)),
            "risk_budget": cfg.allow_min_one_lot or tr.risk_usd <= tr.equity_at_entry_usd * cfg.risk_per_trade_pct / 100 * (1 + 1e-12),
            "sl_level": sl == tr.initial_sl,
            "tp_level": tp == tr.tp_price,
            "contracts": n == tr.contracts,
            "tick_aligned": close(tr.initial_sl / p.tick_size, round(tr.initial_sl / p.tick_size)),
            "tp_fill": tr.exit_reason != "TP" or traded_fill or tr.raw_exit_price == tr.tp_price,
            "be_price": tr.be_price == bt.breakeven_price(tr.entry_price, d, p),
            "be_stop": tr.exit_reason != "BE" or tr.final_sl == tr.be_price,
            "gross": close(gross, tr.gross_pnl_usd),
            "entry_fee": close(fe, tr.entry_fee_usd),
            "exit_fee": close(xe, tr.exit_fee_usd),
            "funding": close(fund, tr.funding_usd),
            "net": close(gross - fe - xe + fund, tr.net_pnl_usd),
            "signal_timing": tr.entry_time == tr.signal_bar + pd.Timedelta(seconds=tf_sec),
            "no_overlap_and_cooldown": tr.symbol not in last_exit or tr.entry_time >= (
                pd.Timestamp((int(last_exit[tr.symbol].timestamp()) // tf_sec + 1 + cfg.cooldown_bars) * tf_sec, unit="s", tz="UTC")
            ),
        }
        errors += [f"{tr.symbol} trade {tr.trade_id}: {k}" for k, ok in checks.items() if not ok]
        last_exit[tr.symbol] = tr.exit_time

    if len(r.funding):
        f = r.funding
        exp = -np.where(f["side"] == "LONG", 1, -1) * f["contracts"] * f["contract_value"] * f["mark_price"] * f["funding_rate"]
        if not np.allclose(exp, f["cashflow_usd"], rtol=tol, atol=tol):
            errors.append("funding cashflow formula")
        if not ((f["time"].dt.hour % 8 == 0) & (f["time"].dt.minute == 0)).all():
            errors.append("funding at non-8h timestamp")
        held = r.trades.set_index("trade_id")
        bad = [row.trade_id for row in f.itertuples()
               if not (held.at[row.trade_id, "entry_time"] < row.time <= held.at[row.trade_id, "exit_time"])]
        if bad:
            errors.append(f"funding outside holding period: trades {bad[:5]}")

    final = r.config.initial_capital_inr + (r.trades["net_pnl_inr"].sum() if len(r.trades) else 0.0)
    if not math.isclose(final, r.summary["final_equity_inr"], rel_tol=1e-9, abs_tol=1e-6):
        errors.append(f"equity identity: {final} vs {r.summary['final_equity_inr']}")
    return errors


# --------------------------------------------------------------------------- #
# Convenience
# --------------------------------------------------------------------------- #
def make_leg(symbol: str, strategy: str, timeframe: str, df_1m: pd.DataFrame | None = None,
             days: int | None = None, config: BacktestConfig | None = None) -> Leg:
    if df_1m is None:
        df_1m = dm.load_unified_1m_dataset(symbol, days=days)
    bars = dm.resample_candles(df_1m, timeframe)
    out = strategies.generate_signals(strategy, bars)
    exits = out["exit_signal"] if "exit_signal" in out.columns else None
    return Leg(ProductSpec.from_metadata(symbol), df_1m, out["signal"], timeframe,
               strategies.atr(bars, 14), strategy, exits, config)


def run_backtest(symbol: str, strategy: str, timeframe: str, config: BacktestConfig | None = None,
                 days: int | None = None, df_1m: pd.DataFrame | None = None) -> BacktestResult:
    return Backtester(config).run_portfolio([make_leg(symbol, strategy, timeframe, df_1m, days)])
