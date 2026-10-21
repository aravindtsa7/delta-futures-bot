# Delta Futures Bot — Operational Runbook

Everything runs from `C:\TSA\delta-futures-bot` with the project venv. Default safety posture:
`MODE="research"`, `LIVE_TRADING_ENABLED=False`, `EXECUTION_MODE="paper"`. Live trading is refused by the
REST adapter, the WebSocket runner and the dashboard unless **both** `DELTA_MODE=live` and
`DELTA_LIVE_TRADING_ENABLED=true` are set.

## 1. Dashboard (backtester + bot panel)

```powershell
.venv\Scripts\python.exe -m uvicorn app:app --reload
```

Open http://127.0.0.1:8000.

- **Backtest Lab** (default view): presets, backtests, walk-forward and leaderboard tables (Steps 3–4B).
- **Bot Operations** (top-bar switch, or http://127.0.0.1:8000/#bot): live telemetry (refreshes every 5 s), the signal table, open positions, the order-intent journal, trade history, and the state/incident log. Controls:

| Button | What it does |
|---|---|
| Run 1 Cycle (`--once`) | One REST reconciliation and frozen-signal evaluation (paper). Refused while a loop is running. |
| Start 24/7 Loop | Spawns `live_bot.py --mode paper\|testnet --loop` (log in `data/logs/`). Refuses `live` and refuses if `data/live_bot.lock` is held. |
| Pause New Entries / Resume | Blocks new entries only. Trailing stops, exits and breakers keep running. |
| Stop Bot Process | Clean shutdown via the control channel. The lock is released; exchange SL/TP orders stay in place. |
| Emergency Flatten All Positions | Type `FLATTEN` to confirm. Closes every bot position (exchange: reduce-only per symbol, escalating to close-all), pauses entries, and shows the report including any `RESIDUAL_EXPOSURE`. |
| Backup State DB | Online backup of `data/bot_state.db` to `data/backups/`. Integrity-checked. |
| Test Telegram Alert | Reports `sent`, `failed: …` or `dry_run_no_credentials`. |

While a loop runs, it is the **only writer** of `data/bot_state.db`. The dashboard sends Pause, Resume, Flatten and Stop as command files in `data/bot_control/`, and the bot executes them within about a second.

## 2. 24/7 paper bot from the CLI

```powershell
.venv\Scripts\python.exe live_bot.py --mode paper --once        # one cycle, prints readiness + signals
.venv\Scripts\python.exe live_bot.py --mode paper --loop        # WebSocket + REST fallback until Ctrl+C
.venv\Scripts\python.exe live_bot.py --mode paper --duration 3600
```

- **Starting up:** the bot moves through `BOOTING → CONNECTING → AUTHENTICATING → SUBSCRIBING → SNAPSHOTTING → RECONCILING → OBSERVED_READY`, and enters new trades only in `OBSERVED_READY` with no halt and no pause.
- **Each closed 1m bar:** trailing stop, exits and funding.
- **Each 1h close:** the frozen signals (ETHUSD Variant C, BTCUSD Variant D). An entry is acted on only within 5 minutes of the close.
- **One instance only:** a second instance is refused (`BotAlreadyRunningError`).
- **Keeping it running:** to survive reboots, run the `--loop` command from Windows Task Scheduler ("At startup", "Restart on failure"), or start it from the dashboard. Use one or the other.

## 3. `.env` configuration

Copy `.env.example` to `.env` (same folder). `.env` holds secrets: never commit or share it.

**Delta India testnet (demo account)**
1. Log in to Delta's demo account and create an API key with *Read Data* + *Trading* permissions. Copy the secret immediately; it is shown only once.
2. Add to `.env`:
   ```
   DELTA_TESTNET_API_KEY=...
   DELTA_TESTNET_API_SECRET=...
   ```
   Demo keys only work against `https://cdn-ind.testnet.deltaex.org`. Production keys only work against `https://api.india.delta.exchange`.
3. Verify with `.venv\Scripts\python.exe step5_verify.py`; section 4f runs read-only testnet calls.
4. Run with `live_bot.py --mode testnet --loop`, or choose *Testnet* in the dashboard. Use a fresh journal: a DB is bound to one mode for life, so move or rename `data/bot_state.db` first.
5. The first real testnet session must confirm two things not yet verified against the exchange:
   - the private-socket `key-auth` handshake;
   - that bracket SL/TP child orders appear in `GET /v2/orders` with `stop_order_type` set.

**Telegram alerts (@BotFather)**
1. In Telegram, message **@BotFather** → `/newbot` → copy the token.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<token>/getUpdates` and copy `"chat":{"id":…}`.
3. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_CHAT_ID=...
   ```
   The `DELTA_`-prefixed names also work.
4. Restart the dashboard and bot, then press **Test Telegram Alert**.
5. Alerts cover:
   - **Lifecycle:** start, stop, pause, resume, reconciling, halted.
   - **Trades:** entry and exit (risk ₹ and net ₹ shown).
   - **Safety incidents:** `OUTCOME_UNKNOWN`, `RESIDUAL_EXPOSURE`, the 5% daily breaker, the 4-loss streak, and sequence gaps/resets.

   Sending never blocks trading, and keys, secrets and signatures are redacted.

## 4. Incidents

| Situation | What you see | What to do |
|---|---|---|
| **`OUTCOME_UNKNOWN`** | Red row in *Order Intents*; incident alert; new entries on that symbol blocked | Do **not** place the order by hand. The bot reconciles on its own (by client order id → active orders → history → fills). If the order is never seen, it is marked `REJECTED` after 60 s. If it stays unknown, check the order on the exchange UI by its `client_order_id`, then restart the bot so it reconciles again. |
| **`RESIDUAL_EXPOSURE`** | Flatten or close-all report lists residual positions (often `market_disrupted_cancel_only_mode`); bot halts | The market is in cancel-only or auction mode, so positions cannot be closed. Exchange SL/TP orders still protect them. Watch the exchange status page, then press **Emergency Flatten** again once trading resumes. Never assume flat until the report says `FLAT`. |
| **`HALTED`** | Red badge; reason = 5% daily loss or 4 consecutive losses | Positions were already flattened. The halt lifts by itself at 00:00 UTC. Review the trades before resuming. Do not delete the halt by editing the DB. |
| **`DEGRADED`** | State badge; *State Transitions* lists the issues (data gap, journal/exchange mismatch, untracked position) | Entries are blocked; protective actions still run. Fix the cause (network, an untracked manual position), then stop and start the bot to reconcile again. |
| **WebSocket drop / sequence gap** | `RECONCILING`, then a new connection generation | Nothing to do: the bot re-snapshots and reconciles over REST without resending orders. Repeated drops (3 in a row) → `DEGRADED`. |

## 5. Backups and restores

```powershell
.venv\Scripts\python.exe backup_state.py backup                      # → data/backups/bot_state_<UTC>.db + .json manifest
.venv\Scripts\python.exe backup_state.py verify data\backups\bot_state_<UTC>.db
.venv\Scripts\python.exe backup_state.py restore data\backups\bot_state_<UTC>.db --target data\bot_state.db
```

- **Backups are safe while the bot runs.** They use SQLite's online backup API, which includes pages still in the `-wal` file. Never copy `bot_state.db` on its own.
- **Restore requires the bot to be stopped.** It is refused while `data/live_bot.lock` or the journal's writer lock is held.
- **How restore works:** the current DB is saved as `bot_state.pre_restore_<UTC>.db`, stale `-wal`/`-shm` files are removed, and the new file is swapped in atomically and integrity-checked.
- **Run a backup daily** (dashboard button or Task Scheduler), and before every upgrade or mode change.

## 5b. Verification suites

`step4_verify.py` … `step8_verify.py` re-check the whole system. `step8_verify.py` runs all of them and takes about 8 minutes.

- **Safe to run while a 24/7 bot is running.** Since Step 8 every suite uses its own lock, journal, control directory and coin list, so the real `data/live_bot.lock`, `data/bot_state.db` and `data/app_symbols.json` are never touched.
- **Shared candle history.** Suites do sync candles into the shared `data/raw_1m/` history.
- **Test files.** Written to `data/test_*.db`, `data/step7_test/` and `data/step8_test/`; they can be deleted.

## 5c. Manual Trade tab and extra coins

Open http://127.0.0.1:8000/#manual. It trades in the **same wallet and journal** as the auto-bot (`data/bot_state.db`).

- **Only one writer.** While the 24/7 loop runs, the order ticket is disabled and a banner offers **Stop Bot & Trade Manually**. Manual orders, protection edits and closes return HTTP 409 until the loop is stopped.
- **Protection while the bot is stopped (paper).** The dashboard process runs a watcher every 2 s against live tickers. It handles SL, TP, trailing stops, liquidation, 8h funding and resting limit fills, so **keep the dashboard running** while you hold paper positions with the bot stopped.
  - The watcher only sees the price every 2 s, so a spike between polls can be missed.
  - When the auto-bot starts again, it takes over every open position, including manual ones, using closed 1m bars.
- **Leverage.** From 1x up to the product maximum (100 / `initial_margin`: BTC/ETH 200x, most alts 100x, SUI 20x).
  - Liquidation is estimated as `entry × (1 ∓ 1/lev ± maintenance%)`.
  - In paper mode a liquidation exits at the bankruptcy price, which **loses the whole position margin**.
  - At 100x the liquidation price is only about 0.5–0.75% away.
- **Fills (paper).**

  | Order | Price | Fee |
  |---|---|---|
  | Market buy / sell | best ask / best bid, plus 2 bps slippage | taker + GST |
  | Marketable limit | like a market order | taker + GST |
  | Resting limit | the limit price, once the last price touches it | maker + GST |
- **Testnet manual trading** supports market opens (leverage set on the exchange, optional bracket), full closes and SL/TP edits. Adding to a position, partial closes and limit orders are paper-only for now.
- **Adding a coin.** "+ Add" (in the Manual Trade ticket or the Backtest Lab) puts the coin in `data/app_symbols.json` and downloads 90 days of history in the background.
- **Added coins join the auto-bot** with the **C_costgate** profile. That profile is unvalidated: it was rejected in Step 4B and never tested on other coins. Override or disable it per coin in `data/bot_profiles.json`:
  ```json
  {"SOLUSD": {"enabled": false}, "XRPUSD": {"variant": "D_both"}}
  ```
  Restart the bot after editing. Each extra coin is another 1%-risk position, so total exposure grows with the coin list.
- **Trading a coin without adding it.** Choosing **Load** for a catalog coin only fetches its metadata. The auto-bot then manages that position's stops but never opens new trades on it.

## 5d. LIVE manual trading (real money, Delta Exchange India)

**`.env` settings.** Un-prefixed or `DELTA_`-prefixed names both work:

```
MODE=live
LIVE_TRADING_ENABLED=true
DELTA_API_KEY=...
DELTA_API_SECRET=...
# AUTO_BOT_LIVE_ENABLED=true   <- only if the 24/7 auto-bot may trade real money (off by default)
```

**Gates.**
- **Manual live orders** need `MODE=live`, `LIVE_TRADING_ENABLED=true`, the key and secret, a switch to **LIVE** in the Manual Trade tab (type `LIVE`), and typing `LIVE` again on every order or close.
- **The auto-bot** refuses real money unless `AUTO_BOT_LIVE_ENABLED=true` is also set. Running `live_bot.py` without `--mode` uses `EXECUTION_MODE`; with `EXECUTION_MODE=live` but no auto-bot flag, it is refused.

**Separate journals per mode:**

| Mode | Journal |
|---|---|
| paper | `data/bot_state.db` |
| testnet | `data/bot_state_testnet.db` |
| live | `data/bot_state_live.db` |

The selected Manual Trade mode is remembered in `data/manual_mode.json`.

**What happens on Delta.**

| Action | Exchange calls |
|---|---|
| Every entry or scale-in | isolated margin is set once per session (`PUT /v2/users/margin_mode`), then the ticket's leverage (`POST /v2/products/{id}/orders/leverage`) |
| New position | market or limit entry with SL/TP attached as a bracket; trigger is mark or last price |
| Scale-in | market add at the same leverage; size and average entry re-read from Delta; SL/TP legs resized |
| Partial close | reduce-only market order; booked from its own fills and commission; remaining legs resized |
| Full close | reduce-only market order; leftover SL/TP legs cancelled |
| SL/TP edit | the live leg is modified (`PUT /v2/orders`); a missing leg is created (`POST /v2/orders/bracket`) |
| Cancel a resting limit | `DELETE /v2/orders` |

- **Trailing stops are paper-only for manual exchange positions.** Move the SL yourself.
- **Wallet bar and positions show Delta's real figures**, using Delta's `*_inr` fields or USD × 85, and Delta's liquidation price.
- **Positions opened elsewhere** appear as "exchange (not journalled)" and can be closed (reduce-only).
- **SL/TP fills made by Delta** are booked on the next sync, which happens on each 2.5 s poll and with the **Verify API & Sync Wallet** button.

**Checks.**
- **Verify API & Sync Wallet** makes a read-only call: the key is masked, and you get the wallet in ₹ and $ plus open positions. A read-only client cannot send orders even by mistake.
- **A ₹0 wallet** gives "please add funds on Delta Exchange India or switch to PAPER mode".
- **Common errors:**

  | Error | Fix |
  |---|---|
  | `ip_not_whitelisted_for_api_key` | Add this PC's public IP to the key on Delta |
  | `invalid_api_key` | Wrong key, or a testnet key used on live |
  | `SignatureExpired` | Sync the Windows clock |

**Untested against the real exchange** (the account had ₹0 at build time): isolated-margin switching, bracket legs on limit entries, and leg resizing. Make your first live trade a single minimum-size contract, and confirm the SL/TP legs on Delta's own Orders page.

## 6. Why paper-trade Variant C / D forward first

The Step 4B screen asked for PF ≥ 1.20, max DD ≤ 15%, ≥ 50 trades and positive in ≥ 2/3 of windows, at 5 bps slippage over 365 days. **Every variant was REJECTED.** The two running here are the closest misses:

| Symbol | Variant | Net (365d, ₹50k, 1% risk) | PF | Max DD | Trades/yr | Bootstrap 5th pct |
|---|---|---:|---:|---:|---:|---|
| ETHUSD | C — 1h Bollinger + cost gate | +₹4,687 | 1.12 | 8.6% | 185 (~3.5/week) | negative |
| BTCUSD | D — + 4h trend filter | +₹2,107 | 1.15 | 6.9% | 78 (~1.5/week) | negative |

- **Thin edge:** these are near-break-even strategies. Slippage alone (5 bps per fill) costs about as much as fees and GST.
- **Selection bias:** the ETH configuration was picked from the same history it was evaluated on.
- **Plan:** paper-trade forward for **at least 8–12 weeks**, which is only about 40 ETH and 15 BTC trades.
- **Compare against the backtest:** per-trade net, fill slippage (paper fills vs. the touch), exit-reason mix and drawdown.
- **Before real capital:** run testnet for a further 2–4 weeks to prove order handling, then decide. If forward PF stays below about 1.2 after costs, the honest conclusion is "no edge". Do not re-tune on the forward data.
