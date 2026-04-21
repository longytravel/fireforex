"""Reconciler fixture tests.

Covers every classification category (matched / missing / extra /
mismatched_entry / mismatched_exit / mismatched_pnl / ±1 bar fuzz) without
MT5 — inputs are hand-built DataFrames so CI has no broker dependency.
"""
from __future__ import annotations

import pandas as pd

from ff.live.reconcile import Tolerances, reconcile, render_report_html, render_report_json


def _bt(rows):
    return pd.DataFrame(rows)


def _live(rows):
    return pd.DataFrame(rows)


def _sig_ts(iso: str) -> pd.Timestamp:
    return pd.Timestamp(iso, tz="UTC")


def test_exact_match_all_categories_within_tolerance():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([{
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_ts": _sig_ts(sig), "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    live = _live([{
        "plan_id": "EUR_USD_" + sig + "_+1",
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_price": 1.08505, "exit_price": 1.08795, "pnl_pips": 29.5,
    }])
    rep = reconcile(bt, live)
    assert len(rep.matched) == 1
    assert rep.matched[0].categories == []
    assert rep.counts.get("matched", 0) == 1


def test_mismatched_entry_price_flagged():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([{
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_ts": _sig_ts(sig), "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    live = _live([{
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_price": 1.0860, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    rep = reconcile(bt, live, Tolerances(entry_price_pips=2.0, exit_price_pips=2.0, pnl_pips=1.0))
    assert "mismatched_entry_price" in rep.matched[0].categories


def test_mismatched_exit_price_flagged():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([{
        "pair": "EUR_USD", "direction": -1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_ts": _sig_ts(sig), "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 1.0850, "exit_price": 1.0820, "pnl_pips": 30.0,
    }])
    live = _live([{
        "pair": "EUR_USD", "direction": -1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_price": 1.0850, "exit_price": 1.0830, "pnl_pips": 20.0,
    }])
    rep = reconcile(bt, live, Tolerances(entry_price_pips=2.0, exit_price_pips=2.0, pnl_pips=1.0))
    cats = rep.matched[0].categories
    assert "mismatched_exit_price" in cats
    assert "mismatched_pnl" in cats


def test_missing_in_live():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([{
        "pair": "EUR_USD", "direction": 1, "signal_bar_ts": _sig_ts(sig),
        "entry_ts": _sig_ts(sig), "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    live = _live([])  # nothing live
    rep = reconcile(bt, live)
    assert rep.counts["missing_in_live"] == 1
    assert rep.counts["extra_in_live"] == 0


def test_extra_in_live():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([])
    live = _live([{
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    rep = reconcile(bt, live)
    assert rep.counts["extra_in_live"] == 1
    assert rep.counts["missing_in_live"] == 0


def test_fuzzy_match_within_one_bar_window():
    """Live signal fires one main-TF bar after backtest — should still match
    with ``signal_bar_window=1`` (default)."""
    bt = _bt([{
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts("2026-04-20T15:00:00Z"),
        "entry_ts": _sig_ts("2026-04-20T15:00:00Z"),
        "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    live = _live([{
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts("2026-04-20T16:00:00Z"),  # +1 H1 bar
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    rep = reconcile(bt, live)
    assert len(rep.matched) == 1
    assert rep.counts["missing_in_live"] == 0


def test_fuzzy_match_rejects_beyond_window():
    bt = _bt([{
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts("2026-04-20T15:00:00Z"),
        "entry_ts": _sig_ts("2026-04-20T15:00:00Z"),
        "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    live = _live([{
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts("2026-04-20T19:00:00Z"),  # +4 H1 bars
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    rep = reconcile(bt, live, Tolerances(signal_bar_window=1))
    assert rep.counts["missing_in_live"] == 1
    assert rep.counts["extra_in_live"] == 1


def test_jpy_pair_pip_scaling():
    """JPY pairs use 0.01 pip unit, not 0.0001."""
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([{
        "pair": "USD_JPY", "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_ts": _sig_ts(sig), "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 150.00, "exit_price": 150.30, "pnl_pips": 30.0,
    }])
    live = _live([{
        "pair": "USD_JPY", "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_price": 150.005, "exit_price": 150.295, "pnl_pips": 29.0,
    }])
    rep = reconcile(bt, live, Tolerances(entry_price_pips=2.0, exit_price_pips=2.0, pnl_pips=1.5))
    # 0.005 JPY / 0.01 = 0.5 pips → within 2 pip tolerance
    assert rep.matched[0].categories == []


def test_render_html_and_json_are_non_empty():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([{
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_ts": _sig_ts(sig), "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    live = _live([{
        "pair": "EUR_USD", "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
    }])
    rep = reconcile(bt, live)
    html = render_report_html(rep)
    assert "Fire Forex" in html
    assert "EUR_USD" in html
    payload = render_report_json(rep)
    assert "matched" in payload
