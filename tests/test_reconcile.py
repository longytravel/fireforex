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


# ── Parity-v2 categories + per-pair rollup ────────────────────────────

def _pairv2_bt(
    pair: str, sig: str, signal_variant_id: int = 42,
    signal_family: str = "ema_cross", spread_entry_pips: float = 0.5,
    exit_reason_name: str = "TP",
) -> dict:
    return {
        "pair": pair, "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_ts": _sig_ts(sig), "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 1.0850, "exit_price": 1.0880, "pnl_pips": 30.0,
        "signal_variant_id": signal_variant_id,
        "signal_family": signal_family,
        "spread_entry_pips": spread_entry_pips,
        "exit_reason_name": exit_reason_name,
    }


def _pairv2_live(
    pair: str, sig: str, signal_variant: int = 42,
    signal_family: str = "ema_cross", spread_pips: float = 0.5,
    slippage_pips: float = 0.2, close_reason: str = "TP",
    pnl_pips: float = 29.9,
) -> dict:
    return {
        "plan_id": f"{pair}_{sig}_+1",
        "pair": pair, "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_price": 1.08505, "exit_price": 1.08795, "pnl_pips": pnl_pips,
        "signal_variant": signal_variant,
        "signal_family": signal_family,
        "spread_pips": spread_pips,
        "slippage_pips": slippage_pips,
        "close_reason": close_reason,
    }


def test_mismatched_signal_variant_flagged():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([_pairv2_bt("EUR_USD", sig, signal_variant_id=10)])
    live = _live([_pairv2_live("EUR_USD", sig, signal_variant=20)])
    rep = reconcile(bt, live)
    assert "mismatched_signal" in rep.matched[0].categories


def test_mismatched_spread_flagged_above_tolerance():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([_pairv2_bt("EUR_USD", sig, spread_entry_pips=0.3)])
    live = _live([_pairv2_live("EUR_USD", sig, spread_pips=2.0)])
    rep = reconcile(bt, live, Tolerances(spread_pips=0.5))
    assert "mismatched_spread" in rep.matched[0].categories


def test_mismatched_slippage_flagged_above_tolerance():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([_pairv2_bt("EUR_USD", sig)])
    live = _live([_pairv2_live("EUR_USD", sig, slippage_pips=5.0)])
    rep = reconcile(bt, live, Tolerances(slippage_pips=1.0))
    assert "mismatched_slippage" in rep.matched[0].categories


def test_mismatched_closure_SL_vs_TP():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([_pairv2_bt("EUR_USD", sig, exit_reason_name="SL")])
    live = _live([_pairv2_live("EUR_USD", sig, close_reason="TP")])
    rep = reconcile(bt, live)
    assert "mismatched_closure" in rep.matched[0].categories


def test_engine_managed_exits_canonicalise_to_other_no_closure_flag():
    """Engine TRAILING vs live EXPERT — both canonicalise to OTHER, no flag."""
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([_pairv2_bt("EUR_USD", sig, exit_reason_name="TRAILING")])
    live = _live([_pairv2_live("EUR_USD", sig, close_reason="EXPERT")])
    rep = reconcile(bt, live)
    assert "mismatched_closure" not in rep.matched[0].categories


def test_by_pair_rollup_across_multiple_pairs():
    sig_a = "2026-04-20T15:00:00Z"
    sig_b = "2026-04-20T16:00:00Z"
    bt = _bt([
        _pairv2_bt("EUR_USD", sig_a),
        _pairv2_bt("USD_JPY", sig_b),
    ])
    live = _live([
        _pairv2_live("EUR_USD", sig_a),
        # USD_JPY live missing → shows up in missing_in_live for USD_JPY.
    ])
    rep = reconcile(bt, live)
    rollup = rep.by_pair()
    assert set(rollup) == {"EUR_USD", "USD_JPY"}
    assert rollup["EUR_USD"]["matched"] == 1
    assert rollup["USD_JPY"]["missing_in_live"] == 1
    assert rollup["EUR_USD"]["delta_pips"] != 0.0  # bt 30 vs live 29.9
