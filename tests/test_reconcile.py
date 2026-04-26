"""Reconciler fixture tests.

Covers every classification category (matched / missing / extra /
mismatched_entry / mismatched_exit / mismatched_pnl / ±1 bar fuzz) without
MT5 — inputs are hand-built DataFrames so CI has no broker dependency.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from ff.live.reconcile import Tolerances, reconcile, render_report_html, render_report_json


def _bt(rows):
    return pd.DataFrame(rows)


def _live(rows):
    return pd.DataFrame(rows)


def _sig_ts(iso: str) -> pd.Timestamp:
    return pd.Timestamp(iso, tz="UTC")


def test_exact_match_all_categories_within_tolerance():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_ts": _sig_ts(sig),
                "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
                "entry_price": 1.0850,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    live = _live(
        [
            {
                "plan_id": "EUR_USD_" + sig + "_+1",
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_price": 1.08505,
                "exit_price": 1.08795,
                "pnl_pips": 29.5,
            }
        ]
    )
    rep = reconcile(bt, live)
    assert len(rep.matched) == 1
    assert rep.matched[0].categories == []
    assert rep.counts.get("matched", 0) == 1


def test_mismatched_entry_price_flagged():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_ts": _sig_ts(sig),
                "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
                "entry_price": 1.0850,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    live = _live(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_price": 1.0860,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    rep = reconcile(bt, live, Tolerances(entry_price_pips=2.0, exit_price_pips=2.0, pnl_pips=1.0))
    assert "mismatched_entry_price" in rep.matched[0].categories


def test_mismatched_exit_price_flagged():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt(
        [
            {
                "pair": "EUR_USD",
                "direction": -1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_ts": _sig_ts(sig),
                "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
                "entry_price": 1.0850,
                "exit_price": 1.0820,
                "pnl_pips": 30.0,
            }
        ]
    )
    live = _live(
        [
            {
                "pair": "EUR_USD",
                "direction": -1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_price": 1.0850,
                "exit_price": 1.0830,
                "pnl_pips": 20.0,
            }
        ]
    )
    rep = reconcile(bt, live, Tolerances(entry_price_pips=2.0, exit_price_pips=2.0, pnl_pips=1.0))
    cats = rep.matched[0].categories
    assert "mismatched_exit_price" in cats
    assert "mismatched_pnl" in cats


def test_missing_in_live():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_ts": _sig_ts(sig),
                "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
                "entry_price": 1.0850,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    live = _live([])  # nothing live
    rep = reconcile(bt, live)
    assert rep.counts["missing_in_live"] == 1
    assert rep.counts["extra_in_live"] == 0


def test_extra_in_live():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([])
    live = _live(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_price": 1.0850,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    rep = reconcile(bt, live)
    assert rep.counts["extra_in_live"] == 1
    assert rep.counts["missing_in_live"] == 0


def test_fuzzy_match_within_one_bar_window():
    """Live signal fires one main-TF bar after backtest — should still match
    with ``signal_bar_window=1`` (default)."""
    bt = _bt(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts("2026-04-20T15:00:00Z"),
                "entry_ts": _sig_ts("2026-04-20T15:00:00Z"),
                "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
                "entry_price": 1.0850,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    live = _live(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts("2026-04-20T16:00:00Z"),  # +1 H1 bar
                "entry_price": 1.0850,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    rep = reconcile(bt, live)
    assert len(rep.matched) == 1
    assert rep.counts["missing_in_live"] == 0


def test_fuzzy_match_rejects_beyond_window():
    bt = _bt(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts("2026-04-20T15:00:00Z"),
                "entry_ts": _sig_ts("2026-04-20T15:00:00Z"),
                "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
                "entry_price": 1.0850,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    live = _live(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts("2026-04-20T19:00:00Z"),  # +4 H1 bars
                "entry_price": 1.0850,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    rep = reconcile(bt, live, Tolerances(signal_bar_window=1))
    assert rep.counts["missing_in_live"] == 1
    assert rep.counts["extra_in_live"] == 1


def test_jpy_pair_pip_scaling():
    """JPY pairs use 0.01 pip unit, not 0.0001."""
    sig = "2026-04-20T15:00:00Z"
    bt = _bt(
        [
            {
                "pair": "USD_JPY",
                "direction": 1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_ts": _sig_ts(sig),
                "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
                "entry_price": 150.00,
                "exit_price": 150.30,
                "pnl_pips": 30.0,
            }
        ]
    )
    live = _live(
        [
            {
                "pair": "USD_JPY",
                "direction": 1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_price": 150.005,
                "exit_price": 150.295,
                "pnl_pips": 29.0,
            }
        ]
    )
    rep = reconcile(bt, live, Tolerances(entry_price_pips=2.0, exit_price_pips=2.0, pnl_pips=1.5))
    # 0.005 JPY / 0.01 = 0.5 pips → within 2 pip tolerance
    assert rep.matched[0].categories == []


def test_render_html_and_json_are_non_empty():
    sig = "2026-04-20T15:00:00Z"
    bt = _bt(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_ts": _sig_ts(sig),
                "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
                "entry_price": 1.0850,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    live = _live(
        [
            {
                "pair": "EUR_USD",
                "direction": 1,
                "signal_bar_ts": _sig_ts(sig),
                "entry_price": 1.0850,
                "exit_price": 1.0880,
                "pnl_pips": 30.0,
            }
        ]
    )
    rep = reconcile(bt, live)
    html = render_report_html(rep)
    assert "Fire Forex" in html
    assert "EUR_USD" in html
    payload = render_report_json(rep)
    assert "matched" in payload


# ── Parity-v2 categories + per-pair rollup ────────────────────────────


def _pairv2_bt(
    pair: str,
    sig: str,
    signal_variant_id: int = 42,
    signal_family: str = "ema_cross",
    spread_entry_pips: float = 0.5,
    exit_reason_name: str = "TP",
) -> dict:
    return {
        "pair": pair,
        "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_ts": _sig_ts(sig),
        "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 1.0850,
        "exit_price": 1.0880,
        "pnl_pips": 30.0,
        "signal_variant_id": signal_variant_id,
        "signal_family": signal_family,
        "spread_entry_pips": spread_entry_pips,
        "exit_reason_name": exit_reason_name,
    }


def _pairv2_live(
    pair: str,
    sig: str,
    signal_variant: int = 42,
    signal_family: str = "ema_cross",
    spread_pips: float = 0.5,
    slippage_pips: float = 0.2,
    close_reason: str = "TP",
    pnl_pips: float = 29.9,
) -> dict:
    return {
        "plan_id": f"{pair}_{sig}_+1",
        "pair": pair,
        "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_price": 1.08505,
        "exit_price": 1.08795,
        "pnl_pips": pnl_pips,
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
    bt = _bt(
        [
            _pairv2_bt("EUR_USD", sig_a),
            _pairv2_bt("USD_JPY", sig_b),
        ]
    )
    live = _live(
        [
            _pairv2_live("EUR_USD", sig_a),
            # USD_JPY live missing → shows up in missing_in_live for USD_JPY.
        ]
    )
    rep = reconcile(bt, live)
    rollup = rep.by_pair()
    assert set(rollup) == {"EUR_USD", "USD_JPY"}
    assert rollup["EUR_USD"]["matched"] == 1
    assert rollup["USD_JPY"]["missing_in_live"] == 1
    assert rollup["EUR_USD"]["delta_pips"] != 0.0  # bt 30 vs live 29.9


# ── Cost-realism propagation (issue #32) ───────────────────────────────


def _bt_with_cost_realism(
    pair: str,
    sig: str,
    *,
    raw_pnl: float = 30.0,
    overlay_delta: float = -1.5,
    adjusted_pnl: float | None = None,
    gated_out_reason: str = "",
):
    """BT row carrying the columns the harness writes after PR #31/#35."""
    if adjusted_pnl is None:
        adjusted_pnl = raw_pnl + overlay_delta
    # When a trade is gated out the harness zeroes the effective pnl;
    # otherwise effective == adjusted.
    effective_pnl = 0.0 if gated_out_reason else adjusted_pnl
    return {
        "pair": pair,
        "direction": 1,
        "signal_bar_ts": _sig_ts(sig),
        "entry_ts": _sig_ts(sig),
        "exit_ts": _sig_ts("2026-04-20T17:00:00Z"),
        "entry_price": 1.0850,
        "exit_price": 1.0880,
        "pnl_pips": raw_pnl,
        # Cost-realism columns the harness persists per PR #31/#35.
        "raw_pnl_pips": raw_pnl,
        "overlay_delta_pips": overlay_delta,
        "adjusted_pnl_pips": adjusted_pnl,
        "gated_out_reason": gated_out_reason,
        "effective_pnl_pips": effective_pnl,
    }


def test_matched_row_carries_cost_realism_columns():
    """A matched trade must surface raw / overlay / adjusted / gated / effective.

    Issue #32: MatchedRow previously dropped these columns, hiding the
    cost-realism overlay from the matched-row report (the headline
    user-visible reconcile output).
    """
    sig = "2026-04-20T15:00:00Z"
    bt = _bt(
        [
            _bt_with_cost_realism(
                "EUR_USD",
                sig,
                raw_pnl=30.0,
                overlay_delta=-1.7,
                adjusted_pnl=28.3,
                gated_out_reason="",
            )
        ]
    )
    live = _live([_pairv2_live("EUR_USD", sig)])
    rep = reconcile(bt, live)
    assert len(rep.matched) == 1
    m = rep.matched[0]
    assert m.bt_raw_pnl_pips == pytest.approx(30.0)
    assert m.bt_overlay_delta_pips == pytest.approx(-1.7)
    assert m.bt_adjusted_pnl_pips == pytest.approx(28.3)
    assert m.bt_gated_out_reason == ""
    assert m.bt_effective_pnl_pips == pytest.approx(28.3)


def test_matched_row_zero_effective_pnl_when_gated():
    """A trade gated out by the BT post-pass surfaces effective_pnl == 0."""
    sig = "2026-04-20T15:00:00Z"
    bt = _bt(
        [
            _bt_with_cost_realism(
                "EUR_USD",
                sig,
                raw_pnl=30.0,
                overlay_delta=-1.0,
                adjusted_pnl=29.0,
                gated_out_reason="rollover",
            )
        ]
    )
    live = _live([_pairv2_live("EUR_USD", sig)])
    rep = reconcile(bt, live)
    m = rep.matched[0]
    assert m.bt_gated_out_reason == "rollover"
    assert m.bt_effective_pnl_pips == pytest.approx(0.0)


def test_by_pair_rollup_uses_effective_pnl_when_present():
    """The pair rollup must total bt_effective_pnl_pips (not raw) so a gated trade reads as zero."""
    sig_a = "2026-04-20T15:00:00Z"
    sig_b = "2026-04-20T16:00:00Z"
    bt = _bt(
        [
            _bt_with_cost_realism("EUR_USD", sig_a, raw_pnl=30.0, overlay_delta=-1.0, adjusted_pnl=29.0),
            _bt_with_cost_realism(
                "EUR_USD",
                sig_b,
                raw_pnl=20.0,
                overlay_delta=-0.5,
                adjusted_pnl=19.5,
                gated_out_reason="rollover",
            ),
        ]
    )
    live = _live([_pairv2_live("EUR_USD", sig_a), _pairv2_live("EUR_USD", sig_b)])
    rep = reconcile(bt, live)
    rollup = rep.by_pair()
    # Effective bt total = 29.0 (kept) + 0.0 (gated) = 29.0.
    assert rollup["EUR_USD"]["matched_effective_pnl_pips_bt"] == pytest.approx(29.0)
    # Raw stays available for the side-by-side display.
    assert rollup["EUR_USD"]["matched_pnl_pips_bt"] == pytest.approx(50.0)


def test_render_report_json_includes_cost_realism_fields():
    """JSON output must serialise the new MatchedRow fields so downstream consumers see them."""
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([_bt_with_cost_realism("EUR_USD", sig, raw_pnl=30.0, overlay_delta=-1.7, adjusted_pnl=28.3)])
    live = _live([_pairv2_live("EUR_USD", sig)])
    rep = reconcile(bt, live)
    payload = json.loads(render_report_json(rep))
    row = payload["matched"][0]
    assert "bt_raw_pnl_pips" in row
    assert "bt_overlay_delta_pips" in row
    assert "bt_adjusted_pnl_pips" in row
    assert "bt_gated_out_reason" in row
    assert "bt_effective_pnl_pips" in row


def test_legacy_bt_without_cost_realism_columns_does_not_crash():
    """A backtest run from before PR #31 has no cost-realism columns. Reconcile must still succeed."""
    sig = "2026-04-20T15:00:00Z"
    bt = _bt([_pairv2_bt("EUR_USD", sig)])  # no raw/overlay/adjusted/gated/effective
    live = _live([_pairv2_live("EUR_USD", sig)])
    rep = reconcile(bt, live)
    assert len(rep.matched) == 1
    m = rep.matched[0]
    # Legacy rows surface as NaN/empty so the column exists but signals "absent".
    assert m.bt_gated_out_reason == ""
    # bt_raw_pnl_pips falls back to bt_pnl_pips so the matched view still has a number.
    assert m.bt_raw_pnl_pips == pytest.approx(m.bt_pnl_pips)
