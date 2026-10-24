# Delta Futures Bot — Operational Runbook

Everything runs from `C:\TSA\delta-futures-bot` with the project venv. Default safety posture:
`MODE="research"`, `LIVE_TRADING_ENABLED=False`, `EXECUTION_MODE="paper"`. Live trading is refused by the
REST adapter, the WebSocket runner and the dashboard unless **both** `MODE=live` and
`LIVE_TRADING_ENABLED=true` are set (un-prefixed or `DELTA_`-prefixed names). The 24/7 auto-bot needs a third
switch, `AUTO_BOT_LIVE_ENABLED=true`, before it trades real money (section 5d).

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
| Start 24/7 Loop | Spawns `live_bot.py --mode paper\|testnet\|live --loop` (log in `data/logs/`). `live` is refused unless `AUTO_BOT_LIVE_ENABLED=true`; any mode is refused if `data/live_bot.lock` is held. |
| Pause New Entries / Resume | Blocks new entries only. Trailing stops, exits and breakers keep running. |
| Stop Bot Process | Clean shutdown via the control channel. The lock is released; exchange SL/TP orders stay in place. |
| Emergency Flatten All Positions | Type `FLATTEN` to confirm. In this order: pauses entries, **cancels every order that could open exposure** (resting / partially filled entry orders, paper and exchange), books any fill that landed before the cancel, closes every position (journalled and unjournalled; reduce-only per symbol, escalating to close-all), then verifies. The report says `FLAT` only when open positions **and** open entry orders are both zero; otherwise `FLATTEN_INCOMPLETE` with what is left. |
| Backup State DB | Online backup of the shown mode's journal (`bot_state.db`, `_testnet` or `_live`) to `data/backups/`. Integrity-checked. |
| Test Telegram Alert | Reports `sent`, `failed: …` or `dry_run_no_credentials`. |

While a loop runs, it is the **only writer** of its journal. The dashboard sends Pause, Resume, Flatten and Stop as command files in `data/bot_control/`, and the bot executes them within about a second (in a worker thread, so the WebSocket heartbeat is never starved).

## 2. 24/7 paper bot from the CLI

```powershell
.venv\Scripts\python.exe live_bot.py --mode paper --once        # one cycle, prints readiness + signals
.venv\Scripts\python.exe live_bot.py --mode paper --loop        # WebSocket + REST fallback until Ctrl+C
.venv\Scripts\python.exe live_bot.py --mode paper --duration 3600
```

- **Starting up:** the bot moves through `BOOTING → CONNECTING → AUTHENTICATING → SUBSCRIBING → SNAPSHOTTING → RECONCILING → OBSERVED_READY`, and enters new trades only in `OBSERVED_READY` with no halt and no pause.
- **Subscriptions are checked per channel and symbol.** If any required public subscription is rejected or never acknowledged, the socket phase is `DEGRADED_REST_FALLBACK` (never `READY`) and the bot runs on its REST candle/quote fallback. In testnet/live, missing private channels (`orders`, `positions`, `v2/user_trades`) keep the bot `DEGRADED`: no new entries.
- **Every reconciliation (testnet/live)** also: resolves unknown order outcomes, books partial / resting fills, applies the account policy (isolated margin + 10x, confirmed by Delta's answer; a product holding a position at another leverage is left alone and blocked for bot entries until flat), and confirms each position's SL/TP legs (same product, opposite side, reduce-only, size = position, stop on the right side of the price). Unconfirmed protection → `DEGRADED` with `missing_confirmed_protection:<symbol>`; a missing leg is re-attached at most every 5 minutes, in between it is only read back.
- **Daily-loss baseline:** the 00:00 UTC equity (including open positions marked to market) is saved in the journal (`system_meta`), so a restart later the same day keeps the same 5% breaker baseline.
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
4. Run with `live_bot.py --mode testnet --loop`, or choose *Testnet* in the dashboard. Each mode has its own journal (`data/bot_state_testnet.db` for testnet), so nothing needs moving.
5. The first real testnet session must confirm what the mocked suites cannot prove about the venue:
   - the private-socket `key-auth` handshake, and whether subscription acks list their symbols;
   - that bracket SL/TP child orders appear in `GET /v2/orders` with `stop_order_type` set, `reduce_only: true`, the opposite side and a size equal to the position (Step 9 only accepts such legs as protection);
   - what Delta does with bracket children when a limit entry fills partially;
   - the response of `PUT /v2/users/margin_mode` when isolated is already active, and whether `GET /v2/users/trading_preferences` reports `margin_mode` (the readback Step 9 requires before treating that answer as benign).

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
   - **Trades:** entry and exit (risk ₹ and net ₹ shown). Every message is tagged with the mode it happened in: `[PAPER]`, `[TESTNET]` or `[LIVE]` (manual trades included).
   - **Safety incidents:** `OUTCOME_UNKNOWN`, `unresolved_position_delta`, `FLATTEN_INCOMPLETE`, unprotected positions, `ws_rest_fallback`, the 5% daily breaker, the 4-loss streak, and sequence gaps/resets.

   Sending never blocks trading, and keys, secrets and signatures are redacted.

## 4. Incidents

| Situation | What you see | What to do |
|---|---|---|
| **`OUTCOME_UNKNOWN`** | Red row in *Order Intents*; incident alert; new entries (bot **and** manual) on that symbol blocked | Do **not** place the order by hand. The bot, and Manual Trade's **Sync**, reconcile on their own: client order id → active orders → history → fills → the real position. The intent becomes `REJECTED` only when, after 60 s, no order, no matching fill **and** no position change is found. A fill found without its order is booked from the fill. |
| **`unresolved_position_delta`** | CRITICAL alert; intent stays `OUTCOME_UNKNOWN`; symbol stays blocked | Delta's position differs from the journal although no order or fill explains it. Check the exchange UI (Orders, Fills, Positions) for that symbol, then press **Sync** / restart the bot. Never clear it by editing the DB. |
| **`FLATTEN_INCOMPLETE`** (was `RESIDUAL_EXPOSURE`) | Flatten report lists remaining positions and/or open entry orders (often `market_disrupted_cancel_only_mode`, or a cancel Delta refused); bot halts | The market may be in cancel-only or auction mode. Exchange SL/TP orders still protect open positions. Cancel any listed entry order on Delta if the bot could not, watch the exchange status page, then press **Emergency Flatten** again. Never assume flat until the report says `FLAT`. |
| **`HALTED`** | Red badge; reason = 5% daily loss or 4 consecutive losses | Resting entry orders were cancelled and positions flattened. The halt lifts by itself at 00:00 UTC. Review the trades before resuming. Do not delete the halt by editing the DB. |
| **`DEGRADED`** | State badge; *State Transitions* lists the issues: data gap, journal/exchange mismatch, untracked position, `missing_confirmed_protection:<symbol>`, `private_stream_unavailable`, `account_policy_failed` | Entries are blocked; protective actions still run. For missing protection, check the SL/TP legs on Delta's Orders page (the bot re-attaches at most every 5 min). Fix the cause, then let the next reconciliation (or a restart) clear it. |
| **`DEGRADED_REST_FALLBACK`** (socket phase) | `ws_rest_fallback` event; WebSocket phase is not `READY` | A required subscription was rejected or never acknowledged. Public channels: the REST poller delivers candles (slower). Private channels in testnet/live: the bot is also `DEGRADED`, so no entries. Check the event detail for the channel and error. |
| **Bot entry blocked by account policy** | `account_policy_blocked` / `entry_blocked_policy` events | Isolated margin or 10x leverage could not be confirmed, or the symbol holds a position at another leverage (left as is). Close or adjust that position on Delta; the next reconciliation re-applies the policy. |
| **WebSocket drop / sequence gap** | `RECONCILING`, then a new connection generation | Nothing to do: the bot re-snapshots and reconciles over REST without resending orders. Repeated drops (3 in a row) → `DEGRADED`. |

## 5. Backups and restores

```powershell
.venv\Scripts\python.exe backup_state.py backup                      # → data/backups/bot_state_<UTC>.db + .json manifest
.venv\Scripts\python.exe backup_state.py verify data\backups\bot_state_<UTC>.db
.venv\Scripts\python.exe backup_state.py restore data\backups\bot_state_<UTC>.db --target data\bot_state.db
```

- **Backups are safe while the bot runs.** They use SQLite's online backup API, which includes pages still in the `-wal` file. Never copy `bot_state.db` on its own.
- **Restore requires the bot to be stopped.** It is refused while `data/live_bot.lock` or the journal's writer lock is held, and it **holds both locks itself** until the replaced file is verified, so no bot or writer can start in the middle.
- **How restore works:** the current DB is saved as `bot_state.pre_restore_<UTC>.db`, stale `-wal`/`-shm` files are removed, and the new file is swapped in atomically and integrity-checked.
- **Run a backup daily** (dashboard button or Task Scheduler), and before every upgrade or mode change.

## 5b. Verification suites

`step4_verify.py` … `step9_repair_verify.py` re-check the whole system. `step9_repair_verify.py` reproduces every review finding (F01–F11, source findings #1–#7) against fake exchanges, then runs the full chain (`step8b` → `step8` → `step7` → `step6` → `step5` → `step4`) plus the Step 4B execution-path checks; allow 20–30 minutes. `--skip-regression` runs only the Step 9 checks.

- **Safe to run while a 24/7 bot is running.** Since Step 8 every suite uses its own lock, journal, control directory and coin list, so the real `data/live_bot.lock`, the `data/bot_state*.db` journals and `data/app_symbols.json` are never touched. Every suite imports `testenv.py` first, which forces research mode and blanks the live keys.
- **Real exchange calls.** Only Step 8B's probe touches the real live server, with two read-only GETs (wallet, positions); its client refuses any order, cancel or margin change locally. Everything else uses fake exchanges.
- **Shared candle history.** Suites do sync candles into the shared `data/raw_1m/` history. The saved research results in `data/results/` are checked byte-for-byte to be unchanged.
- **Test files.** Written to `data/test_*.db` and `data/step*_test/`; they can be deleted.

## 5c. Manual Trade tab and extra coins

Open http://127.0.0.1:8000/#manual. In each mode it trades in the **same wallet and journal** as the auto-bot of that mode (paper: `data/bot_state.db`; see 5d for testnet/live).

- **Only one writer.** While the 24/7 loop runs, the order ticket is disabled and a banner offers **Stop Bot & Trade Manually**. Manual orders, protection edits, closes and cancels return HTTP 409 until the loop is stopped.
- **Every action is bound to the mode you saw.** The mode bar shows `mode #N` (the `mode_generation`, bumped by every mode switch). Orders, closes, SL/TP edits and cancels carry `expected_mode` + `mode_generation`; the server checks them under the same lock as mode switches. A ticket priced in PAPER that arrives after someone switched to LIVE is refused with HTTP 409 and nothing is sent: re-check the preview. Each order also carries a `client_request_id`, so a double click or a network retry never creates a second order.
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
- **Testnet and live manual trading** support the full set: market and limit entries, scale-ins, partial and full closes, SL/TP edits and cancels (section 5d).
- **Adding a coin.** "+ Add" (in the Manual Trade ticket or the Backtest Lab) puts the coin in `data/app_symbols.json` and downloads 90 days of history in the background.
- **Added coins do NOT auto-trade by default** (since Step 9). Adding a coin writes `{"variant": "C_costgate", "enabled": false}` for it in `data/bot_profiles.json`. To let the auto-bot trade it, add it with `"enable_auto_bot": true` (`POST /api/manual/symbols`) or set `"enabled": true` in the file. The C_costgate profile is unvalidated: it was rejected in Step 4B and never tested on other coins.
  ```json
  {"SOLUSD": {"variant": "C_costgate", "enabled": false}, "XRPUSD": {"variant": "D_both", "enabled": true}}
  ```
  Coins added before Step 9 have no entry and keep the old default (C_costgate, enabled): add `{"enabled": false}` for them if they should not auto-trade. Restart the bot after editing. Each extra coin is another 1%-risk position, so total exposure grows with the coin list.
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
- **Manual live orders** need `MODE=live`, `LIVE_TRADING_ENABLED=true`, the key and secret, a switch to **LIVE** in the Manual Trade tab (type `LIVE`), and typing `LIVE` again on every order, close, SL/TP edit and cancel. The server enforces it: each LIVE request must carry `confirm="LIVE"` and the current `mode_generation`, else HTTP 422 / 409 and nothing is sent.
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
| Every entry or scale-in | unknown outcomes on the symbol are reconciled first; no entry while one is unresolved or a limit rests on that symbol. Then isolated margin (`PUT /v2/users/margin_mode`, cached only after a confirmed success) and the ticket's leverage (`POST /v2/products/{id}/orders/leverage`) |
| New position | market or limit entry with SL/TP attached as a bracket; trigger is mark or last price |
| Partial fill of a limit | booked at once (each new fill slice, exactly once); SL/TP legs sized to the filled contracts while the rest of the order rests |
| Scale-in | market add at the same leverage; booked from its own fills (weighted average entry); SL/TP legs resized |
| Partial close | reduce-only market order; booked from its own fills and commission; remaining legs resized |
| Full close | reduce-only market order; leftover SL/TP legs cancelled |
| SL/TP edit | the live leg is modified (`PUT /v2/orders`); a missing leg is created (`POST /v2/orders/bracket`) |
| Cancel a resting limit | `DELETE /v2/orders`; any part that filled first is booked |

- **Fees** are Delta's own commission per fill (`paid_commission` / `commission`) whenever Delta reports it; the taker + GST estimate is used only when it does not.
- **Timeouts never resend.** A timed-out order or close is `OUTCOME_UNKNOWN` until Sync finds it (by order, fills or the real position) and books it through the right path: an entry opens/adds, a close books the trade. A second click on the same ticket is refused.
- **Trailing stops are paper-only for manual exchange positions.** Move the SL yourself.
- **Wallet bar and positions show Delta's real figures**, using Delta's `*_inr` fields or USD × 85, and Delta's liquidation price.
- **Positions opened elsewhere** appear as "exchange (not journalled)" and can be closed (reduce-only).
- **Changes made on Delta to a journalled position** are reconciled on the next sync: SL/TP fills and external closes book a trade (`SL`/`TP`/`EXT`), an external add adopts Delta's size and average entry, a reversal closes the journal position (the new side shows as unjournalled). SL/TP legs follow the new size. Sync runs on each 2.5 s poll and with the **Verify API & Sync Wallet** button.
- **Funding on manual exchange positions** is not taken from Delta's ledger yet: it is estimated from candles only while the auto-bot manages the position, and not recorded at all in manual-only sessions. Compare with Delta's transaction history.

**Checks.**
- **Verify API & Sync Wallet** makes a read-only call: the key is masked, and you get the wallet in ₹ and $ plus open positions. A read-only client cannot send orders even by mistake.
- **A ₹0 wallet** gives "please add funds on Delta Exchange India or switch to PAPER mode".
- **Common errors:**

  | Error | Fix |
  |---|---|
  | `ip_not_whitelisted_for_api_key` | Add this PC's public IP to the key on Delta |
  | `invalid_api_key` | Wrong key, or a testnet key used on live |
  | `SignatureExpired` | Sync the Windows clock |

**Untested against the real exchange** (the account had ₹0 at build time; Steps 8B and 9 are mock-tested only): isolated-margin switching and its "already isolated" answer, bracket legs on limit entries (including partial fills), the `reduce_only` flag and size on Delta's child legs, and leg resizing. Prove these on testnet first (section 3). Then make your first live trade a single minimum-size contract, and confirm the SL/TP legs on Delta's own Orders page.

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
