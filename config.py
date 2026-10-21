"""Central configuration for the Delta Exchange India futures bot.

Every value below can be overridden with an environment variable (prefix
``DELTA_``) or a ``.env`` file in the project root, e.g.::

    DELTA_ENVIRONMENT=india_testnet
    DELTA_SYMBOLS='["BTCUSD","ETHUSD","SOLUSD"]'
    DELTA_RISK_PER_TRADE_PCT=1.5

Import the singleton:  ``from config import settings``
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- #
# Environments
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class DeltaEnvironment:
    name: str
    rest_url: str
    public_ws_url: str
    private_ws_url: str


ENVIRONMENTS: dict[str, DeltaEnvironment] = {
    "india_prod": DeltaEnvironment(
        name="india_prod",
        rest_url="https://api.india.delta.exchange",
        public_ws_url="wss://public-socket.india.delta.exchange",
        private_ws_url="wss://socket.india.delta.exchange",
    ),
    "india_testnet": DeltaEnvironment(
        name="india_testnet",
        rest_url="https://cdn-ind.testnet.deltaex.org",
        public_ws_url="wss://socket-ind-pub.testnet.deltaex.org",
        private_ws_url="wss://socket-ind.testnet.deltaex.org",
    ),
}

EnvironmentName = Literal["india_prod", "india_testnet"]
Mode = Literal["research", "paper", "live"]


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DELTA_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_assignment=True,
    )

    # --- Runtime --------------------------------------------------------- #
    ENVIRONMENT: EnvironmentName = "india_prod"
    # The live gates and live keys also accept un-prefixed names in .env (MODE=live, LIVE_TRADING_ENABLED=true,
    # DELTA_API_KEY=..., DELTA_API_SECRET=...). Test suites force them off via testenv.py.
    MODE: Mode = Field("research", validation_alias=AliasChoices("DELTA_MODE", "MODE"))
    # Second, explicit switch: MODE="live" alone is not enough to place orders.
    LIVE_TRADING_ENABLED: bool = Field(False, validation_alias=AliasChoices("DELTA_LIVE_TRADING_ENABLED", "LIVE_TRADING_ENABLED"))
    # Third switch, only for the 24/7 AUTO-BOT on real money (its strategies failed the Step 4B screen).
    # Manual live trading needs only MODE=live + LIVE_TRADING_ENABLED=true + live keys.
    AUTO_BOT_LIVE_ENABLED: bool = Field(False, validation_alias=AliasChoices("DELTA_AUTO_BOT_LIVE_ENABLED", "AUTO_BOT_LIVE_ENABLED"))
    USER_AGENT: str = "delta-python-bot/1.0"
    HTTP_TIMEOUT_SECONDS: float = 15.0

    # --- Execution (Step 5) ---------------------------------------------- #
    # paper: public prices only, simulated fills in SQLite (no key needed).
    # testnet: Delta India demo account. live: also needs MODE="live" AND LIVE_TRADING_ENABLED=True.
    EXECUTION_MODE: Literal["paper", "testnet", "live"] = Field(
        "paper", validation_alias=AliasChoices("DELTA_EXECUTION_MODE", "EXECUTION_MODE"))
    TESTNET_API_KEY: str = ""       # .env: DELTA_TESTNET_API_KEY=...
    TESTNET_API_SECRET: str = ""    # .env: DELTA_TESTNET_API_SECRET=...
    LIVE_API_KEY: str = Field("", validation_alias=AliasChoices("DELTA_LIVE_API_KEY", "DELTA_API_KEY"))
    LIVE_API_SECRET: str = Field("", validation_alias=AliasChoices("DELTA_LIVE_API_SECRET", "DELTA_API_SECRET"))
    BOT_DB_FILE: str = "bot_state.db"
    # --- Alerts (Step 7) --------------------------------------------------- #
    # .env: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (or the DELTA_-prefixed names)
    TELEGRAM_BOT_TOKEN: str = Field("", validation_alias=AliasChoices("DELTA_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"))
    TELEGRAM_CHAT_ID: str = Field("", validation_alias=AliasChoices("DELTA_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"))
    SIZING_SLIPPAGE_BPS: float = Field(2.0, ge=0, le=100)  # per-fill buffer in the lot sizer / paper fills

    # --- Symbols (plug-and-play: add a perpetual symbol string here) ----- #
    SYMBOLS: list[str] = Field(default_factory=lambda: ["BTCUSD", "ETHUSD"])

    # --- Capital & risk -------------------------------------------------- #
    INITIAL_CAPITAL_INR: float = Field(50_000.0, gt=0)  # Step 4B: was 10,000 (Step 3C presets pin 10,000)
    FIXED_USD_INR_RATE: float = Field(85.0, gt=0)  # Delta India: 1 USD = 85 INR
    DEFAULT_LEVERAGE: int = Field(10, ge=1, le=200)
    MARGIN_MODE: Literal["isolated", "cross"] = "isolated"
    RISK_PER_TRADE_PCT: float = Field(2.0, gt=0, le=100)
    STOP_LOSS_PCT: float = Field(1.0, gt=0, le=100)
    TAKE_PROFIT_PCT: float = Field(2.0, gt=0)
    MAX_DAILY_LOSS_PCT: float = Field(5.0, gt=0, le=100)

    # --- Fees & tax (Delta India futures) -------------------------------- #
    MAKER_FEE: float = Field(0.0002, ge=0)  # 0.02%
    TAKER_FEE: float = Field(0.0005, ge=0)  # 0.05%
    GST_ON_FEES: float = Field(0.18, ge=0)  # 18% GST applied on the fee amount

    # --- Data ------------------------------------------------------------ #
    BASE_TIMEFRAME: str = "1m"
    SUPPORTED_RESAMPLE_TIMEFRAMES: list[str] = Field(
        default_factory=lambda: ["3m", "5m", "15m", "1h", "2h", "4h"]
    )
    DEFAULT_LOOKBACK_DAYS: int = Field(90, ge=1)
    DATA_DIR: str = "data"

    # --- Validators ------------------------------------------------------ #
    @field_validator("SYMBOLS")
    @classmethod
    def _normalise_symbols(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip().upper() for s in v if s and s.strip()]
        if not cleaned:
            raise ValueError("SYMBOLS must contain at least one symbol")
        return list(dict.fromkeys(cleaned))  # de-duplicate, keep order

    @model_validator(mode="after")
    def _check_live_guard(self) -> "Settings":
        if self.LIVE_TRADING_ENABLED and self.MODE != "live":
            raise ValueError("LIVE_TRADING_ENABLED=True requires MODE='live'")
        return self

    # --- Derived values -------------------------------------------------- #
    @property
    def env(self) -> DeltaEnvironment:
        return ENVIRONMENTS[self.ENVIRONMENT]

    @property
    def is_live(self) -> bool:
        return self.MODE == "live" and self.LIVE_TRADING_ENABLED

    @property
    def initial_capital_usd(self) -> float:
        return self.INITIAL_CAPITAL_INR / self.FIXED_USD_INR_RATE

    @property
    def maker_fee_with_gst(self) -> float:
        return self.MAKER_FEE * (1 + self.GST_ON_FEES)

    @property
    def taker_fee_with_gst(self) -> float:
        return self.TAKER_FEE * (1 + self.GST_ON_FEES)

    @property
    def data_path(self) -> Path:
        p = Path(self.DATA_DIR)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def http_headers(self) -> dict[str, str]:
        return {"User-Agent": self.USER_AGENT, "Accept": "application/json"}


settings = Settings()


def get_mode_db_path(exec_mode: str) -> Path:
    """Runtime journal per execution mode: paper -> data/bot_state.db (unchanged), testnet -> data/bot_state_testnet.db,
    live -> data/bot_state_live.db. Each file stays bound to its own mode for life. Explicit --db-path wins."""
    if exec_mode not in ("paper", "testnet", "live"):
        raise ValueError(f"unknown execution mode {exec_mode!r}")
    base = settings.data_path / settings.BOT_DB_FILE
    return base if exec_mode == "paper" else base.with_name(f"{base.stem}_{exec_mode}{base.suffix}")


def mask_secret(value: str | None) -> str | None:
    """'abcd...xyz' style display; never the full value."""
    if not value:
        return None
    return f"{value[:4]}...{value[-3:]}" if len(value) >= 10 else "***"
