"""Parquet file inventory for the Fire Forex Data tab.

Scans the known data roots, reads parquet *headers only* (no full load) and
surfaces rows / date-range / file-size / mtime plus a coarse OK / WARN / FAIL
status. Results are cached to ``artifacts/data_inventory.json`` with a 1-hour
TTL; hitting ``scan(force=True)`` or the Rescan button rebuilds it.
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

ROOTS: tuple[Path, ...] = (
    Path(r"G:\My Drive\BackTestData"),
    Path(r"C:\Users\ROG\Projects\ForexPipeline\data"),
)

_TF_ORDER = {"M1": 0, "M5": 1, "M15": 2, "M30": 3, "H1": 4, "H4": 5, "D": 6, "W": 7}

# Cache file lives under artifacts/ alongside volatility_cache.json.
_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "artifacts" / "data_inventory.json"
_CACHE_TTL_SECONDS = 3600


def _iter_parquet_files(roots: Iterable[Path]) -> Iterable[Path]:
    for root in roots:
        if not root.exists():
            continue
        for p in root.glob("*.parquet"):
            yield p


def _parse_name(stem: str) -> tuple[str, str] | None:
    """``EUR_USD_H1`` → ``("EUR_USD", "H1")``. Returns None on bad names."""
    parts = stem.split("_")
    if len(parts) < 3:
        return None
    return "_".join(parts[:-1]), parts[-1]


def _read_metadata(path: Path) -> dict[str, Any]:
    """Return header-only info: bars, start_ts, end_ts, has_spread, has_volume.

    Reads the parquet *metadata* block plus a tiny 1-row sample for the two
    endpoints. Never loads the full file. Any IO error is swallowed and
    reported as ``status="error"``.
    """
    import pyarrow.parquet as pq
    import pandas as pd

    info: dict[str, Any] = {"bars": 0, "start_ts": None, "end_ts": None,
                            "has_spread": False, "has_volume": False}
    pf = pq.ParquetFile(path)
    meta = pf.metadata
    info["bars"] = int(meta.num_rows)

    schema_names = [n.lower() for n in pf.schema_arrow.names]
    info["has_spread"] = "spread" in schema_names
    info["has_volume"] = "volume" in schema_names

    if "timestamp" not in schema_names or meta.num_rows == 0:
        return info

    # Read only timestamp to find min/max; Parquet is columnar so this is cheap.
    ts_col = pf.read(columns=["timestamp"]).column(0).to_pandas()
    if len(ts_col) == 0:
        return info
    ts_col = pd.to_datetime(ts_col, utc=True, errors="coerce").dropna()
    if len(ts_col) == 0:
        return info
    info["start_ts"] = ts_col.iloc[0].isoformat()
    info["end_ts"] = ts_col.iloc[-1].isoformat()
    return info


def _status_for(info: dict[str, Any]) -> str:
    if info.get("error"):
        return "error"
    bars = info.get("bars", 0)
    if bars == 0:
        return "empty"
    if bars < 500:
        return "thin"
    return "ok"


def scan(force: bool = False, roots: Iterable[Path] | None = None) -> list[dict[str, Any]]:
    """Return one record per parquet file. Cached for 1h unless ``force=True``.

    Record shape::

        {pair, tf, path, bars, start_ts, end_ts, size_bytes, mtime,
         has_spread, has_volume, status}
    """
    if roots is None:
        roots = ROOTS

    if not force:
        cached = _load_cache()
        if cached is not None:
            return cached

    rows: list[dict[str, Any]] = []
    for p in sorted(_iter_parquet_files(roots)):
        parsed = _parse_name(p.stem)
        if not parsed:
            continue
        pair, tf = parsed
        try:
            st = p.stat()
            size = int(st.st_size)
            mtime = float(st.st_mtime)
        except OSError:
            size, mtime = 0, 0.0
        record: dict[str, Any] = {
            "pair": pair,
            "tf": tf,
            "path": str(p),
            "size_bytes": size,
            "mtime": mtime,
        }
        try:
            record.update(_read_metadata(p))
        except Exception as exc:  # pragma: no cover — rare; surfaces in UI
            record.update({"bars": 0, "start_ts": None, "end_ts": None,
                           "has_spread": False, "has_volume": False,
                           "error": f"{type(exc).__name__}: {exc}"})
        record["status"] = _status_for(record)
        rows.append(record)

    rows.sort(key=lambda r: (r["pair"], _TF_ORDER.get(r["tf"], 99)))
    _save_cache(rows)
    return rows


def inventory_by_pair(force: bool = False) -> dict[str, list[str]]:
    """Back-compat adapter: ``{pair: [tfs_sorted]}``.

    Preserves the shape the old ``app/pairs_scan.py::scan_pairs`` returned, so
    `/api/pairs` keeps working unchanged.
    """
    rows = scan(force=force)
    out: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        out[r["pair"]].add(r["tf"])
    return {
        pair: sorted(tfs, key=lambda t: _TF_ORDER.get(t, 99))
        for pair, tfs in sorted(out.items())
    }


def date_range_for(pair: str, tf: str) -> tuple[str | None, str | None]:
    """Return the (start_ts, end_ts) ISO strings for one file, or (None, None)."""
    for r in scan():
        if r["pair"] == pair and r["tf"] == tf:
            return r.get("start_ts"), r.get("end_ts")
    return None, None


def invalidate() -> None:
    """Force the next ``scan()`` call to re-read parquet metadata."""
    try:
        if _CACHE_PATH.exists():
            _CACHE_PATH.unlink()
    except OSError:
        pass


# ── Cache I/O ─────────────────────────────────────────────────────────────

def _load_cache() -> list[dict[str, Any]] | None:
    try:
        if not _CACHE_PATH.exists():
            return None
        age = time.time() - _CACHE_PATH.stat().st_mtime
        if age > _CACHE_TTL_SECONDS:
            return None
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "rows" in data:
            return data["rows"]
        if isinstance(data, list):
            return data
    except Exception:
        return None
    return None


def _save_cache(rows: list[dict[str, Any]]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": time.time(), "rows": rows}
        _CACHE_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        pass
