"""Discover available (pair, timeframe) combinations.

Thin adapter around ``ff.data.inventory`` — kept so existing call sites don't
break. The canonical inventory (including bar counts, date ranges, file sizes,
health status) lives in ``ff/data/inventory.py``.
"""
from __future__ import annotations

from ff.data import inventory as _inv

DATA_ROOTS = _inv.ROOTS


def scan_pairs(roots=None) -> dict[str, list[str]]:
    """Return ``{pair: [tfs_sorted]}``. Empty dict if no data roots exist."""
    if roots is None:
        return _inv.inventory_by_pair()
    # Rare legacy caller with custom roots — do a one-off scan bypassing cache.
    rows = _inv.scan(force=True, roots=roots)
    from collections import defaultdict
    out: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        out[r["pair"]].add(r["tf"])
    _TF_ORDER = {"M1": 0, "M5": 1, "M15": 2, "M30": 3, "H1": 4, "H4": 5, "D": 6, "W": 7}
    return {
        pair: sorted(tfs, key=lambda t: _TF_ORDER.get(t, 99))
        for pair, tfs in sorted(out.items())
    }


def scan_pairs_cached() -> dict[str, list[str]]:
    """Cached pair → TF map. 1-hour TTL via ``ff.data.inventory``."""
    return _inv.inventory_by_pair()


if __name__ == "__main__":  # pragma: no cover
    for pair, tfs in scan_pairs().items():
        print(f"{pair:10s}  {tfs}")
