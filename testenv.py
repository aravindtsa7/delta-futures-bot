"""Imported FIRST by every stepN_verify.py (before config.py is loaded).

Verification suites must never run with live trading armed or with real live credentials, whatever .env says.
Process environment variables take precedence over .env in pydantic-settings, so these overrides win for the test
process and for every subprocess it spawns (bot loops, CLI smoke tests, nested suites).
Testnet keys and Telegram settings are left alone (suites already handle their presence/absence).
"""

import os

SAFE_ENV = {
    "DELTA_MODE": "research", "MODE": "research",
    "DELTA_LIVE_TRADING_ENABLED": "false", "LIVE_TRADING_ENABLED": "false",
    "DELTA_EXECUTION_MODE": "paper", "EXECUTION_MODE": "paper",
    "DELTA_AUTO_BOT_LIVE_ENABLED": "false", "AUTO_BOT_LIVE_ENABLED": "false",
    "DELTA_LIVE_API_KEY": "", "DELTA_LIVE_API_SECRET": "",
    "DELTA_API_KEY": "", "DELTA_API_SECRET": "",
}
os.environ.update(SAFE_ENV)
