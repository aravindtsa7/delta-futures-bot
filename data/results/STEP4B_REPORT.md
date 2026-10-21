# Step 4B report — ₹50,000 capital, 4h trend filter, volatility/cost gate, slippage stress

Generated 2026-10-20 04:34 UTC by `step4b_research.py`. Research only; live trading stays disabled.

## Verdict

**No CANDIDATE.** Every variant on both symbols fails at least one predeclared criterion under the primary screen (₹50,000, 1% risk, all-taker + GST, funding, 5 bps adverse slippage per fill, full extended history). Per protocol no further grid search was run.

| symbol | variant | net_pnl_inr | profit_factor | max_drawdown_pct | total_trades | win60_share | win30_share | net>0 @5bps | PF>=1.20 | DD<=15% | trades>=50 | >=2/3 of 60d windows + | verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSD | A_baseline | -5,917.46 | 0.83 | 16.79 | 200 | 0.33 | 0.17 | FAIL | FAIL | FAIL | pass | FAIL | REJECT |
| BTCUSD | B_trend4h | 191.03 | 1.01 | 8.55 | 106 | 0.50 | 0.33 | pass | FAIL | pass | pass | FAIL | REJECT |
| BTCUSD | C_costgate | -2,851.73 | 0.90 | 14.35 | 153 | 0.33 | 0.25 | FAIL | FAIL | pass | pass | FAIL | REJECT |
| BTCUSD | D_both | 2,106.89 | 1.15 | 6.86 | 78 | 0.50 | 0.42 | pass | FAIL | pass | pass | FAIL | REJECT |
| ETHUSD | A_baseline | 2,608.98 | 1.06 | 10.69 | 201 | 0.67 | 0.50 | pass | FAIL | pass | pass | pass | REJECT |
| ETHUSD | B_trend4h | 561.74 | 1.02 | 13.12 | 117 | 0.33 | 0.50 | pass | FAIL | pass | pass | FAIL | REJECT |
| ETHUSD | C_costgate | 4,687.45 | 1.12 | 8.58 | 185 | 1.00 | 0.50 | pass | FAIL | pass | pass | pass | REJECT |
| ETHUSD | D_both | -417.67 | 0.98 | 11.70 | 103 | 0.33 | 0.50 | FAIL | FAIL | pass | pass | FAIL | REJECT |

Screen: net > 0 at 5 bps; profit factor ≥ 1.20; max drawdown ≤ 15%; ≥ 50 trades; net-profitable in ≥ 2/3 of the consecutive 60-day windows of the extended history (30-day share shown for information). These windows are retrospective diagnostics on data that informed earlier steps — not untouched out-of-sample data.

## Dataset and identity

- Extended history: `2025-10-20T04:30:00+00:00` → `2026-10-20T04:30:00+00:00` (365 days) after indicator warm-up from `2025-09-05T04:14:00+00:00`.
- Pinned primary benchmark (Step 3C): `2026-04-23T02:35:00+00:00` → `2026-10-20T02:35:00+00:00`, warm-up from `2026-03-09T02:35:00+00:00`.
- No git repository; source SHA-256 (first 16 hex): `config.py` 55f11bf3f3793c0e, `data_manager.py` 7d0c2f7fe870d42f, `strategies.py` 3e8e5ed1267432ae, `backtester.py` 73fe3a3ae3b76232, `app.py` ad28d73dbe9424ed, `step4b_research.py` 81f591f0ec66cf71.
- Window data fingerprints: BTCUSD/pinned_180d `6b353f433da0c2b7`, ETHUSD/pinned_180d `cff0784a745da7de`, BTCUSD/full_extended `8bf60227fe0b59ba`, ETHUSD/full_extended `4981ec28c6f52c40`.
- Primary config identity `87e0381c7153203f`: initial_capital_inr=50000.0, leverage=10, risk_per_trade_pct=1.0, sl_mode=atr, stop_loss_pct=1.0, take_profit_pct=2.0, sl_atr_mult=2.0, tp_atr_mult=5.0, atr_sl_min_pct=0.8, atr_sl_max_pct=4.0, trailing_sl_pct=0.0, trailing_atr_mult=2.5, breakeven_at_1r=False, exit_on_opposite_signal=False, cooldown_bars=1, max_daily_loss_pct=5.0, fee_mode=taker, trigger_source=trade, usd_inr_rate=85.0, gst_rate=0.18, margin_buffer=0.95, min_lot_max_loss_pct=5.0, allow_min_one_lot=False, slippage_bps=5.0, mark_fill_mode=traded.

| symbol | series | rows | earliest (UTC) | latest (UTC) | days | gaps |
| --- | --- | --- | --- | --- | --- | --- |
| BTCUSD | TRADE | 590418 | 2025-09-05 04:12:00 | 2026-10-20 04:29:00 | 410.01 | 0 |
| BTCUSD | MARK | 590418 | 2025-09-05 04:12:00 | 2026-10-20 04:29:00 | 410.01 | 0 |
| BTCUSD | FUNDING | 590417 | 2025-09-05 04:13:00 | 2026-10-20 04:29:00 | 410.01 | 0 |
| ETHUSD | TRADE | 590416 | 2025-09-05 04:14:00 | 2026-10-20 04:29:00 | 410.01 | 0 |
| ETHUSD | MARK | 590416 | 2025-09-05 04:14:00 | 2026-10-20 04:29:00 | 410.01 | 0 |
| ETHUSD | FUNDING | 590415 | 2025-09-05 04:15:00 | 2026-10-20 04:29:00 | 410.01 | 0 |

The FUNDING series had 33 one-minute holes per symbol at download-chunk starts before Oct 2025 (the API omits the first minute of a query that starts exactly on a funding boundary in older history). `data_manager` now re-fetches gaps from one funding interval earlier; all series have 0 gaps.

## Baseline reproduction (pinned 180 days, ETHUSD bollinger 1h ATR 2x/5x trail 2.5x)

| label | expected_net | net_pnl_inr | expected_trades | total_trades | expected_pf | profit_factor | expected_dd | max_drawdown_pct | match |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1  ₹10k 2% taker_maker_tp trade | 2,660.98 | 2,660.98 | 92.00 | 92 | 1.34 | 1.34 | 11.94 | 11.94 | True |
| 2  ₹10k 2% taker trade | 2,575.91 | 2,575.91 | 92.00 | 92 | 1.33 | 1.33 | 12.20 | 12.20 | True |
| 3  ₹10k 2% taker mark | 2,439.95 | 2,439.95 | 90.00 | 90 | 1.31 | 1.31 | 10.96 | 10.96 | True |
| 4  ₹10k 1% taker trade | 893.68 | 893.68 | 92.00 | 92 | 1.26 | 1.26 | 6.51 | 6.51 | True |

₹50,000 baselines. Left block: legacy UI semantics (min-1-lot floor allowed, mark-triggered stops fill at the level). Right block: Step 4B semantics (strict whole-lot risk cap, mark-triggered fills from the traded 1m range).

| symbol | label | net_pnl_inr | total_trades | profit_factor | max_drawdown_pct | step4b_semantics_net | step4b_semantics_trades | step4b_semantics_pf | step4b_semantics_dd |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSD | ₹50k 1% taker trade | 975.86 | 97 | 1.06 | 8.53 | 975.86 | 97.00 | 1.06 | 8.53 |
| BTCUSD | ₹50k 1% taker mark | 406.54 | 97 | 1.02 | 9.13 | 415.69 | 97.00 | 1.02 | 9.11 |
| BTCUSD | ₹50k 2% taker trade | 1,613.42 | 97 | 1.05 | 17.58 | 1,613.42 | 97.00 | 1.05 | 17.58 |
| BTCUSD | ₹50k 2% taker mark | 20.38 | 97 | 1.00 | 18.96 | 39.60 | 97.00 | 1.00 | 18.93 |
| ETHUSD | ₹50k 1% taker trade | 6,373.72 | 92 | 1.32 | 6.75 | 6,373.72 | 92.00 | 1.32 | 6.75 |
| ETHUSD | ₹50k 1% taker mark | 6,238.07 | 90 | 1.32 | 5.83 | 6,316.33 | 90.00 | 1.33 | 5.69 |
| ETHUSD | ₹50k 2% taker trade | 12,833.43 | 92 | 1.31 | 13.64 | 12,833.43 | 92.00 | 1.31 | 13.64 |
| ETHUSD | ₹50k 2% taker mark | 12,718.74 | 90 | 1.31 | 11.46 | 12,769.23 | 90.00 | 1.31 | 11.38 |

## Variants — pinned_180d (₹50,000, 1% risk, all-taker, trade trigger)

Benchmarks: cash = ₹0; `bh_return_pct` = unlevered 1x buy & hold after round-trip taker fee + GST (no funding).

| symbol | variant | slippage_bps | total_trades | net_pnl_inr | net_return_pct | gross_pnl_inr | fees_gst_inr | funding_inr | slippage_inr | profit_factor | max_drawdown_pct | exposure_pct | bh_return_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSD | A_baseline | 0.00 | 97 | 975.86 | 1.95 | 5,571.45 | 4,618.10 | 22.51 | 0.00 | 1.06 | 8.53 | 33.55 | 4.62 |
| BTCUSD | A_baseline | 2.00 | 97 | -351.51 | -0.70 | 3,987.47 | 4,367.95 | 28.97 | 1,480.66 | 0.98 | 10.18 | 33.32 | 4.62 |
| BTCUSD | A_baseline | 5.00 | 97 | -1,808.57 | -3.62 | 2,209.83 | 4,044.89 | 26.49 | 3,427.88 | 0.89 | 10.84 | 33.31 | 4.62 |
| BTCUSD | B_trend4h | 0.00 | 50 | 1,729.80 | 3.46 | 4,108.77 | 2,416.22 | 37.25 | 0.00 | 1.22 | 4.17 | 13.29 | 4.62 |
| BTCUSD | B_trend4h | 2.00 | 50 | 1,281.68 | 2.56 | 3,568.27 | 2,319.16 | 32.57 | 786.16 | 1.17 | 4.76 | 13.05 | 4.62 |
| BTCUSD | B_trend4h | 5.00 | 50 | 302.90 | 0.61 | 2,442.95 | 2,168.67 | 28.62 | 1,837.86 | 1.04 | 5.68 | 13.05 | 4.62 |
| BTCUSD | C_costgate | 0.00 | 94 | 822.50 | 1.65 | 5,189.68 | 4,412.32 | 45.15 | 0.00 | 1.05 | 8.83 | 32.90 | 4.62 |
| BTCUSD | C_costgate | 2.00 | 86 | 278.04 | 0.56 | 4,010.23 | 3,778.50 | 46.30 | 1,280.85 | 1.02 | 8.64 | 30.34 | 4.62 |
| BTCUSD | C_costgate | 5.00 | 65 | 1,288.38 | 2.58 | 3,715.76 | 2,482.69 | 55.31 | 2,103.98 | 1.11 | 6.47 | 25.39 | 4.62 |
| BTCUSD | D_both | 0.00 | 47 | 1,562.53 | 3.13 | 3,724.36 | 2,219.56 | 57.73 | 0.00 | 1.21 | 4.54 | 12.63 | 4.62 |
| BTCUSD | D_both | 2.00 | 41 | 1,603.26 | 3.21 | 3,369.14 | 1,812.19 | 46.31 | 614.30 | 1.23 | 3.35 | 11.09 | 4.62 |
| BTCUSD | D_both | 5.00 | 30 | 2,891.31 | 5.78 | 3,992.08 | 1,157.40 | 56.63 | 980.85 | 1.69 | 2.73 | 8.19 | 4.62 |
| ETHUSD | A_baseline | 0.00 | 92 | 6,373.72 | 12.75 | 10,219.42 | 3,843.73 | -1.97 | 0.00 | 1.32 | 6.75 | 27.88 | 1.74 |
| ETHUSD | A_baseline | 2.00 | 92 | 4,680.74 | 9.36 | 8,384.38 | 3,701.24 | -2.41 | 1,254.66 | 1.24 | 7.19 | 27.81 | 1.74 |
| ETHUSD | A_baseline | 5.00 | 92 | 3,065.24 | 6.13 | 6,550.24 | 3,481.80 | -3.20 | 2,950.68 | 1.16 | 8.20 | 27.58 | 1.74 |
| ETHUSD | B_trend4h | 0.00 | 56 | 5,310.82 | 10.62 | 7,755.44 | 2,419.01 | -25.62 | 0.00 | 1.52 | 7.05 | 13.20 | 1.74 |
| ETHUSD | B_trend4h | 2.00 | 56 | 4,525.82 | 9.05 | 6,874.94 | 2,325.04 | -24.08 | 788.15 | 1.44 | 7.10 | 13.13 | 1.74 |
| ETHUSD | B_trend4h | 5.00 | 56 | 3,321.74 | 6.64 | 5,535.25 | 2,193.25 | -20.25 | 1,858.69 | 1.32 | 7.22 | 12.95 | 1.74 |
| ETHUSD | C_costgate | 0.00 | 91 | 6,611.88 | 13.22 | 10,394.86 | 3,780.82 | -2.16 | 0.00 | 1.34 | 6.36 | 27.85 | 1.74 |
| ETHUSD | C_costgate | 2.00 | 89 | 4,744.51 | 9.49 | 8,259.94 | 3,523.22 | 7.79 | 1,194.31 | 1.25 | 6.15 | 27.07 | 1.74 |
| ETHUSD | C_costgate | 5.00 | 82 | 2,800.01 | 5.60 | 5,702.92 | 2,907.68 | 4.77 | 2,464.14 | 1.16 | 8.53 | 25.27 | 1.74 |
| ETHUSD | D_both | 0.00 | 55 | 5,623.35 | 11.25 | 8,018.26 | 2,368.67 | -26.24 | 0.00 | 1.56 | 7.05 | 13.17 | 1.74 |
| ETHUSD | D_both | 2.00 | 53 | 4,707.59 | 9.42 | 6,877.13 | 2,154.72 | -14.81 | 730.41 | 1.47 | 7.10 | 12.39 | 1.74 |
| ETHUSD | D_both | 5.00 | 47 | 857.28 | 1.71 | 2,503.76 | 1,639.10 | -7.38 | 1,389.07 | 1.09 | 6.35 | 11.00 | 1.74 |

## Variants — full_extended (₹50,000, 1% risk, all-taker, trade trigger)

Benchmarks: cash = ₹0; `bh_return_pct` = unlevered 1x buy & hold after round-trip taker fee + GST (no funding).

| symbol | variant | slippage_bps | total_trades | net_pnl_inr | net_return_pct | gross_pnl_inr | fees_gst_inr | funding_inr | slippage_inr | profit_factor | max_drawdown_pct | exposure_pct | bh_return_pct |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSD | A_baseline | 0.00 | 200 | 230.28 | 0.46 | 8,226.30 | 8,035.16 | 39.14 | 0.00 | 1.01 | 11.63 | 30.44 | -25.51 |
| BTCUSD | A_baseline | 2.00 | 200 | -2,283.21 | -4.57 | 5,279.01 | 7,601.36 | 39.14 | 2,576.73 | 0.94 | 12.47 | 30.35 | -25.51 |
| BTCUSD | A_baseline | 5.00 | 200 | -5,917.46 | -11.83 | 957.85 | 6,893.58 | 18.27 | 5,842.02 | 0.83 | 16.79 | 30.41 | -25.51 |
| BTCUSD | B_trend4h | 0.00 | 106 | 2,187.93 | 4.38 | 6,379.51 | 4,352.49 | 160.92 | 0.00 | 1.12 | 8.09 | 13.25 | -25.51 |
| BTCUSD | B_trend4h | 2.00 | 106 | 1,479.63 | 2.96 | 5,506.96 | 4,179.69 | 152.36 | 1,416.84 | 1.08 | 8.38 | 13.16 | -25.51 |
| BTCUSD | B_trend4h | 5.00 | 106 | 191.03 | 0.38 | 3,976.20 | 3,931.58 | 146.41 | 3,331.85 | 1.01 | 8.55 | 13.23 | -25.51 |
| BTCUSD | C_costgate | 0.00 | 194 | 685.04 | 1.37 | 8,446.07 | 7,807.61 | 46.59 | 0.00 | 1.02 | 10.52 | 29.86 | -25.51 |
| BTCUSD | C_costgate | 2.00 | 184 | -1,642.62 | -3.29 | 5,059.41 | 6,752.34 | 50.31 | 2,288.93 | 0.95 | 12.60 | 28.37 | -25.51 |
| BTCUSD | C_costgate | 5.00 | 153 | -2,851.73 | -5.70 | 1,838.30 | 4,741.03 | 51.00 | 4,017.82 | 0.90 | 14.35 | 24.97 | -25.51 |
| BTCUSD | D_both | 0.00 | 100 | 2,809.96 | 5.62 | 6,686.23 | 4,058.16 | 181.89 | 0.00 | 1.16 | 7.39 | 12.81 | -25.51 |
| BTCUSD | D_both | 2.00 | 93 | 2,286.40 | 4.57 | 5,616.65 | 3,492.90 | 162.66 | 1,184.03 | 1.13 | 7.61 | 12.02 | -25.51 |
| BTCUSD | D_both | 5.00 | 78 | 2,106.89 | 4.21 | 4,460.50 | 2,495.91 | 142.30 | 2,115.18 | 1.15 | 6.86 | 10.23 | -25.51 |
| ETHUSD | A_baseline | 0.00 | 201 | 8,884.10 | 17.77 | 16,311.03 | 7,391.86 | -35.07 | 0.00 | 1.21 | 9.53 | 29.83 | -40.28 |
| ETHUSD | A_baseline | 2.00 | 201 | 5,848.96 | 11.70 | 12,888.64 | 7,008.06 | -31.62 | 2,375.61 | 1.14 | 10.03 | 29.80 | -40.28 |
| ETHUSD | A_baseline | 5.00 | 201 | 2,608.98 | 5.22 | 9,168.81 | 6,525.98 | -33.84 | 5,530.50 | 1.06 | 10.69 | 29.69 | -40.28 |
| ETHUSD | B_trend4h | 0.00 | 117 | 3,799.54 | 7.60 | 7,821.66 | 4,093.77 | 71.66 | 0.00 | 1.17 | 11.27 | 14.93 | -40.28 |
| ETHUSD | B_trend4h | 2.00 | 117 | 2,575.17 | 5.15 | 6,418.65 | 3,914.32 | 70.84 | 1,326.89 | 1.11 | 11.60 | 14.89 | -40.28 |
| ETHUSD | B_trend4h | 5.00 | 117 | 561.74 | 1.12 | 4,186.03 | 3,697.87 | 73.58 | 3,133.79 | 1.02 | 13.12 | 14.80 | -40.28 |
| ETHUSD | C_costgate | 0.00 | 200 | 9,217.43 | 18.43 | 16,584.10 | 7,331.73 | -34.94 | 0.00 | 1.22 | 9.53 | 29.81 | -40.28 |
| ETHUSD | C_costgate | 2.00 | 198 | 5,370.40 | 10.74 | 12,185.06 | 6,797.81 | -16.85 | 2,304.34 | 1.13 | 10.55 | 29.41 | -40.28 |
| ETHUSD | C_costgate | 5.00 | 185 | 4,687.45 | 9.37 | 10,546.31 | 5,837.58 | -21.27 | 4,947.11 | 1.12 | 8.58 | 28.19 | -40.28 |
| ETHUSD | D_both | 0.00 | 116 | 3,997.31 | 7.99 | 7,961.36 | 4,035.71 | 71.66 | 0.00 | 1.18 | 11.27 | 14.91 | -40.28 |
| ETHUSD | D_both | 2.00 | 114 | 1,971.98 | 3.94 | 5,602.69 | 3,714.48 | 83.77 | 1,259.15 | 1.09 | 13.08 | 14.51 | -40.28 |
| ETHUSD | D_both | 5.00 | 103 | -417.67 | -0.84 | 2,445.04 | 2,947.59 | 84.88 | 2,497.96 | 0.98 | 11.70 | 13.43 | -40.28 |

## Secondary settings at 5 bps (full extended): 2% risk and mark-price triggers

| symbol | variant | risk_pct | trigger | total_trades | net_pnl_inr | profit_factor | max_drawdown_pct |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSD | A_baseline | 1.00 | trade | 200 | -5,917.46 | 0.83 | 16.79 |
| BTCUSD | A_baseline | 1.00 | mark | 200 | -5,600.65 | 0.84 | 16.25 |
| BTCUSD | A_baseline | 2.00 | trade | 200 | -11,653.28 | 0.83 | 32.01 |
| BTCUSD | A_baseline | 2.00 | mark | 200 | -11,287.72 | 0.84 | 31.15 |
| BTCUSD | B_trend4h | 1.00 | trade | 106 | 191.03 | 1.01 | 8.55 |
| BTCUSD | B_trend4h | 1.00 | mark | 106 | 937.90 | 1.05 | 8.61 |
| BTCUSD | B_trend4h | 2.00 | trade | 106 | -855.97 | 0.98 | 17.28 |
| BTCUSD | B_trend4h | 2.00 | mark | 106 | 1,601.38 | 1.04 | 16.91 |
| BTCUSD | C_costgate | 1.00 | trade | 153 | -2,851.73 | 0.90 | 14.35 |
| BTCUSD | C_costgate | 1.00 | mark | 153 | -2,723.41 | 0.90 | 12.60 |
| BTCUSD | C_costgate | 2.00 | trade | 153 | -7,312.92 | 0.86 | 28.64 |
| BTCUSD | C_costgate | 2.00 | mark | 153 | -5,344.27 | 0.90 | 24.71 |
| BTCUSD | D_both | 1.00 | trade | 78 | 2,106.89 | 1.15 | 6.86 |
| BTCUSD | D_both | 1.00 | mark | 78 | 3,022.94 | 1.22 | 6.91 |
| BTCUSD | D_both | 2.00 | trade | 78 | 3,528.05 | 1.12 | 14.38 |
| BTCUSD | D_both | 2.00 | mark | 78 | 6,027.73 | 1.20 | 14.36 |
| ETHUSD | A_baseline | 1.00 | trade | 201 | 2,608.98 | 1.06 | 10.69 |
| ETHUSD | A_baseline | 1.00 | mark | 198 | 3,210.83 | 1.08 | 10.68 |
| ETHUSD | A_baseline | 2.00 | trade | 201 | 4,326.67 | 1.05 | 20.28 |
| ETHUSD | A_baseline | 2.00 | mark | 198 | 5,782.71 | 1.07 | 20.38 |
| ETHUSD | B_trend4h | 1.00 | trade | 117 | 561.74 | 1.02 | 13.12 |
| ETHUSD | B_trend4h | 1.00 | mark | 115 | 1,157.23 | 1.05 | 12.72 |
| ETHUSD | B_trend4h | 2.00 | trade | 117 | 1,099.02 | 1.02 | 24.98 |
| ETHUSD | B_trend4h | 2.00 | mark | 115 | 2,309.55 | 1.05 | 24.21 |
| ETHUSD | C_costgate | 1.00 | trade | 185 | 4,687.45 | 1.12 | 8.58 |
| ETHUSD | C_costgate | 1.00 | mark | 183 | 4,729.66 | 1.12 | 8.46 |
| ETHUSD | C_costgate | 2.00 | trade | 185 | 7,888.28 | 1.10 | 17.31 |
| ETHUSD | C_costgate | 2.00 | mark | 183 | 8,380.87 | 1.10 | 16.56 |
| ETHUSD | D_both | 1.00 | trade | 103 | -417.67 | 0.98 | 11.70 |
| ETHUSD | D_both | 1.00 | mark | 102 | -92.00 | 1.00 | 11.02 |
| ETHUSD | D_both | 2.00 | trade | 103 | -1,154.60 | 0.97 | 22.33 |
| ETHUSD | D_both | 2.00 | mark | 102 | -784.76 | 0.98 | 21.49 |

## Filter accounting (full extended, 5 bps)

`raw_transitions` = bars where the unfiltered breakout condition turns true. Rejections are counted at those bars (`rejected_by_all` = blocked by both filters). Filters are applied before the edge trigger, so an entry can also fire later when a filter turns eligible while the breakout condition is still true (`fired_not_at_raw_transition`). Entries fired can exceed trades because signals arriving during an open position or cooldown are skipped.

| symbol | variant | filter_raw_transitions | filter_rejected_by_trend4h | filter_rejected_by_costgate | filter_rejected_by_all | filter_entries_fired | filter_entries_fired_not_at_raw_transition | total_trades | skipped_lot_budget |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSD | A_baseline | 310 | 0 | 0 | 0 | 310 | 0 | 200 | 0 |
| BTCUSD | B_trend4h | 310 | 159 | 0 | 0 | 151 | 0 | 106 | 0 |
| BTCUSD | C_costgate | 310 | 0 | 88 | 0 | 224 | 2 | 153 | 0 |
| BTCUSD | D_both | 310 | 159 | 88 | 40 | 104 | 1 | 78 | 0 |
| ETHUSD | A_baseline | 296 | 0 | 0 | 0 | 296 | 0 | 201 | 0 |
| ETHUSD | B_trend4h | 296 | 132 | 0 | 0 | 165 | 1 | 117 | 0 |
| ETHUSD | C_costgate | 296 | 0 | 33 | 0 | 269 | 6 | 185 | 0 |
| ETHUSD | D_both | 296 | 132 | 33 | 7 | 143 | 5 | 103 | 0 |

## Sizing, direction, concentration and bootstrap (full extended, 5 bps)

Realized risk = contracts × SL loss per contract (price move + fees with GST + 2 × slippage) / equity at entry. Top-3 concentration = sum of the 3 best trades / total net (> 100% means all other trades lost money net). Bootstrap: stationary block bootstrap of trade net PnL, mean block 5 trades, 1000 resamples (ignores compounding).

| symbol | variant | risk_pct_min | risk_pct_median | risk_pct_max | contracts_min | contracts_median | contracts_max | long_trades | long_net_inr | short_trades | short_net_inr | top3_concentration_pct | boot_p5 | boot_p50 | boot_p95 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSD | A_baseline | 0.67 | 0.90 | 1.00 | 2 | 4.00 | 8 | 91 | -1,476.07 | 109 | -4,441.39 | – | -14,178.18 | -5,778.67 | 2,740.17 |
| BTCUSD | B_trend4h | 0.67 | 0.90 | 1.00 | 2 | 5.00 | 8 | 39 | 2,084.25 | 67 | -1,893.22 | 1,574.78 | -6,725.82 | 299.86 | 7,580.20 |
| BTCUSD | C_costgate | 0.68 | 0.90 | 1.00 | 2 | 4.00 | 7 | 64 | 786.67 | 89 | -3,638.40 | – | -9,879.20 | -2,781.87 | 4,580.36 |
| BTCUSD | D_both | 0.67 | 0.89 | 1.00 | 2 | 4.00 | 8 | 24 | 3,368.54 | 54 | -1,261.65 | 142.79 | -4,682.52 | 1,915.21 | 8,666.49 |
| ETHUSD | A_baseline | 0.85 | 0.97 | 1.00 | 5 | 14.00 | 31 | 98 | 2,340.40 | 103 | 268.58 | 126.43 | -8,380.66 | 2,887.63 | 13,835.65 |
| ETHUSD | B_trend4h | 0.82 | 0.96 | 1.00 | 4 | 13.00 | 31 | 49 | 1,469.87 | 68 | -908.12 | 562.39 | -8,075.79 | 444.70 | 9,804.93 |
| ETHUSD | C_costgate | 0.85 | 0.97 | 1.00 | 5 | 14.00 | 31 | 87 | 1,823.53 | 98 | 2,863.92 | 74.02 | -5,591.14 | 4,736.24 | 15,538.79 |
| ETHUSD | D_both | 0.82 | 0.96 | 1.00 | 4 | 12.00 | 30 | 38 | -637.58 | 65 | 219.91 | – | -7,701.37 | -406.44 | 7,127.84 |

## Cost sensitivity

Fixed-trade attribution: Variant A's exact 0-bps trades re-priced with adverse slippage held fixed.

| window | symbol | slippage_bps | total_trades | net_pnl_inr | slippage_inr | delta_vs_0bps_inr |
| --- | --- | --- | --- | --- | --- | --- |
| pinned_180d | BTCUSD | 0.00 | 97 | 975.86 | 0.00 | 0.00 |
| pinned_180d | BTCUSD | 2.00 | 97 | -589.60 | 1,565.46 | -1,565.46 |
| pinned_180d | BTCUSD | 5.00 | 97 | -2,937.78 | 3,913.65 | -3,913.64 |
| pinned_180d | ETHUSD | 0.00 | 92 | 6,373.72 | 0.00 | 0.00 |
| pinned_180d | ETHUSD | 2.00 | 92 | 5,070.76 | 1,302.96 | -1,302.96 |
| pinned_180d | ETHUSD | 5.00 | 92 | 3,116.32 | 3,257.40 | -3,257.40 |
| full_extended | BTCUSD | 0.00 | 200 | 230.28 | 0.00 | -0.00 |
| full_extended | BTCUSD | 2.00 | 200 | -2,493.51 | 2,723.78 | -2,723.78 |
| full_extended | BTCUSD | 5.00 | 200 | -6,579.18 | 6,809.46 | -6,809.46 |
| full_extended | ETHUSD | 0.00 | 201 | 8,884.10 | 0.00 | -0.00 |
| full_extended | ETHUSD | 2.00 | 201 | 6,378.39 | 2,505.72 | -2,505.71 |
| full_extended | ETHUSD | 5.00 | 201 | 2,619.81 | 6,264.29 | -6,264.29 |

## Retrospective diagnostic windows (extended history, 5 bps, ₹50,000, 1%)

30-day slices (net ₹):

| symbol | variant | M1 | M2 | M3 | M4 | M5 | M6 | M7 | M8 | M9 | M10 | M11 | M12 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSD | A_baseline | -2,372 | -1,532 | -1,696 | -306 | 3,171 | -44 | -940 | -651 | -817 | -3,011 | -1,237 | 4,082 |
| BTCUSD | B_trend4h | -669 | -1,255 | -649 | 1,600 | 702 | -301 | -908 | -581 | -612 | -664 | 479 | 2,358 |
| BTCUSD | C_costgate | -2,395 | -1,287 | -511 | -928 | 1,885 | 311 | -536 | -276 | -350 | -1,497 | -416 | 3,494 |
| BTCUSD | D_both | -669 | -1,255 | 214 | 978 | -148 | -121 | -527 | 853 | -612 | -289 | 1,597 | 1,713 |
| ETHUSD | A_baseline | 951 | -791 | -2,534 | 3,049 | 1,699 | -1,729 | -1,405 | 2,372 | 1,678 | -2,536 | -23 | 1,716 |
| ETHUSD | B_trend4h | 283 | -1,148 | -2,708 | 2,910 | -421 | -665 | -914 | 551 | 527 | -1,600 | 3,529 | 1,073 |
| ETHUSD | C_costgate | 951 | -791 | -1,314 | 3,084 | 1,699 | -1,322 | -539 | 2,372 | 1,678 | -2,452 | -1,241 | 1,959 |
| ETHUSD | D_both | 283 | -1,148 | -1,647 | 3,013 | -421 | -665 | -1,269 | 543 | 527 | -1,521 | 1,246 | 1,171 |

60-day slices (net ₹):

| symbol | variant | B1 | B2 | B3 | B4 | B5 | B6 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BTCUSD | A_baseline | -3,722 | -1,368 | 3,016 | -1,566 | -3,668 | 2,799 |
| BTCUSD | B_trend4h | -1,762 | 1,005 | 401 | -1,598 | -1,276 | 2,804 |
| BTCUSD | C_costgate | -3,499 | -1,405 | 2,030 | -585 | -1,787 | 3,078 |
| BTCUSD | D_both | -1,762 | 1,192 | -268 | 326 | -861 | 3,310 |
| ETHUSD | A_baseline | 3 | 179 | -67 | 1,043 | -64 | 1,693 |
| ETHUSD | B_trend4h | -910 | 96 | -1,086 | -660 | -1,123 | 4,666 |
| ETHUSD | C_costgate | 3 | 1,672 | 366 | 1,834 | 13 | 625 |
| ETHUSD | D_both | -910 | 1,385 | -1,086 | -992 | -997 | 2,487 |

All slices, expanding windows and 0/2 bps runs are in `step4b_expanding_windows.csv`.

## Invariant and execution tests

| section | check | result | detail |
| --- | --- | --- | --- |
| data | extended history covers >= 365 days after warm-up | PASS | 365 days |
| data | 0 gaps in TRADE/MARK/FUNDING | PASS | 0 gaps |
| baseline | UI run 1  ₹10k 2% taker_maker_tp trade | PASS | ₹2,660.98 / 92 / PF 1.34 / DD 11.94% |
| baseline | UI run 2  ₹10k 2% taker trade | PASS | ₹2,575.91 / 92 / PF 1.33 / DD 12.20% |
| baseline | UI run 3  ₹10k 2% taker mark | PASS | ₹2,439.95 / 90 / PF 1.31 / DD 10.96% |
| baseline | UI run 4  ₹10k 1% taker trade | PASS | ₹893.68 / 92 / PF 1.26 / DD 6.51% |
| lookahead | BTCUSD: 4h-filtered 1h signals identical on 40/65/85/100% truncations | PASS | 114,128 bar comparisons, 0 mismatches |
| lookahead | BTCUSD: 1h bars closing at 01/02/03h (mod 4h) never read the forming 4h bar | PASS | 7,374 such bars; all use a 4h bar closed <= their close |
| lookahead | BTCUSD: join uses the latest closed 4h bar (lag < 8h for >99% of bars) | PASS | median lag 0 days 05:00:00 |
| lookahead | ETHUSD: 4h-filtered 1h signals identical on 40/65/85/100% truncations | PASS | 114,128 bar comparisons, 0 mismatches |
| lookahead | ETHUSD: 1h bars closing at 01/02/03h (mod 4h) never read the forming 4h bar | PASS | 7,374 such bars; all use a 4h bar closed <= their close |
| lookahead | ETHUSD: join uses the latest closed 4h bar (lag < 8h for >99% of bars) | PASS | median lag 0 days 05:00:00 |
| engine | trailing stop set on candle t is active only from t+1 | PASS | exit 01:03 TSL @ 1020 (candle 01:01 low 1019 < 1020 ignored) |
| engine | breakeven armed on candle t is active only from t+1 | PASS | exit 01:03 BE @ 1001.5 (BE 1001.5; 01:01 low 1000 ignored) |
| engine | gap-through stop fills at the 1m open | PASS | stop 980, filled 975 |
| engine | SL+TP in one 1m candle -> SL first, counted | PASS | SL @ 980, ambiguous_1m_bars=1 |
| engine | 5 bps adverse slippage on entry & exit, audited | PASS | entry 1000 -> 1000.5, TP 1100.5 -> 1099.94975, slippage $0.273065 |
| engine | mark-triggered stop fills from the traded 1m range | PASS | mark low 979 <= stop 980; traded low 985 -> fill 985 |
| engine | strict lot cap skips instead of rounding up to 1 lot | PASS | ₹425 capital, 1% budget ₹4.25 < 1-lot SL loss: strict skipped_lot_budget=1, legacy floor used=1 |
| audit | audit_result() passes on every Step 4B run | PASS | 192/192 |
| audit | equity identity initial + Σnet = final (<= 1e-9 x capital) | PASS | max abs diff ₹1.16e-10 |
| audit | max stop distance (+fees+slippage) inside 10x liquidation room (IM 10% - MM) | PASS | 4.218% < 9.75% (MM 0.25% from product metadata); intra-trade liquidation approximated by this guard |
| attribution | pinned_180d BTCUSD: 0 bps re-pricing reproduces the engine net | PASS | ₹975.8592 vs ₹975.8592 |
| attribution | pinned_180d ETHUSD: 0 bps re-pricing reproduces the engine net | PASS | ₹6,373.7186 vs ₹6,373.7186 |
| attribution | full_extended BTCUSD: 0 bps re-pricing reproduces the engine net | PASS | ₹230.2760 vs ₹230.2760 |
| attribution | full_extended ETHUSD: 0 bps re-pricing reproduces the engine net | PASS | ₹8,884.1006 vs ₹8,884.1006 |
| audit | audit_result() passes on every diagnostic-window run | PASS |  |

## Caveats

- Exchange liquidation is not simulated; it is approximated by requiring the widest stop (+ fees + slippage) to sit inside the 10x initial-margin minus maintenance-margin distance. Gaps larger than that would be understated.
- Slippage is a flat adverse bps per fill; real impact grows with size and volatility.
- All results are historical simulations on 1m candles (intrabar order unknown; SL-first on ambiguity).
- Variant specs and thresholds were fixed before this run; the extended history overlaps data used in earlier steps.
