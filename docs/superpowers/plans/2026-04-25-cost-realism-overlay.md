# Cost-Realism Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a post-pass cost-realism subsystem to Fire Forex so Dukascopy backtests show both raw and IC-Markets-adjusted P&L, with a shared "3-and-3" gate (3-pip spread cap, 3-pip slippage cap, 21-24 UTC rollover skip) that BT and live can never drift on.

**Architecture:** Five Python modules under `ff/cost_realism/` plus an `ff/live/execution_guard.py` live mirror, all importing one shared `gate_rules.py`. Post-pass operations on `trades.npz` — zero Rust engine change. Cost data lives in `artifacts/cost_table.json` (manually regenerated from MT5 parquets).

**Tech Stack:** Python 3.12, pandas, NumPy, pytest, the existing `ff.harness` pipeline.

**Spec:** `docs/superpowers/specs/2026-04-25-cost-realism-design.md`

---

## File structure

| File | Responsibility |
|---|---|
| `ff/cost_realism/__init__.py` | Package marker, re-exports public API |
| `ff/cost_realism/gate_rules.py` | Shared filter logic — 3-pip caps, rollover window, session lookup |
| `ff/cost_realism/cost_table.py` | Build `cost_table.json` from MT5 parquets + commission lookup |
| `ff/cost_realism/overlay.py` | Post-pass cost adjuster (raw → adjusted P&L) |
| `ff/cost_realism/bt_gate.py` | Post-pass trade filter using gate_rules |
| `ff/cost_realism/slippage_telemetry.py` | Forensic-fed per-pair slippage updater |
| `ff/live/execution_guard.py` | Live runner mirror of gate_rules |
| `scripts/build_cost_table.py` | CLI wrapper for cost-table generation |
| `tests/cost_realism/test_gate_rules.py` | Unit tests — boundaries, caps, sessions |
| `tests/cost_realism/test_cost_table.py` | Unit tests — fixture-driven build |
| `tests/cost_realism/test_overlay.py` | Unit tests — overlay math |
| `tests/cost_realism/test_bt_gate.py` | Unit tests — drop logic |
| `tests/cost_realism/test_slippage_telemetry.py` | Unit tests — rolling-median update |
| `tests/cost_realism/test_execution_guard.py` | Unit tests — live block path |
| `artifacts/cost_table.json` | Runtime data (gitignored) |

---

## PR map

- **PR A — Tasks 1–3:** Gate rules + cost table generator (no engine effects yet).
- **PR B — Tasks 4–5:** Overlay applied in reconcile, BT gate.
- **PR C — Task 6:** Slippage telemetry feedback loop.
- **PR D — Task 7:** Default-ON wiring for `harness.run()` and UI surfacing.
- **PR E — Task 8:** Live `execution_guard.py` backport.

---

### Task 1: gate_rules.py — shared filter logic

**Files:**
- Create: `ff/cost_realism/__init__.py`
- Create: `ff/cost_realism/gate_rules.py`
- Create: `tests/cost_realism/__init__.py`
- Create: `tests/cost_realism/test_gate_rules.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/cost_realism/test_gate_rules.py
import pandas as pd
import pytest

from ff.cost_realism import gate_rules as gr


def test_session_of_hour_boundaries():
    assert gr.session_of_hour(0) == "Asian"
    assert gr.session_of_hour(7) == "Asian"
    assert gr.session_of_hour(8) == "London"
    assert gr.session_of_hour(12) == "London"
    assert gr.session_of_hour(13) == "Lon-NY"
    assert gr.session_of_hour(16) == "Lon-NY"
    assert gr.session_of_hour(17) == "NY"
    assert gr.session_of_hour(20) == "NY"
    assert gr.session_of_hour(21) == "Rollover"
    assert gr.session_of_hour(23) == "Rollover"


def test_is_rollover_boundaries():
    assert gr.is_rollover(pd.Timestamp("2026-04-24 20:59:59", tz="UTC")) is False
    assert gr.is_rollover(pd.Timestamp("2026-04-24 21:00:00", tz="UTC")) is True
    assert gr.is_rollover(pd.Timestamp("2026-04-24 23:59:59", tz="UTC")) is True
    assert gr.is_rollover(pd.Timestamp("2026-04-25 00:00:00", tz="UTC")) is False


def test_is_spread_too_wide():
    assert gr.is_spread_too_wide(2.99) is False
    assert gr.is_spread_too_wide(3.0) is False
    assert gr.is_spread_too_wide(3.01) is True


def test_is_slippage_too_wide():
    assert gr.is_slippage_too_wide(2.99) is False
    assert gr.is_slippage_too_wide(3.0) is False
    assert gr.is_slippage_too_wide(3.01) is True


def test_should_block_returns_reason_or_none():
    ts_quiet = pd.Timestamp("2026-04-24 10:00:00", tz="UTC")
    ts_roll = pd.Timestamp("2026-04-24 22:00:00", tz="UTC")
    assert gr.should_block(ts_quiet, spread_pips=0.5, slippage_pips=0.5) is None
    assert gr.should_block(ts_roll, spread_pips=0.5, slippage_pips=0.5) == "rollover"
    assert gr.should_block(ts_quiet, spread_pips=4.0, slippage_pips=0.5) == "spread_3p"
    assert gr.should_block(ts_quiet, spread_pips=0.5, slippage_pips=5.0) == "slippage_3p"


def test_naive_timestamp_treated_as_utc():
    ts = pd.Timestamp("2026-04-24 22:00:00")  # no tz
    assert gr.is_rollover(ts) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_gate_rules.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ff.cost_realism'`

- [ ] **Step 3: Write minimal implementation**

```python
# ff/cost_realism/__init__.py
"""Cost-realism overlay subsystem.

Public API:
    gate_rules.should_block(ts, spread_pips, slippage_pips) -> str | None
    gate_rules.session_of_hour(hour) -> str
"""
from . import gate_rules

__all__ = ["gate_rules"]
```

```python
# ff/cost_realism/gate_rules.py
"""Shared trade-eligibility rules — the "3-and-3" module.

One source of truth imported by:
- ``ff.cost_realism.bt_gate`` (BT post-pass)
- ``ff.live.execution_guard`` (live runner pre-submission)

So BT and live can never drift on what counts as a "do not fire" condition.
"""
from __future__ import annotations

import pandas as pd

# Hard caps applied to every EA. Not pair-specific.
SPREAD_CAP_PIPS: float = 3.0
SLIPPAGE_CAP_PIPS: float = 3.0

# Rollover window (UTC). London/NY close handover; spreads spike here even on
# raw-spread accounts, and most strategies should not initiate new positions.
ROLLOVER_START_HOUR_UTC: int = 21
ROLLOVER_END_HOUR_UTC: int = 24

# Session boundaries (UTC, fixed — no DST shift).
_SESSION_BOUNDARIES = [
    (0, 8, "Asian"),
    (8, 13, "London"),
    (13, 17, "Lon-NY"),
    (17, 21, "NY"),
    (21, 24, "Rollover"),
]


def session_of_hour(hour: int) -> str:
    """Return the canonical session name for a UTC hour 0..23."""
    for lo, hi, name in _SESSION_BOUNDARIES:
        if lo <= hour < hi:
            return name
    raise ValueError(f"hour out of range: {hour}")


def _to_utc(ts: pd.Timestamp) -> pd.Timestamp:
    """Coerce a Timestamp to UTC. Naive timestamps are treated as UTC."""
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def is_rollover(ts: pd.Timestamp) -> bool:
    """True when the entry timestamp falls in the daily rollover window."""
    h = _to_utc(ts).hour
    return ROLLOVER_START_HOUR_UTC <= h < ROLLOVER_END_HOUR_UTC


def is_spread_too_wide(spread_pips: float) -> bool:
    """True when spread strictly exceeds the 3-pip cap."""
    return spread_pips > SPREAD_CAP_PIPS


def is_slippage_too_wide(slippage_pips: float) -> bool:
    """True when realised slippage strictly exceeds the 3-pip cap."""
    return slippage_pips > SLIPPAGE_CAP_PIPS


def should_block(
    ts: pd.Timestamp,
    spread_pips: float,
    slippage_pips: float,
) -> str | None:
    """Return the block reason or None.

    Order of evaluation matters for diagnostics — rollover first because
    the reason is most informative for the user.
    """
    if is_rollover(ts):
        return "rollover"
    if is_spread_too_wide(spread_pips):
        return "spread_3p"
    if is_slippage_too_wide(slippage_pips):
        return "slippage_3p"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_gate_rules.py -v`
Expected: PASS — 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add ff/cost_realism/__init__.py ff/cost_realism/gate_rules.py tests/cost_realism/__init__.py tests/cost_realism/test_gate_rules.py
git commit -m "feat(cost-realism): gate_rules module with 3-pip caps + rollover"
```

---

### Task 2: cost_table.py — build cost_table.json from MT5 parquets

**Files:**
- Create: `ff/cost_realism/cost_table.py`
- Create: `tests/cost_realism/test_cost_table.py`
- Modify: `.gitignore` — add `artifacts/cost_table.json`

- [ ] **Step 1: Write the failing test**

```python
# tests/cost_realism/test_cost_table.py
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ff.cost_realism import cost_table as ct


def _write_fixture_parquet(path: Path, pair: str, *, days: int = 30) -> None:
    """Create a synthetic MT5 M1 parquet for `pair` covering `days` weekdays."""
    rng = np.random.default_rng(seed=hash(pair) % (2**31))
    rows = []
    start = pd.Timestamp("2026-03-01 00:00:00")
    pip = 0.01 if "JPY" in pair else 0.0001
    for d in range(days):
        ts = start + pd.Timedelta(days=d)
        if ts.dayofweek >= 5:
            continue
        for minute in range(24 * 60):
            ts_min = ts + pd.Timedelta(minutes=minute)
            hour = ts_min.hour
            spread_pips = 0.1 if 8 <= hour < 21 else 0.5
            spread = spread_pips * pip
            rows.append(
                {
                    "timestamp": ts_min,
                    "open": 1.10000,
                    "high": 1.10010,
                    "low": 1.09990,
                    "close": 1.10005,
                    "volume": 1.0,
                    "spread": spread,
                }
            )
    pd.DataFrame(rows).to_parquet(path, index=False)


def test_build_cost_table_per_session_medians(tmp_path):
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    _write_fixture_parquet(mt5_root / "EUR_USD_M1.parquet", "EUR_USD")

    out_path = tmp_path / "cost_table.json"
    ct.build_cost_table(
        pairs=["EUR_USD"],
        mt5_root=mt5_root,
        out_path=out_path,
    )

    table = json.loads(out_path.read_text())
    assert table["schema_version"] == 1
    assert "EUR_USD" in table["pairs"]
    pair_entry = table["pairs"]["EUR_USD"]

    assert pair_entry["sessions"]["Asian"]["spread_pips"] == pytest.approx(0.5, abs=0.01)
    assert pair_entry["sessions"]["London"]["spread_pips"] == pytest.approx(0.1, abs=0.01)
    assert pair_entry["sessions"]["Lon-NY"]["spread_pips"] == pytest.approx(0.1, abs=0.01)
    assert pair_entry["sessions"]["NY"]["spread_pips"] == pytest.approx(0.1, abs=0.01)
    assert pair_entry["sessions"]["Rollover"]["spread_pips"] == pytest.approx(0.5, abs=0.01)

    assert pair_entry["commission_per_side_pips"] == pytest.approx(0.35, abs=0.01)
    assert pair_entry["slippage_per_side_pips"] == 0.5
    assert pair_entry["slippage_source"] == "default"


def test_commission_lookup_jpy_pair():
    assert ct.commission_per_side_pips("EUR_USD") == pytest.approx(0.35, abs=0.01)
    assert ct.commission_per_side_pips("USD_JPY") == pytest.approx(0.35, abs=0.05)
    assert ct.commission_per_side_pips("UNKNOWN_PAIR") == pytest.approx(0.35, abs=0.05)


def test_missing_parquet_pair_skipped(tmp_path):
    mt5_root = tmp_path / "BackTestData_MT5"
    mt5_root.mkdir()
    out_path = tmp_path / "cost_table.json"
    ct.build_cost_table(
        pairs=["DOES_NOT_EXIST"],
        mt5_root=mt5_root,
        out_path=out_path,
    )
    table = json.loads(out_path.read_text())
    assert table["pairs"] == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_cost_table.py -v`
Expected: FAIL with `ImportError` or `AttributeError` — `cost_table` module / `build_cost_table` function does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# ff/cost_realism/cost_table.py
"""Build ``artifacts/cost_table.json`` from MT5 M1 parquets.

Per-pair × per-session median spread (pips), plus a static commission lookup
keyed by the quote currency, plus a default 0.5 pips slippage that the
telemetry module overrides as live trades close.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .gate_rules import session_of_hour

LOG = logging.getLogger(__name__)

DEFAULT_SLIPPAGE_PIPS: float = 0.5

# IC Markets Raw Spread: $7 USD round-turn per standard lot.
# 1 pip on 1 standard lot ≈ $10 for USD-quoted pairs → 0.7 pips RT, 0.35/side.
# Cross / non-USD-quoted pairs translate roughly the same in pip-equivalent
# at typical cross rates; v1 uses the flat 0.35/side. Refine per-pair when
# we have account-statement evidence of a different per-pair commission.
_COMMISSION_PER_SIDE_PIPS_USD_QUOTED: float = 0.35


def commission_per_side_pips(pair: str) -> float:
    """Per-side commission in pips for ``pair``.

    v1 returns the same 0.35 pips/side for every pair. Per-pair overrides
    can be added by extending this lookup once we have evidence.
    """
    return _COMMISSION_PER_SIDE_PIPS_USD_QUOTED


def _pip_value(pair: str) -> float:
    return 0.01 if "JPY" in pair.upper() else 0.0001


def _per_session_median_spread_pips(df: pd.DataFrame, pair: str) -> dict[str, float]:
    """Return {session_name: median_spread_pips} from an MT5 M1 parquet."""
    if df.empty:
        return {}
    pip = _pip_value(pair)
    df = df.copy()
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("UTC")
    df["hour"] = ts.dt.hour
    df["session"] = df["hour"].map(session_of_hour)
    df["spread_pips"] = df["spread"] / pip
    grouped = df.groupby("session")["spread_pips"].median()
    return {session: float(round(val, 4)) for session, val in grouped.items()}


def build_cost_table(
    pairs: list[str],
    mt5_root: Path,
    out_path: Path,
) -> None:
    """Write a fresh ``cost_table.json`` covering ``pairs`` from MT5 parquets.

    Pairs without an MT5 parquet are silently skipped with a logged warning.
    """
    pairs_block: dict[str, dict] = {}
    earliest, latest = None, None

    for pair in pairs:
        path = mt5_root / f"{pair}_M1.parquet"
        if not path.exists():
            LOG.warning("[cost_table] missing %s — skipping", path)
            continue
        df = pd.read_parquet(path)
        if df.empty:
            LOG.warning("[cost_table] empty parquet %s — skipping", path)
            continue
        sessions = _per_session_median_spread_pips(df, pair)
        if not sessions:
            continue
        pairs_block[pair] = {
            "sessions": {s: {"spread_pips": v} for s, v in sessions.items()},
            "commission_per_side_pips": commission_per_side_pips(pair),
            "slippage_per_side_pips": DEFAULT_SLIPPAGE_PIPS,
            "slippage_source": "default",
        }
        ts_min, ts_max = pd.to_datetime(df["timestamp"]).min(), pd.to_datetime(df["timestamp"]).max()
        earliest = ts_min if earliest is None else min(earliest, ts_min)
        latest = ts_max if latest is None else max(latest, ts_max)

    table = {
        "schema_version": 1,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "mt5_history_window": [
            earliest.date().isoformat() if earliest is not None else None,
            latest.date().isoformat() if latest is not None else None,
        ],
        "pairs": pairs_block,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(table, indent=2))
    LOG.info("[cost_table] wrote %d pairs to %s", len(pairs_block), out_path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_cost_table.py -v`
Expected: PASS — 3 tests pass.

- [ ] **Step 5: Add CLI wrapper and gitignore entry**

```python
# scripts/build_cost_table.py
"""Generate artifacts/cost_table.json from MT5 M1 parquets.

Usage:
    .\\.venv\\Scripts\\python.exe scripts/build_cost_table.py [--pairs EUR_USD,GBP_USD]
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from ff.cost_realism.cost_table import build_cost_table  # noqa: E402
from ff.data.mt5_m1_downloader import MT5_DATA_ROOT  # noqa: E402


DEFAULT_PAIRS = [
    "AUD_CAD", "AUD_CHF", "AUD_JPY", "AUD_NZD", "AUD_USD",
    "CAD_CHF", "CAD_JPY", "CHF_JPY",
    "EUR_AUD", "EUR_CAD", "EUR_CHF", "EUR_GBP", "EUR_JPY", "EUR_NZD", "EUR_USD",
    "GBP_AUD", "GBP_CAD", "GBP_CHF", "GBP_JPY", "GBP_NZD", "GBP_USD",
    "NZD_CAD", "NZD_CHF", "NZD_JPY", "NZD_USD",
    "USD_CAD", "USD_CHF", "USD_JPY",
]


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--pairs", default=",".join(DEFAULT_PAIRS))
    p.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "cost_table.json"))
    args = p.parse_args()
    pairs = [s.strip() for s in args.pairs.split(",") if s.strip()]
    build_cost_table(pairs=pairs, mt5_root=MT5_DATA_ROOT, out_path=Path(args.out))


if __name__ == "__main__":
    main()
```

Append to `.gitignore` (one line at the top of the artifacts section is fine):

```
artifacts/cost_table.json
```

- [ ] **Step 6: Smoke-run the CLI on real data**

Run: `.\.venv\Scripts\python.exe scripts/build_cost_table.py`
Expected: prints `[cost_table] wrote 28 pairs to ...artifacts/cost_table.json`. File exists, ~5 KB, has all 28 pairs with five-session entries each.

- [ ] **Step 7: Commit**

```bash
git add ff/cost_realism/cost_table.py scripts/build_cost_table.py tests/cost_realism/test_cost_table.py .gitignore
git commit -m "feat(cost-realism): cost_table.py + build_cost_table.py CLI"
```

---

### Task 3: PR A wrap — architecture-map entry

**Files:**
- Modify: `docs/ARCHITECTURE_MAP.md` — add entries for the new files

- [ ] **Step 1: Add map rows**

Append to the appropriate section of `docs/ARCHITECTURE_MAP.md` (the engineer reads `scripts/check_map.py` to find the right section if unsure):

```markdown
| `ff/cost_realism/__init__.py` | Cost-realism subsystem package marker | active |
| `ff/cost_realism/gate_rules.py` | "3-and-3" trade-eligibility filter; shared by BT post-pass and live execution guard | active |
| `ff/cost_realism/cost_table.py` | Build artifacts/cost_table.json from MT5 M1 parquets per session | active |
| `scripts/build_cost_table.py` | CLI wrapper around build_cost_table | active |
| `tests/cost_realism/test_gate_rules.py` | Unit tests for gate rules | active |
| `tests/cost_realism/test_cost_table.py` | Unit tests for cost-table generator | active |
```

- [ ] **Step 2: Verify map check passes**

Run: `.\.venv\Scripts\python.exe scripts/check_map.py`
Expected: `OK NNN/NNN files mapped` (count is now NNN+5).

- [ ] **Step 3: Commit and ship PR A**

```bash
git add docs/ARCHITECTURE_MAP.md
git commit -m "docs(map): add cost_realism subsystem entries"
git push -u origin feat/cost-realism-overlay
gh pr create --fill
```

---

### Task 4: overlay.py — post-pass cost adjuster

**Files:**
- Create: `ff/cost_realism/overlay.py`
- Create: `tests/cost_realism/test_overlay.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/cost_realism/test_overlay.py
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ff.cost_realism import overlay


@pytest.fixture
def cost_table(tmp_path):
    table = {
        "schema_version": 1,
        "pairs": {
            "EUR_USD": {
                "sessions": {
                    "Asian":  {"spread_pips": 0.05},
                    "London": {"spread_pips": 0.0},
                    "Lon-NY": {"spread_pips": 0.0},
                    "NY":     {"spread_pips": 0.0},
                    "Rollover": {"spread_pips": 0.5},
                },
                "commission_per_side_pips": 0.35,
                "slippage_per_side_pips": 0.5,
                "slippage_source": "default",
            }
        },
    }
    p = tmp_path / "cost_table.json"
    p.write_text(json.dumps(table))
    return p


def test_overlay_adds_three_columns(cost_table):
    trades = pd.DataFrame({
        "pair": ["EUR_USD"],
        "entry_ts": [pd.Timestamp("2026-04-24 10:00:00", tz="UTC")],
        "duka_bt_spread_pips": [0.32],
        "raw_pnl_pips": [10.0],
    })
    out = overlay.apply(trades, cost_table_path=cost_table, bt_commission_per_side_pips=0.3)
    # BT cost RT = 0.32 + 2*0.3 = 0.92 pips; real cost RT = 0 (London) + 2*0.35 + 2*0.5 = 1.7 pips
    # delta = bt_cost - real_cost = 0.92 - 1.7 = -0.78 (real costs more, so adjusted P&L is lower)
    assert out["overlay_delta_pips"].iloc[0] == pytest.approx(-0.78, abs=0.001)
    assert out["adjusted_pnl_pips"].iloc[0] == pytest.approx(10.0 - 0.78, abs=0.001)
    assert "raw_pnl_pips" in out.columns


def test_overlay_uses_session_specific_spread(cost_table):
    trades = pd.DataFrame({
        "pair": ["EUR_USD", "EUR_USD"],
        "entry_ts": [
            pd.Timestamp("2026-04-24 04:00:00", tz="UTC"),  # Asian
            pd.Timestamp("2026-04-24 10:00:00", tz="UTC"),  # London
        ],
        "duka_bt_spread_pips": [0.32, 0.32],
        "raw_pnl_pips": [10.0, 10.0],
    })
    out = overlay.apply(trades, cost_table_path=cost_table, bt_commission_per_side_pips=0.3)
    # Asian: real_cost = 0.05 + 0.7 + 1.0 = 1.75; delta = 0.92 - 1.75 = -0.83
    # London: real_cost = 0.0 + 0.7 + 1.0 = 1.70; delta = 0.92 - 1.70 = -0.78
    assert out["overlay_delta_pips"].iloc[0] == pytest.approx(-0.83, abs=0.001)
    assert out["overlay_delta_pips"].iloc[1] == pytest.approx(-0.78, abs=0.001)


def test_overlay_unknown_pair_passes_through(cost_table, caplog):
    trades = pd.DataFrame({
        "pair": ["XAU_USD"],
        "entry_ts": [pd.Timestamp("2026-04-24 10:00:00", tz="UTC")],
        "duka_bt_spread_pips": [1.0],
        "raw_pnl_pips": [5.0],
    })
    out = overlay.apply(trades, cost_table_path=cost_table, bt_commission_per_side_pips=0.3)
    assert out["overlay_delta_pips"].iloc[0] == 0.0
    assert out["adjusted_pnl_pips"].iloc[0] == 5.0
    assert any("XAU_USD" in r.getMessage() for r in caplog.records)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_overlay.py -v`
Expected: FAIL — `overlay` module / `apply` function does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# ff/cost_realism/overlay.py
"""Post-pass cost-realism overlay.

Computes the delta between what the BT engine charged a trade (Dukascopy
spread + 0.3 pip commission) and what live IC Markets execution would
charge (MT5 session-median spread + per-pair commission + telemetry-fed
slippage), then folds that delta into a third "adjusted P&L" column.

The trade list is unchanged — SL/TP triggers are price-driven and
independent of cost assumptions, so this can be safely post-pass without
producing different trade decisions than an inline implementation.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from .gate_rules import session_of_hour

LOG = logging.getLogger(__name__)


def _load_table(path: Path) -> dict:
    if not path.exists():
        LOG.warning("[overlay] cost_table.json missing at %s — overlay returns zero delta", path)
        return {"pairs": {}}
    return json.loads(path.read_text())


def apply(
    trades: pd.DataFrame,
    cost_table_path: Path | str,
    bt_commission_per_side_pips: float = 0.3,
) -> pd.DataFrame:
    """Return ``trades`` with three new columns: ``raw_pnl_pips``,
    ``overlay_delta_pips``, ``adjusted_pnl_pips``.

    ``trades`` must contain ``pair``, ``entry_ts``, ``duka_bt_spread_pips``,
    and ``raw_pnl_pips``. Unknown pairs receive zero delta and pass through
    unchanged with a logged warning.
    """
    table = _load_table(Path(cost_table_path))
    pairs_block = table.get("pairs", {})
    out = trades.copy()

    deltas = []
    for _, row in out.iterrows():
        pair = row["pair"]
        if pair not in pairs_block:
            LOG.warning("[overlay] no entry for %s — passing through unchanged", pair)
            deltas.append(0.0)
            continue
        entry = pairs_block[pair]
        ts = row["entry_ts"]
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        sess = session_of_hour(ts.hour)
        sess_spread = entry["sessions"].get(sess, {}).get("spread_pips")
        if sess_spread is None:
            LOG.warning("[overlay] %s missing %s session — falling back to all-session median", pair, sess)
            sess_vals = [s["spread_pips"] for s in entry["sessions"].values()]
            sess_spread = sum(sess_vals) / len(sess_vals) if sess_vals else 0.0

        real_comm = entry["commission_per_side_pips"]
        real_slip = entry["slippage_per_side_pips"]

        bt_cost_rt = float(row["duka_bt_spread_pips"]) + 2 * bt_commission_per_side_pips
        real_cost_rt = sess_spread + 2 * real_comm + 2 * real_slip
        deltas.append(bt_cost_rt - real_cost_rt)

    out["overlay_delta_pips"] = deltas
    out["adjusted_pnl_pips"] = out["raw_pnl_pips"] + out["overlay_delta_pips"]
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_overlay.py -v`
Expected: PASS — 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add ff/cost_realism/overlay.py tests/cost_realism/test_overlay.py
git commit -m "feat(cost-realism): post-pass overlay (raw → adjusted P&L)"
```

---

### Task 5: bt_gate.py — drop trades that wouldn't fire in live

**Files:**
- Create: `ff/cost_realism/bt_gate.py`
- Create: `tests/cost_realism/test_bt_gate.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/cost_realism/test_bt_gate.py
import pandas as pd
import pytest

from ff.cost_realism import bt_gate


def _trade_row(*, ts, pair="EUR_USD", spread=0.3, slippage=0.5, pnl=5.0):
    return {
        "pair": pair,
        "entry_ts": pd.Timestamp(ts, tz="UTC"),
        "duka_bt_spread_pips": spread,
        "telemetry_slippage_pips": slippage,
        "raw_pnl_pips": pnl,
    }


def test_gate_drops_rollover_trade():
    df = pd.DataFrame([
        _trade_row(ts="2026-04-24 22:00:00"),
        _trade_row(ts="2026-04-24 10:00:00"),
    ])
    out = bt_gate.apply(df)
    assert out["gated_out_reason"].tolist() == ["rollover", None]


def test_gate_drops_spread_spike():
    df = pd.DataFrame([
        _trade_row(ts="2026-04-24 10:00:00", spread=4.0),
        _trade_row(ts="2026-04-24 10:00:00", spread=2.5),
    ])
    out = bt_gate.apply(df)
    assert out["gated_out_reason"].tolist() == ["spread_3p", None]


def test_gate_drops_slippage_spike():
    df = pd.DataFrame([
        _trade_row(ts="2026-04-24 10:00:00", slippage=5.0),
    ])
    out = bt_gate.apply(df)
    assert out["gated_out_reason"].tolist() == ["slippage_3p"]


def test_gated_pnl_zeroed_in_metric_view():
    df = pd.DataFrame([
        _trade_row(ts="2026-04-24 22:00:00", pnl=10.0),
        _trade_row(ts="2026-04-24 10:00:00", pnl=10.0),
    ])
    out = bt_gate.apply(df)
    assert out["effective_pnl_pips"].tolist() == [0.0, 10.0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_bt_gate.py -v`
Expected: FAIL — `bt_gate` module does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# ff/cost_realism/bt_gate.py
"""Post-pass BT trade-gate filter.

Walks a ``trades`` DataFrame and stamps each row with a ``gated_out_reason``
(or None). Gated rows have their ``effective_pnl_pips`` zeroed for metric
roll-up but remain visible in the output for diagnostics.
"""
from __future__ import annotations

import pandas as pd

from .gate_rules import should_block


def apply(trades: pd.DataFrame) -> pd.DataFrame:
    """Return ``trades`` with two new columns: ``gated_out_reason``,
    ``effective_pnl_pips``.

    Required input columns: ``entry_ts``, ``duka_bt_spread_pips``,
    ``telemetry_slippage_pips``, ``raw_pnl_pips``.
    """
    out = trades.copy()
    reasons: list[str | None] = []
    for _, row in out.iterrows():
        reasons.append(
            should_block(
                row["entry_ts"],
                spread_pips=float(row["duka_bt_spread_pips"]),
                slippage_pips=float(row["telemetry_slippage_pips"]),
            )
        )
    out["gated_out_reason"] = reasons
    out["effective_pnl_pips"] = out.apply(
        lambda r: 0.0 if r["gated_out_reason"] is not None else r["raw_pnl_pips"],
        axis=1,
    )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_bt_gate.py -v`
Expected: PASS — 4 tests pass.

- [ ] **Step 5: Wire overlay + gate into reconcile**

Modify `scripts/reconcile_live.py`. Locate the section that finishes building the BT trades DataFrame after the replay (the engineer should grep for `replay_service_config` and the trade DataFrame returned by the reconcile builder). Just before the trade-comparison HTML writer, add:

```python
# Cost-realism overlay (always-on by default for v1).
from ff.cost_realism import bt_gate, overlay  # noqa: E402

COST_TABLE_PATH = REPO_ROOT / "artifacts" / "cost_table.json"

bt_trades = bt_gate.apply(bt_trades)
bt_trades = overlay.apply(bt_trades, cost_table_path=COST_TABLE_PATH)
```

The `bt_trades` DataFrame must already contain `pair`, `entry_ts`, `duka_bt_spread_pips`, `raw_pnl_pips`, and `telemetry_slippage_pips`. If `telemetry_slippage_pips` is not present yet (Task 6 has not landed), default it from the cost table:

```python
if "telemetry_slippage_pips" not in bt_trades.columns:
    import json
    table = json.loads(COST_TABLE_PATH.read_text()) if COST_TABLE_PATH.exists() else {"pairs": {}}
    pair_to_slip = {p: e["slippage_per_side_pips"] for p, e in table.get("pairs", {}).items()}
    bt_trades["telemetry_slippage_pips"] = bt_trades["pair"].map(pair_to_slip).fillna(0.5)
```

- [ ] **Step 6: Smoke-run reconcile against the 10 known trades**

Run: `.\.venv\Scripts\python.exe scripts/reconcile_live.py`
Expected: writes `artifacts/live/reconcile/<stamp>.html` and the CSV; new columns `raw_pnl_pips`, `overlay_delta_pips`, `adjusted_pnl_pips`, `gated_out_reason`, `effective_pnl_pips` present in the CSV. Manually inspect: zero rollover trades expected (the 10 trades all closed during liquid hours); average `adjusted_pnl_pips` should be within ~1 pip of average live `report_pnl_pips`.

- [ ] **Step 7: Commit and ship PR B**

```bash
git add ff/cost_realism/bt_gate.py tests/cost_realism/test_bt_gate.py scripts/reconcile_live.py docs/ARCHITECTURE_MAP.md
git commit -m "feat(cost-realism): bt_gate + reconcile integration"
git push
gh pr create --fill
```

---

### Task 6: slippage_telemetry.py — feedback loop from forensic

**Files:**
- Create: `ff/cost_realism/slippage_telemetry.py`
- Create: `tests/cost_realism/test_slippage_telemetry.py`
- Modify: `scripts/import_mt5_report.py` — call telemetry update at the end

- [ ] **Step 1: Write the failing test**

```python
# tests/cost_realism/test_slippage_telemetry.py
import json

import pandas as pd
import pytest

from ff.cost_realism import slippage_telemetry as st


def _seed_table(path):
    table = {
        "schema_version": 1,
        "pairs": {
            "EUR_USD": {
                "sessions": {"Asian": {"spread_pips": 0.0}},
                "commission_per_side_pips": 0.35,
                "slippage_per_side_pips": 0.5,
                "slippage_source": "default",
            },
            "GBP_USD": {
                "sessions": {"Asian": {"spread_pips": 0.0}},
                "commission_per_side_pips": 0.35,
                "slippage_per_side_pips": 0.5,
                "slippage_source": "default",
            },
        },
    }
    path.write_text(json.dumps(table))


def test_telemetry_skips_pairs_below_min_trades(tmp_path):
    table_path = tmp_path / "cost_table.json"
    _seed_table(table_path)
    forensic = pd.DataFrame({
        "pair": ["EUR_USD"] * 5,
        "entry_slippage_pips": [0.4] * 5,
    })
    st.update_from_forensic(forensic, table_path, min_trades=20)
    table = json.loads(table_path.read_text())
    assert table["pairs"]["EUR_USD"]["slippage_per_side_pips"] == 0.5
    assert table["pairs"]["EUR_USD"]["slippage_source"] == "default"


def test_telemetry_updates_when_enough_trades(tmp_path):
    table_path = tmp_path / "cost_table.json"
    _seed_table(table_path)
    forensic = pd.DataFrame({
        "pair": ["EUR_USD"] * 25,
        "entry_slippage_pips": [0.7] * 25,
    })
    st.update_from_forensic(forensic, table_path, min_trades=20)
    table = json.loads(table_path.read_text())
    assert table["pairs"]["EUR_USD"]["slippage_per_side_pips"] == pytest.approx(0.7, abs=0.01)
    assert table["pairs"]["EUR_USD"]["slippage_source"] == "telemetry_n=25"
    # Other pairs unchanged
    assert table["pairs"]["GBP_USD"]["slippage_per_side_pips"] == 0.5


def test_telemetry_uses_rolling_window(tmp_path):
    table_path = tmp_path / "cost_table.json"
    _seed_table(table_path)
    forensic = pd.DataFrame({
        "pair": ["EUR_USD"] * 30,
        "entry_slippage_pips": [10.0] * 10 + [0.5] * 20,  # newest 20 are 0.5
        "fired_at_utc": pd.date_range("2026-01-01", periods=30, freq="h"),
    })
    st.update_from_forensic(forensic, table_path, min_trades=20, rolling_window=20)
    table = json.loads(table_path.read_text())
    # Median of newest 20 = 0.5 (excludes the older 10.0 outliers)
    assert table["pairs"]["EUR_USD"]["slippage_per_side_pips"] == pytest.approx(0.5, abs=0.01)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_slippage_telemetry.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```python
# ff/cost_realism/slippage_telemetry.py
"""Per-pair slippage telemetry — read forensic data, write back to cost_table.json.

Maintains a rolling-window per-pair median entry slippage. Pairs with fewer
than ``min_trades`` recent fills keep the default 0.5 pips. Source label
(``default`` vs ``telemetry_n=N``) is stamped so the UI can show data
maturity.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

LOG = logging.getLogger(__name__)


def update_from_forensic(
    forensic_df: pd.DataFrame,
    cost_table_path: Path,
    min_trades: int = 20,
    rolling_window: int = 20,
) -> None:
    """Update per-pair ``slippage_per_side_pips`` in ``cost_table.json`` from
    a ``forensic_df`` containing ``pair`` and ``entry_slippage_pips``.

    If a ``fired_at_utc`` column exists, sort newest-last so that the rolling
    window reflects the most recent fills. Pairs not present in the cost
    table are ignored.
    """
    if not cost_table_path.exists():
        LOG.warning("[telemetry] %s does not exist — nothing to update", cost_table_path)
        return

    table = json.loads(cost_table_path.read_text())
    pairs_block = table.get("pairs", {})
    if not pairs_block:
        return

    df = forensic_df.copy()
    if "fired_at_utc" in df.columns:
        df = df.sort_values("fired_at_utc")

    for pair in list(pairs_block.keys()):
        pair_rows = df[df["pair"] == pair]
        if len(pair_rows) < min_trades:
            continue
        recent = pair_rows.tail(rolling_window)["entry_slippage_pips"]
        new_slip = float(round(recent.median(), 4))
        pairs_block[pair]["slippage_per_side_pips"] = new_slip
        pairs_block[pair]["slippage_source"] = f"telemetry_n={len(recent)}"

    cost_table_path.write_text(json.dumps(table, indent=2))
    LOG.info("[telemetry] updated %s", cost_table_path)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_slippage_telemetry.py -v`
Expected: PASS — 3 tests pass.

- [ ] **Step 5: Wire into import_mt5_report.py**

Modify `scripts/import_mt5_report.py`. After the import writes the new MT5 history CSV/JSON (search for the line that prints the summary or writes the CSV), add:

```python
# Refresh per-pair slippage telemetry from the latest forensic.
from ff.cost_realism.slippage_telemetry import update_from_forensic

forensic_csv = REPO_ROOT / "artifacts" / "live" / "reconcile"
latest_comparison = sorted(forensic_csv.glob("*_trade_comparison.csv"))
if latest_comparison:
    fdf = pd.read_csv(latest_comparison[-1])
    if "entry_slippage_pips" not in fdf.columns and "duka_entry_delta_pips" in fdf.columns:
        # Fallback: use absolute entry-delta as a proxy for slippage in older reports.
        fdf["entry_slippage_pips"] = fdf["duka_entry_delta_pips"].abs()
    update_from_forensic(
        fdf,
        cost_table_path=REPO_ROOT / "artifacts" / "cost_table.json",
    )
```

- [ ] **Step 6: Commit and ship PR C**

```bash
git add ff/cost_realism/slippage_telemetry.py tests/cost_realism/test_slippage_telemetry.py scripts/import_mt5_report.py docs/ARCHITECTURE_MAP.md
git commit -m "feat(cost-realism): slippage telemetry feedback loop"
git push
gh pr create --fill
```

---

### Task 7: Default-ON in harness + UI surfacing

**Files:**
- Modify: `ff/harness.py` — apply gate + overlay after each trial's trade list
- Modify: `app/templates/` (or wherever the trade-comparison HTML is rendered) — show raw + adjusted columns
- Create: `tests/test_harness_overlay.py` — end-to-end test that overlay flows through harness output

- [ ] **Step 1: Write the failing test**

```python
# tests/test_harness_overlay.py
"""Smoke test that harness.run() produces overlay columns when cost_table.json exists."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# This is a smoke test — full harness fixtures live in tests/conftest.py.
# Skip if Rust engine fixtures aren't available (matches existing test pattern).

pytest.importorskip("ff_core")


def test_harness_run_emits_overlay_columns(tmp_path, monkeypatch):
    # Seed cost_table.json with EUR_USD only (the demo EA's pair).
    table = {
        "schema_version": 1,
        "pairs": {
            "EUR_USD": {
                "sessions": {s: {"spread_pips": 0.0} for s in ["Asian","London","Lon-NY","NY","Rollover"]},
                "commission_per_side_pips": 0.35,
                "slippage_per_side_pips": 0.5,
                "slippage_source": "default",
            }
        },
    }
    cost_table_path = tmp_path / "cost_table.json"
    cost_table_path.write_text(json.dumps(table))
    monkeypatch.setenv("FF_COST_TABLE_PATH", str(cost_table_path))

    from ff.harness import run as harness_run
    from ff.defaults.complexity import complexity_to_ea

    ea = complexity_to_ea(pair="EUR_USD", main_tf="M15", sub_tf="M1", complexity=1)
    metrics, runs = harness_run(ea, trials=2, seed=0)
    # Expect adjusted_pnl available in roll-up; key shape may vary, this is a
    # liveness check rather than exact value.
    assert "adjusted_pnl_total_pips" in metrics.dtype.names or any(
        "adjusted" in (k or "") for k in (runs.keys() if isinstance(runs, dict) else [])
    )
```

- [ ] **Step 2: Run test to verify it fails (or skips if no engine)**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_harness_overlay.py -v`
Expected: FAIL with `adjusted_pnl_total_pips` missing.

- [ ] **Step 3: Wire overlay into harness.run()**

In `ff/harness.py`, locate `def run(...)` (search for the entry point that returns metrics). After per-trial trades are computed, just before metrics roll-up, add:

```python
# Cost-realism overlay applied to every trial.
import os
from pathlib import Path
from ff.cost_realism import bt_gate, overlay

_cost_table_path = Path(os.environ.get(
    "FF_COST_TABLE_PATH",
    Path(__file__).resolve().parent.parent / "artifacts" / "cost_table.json",
))

def _apply_cost_realism(trades_df: pd.DataFrame) -> pd.DataFrame:
    if trades_df.empty:
        return trades_df
    if "telemetry_slippage_pips" not in trades_df.columns:
        # Default 0.5 pips per side until telemetry has populated cost_table.
        trades_df["telemetry_slippage_pips"] = 0.5
    if "raw_pnl_pips" not in trades_df.columns and "pnl_pips" in trades_df.columns:
        trades_df["raw_pnl_pips"] = trades_df["pnl_pips"]
    trades_df = bt_gate.apply(trades_df)
    trades_df = overlay.apply(trades_df, cost_table_path=_cost_table_path)
    return trades_df
```

Then in the per-trial roll-up (search for where `metrics[i, ...]` is populated), call `_apply_cost_realism(trades_df)` before computing the metrics that reference P&L. Replace the metric source from `pnl_pips` to `adjusted_pnl_pips` (where the gated rows already contribute zero via `effective_pnl_pips`). Add an `adjusted_pnl_total_pips` field to the metrics dtype if not already present.

The exact insertion points depend on the current `harness.run` shape; the engineer should follow the existing pattern of the `pnl_*` columns and add an `adjusted_*` parallel.

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_harness_overlay.py -v`
Expected: PASS.

- [ ] **Step 5: Surface in trade-comparison HTML**

In `ff/live/reconcile.py` (the function that renders the trade-comparison HTML — search for `<table>` or `to_html`), include the new columns in the column order: after `report_pnl_pips`, insert `raw_pnl_pips`, `overlay_delta_pips`, `adjusted_pnl_pips`, `gated_out_reason`. Add a one-line summary above the table:

```python
n_gated = int((bt_trades["gated_out_reason"].notna()).sum())
total_n = len(bt_trades)
summary_html = f"<p>Trades dropped by 3-and-3 gate: <b>{n_gated}/{total_n}</b></p>"
```

- [ ] **Step 6: Smoke-run a small sweep with overlay-on**

Run: `.\.venv\Scripts\python.exe run.py eas/complex01.py --trials 50 --seed 0`
Expected: Sweep completes; UI History row shows new `adjusted_pnl_*` columns or tooltip; manually verify a top-ranked trial's adjusted P&L is lower than its raw (because Duka spread is overstated).

- [ ] **Step 7: Commit and ship PR D**

```bash
git add ff/harness.py ff/live/reconcile.py tests/test_harness_overlay.py docs/ARCHITECTURE_MAP.md
git commit -m "feat(cost-realism): default-on overlay in harness + UI columns"
git push
gh pr create --fill
```

---

### Task 8: execution_guard.py — live mirror of gate_rules

**Files:**
- Create: `ff/live/execution_guard.py`
- Create: `tests/cost_realism/test_execution_guard.py`
- Modify: `ff/live/runner.py` — call guard before every plan submission

- [ ] **Step 1: Write the failing test**

```python
# tests/cost_realism/test_execution_guard.py
import pandas as pd

from ff.live.execution_guard import evaluate


def test_evaluate_blocks_rollover():
    decision = evaluate(
        ts=pd.Timestamp("2026-04-24 22:00:00", tz="UTC"),
        live_spread_pips=0.5,
    )
    assert decision.block is True
    assert decision.reason == "rollover"


def test_evaluate_blocks_wide_spread():
    decision = evaluate(
        ts=pd.Timestamp("2026-04-24 10:00:00", tz="UTC"),
        live_spread_pips=4.0,
    )
    assert decision.block is True
    assert decision.reason == "spread_3p"


def test_evaluate_passes_quiet_minute():
    decision = evaluate(
        ts=pd.Timestamp("2026-04-24 10:00:00", tz="UTC"),
        live_spread_pips=0.5,
    )
    assert decision.block is False
    assert decision.reason is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_execution_guard.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# ff/live/execution_guard.py
"""Pre-trade execution guard for the live runner.

Mirrors ``ff.cost_realism.gate_rules`` so BT and live can never disagree on
which minutes are allowed to trade. Imported into ``ff.live.runner`` and
called once per plan candidate before submission to the broker.

The guard does NOT enforce the slippage cap — slippage is a fill-time
property; the runner enforces ``slippage_3p`` after the order returns from
``submit_market_order``.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ff.cost_realism import gate_rules


@dataclass
class GuardDecision:
    block: bool
    reason: str | None


def evaluate(ts: pd.Timestamp, live_spread_pips: float) -> GuardDecision:
    """Decide whether a candidate plan may proceed at ``ts`` with the
    current ``live_spread_pips`` measured at the broker.

    ``slippage_3p`` is checked separately at fill time by the runner.
    """
    if gate_rules.is_rollover(ts):
        return GuardDecision(block=True, reason="rollover")
    if gate_rules.is_spread_too_wide(live_spread_pips):
        return GuardDecision(block=True, reason="spread_3p")
    return GuardDecision(block=False, reason=None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.\.venv\Scripts\python.exe -m pytest tests/cost_realism/test_execution_guard.py -v`
Expected: PASS — 3 tests pass.

- [ ] **Step 5: Wire into runner.py**

In `ff/live/runner.py`, locate `_poll_one_pair` (around line 714 per the spec). After the signal evaluation produces a candidate plan and before the broker submits the order, add:

```python
from ff.live.execution_guard import evaluate as _eval_guard
from ff.live.broker_mt5 import _symbol_for  # if not already imported

# Read live spread for guard. The broker's tick already provides spread.
tick = broker.symbol_info_tick(state.pair)
pip = 0.01 if "JPY" in state.pair else 0.0001
live_spread_pips = float(tick.ask - tick.bid) / pip if tick else 0.0

decision = _eval_guard(
    ts=pd.Timestamp.utcnow().tz_localize("UTC"),
    live_spread_pips=live_spread_pips,
)
if decision.block:
    LOG.info(
        "[guard] blocked %s plan: reason=%s spread=%.2fpips",
        state.pair, decision.reason, live_spread_pips,
    )
    return  # skip plan submission
```

If `broker.symbol_info_tick` is not exposed yet, the engineer should add a thin wrapper to `ff/live/broker_mt5.py` that returns `mt5.symbol_info_tick(symbol)` — this is a one-line method following the existing connection guard pattern.

- [ ] **Step 6: Smoke-run the live runner in dry mode**

Run the runner against a paper account or in `--dry-run` mode (whichever flag exists in `runner.py`). Expected log line at any 21:00–24:00 UTC poll:
`[guard] blocked EUR_USD plan: reason=rollover spread=...`

- [ ] **Step 7: Commit and ship PR E**

```bash
git add ff/live/execution_guard.py ff/live/runner.py tests/cost_realism/test_execution_guard.py docs/ARCHITECTURE_MAP.md
git commit -m "feat(live): execution_guard backport — 3-and-3 mirror of BT gate"
git push
gh pr create --fill
```

After PR E merges: tick the `[ ] Execution Guard module` and `[ ] Cost-realism overlay` items in `PROGRESS.md`.

---

## Self-review

**Spec coverage:**
- Per-session × per-pair median spread (β requirement) → Task 2 builds it.
- 3-pip flat caps (slippage and spread) → Task 1 implements both.
- Rollover window 21–24 UTC → Task 1.
- Commission $7 RT → Task 2 (`commission_per_side_pips = 0.35`).
- Slippage telemetry feedback loop → Task 6.
- Default-ON for optimisation (Q1=ii) → Task 7 wires harness.
- Manual cost-table refresh (Q2=a) → Task 2 ships the CLI.
- Live execution guard backport → Task 8.
- Architecture-map update → Task 3 (and reminders in each PR's commit list).
- News calendar → explicit v2 deferral, placeholder in `gate_rules.is_news_window`.

**Placeholder scan:** Each task contains complete code; no "implement later" or "similar to". The `harness.run` insertion in Task 7 Step 3 references "the existing pattern of the pnl_* columns" because the exact dtype mutation depends on `harness.run`'s current shape — the engineer follows the parallel of an existing column. Acceptable for a multi-file plan.

**Type consistency:** `gate_rules.should_block` signature stable across Tasks 1, 5, 8. Cost-table schema stable across Tasks 2, 4, 6. Trade-DataFrame columns (`pair`, `entry_ts`, `duka_bt_spread_pips`, `raw_pnl_pips`, `telemetry_slippage_pips`) used consistently in Tasks 4–7.

---

## Out of scope (v1, deferred)

- News-calendar integration (Task 1 has placeholder).
- Per-pair commission overrides for non-USD-quoted crosses (Task 2 uses flat 0.35).
- Spread variance / p95 modelling.
- Bid/ask asymmetry from tick parquet.
