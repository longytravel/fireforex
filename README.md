# Fire Forex — Handover

**Purpose:** a local optimisation workbench for forex strategies. The Rust engine lives in `core/` (`ff_core`) and is built in place with maturin — no external dependencies.

**Rule:** nothing lands here that doesn't work end-to-end with a number to back it up. Every change must update the "History" section at the bottom of this file so you can follow along.

---

## 0. Running the local web UI

```powershell
cd "C:\Users\ROG\Projects\Fire Forex"
.\.venv\Scripts\python.exe -m pip install -r requirements-web.txt   # first run only; uses uv under the hood
.\.venv\Scripts\python.exe run.py web
```

Open **http://127.0.0.1:8000/** — one-page app with four tabs:

- **Parameters** — pair / main TF / sub TF dropdowns, complexity slider (1–10) as a big preset, step-granularity and feature-set presets, and a MT5-style table of every feature (stop loss, take profit, trailing, breakeven, partial, stale, session, days, max-bars) with per-knob **tickbox · min · step · max** columns. Every control has a hover `?` with a plain-English explanation.
- **Run** — trials / seed / layer name, a progress bar, one backtest at a time.
- **Results** — 8 KPI tiles with a delta-vs-baseline line, equity curve, winning parameters in plain English, and a **Pin as baseline** button.
- **History** — last 30 rows of `artifacts/history.csv`.

Backend: FastAPI bound to `127.0.0.1`. All business logic reuses the existing `ff/` package. Override edits are applied server-side via `ff/defaults/overrides.py`. The pinned baseline persists to `artifacts/baseline.json`.

See `docs/next-session-handover.md` (new-session intro), `docs/ARCHITECTURE.md` (module + data flow) and `CLAUDE.md` (operating manual for Claude sessions) for the deeper docs.

---

## 1. The current baseline (2026-04-18 · `baseline_random`)

**One script:** `demo_speed.py`. Run it:

```powershell
cd "C:\Users\ROG\Projects\Fire Forex"
.\.venv\Scripts\python.exe demo_speed.py
```

**It's a harness.** Same data, same 2,257 signals, same seed (42) — only one thing changes between runs: the **optimiser** (today: random 500-sample). Every run:

1. Prints the **8 numbers** to the console (below).
2. Appends one row to `artifacts/history.csv`.
3. Saves the run's quality/equity arrays to `artifacts/runs/{layer}_{stamp}.npz`.
4. Regenerates `artifacts/comparison.html` — opens in browser automatically.

Opening `comparison.html` shows **every layer side-by-side** — table + running-best curves + equity curves + speed bars. That's where you see the system actually improving.

### The 8 numbers (today — seed=42, reproducible)

| Group | Metric | Baseline value | What it means |
|---|---|---:|---|
| **Speed** | backtests/sec | **966** | Rust engine throughput |
| **Speed** | total runtime | **1.06 s** | end-to-end wall-clock |
| **Activity** | trades (best variant) | 2,193 | strategy actually fires |
| **Activity** | win rate | 23.16 % | % of trades closing green |
| **Money** | total pips | +606 | cumulative pips across all trades |
| **Money** | expectancy | +0.28 pips/trade | net edge per trade |
| **Risk** | max drawdown | 100.00 % | worst peak-to-trough (blew up) |
| **Risk** | profit factor | 1.012 | wins ÷ losses (>1 profitable) |

**Best params:** SL_ATR_MULT = 1.856 · TP_RR_RATIO = 3.340

### How to read this
Machinery works end-to-end at 966 bt/sec. The *strategy* is barely above breakeven (PF 1.012) with random parameter sampling. A smart optimiser should do better — that's Layer 1. The 100% DD means the account blew up somewhere along the 19-year equity curve. Position sizing / risk management will fix that — later layer.

---

## 2. What's in this folder

```
Fire Forex/
├── README.md                ← this file (the handover)
├── demo_speed.py            ← the one working script, fully self-contained
├── .venv/                   ← independent Python 3.12 venv (uv-managed)
├── artifacts/
│   ├── demo_speed.html      ← per-run chart (this run only)
│   ├── comparison.html      ← ⭐ all layers side-by-side (opens on run)
│   ├── history.csv          ← one row per run, grows forever
│   └── runs/                ← per-run npz snapshots (quality, equity, params)
└── .claude/                 ← Claude Code local state (ignore)
```

**Anything else in this folder should be deleted.**

---

## 3. How the venv was set up (already done, don't redo)

```powershell
cd "C:\Users\ROG\Projects\Fire Forex"
uv venv --python 3.12
.\.venv\Scripts\python.exe -m pip install numpy pandas pyarrow plotly maturin
.\.venv\Scripts\maturin.exe develop --release
```

**`ff_core` is the Rust backtest engine**, source in `core/src/`. `maturin develop --release` compiles it and drops the `.pyd` into `.venv/Lib/site-packages/`. Rebuild any time you edit files under `core/`.

---

## 4. Data on G: drive (already there, no setup)

- **Primary:** `G:\My Drive\BackTestData\{PAIR}_{TF}.parquet`
- **25 currency pairs × 8 timeframes** (M1, M5, M15, M30, H1, H4, D, W) = 200 files
- **Schema (every file):** `open, high, low, close, volume, spread (real!), timestamp (UTC)`
- **Example sizes:** EUR_USD_H1 = 96k bars, EUR_USD_M1 = 5.7M bars — both span 2007-01 → 2026-02

To switch pair or timeframe, change two lines at the top of `demo_speed.py`:
```python
DATA_H1 = Path(r"G:\My Drive\BackTestData\EUR_USD_H1.parquet")
DATA_M1 = Path(r"G:\My Drive\BackTestData\EUR_USD_M1.parquet")
```

---

## 5. The journey — why this is the current shape

A brutally honest log of what we tried and why we landed here.

| Attempt | Result | Lesson |
|---|---|---|
| Rebuild everything from scratch in vectorbt (scalar) | **4 backtests/sec** | Too slow |
| Vectorbt batch mode with DataFrame signals | **23 backtests/sec**, random JIT crashes on i9-14900HX | Still too slow; hybrid CPU breaks LLVM JIT |
| Point at an existing pyo3 Rust engine | **18,700 bt/sec** on H1, **722 bt/sec** on M1 single-TF | Stop rebuilding, use what works |
| Vendor the Rust crate into `core/` and build locally | Same speed, no external dependencies | ✅ Current baseline foundation |
| H1 entries + M1 fills (multi-TF, 2026-04-18) | **800 bt/sec**, cleaner signals, 2257 over 20yr | ✅ Current baseline |

---

## 6. Key decisions (read before changing anything)

1. **The Rust engine lives in `core/`.** Edit source there, rebuild with `maturin develop --release`. No external repo to keep in sync.
2. **Use the Rust engine via pyo3 — don't write your own.** It already does 10,000,000+ trade simulations per second when fed correctly.
3. **H1 entries + M1 fills is the fair baseline.** Faster scans (H1), realistic fills (M1). Matches how real trades behave.
4. **Single script, one file.** Until we have three separate working experiments, `demo_speed.py` stays the one file.
5. **Random parameter sampling is the current default.** It's the honest baseline — it shows what the strategy does without any optimiser magic.
6. **`bars_per_year` uses H1 value (24×252 = 6,048)** — this is what Sharpe is annualised against. The main loop is on H1 even though fills are M1.

---

## 7. What's proven vs what's not

**Proven:**
- The engine works and is fast. 800 bt/sec on 20yr multi-TF is the measured floor.
- The wheel is portable — installs cleanly into a new venv.
- Data loading from G: is fast (~0.6s for 5.7M bars).
- H1→M1 alignment works via `numpy.searchsorted`.
- Best random variant is slightly profitable (PF 1.006) — machinery sane.

**Not proven:**
- Whether EMA-cross has a *real* edge on EUR/USD (results are right on the knife-edge — could be luck).
- Whether any optimiser finds consistently good parameters (haven't run one yet — still random).
- Whether those parameters hold up out-of-sample (no walk-forward yet).
- Whether other strategies beat EMA-cross (haven't tested).
- Whether other pairs behave similarly (EUR/USD only).

---

## 8. What's next — the queue (keep it simple)

**One variable changes per layer. Everything else frozen.** Data, signals, seed, budget — all stay identical. The only difference between runs is **how we pick the 500 parameter combinations to test**.

Each new layer just:
1. Changes `LAYER_NAME` + `OPTIMIZER` constants at the top of `demo_speed.py`
2. Swaps the innards of `build_param_matrix()` to use the new optimiser
3. Runs the script → `comparison.html` shows the new layer against all previous

### Layer 1 — Optuna (TPE)

**What changes:** `build_param_matrix()` becomes a mini-loop. Optuna's TPE sampler proposes 500 candidates one-by-one, we score each with a single-row `batch_evaluate` call, feed score back to Optuna.

**What you'll see in `comparison.html`:**
- **Running-best** curve should **climb faster** than random's flat scatter.
- **Final quality** should be higher (better profit factor, smaller DD).
- **Speed** will drop — Optuna is sequential (bc.NUM_PL calls instead of 1 batched call). **This is the price of "smart" search**, and we'll measure exactly how much.

### Layer 2 — CatCMAwM (from `cmaes`)

**What changes:** population-based, batch-friendly. Each generation proposes N candidates, we evaluate all N in **one** `batch_evaluate` call, feed quality scores back as fitness.

**What you'll see:**
- **Speed** should recover most of Optuna's loss (batching restored).
- **Running-best** should keep climbing past Optuna's ceiling (CMA-ES learns param covariance → captures SL↔TP interactions).

### The honest goal

A `comparison.html` where the three running-best lines are clearly ordered: **random < Optuna < CatCMAwM**, with a separate speed bar showing each layer still runs in single-digit seconds.

Out of scope for now (explicitly): other pairs, other strategies, walk-forward, position sizing. **Finish the optimiser comparison first.**

---

## 9. How to add a new layer (the workflow)

The whole point of the harness: **adding a layer is a 3-line change**.

1. At the top of `demo_speed.py`, change:
   ```python
   LAYER_NAME = "layer1_optuna"     # new unique label
   OPTIMIZER = "optuna"             # name of the method
   ```
2. Replace the body of `build_param_matrix()` with the new optimiser's loop.
3. Run the script. Browser opens `comparison.html` with the new layer alongside all previous.

That's it. `history.csv` and `runs/*.npz` grow automatically. **Never delete rows.** If a layer turns out worse, keep it — that's the point.

**Keep frozen across layers:** `N_TRIALS`, `SEED`, `EMA_FAST`, `EMA_SLOW`, `ATR_PERIOD`, `SL_ATR_RANGE`, `TP_RR_RANGE`, `COMMISSION_PIPS`, data files. **Only the optimiser changes.**

**Quick reset** if the harness gets confused: delete `artifacts/history.csv` and `artifacts/runs/` — the next run will recreate them.

---

## 10. History

Chronology of *this harness*. For the per-run numbers, open `artifacts/history.csv` or `comparison.html`.

| Date | What changed | Notable |
|---|---|---|
| 2026-04-18 | Rebuild everything in vectorbt (scalar) | 4 bt/sec — abandoned |
| 2026-04-18 | Vectorbt batch with DataFrame signals | 23 bt/sec — abandoned (LLVM JIT crashes on i9 hybrid cores) |
| 2026-04-18 | Point at existing Rust engine via ClaudeBackTester venv | 18,700 bt/sec on 20yr H1 |
| 2026-04-18 | Self-contained: Rust wheel installed in local venv | Same speed, zero dependency on CBT |
| 2026-04-18 | 20yr **M1 single-TF** sweep (148k signals) | 722 bt/sec |
| 2026-04-18 | Sample trades confirm MA(4/12)+4p SL+12p TP has no raw edge | -474k pips, -0.95 pips expectancy |
| 2026-04-18 | H1 entries + M1 fills multi-TF | 800 bt/sec · 2,257 signals |
| 2026-04-18 | Comparison harness — seed=42, `history.csv`, per-layer npz, `comparison.html` | `baseline_random` → 966 bt/sec · PF 1.012 · DD 100% |
| **2026-04-18** | **⭐ Extensible EA foundation (`ff/` + `eas/` + `run.py`)** — declarative schema (FloatRange/IntRange/Choice + Group + Branch), signal-family registry, on/off group handling in sampler, TF-agnostic harness, pre-flight sizing report | foundation for any EA, any pair, any TF |
| **2026-04-18** | **⭐ Complex01 EA — 66-variant signal library × 22-dim engine schema (SL/TP/trail/BE/partial/stale/session)** | 2000 trials · 936 bt/sec · **PF 1.247 · DD 25.8% · +2,225 pips** |
| *(next)* | Layer 1 — Optuna (TPE) into `ff/sampler.py` interface | *compare in `comparison.html`* |
| *(next)* | Layer 2 — CatCMAwM (mixed-integer + masking) | *compare in `comparison.html`* |

---

## 10b. How to add a new EA (the workflow since 2026-04-18 foundation)

A new EA is a single Python file in `eas/` with one top-level `EA` dict. The
system reads it and figures out everything else.

**Worked example:** `eas/complex01.py`. Copy it, rename, edit. The six sections:

1. **`data`** — `pair`, `main_tf`, `sub_tf`. Change these to run on a different
   pair or timeframe. A 4-hour strategy is literally `"main_tf": "H4"` and
   `"sub_tf": "M15"`.
2. **`execution`** — `pip_value` (None = auto from pair), `commission_pips`,
   `max_spread_pips`, `slippage_pips`, `atr_period`.
3. **`signals`** — per-family parameter grid. Each family (`ema_cross`,
   `macd_cross`, `donchian`, `rsi_reversal`, …) has an entry mapping its params
   to `IntRange(min, max, step=N)` / `Choice([...])`. The system expands the
   Cartesian product, drops invalid combos, and produces one pre-computed
   signal-variant per valid combo. **The step size directly drives library size**
   — the pre-flight report shows combos + estimated build time before running.
4. **`engine_schema`** — the 27 `PL_*` Rust slots as schema nodes:
   - `FloatRange(min, max, scale="linear"|"log")` — continuous knob
   - `IntRange(min, max, step=1)` — stepped integer
   - `Choice([...])` — categorical
   - `Group(test=Choice([True,False]), when_on={...})` — on/off block; sub-knobs
     *only sampled when on* (random sampler skips them when off — no wasted budget)
   - `Branch(selector=Choice([...]), arms={name: {...}})` — exclusive N-way
5. **`engine_mapping`** — list of `(slot_index, encoder_fn)` pairs translating a
   sampled trial dict to the `(N, NUM_PL)` float64 matrix the engine expects.
   Use the helpers in `ff.encoding`: `slot_const`, `slot_float`, `slot_int`,
   `slot_categorical`, `slot_bool_to_int`, `slot_mode_or_off`, `slot_if_on`,
   `slot_branch_field`.
6. *(optional — new indicator)* Register a signal family in `ff/signal_lib.py`
   via `@register("my_family")`. The function takes a main-TF DataFrame and
   returns a `SignalSet`. Raise `InvalidCombo` to skip invalid parameter combos.

Run it:
```powershell
cd "C:\Users\ROG\Projects\Fire Forex"
.\.venv\Scripts\python.exe run.py eas/my_ea.py --trials 2000 --seed 42
```

CLI flags: `--layer`, `--optimizer`, `--seed`, `--trials`, `--no-preflight`,
`--no-pause`, `--no-browser`.

**Adaptivity status:**
- New entry signals → `@register` a new family. No Rust change.
- New pairs / timeframes → edit `data` section only.
- Session-based trade gating → tag signals via `ff.signal_lib.session_of_hour`
  and filter via `PL_BUY_FILTER_MAX` / `PL_SELL_FILTER_MIN`.
- Real per-bar spread → already in the data files, engine uses it natively.
- **New exit mechanisms, per-session slippage, tunable slippage** → see
  `docs/rust-wishlist.md`; these need Rust engine changes, tracked separately.

---

## 11. Glossary (plain English)

| Term | What it means |
|---|---|
| **Bar** | One candle. 1 H1 bar = 1 hour of price. 1 M1 bar = 1 minute of price. |
| **Signal** | A moment where the strategy says "enter here" (e.g. an EMA cross). |
| **Trade** | One completed signal, from entry to SL/TP hit. |
| **Backtest** | Simulating one full strategy + parameter set over the whole history. 500 backtests = 500 different parameter combos. |
| **Multi-TF** | Using two timeframes at once: slower TF for entries (less noise), faster TF for exits (accurate fills). |
| **Sharpe** | Risk-adjusted return. >1 is good, >2 is great, <0 is losing. |
| **Profit factor** | Total winnings ÷ total losses. >1 is profitable, <1 is losing. 1.006 is "barely breakeven." |
| **Max drawdown** | The worst peak-to-trough loss. 100%+ means the account blew up. |
| **Walk-forward** | Optimise on year 1, test on year 2. Optimise on years 1-2, test on year 3. And so on. Proves the strategy isn't just curve-fit. |
| **CatCMAwM** | An optimiser that learns parameter relationships automatically. Smart cousin of random sampling. |
| **ATR** | Average True Range — a measure of how much the price moves per bar. Used to size SL/TP relative to volatility. |

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| `FileNotFoundError: G:\My Drive\BackTestData\...` | Google Drive not mounted / still syncing |
| `ModuleNotFoundError: ff_core` | Run `.\.venv\Scripts\maturin.exe develop --release` from the repo root, then restart whatever process needs it |
| Backtest speed is much slower than quoted | First run pays a 0.6s one-time cost. Re-run and compare |
| Want to switch pair/TF | Edit `DATA_H1` and `DATA_M1` at the top of `demo_speed.py` |
| Rebuild the Rust engine | Edit files in `core/src/`, then `.\.venv\Scripts\maturin.exe develop --release` from the repo root |
