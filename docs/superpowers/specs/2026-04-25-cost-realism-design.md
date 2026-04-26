# Cost-Realism Overlay — Design (2026-04-25)

## Problem

Dukascopy backtests systematically overstate execution costs vs IC Markets reality:

- **Spread:** Dukascopy median is 5–130× wider than MT5 in liquid hours (e.g. EUR_USD 0.32 vs 0.0; GBP_AUD 2.70 vs 0.10 pips).
- **Commission:** current default 0.3 pips/side does not reflect IC Markets $7 round-turn (≈ 0.35 pips/side on USD-quoted majors, varies on cross pairs).
- **Slippage:** current default 0.0 pips. Live forensic shows ~0.47 pips average entry slippage.
- **Rollover/news windows:** Dukascopy spread spikes massively at 21:00 UTC (e.g. GBP_AUD 22.40 pips median); MT5 stays flat (0.20 pips). BT trades fired at rollover post-overlay would still be artificial because in live they would be blocked by the execution-guard module.

Result: optimisation may favour wide-stop strategies that "win" only because Dukascopy's inflated spread eats tighter-stop alternatives.

## Goal

Surface a realistic-cost view of every BT run alongside the raw BT view, with no engine change. The overlay applies to **all BT runs** (optimisation included), so the optimiser ranks strategies on adjusted P&L by default.

## Architecture

```
ff/cost_realism/
  ├── gate_rules.py          single source of truth for "would this trade fire in live?"
  ├── bt_gate.py             post-pass filter that drops gated trades from trades.npz
  ├── overlay.py             post-pass cost adjuster (spread + commission + slippage)
  ├── cost_table.py          generator: MT5 medians + commission lookup → cost_table.json
  └── slippage_telemetry.py  forensic-fed per-pair slippage updater

ff/live/execution_guard.py   pre-trade live mirror of gate_rules

artifacts/cost_table.json    runtime data, ~28 pair entries × 5 sessions
```

The overlay attaches at the boundary between `harness.run()` and any consumer of `trades.npz`. It does not modify the Rust engine, the BT call, or the existing pipeline ordering.

## Components

### gate_rules.py — shared filter logic (the "3-and-3" module)
One module imported by both BT post-pass (`bt_gate.py`) and live (`execution_guard.py`). Standardised flat caps that apply to every EA — no per-pair tuning:
- `is_rollover(ts_utc) -> bool` — true when `21:00 ≤ hour < 24:00 UTC`.
- `is_spread_too_wide(spread_pips) -> bool` — true when `spread_pips > 3.0`. Hard cap, no pair-specific override.
- `is_slippage_too_wide(slippage_pips) -> bool` — true when realised entry slippage exceeds `3.0` pips. Used at fill-time in live, retroactively in BT against per-pair telemetry slippage.
- `is_news_window(ts_utc) -> bool` — v1 returns False (placeholder); v2 reads a calendar.
- `should_block(...) -> str | None` — returns reason string ("rollover" / "spread_3p" / "slippage_3p" / None) or None.

These two flat caps — **3-pip spread, 3-pip slippage** — are the standard execution discipline applied to every EA in the system. One source of truth, BT and live identical.

### bt_gate.py — BT post-pass trade filter
`apply(trades_df, cost_table) -> trades_df_with_gate_col`. For each trade, runs `should_block(entry_ts, duka_spread_at_entry, pair_telemetry_slippage)`. Adds `gated_out_reason` column (None / "rollover" / "spread_3p" / "slippage_3p"). Gated trades have their P&L zeroed in metric roll-up but remain visible in the report.

### overlay.py — post-pass cost adjuster
`apply(trades_df, cost_table) -> trades_df_with_overlay_cols`. For each surviving trade:

```
session       = session_of_hour(entry_ts.hour)              # Tokyo/London/Lon-NY/NY/Late
real_spread   = cost_table[pair].sessions[session].spread   # MT5 median for that session
real_comm     = cost_table[pair].commission_per_side        # static, $7 RT → pips
real_slip     = cost_table[pair].slippage_per_side          # from telemetry, default 0.5
bt_spread_at_entry = trade.duka_bt_spread_pips              # already in trades.npz
bt_cost_rt   = bt_spread_at_entry + 2 × 0.3                 # current BT charge per RT
real_cost_rt = real_spread + 2 × real_comm + 2 × real_slip  # what live would cost
overlay_delta_pips = bt_cost_rt - real_cost_rt              # positive = BT overstated
adjusted_pnl_pips  = raw_pnl_pips + overlay_delta_pips
```

Output adds three columns: `raw_pnl_pips`, `overlay_delta_pips`, `adjusted_pnl_pips`.

### cost_table.py — generator (manual run for v1)
Reads each `BackTestData_MT5/{pair}_M1.parquet`, computes per-pair × per-session median spread (5 sessions: Tokyo/London/Lon-NY/NY/Late). Looks up `commission_per_side` from a static per-pair pip-equivalent table built from `$7 USD round-turn / 2 / pip-value-per-lot`. Initialises `slippage_per_side = 0.5` for every pair. Writes `artifacts/cost_table.json`.

### slippage_telemetry.py — feedback loop
Reads forensic data (`artifacts/live/forensic/*.json` or the trade-comparison CSV). Per pair, computes the median entry slippage over the most recent 20 closed trades. Writes back to `cost_table.json` under `slippage_per_side`. Falls back to 0.5 default until 20 trades exist for that pair. Auto-graduates per-pair as the live history grows.

### execution_guard.py — live mirror
Imported into `ff/live/runner.py` before plan submission. Calls `gate_rules.should_block(...)` with the current MT5 spread; if non-None, logs the reason and skips the plan instead of placing it. Lays the groundwork for the Execution Guard module already in PROGRESS.md backlog.

## Data flow

```
BT engine (Rust, no change)
        │  trades.npz (raw)
        ▼
bt_gate.apply()           — drops rollover / spread-spike trades
        │  trades.npz + gated_out_reason
        ▼
overlay.apply()           — adds raw_pnl / overlay_delta / adjusted_pnl
        │  trades.npz + 3 new cols
        ▼
metrics roll-up           — reads adjusted_pnl_pips for total / mean / sharpe
        │
        ▼
UI / forensic / reconcile — show raw + adjusted side-by-side, plus "X gated" line
```

For optimisation: each trial's trades pass through `bt_gate → overlay` before the trial's score is computed. Optimiser sorts by adjusted score by default.

## cost_table.json schema

```json
{
  "schema_version": 1,
  "generated_at": "2026-04-25T20:00:00Z",
  "mt5_history_window": ["2026-02-25", "2026-04-25"],
  "pairs": {
    "EUR_USD": {
      "sessions": {
        "Asian":      {"spread_pips": 0.05},
        "London":     {"spread_pips": 0.0},
        "Lon-NY":     {"spread_pips": 0.0},
        "NY":         {"spread_pips": 0.0},
        "Rollover":   {"spread_pips": 0.5}
      },
      "commission_per_side_pips": 0.35,
      "slippage_per_side_pips": 0.5,
      "slippage_source": "default"
    }
  }
}
```

Sessions (all UTC, fixed boundaries — no DST shift): `Asian` 00–08, `London` 08–13, `Lon-NY` 13–17, `NY` 17–21, `Late/Rollover` 21–24. London opens at 08:00 UTC year-round.

**Broker time:** MT5 server time at IC Markets is GMT+2 (winter) / GMT+3 (summer). All MT5 parquet timestamps are already converted to UTC upstream by `ff/live/broker_mt5.py:178-181` (the `_broker_to_utc_sec` offset measured at connect). Session boundaries here speak UTC and never need broker-time arithmetic at gate-evaluation time.

## Error handling

- Missing pair in `cost_table.json` → log warning, skip overlay (raw_pnl flows through unchanged), do not fail the BT.
- Missing session for a pair → fall back to all-session median for that pair.
- `cost_table.json` missing entirely → log warning, BT runs as today (raw P&L). User runs `python scripts/build_cost_table.py` to generate.
- Forensic data unavailable → slippage stays at default 0.5 pips.
- Gate sees a trade entry timestamp with no timezone → treat as UTC, log once.

## Testing

- `tests/test_gate_rules.py` — rollover boundary cases (20:59:59, 21:00:00, 23:59:59, 00:00:00); spread-spike threshold edge cases.
- `tests/test_overlay_math.py` — synthetic trade with known costs, verify adjusted P&L matches hand calculation.
- `tests/test_cost_table_build.py` — golden parquet fixture with 100 known M1 bars per session, verify medians come out as expected.
- `tests/test_slippage_telemetry.py` — feed 25 synthetic forensic trades for one pair, verify the table updates with the rolling median and other pairs stay at default.
- `tests/test_overlay_optimisation.py` — run a 3-trial sweep with the overlay enabled vs disabled, verify ranking can flip.

## Rollout plan

1. **Build `cost_table.py` and generate `cost_table.json`.** Verifiable in isolation.
2. **Add `overlay.py` (no gate yet).** Apply to a single replay, surface `raw_pnl` vs `adjusted_pnl` in the trade-comparison HTML. Compare against the 10 closed trades from 2026-04-24.
3. **Add `bt_gate.py`** with rollover + 3-pip spread cap + 3-pip slippage cap. Re-run the comparison; expect 1–2 of the 10 trades to be gated out (most likely on cross-pair NY-close fills).
4. **Add `slippage_telemetry.py`** and wire it to run after every `import_mt5_report.py`.
5. **Default the overlay ON for every BT run** (optimisation included).
6. **Backport `execution_guard.py`** to live runner using the same `gate_rules`.

Each step ships as its own small PR; each is independently useful.

## Acceptance criteria

- The 10 closed live trades from 2026-04-24 reconcile such that `adjusted_pnl` average is within ≤1 pip of live P&L average (currently raw P&L is already within 0.2 pips, so this should hold easily).
- A 100-trial optimisation sweep with overlay-on shows different top-10 strategy rankings vs overlay-off — confirming the overlay actually moves the optimiser.
- After 50+ closed trades on EUR_USD, `slippage_telemetry.py` has overwritten the default slippage with a real per-pair number.
- Live execution guard blocks at least one rollover-window plan in the next two weeks of live trading (validates the gate is real-world-relevant).

## Out of scope (v1)

- News calendar integration. Gate has a `is_news_window` placeholder; v2 wires a calendar source.
- Spread variance modelling beyond per-session median (e.g. p95-aware stress tests).
- Per-strategy slippage profiles (some strategies fire mid-bar, others on close — could differ).
- Bid/ask asymmetry from tick parquet — current overlay treats spread as symmetric.
