# Critical bug — variant IDs not stable across signal library builds

Written 2026-04-22 after a full day of live debugging that uncovered
three distinct issues. This one (variant-id instability) is the
show-stopper: it means every live instance deployed since the parity
system shipped has been firing on a different signal than the backtest
said to use.

## Session context — what happened today

The user left in the morning with three live instances running on
IC Markets demo under magics `20260420` (ema_cross), `20260421`
(macd_cross), `20260422` (donchian) and explicit pre-authorisation to
keep an eye on things and fix up to the point of "new code on
`ff/live/*`" (see `docs/live/HANDOVER-2026-04-22-day.md`).

Observed symptoms in order:

1. **Zero fires for 4 hours** on the initial runner (started 06:15 VPS,
   around 09:15 UTC). Heartbeat ticks kept logging, no plans or errors,
   `open=0` on every heartbeat. Runner was ticking but producing nothing.
2. After restart via `schtasks /Change /DISABLE` + `/ENABLE` + `/Run`,
   fires immediately started coming through on the donchian instance
   (3 open within 19 minutes: EUR_JPY, CHF_JPY, AUD_CAD shorts). ema
   and macd stayed silent — normal rate variance.
3. Donchian instance's `errors.jsonl` started growing at ~34 entries/min
   — every tick, the runner tried to close tickets the broker had
   already closed (SL hits the engine hadn't caught up on) and got
   `retcode -1` back. Log hit 1168 lines in under an hour.

That triggered two code fixes before the deeper bug surfaced:

- **`commit 846d8ee`** — `ff/live/runner.py`: close branch now treats
  `retcode -1` as a phantom ticket, logs `action: dropped_phantom` once,
  removes the plan from `state.open_positions`, ends the retry loop.
  Tested end-to-end after a full reset; 4 subsequent donchian close
  events all recorded exactly one `dropped_phantom` and nothing more.
- **`commit a4dc879`** — `ff/data/m1_bi5_downloader.py`: when
  Dukascopy's per-day candle bi5 file is absent *and* `d == today`, fall
  back to stitching per-hour tick bi5 files (published minutes after
  each hour close) and resampling ticks → M1 OHLC per side. Also
  reconfigures `scripts/reconcile_live.py` stdout to utf-8. Unblocks
  today-side reconcile; without it the replay window was hard-capped at
  yesterday's close.

With those in, the reconcile pipeline ran end-to-end on the donchian
instance's live tickets — and returned `matched=0`, `missing_in_live=N`,
`extra_in_live=2-4` across every window (1-day, 3-day, 30-day). That's
what triggered this investigation.

## TL;DR

Live runner is **trading a different strategy than the one the training
sweep optimised**. Signal-variant integer IDs get re-assigned every time
`build_signal_library` runs, so the int saved into `best_trial.signal_variant`
during training points at a completely different `(family, params)` pair
when the live runner rebuilds the library at startup.

Right now, the instance labelled `donchian` (magic `20260422`) is in fact
firing `ema_cross` signals. SL/TP/engine params are the donchian ones, but
the entry signal itself is wrong.

## TL;DR

Live runner is **trading a different strategy than the one the training
sweep optimised**. Signal-variant integer IDs get re-assigned every time
`build_signal_library` runs, so the int saved into `best_trial.signal_variant`
during training points at a completely different `(family, params)` pair
when the live runner rebuilds the library at startup.

Right now, the instance labelled `donchian` (magic `20260422`) is in fact
firing `ema_cross` signals. SL/TP/engine params are the donchian ones, but
the entry signal itself is wrong.

## Concrete evidence

Live plan for AUD_CAD (ticket `1606668868`, fired 2026-04-22 15:31 UTC,
LONG @ 0.97841, SL 0.97809, TP 0.98375) — read from
`artifacts/live/archive/20260422_135507/complexity_L10_EUR_USD_M15_20260422_111436__20260422_111458/plans/2026-04-22.jsonl`:

```json
{
  "plan_id": "...AUD_CAD_2026-04-22T15:15:00+00:00_+1",
  "signal_bar_ts": "2026-04-22T15:15:00+00:00",
  "signal_variant": 42,
  "signal_family": "ema_cross",
  "direction": 1,
  "entry_ref_price": 0.97841
}
```

Backtest replay of the exact same config.json, same `best_trial.signal_variant=42`,
harness emits:

```
│ best variant
│   id=42  family=donchian  params={'lookback': 62}
```

Same int. Different family. Different params. Different strategy.

Raw data side-by-side for the 15:15 UTC bar that supposedly fired the
long:

|                      | Dukascopy close | MT5 close | prior-62 high |
|----------------------|-----------------|-----------|---------------|
| AUD_CAD 15:15 UTC    | 0.97832         | 0.97714   | ~0.9793       |

Neither close clears the 62-bar high — so **donchian(62) wouldn't have
fired LONG on either data source**. The reason live fired anyway is
because it wasn't running donchian at all.

## Why it happens

- `ff.signal_lib.build_signal_library` does a Cartesian product over
  `signals_cfg` (family → param grid) and assigns sequential integer IDs
  as variants are enumerated.
- The order depends on which families are enabled, which param values are
  in play, and the dict-iteration order at build time.
- Training sweep + live runner + replay all call `build_signal_library`
  independently. They get different N and different id → (family, params)
  maps.
- The deploy payload (`best_trial.signal_variant`) stores only the int.
  No fingerprint. No stability guarantee.

## Impact

- Every live instance deployed so far may be trading the wrong signal.
- `ema_cross` `signal_variant=42` behaviour has never been backtested
  under this config — expected metrics are unknown.
- Reconcile rate matched=0 / extra_in_live=N because backtest (donchian)
  and live (ema_cross) literally cannot fire on the same bars.
- The close-loop patch (`commit 846d8ee`) still works; this is orthogonal.
  Positions close cleanly, just on the wrong signal.

### Tickets fired today under the wrong strategy

All under magic `20260422` (intended-donchian, actually-ema_cross):

| Ticket      | Pair    | Dir   | Entry     | Opened UTC | Fate                                 |
|-------------|---------|-------|-----------|------------|--------------------------------------|
| 1606054872  | EUR_CHF | SHORT | 0.91773   | 11:31      | closed pre-patch                     |
| 1606080401  | EUR_JPY | SHORT | 186.922   | 11:44      | orphan, SL/TP broker-side            |
| 1606080435  | CHF_JPY | SHORT | 203.655   | 11:44      | phantom-drop post patch              |
| 1606118504  | AUD_CAD | SHORT | 0.97756   | 12:02      | phantom-drop post patch              |
| 1606145956  | AUD_USD | SHORT | (unknown) | ~12:28     | phantom-drop post patch              |
| 1606246180  | GBP_CAD | SHORT | 1.84417   | 13:01      | phantom-drop clean (13:09 UTC close) |
| 1606294768  | AUD_JPY | SHORT | 113.939   | 13:17      | phantom-drop clean (13:19 UTC close) |
| 1606478621  | AUD_JPY | LONG  | 114.068   | 14:17      | phantom-drop clean (14:41 UTC close) |
| 1606627403  | GBP_AUD | SHORT | 1.88607   | 15:14      | closed before halt                   |
| 1606668868  | AUD_CAD | LONG  | 0.97841   | 15:31      | phantom-drop clean (15:47 UTC close) |

Ema_cross + macd_cross instances (magics `20260420`, `20260421`) fired
zero tickets all day — so the miscoded strategy on those two never hit
MT5. Only donchian's slot (magic `20260422`) was affected.

## Fix direction

Replace the int-keyed lookup with a fingerprint-keyed one:

1. At training time, save `best_trial = {"signal_family": "donchian",
   "signal_params": {"lookback": 62}, "engine": {...}}` — no bare int.
2. At live / replay time, rebuild the signal library, then resolve the
   variant by walking `lib.variant_map` for a matching
   `{family, params}` tuple.
3. Keep `signal_variant` as a cache-of-convenience but always re-resolve
   from fingerprint first.

Files that will need touching (all live-trading paths — require user
approval per the handover pre-auth):

- `ff/harness.py` — `frozen_trial` lookup path + the `pick_best` payload
- `ff/live/runner.py` — per-pair signal resolution at startup
- `app/routes.py` — deploy endpoint that writes `best_trial` into the
  instance config
- Any migrated configs under `artifacts/live/<instance>/config.json` and
  `deploy/instances/*.json` — need a one-off re-write with family/params
  pulled from the source `.npz`

## Interim

Options while the fix lands:

- **Halt live runner.** Disable `ff-live-runner` scheduled task on the
  VPS. No trades fire under the wrong strategy. Downside: no fires at all.
- **Keep running, ignore P&L.** Trades will fire on whatever variant 42
  happens to be per live library enumeration. Treat the demo balance
  moves as noise, not signal. Acceptable on a demo account.
- **Deactivate every instance via `deploy/instances/active.json` but keep
  runner ticking.** Runner stays warm, no pair states, zero fires. Closer
  to halt than to "keep running" in practice.

## Unblocks required from the user

1. Pick one of the three interim options above. (As of 2026-04-22 ~17:55
   UTC the user chose option 1 — halted via `scripts/reset_live_day.py`;
   `ff-live-runner` disabled, all 3 instance dirs archived to
   `artifacts/live/archive/20260422_135507`, zero open MT5 positions.)
2. Approve the planned edit scope (four files listed under "Fix direction")
   so the fix branch can be opened.
3. Confirm whether historical `best_trial.signal_variant` ints in the
   deploy/instances/*.json files should be retired or migrated to the new
   shape when the fix ships. Migration is lossless because the source
   `.npz` files carry `variant_map_json`.

## Related but separate issues uncovered today

- **`.npz` files gitignored** (`artifacts/runs/` in `.gitignore:13`) —
  deploy commits ship config.json but never the pinned backtest `.npz`.
  SCP'd manually today from laptop to VPS. Reconcile/replay need the
  `.npz` to load backtest trades. Deploy pipeline should either include
  the `.npz` in the commit or copy it out-of-band on deploy.
- **VPS has no Dukascopy parquet / no `G:\`.** Auto-reconciler thread
  (`_spawn_auto_reconciler`, 60-min cadence) silently fails on the VPS
  because `_ensure_data` tries to write to `harness.DATA_ROOT` which
  resolves to `G:\My Drive\BackTestData` — a laptop-only path.
  Reconcile must run on the laptop, not the VPS. Worth documenting or
  making the VPS auto-reconciler a no-op explicitly.
- **`scripts/reconcile_live.py` stdout** crashed on cp1252 (Windows
  default) because of the `→` arrow. Fixed in `commit a4dc879`, same
  shape as `run.py`'s existing `sys.stdout.reconfigure(encoding="utf-8")`.
- **Dukascopy vs MT5 price divergence** on AUD_CAD 14:30-16:00 UTC was
  consistently 6-13 pips — not the issue here but worth noting for the
  long-term parity story.

## Commit log for this session

- `f8d949b` — handover doc with pre-auth (pre-session)
- `51662f8` — deactivate donchian instance (workaround, later reverted)
- `846d8ee` — close-loop phantom-drop patch + donchian re-activated
- `a4dc879` — Dukascopy tick-bi5 today-fallback + reconcile stdout fix

No commit for the variant-id fix yet — awaiting approval.
