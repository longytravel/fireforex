"""Per-trade forensic reconciliation report.

For each closed live trade, walk the full chain and explain every
millisecond / pip of drift between live and backtest:

  1. Fire timing   — signal-bar close → fire_at → broker fill
  2. Entry         — plan ref → broker fill → BT entry (with spreads)
  3. Exit          — live SL price / time  vs  BT SL price / time
  4. Narrative     — "why" sentence synthesised from the numbers

Joins:
  artifacts/live/<instance>/plans/<pair>.jsonl
  artifacts/live/<instance>/tickets.jsonl
  artifacts/live/<instance>/deals.jsonl
  artifacts/live/<instance>/config.json        (EA / knob snapshot)
  artifacts/replay/<run>/<stamp>_<source>/trades.npz

Output:
  artifacts/live/reconcile/<stamp>_forensic.html
  artifacts/live/reconcile/forensic.html       (mirror for UI)
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "artifacts" / "live"
RECONCILE = LIVE / "reconcile"

JPY = {"USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY", "NZD_JPY",
       "CAD_JPY", "CHF_JPY"}


def _pip(pair: str) -> float:
    return 0.01 if pair in JPY else 0.0001


def _iso(ts) -> datetime | None:
    if ts in (None, ""):
        return None
    try:
        s = str(ts).replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except Exception:
        return None


def _read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _load_instance(inst: Path) -> dict:
    tickets = _read_jsonl(inst / "tickets.jsonl")
    deals = _read_jsonl(inst / "deals.jsonl")
    plans: list[dict] = []
    for pf in sorted((inst / "plans").glob("*.jsonl")):
        plans.extend(_read_jsonl(pf))
    cfg_path = inst / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig")) if cfg_path.exists() else {}
    return {"name": inst.name, "dir": inst,
            "tickets": tickets, "deals": deals, "plans": plans, "cfg": cfg}


def _signal_from_comment(comment: str) -> str:
    c = (comment or "").lower()
    if "ema" in c:
        return "ema_cross"
    if "macd" in c:
        return "macd_cross"
    if "donch" in c:
        return "donchian"
    return c


def _sl_price_from_close_comment(comment: str) -> float | None:
    """MT5 writes '[sl 1.05815]' into the comment of the closing deal."""
    if not comment:
        return None
    m = re.search(r"\[(sl|tp)\s+([0-9.]+)\]", comment, re.IGNORECASE)
    if m:
        try:
            return float(m.group(2))
        except ValueError:
            return None
    return None


def _load_replay_df(inst_dir: Path, source: str):
    import numpy as np
    import pandas as pd
    cfg_path = inst_dir / "config.json"
    if not cfg_path.exists():
        return None
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    run_id = cfg.get("source_run_id")
    if not run_id:
        return None
    stamp_file = ROOT / "artifacts" / "replay" / run_id / f"latest_stamp_{source}.txt"
    if not stamp_file.exists():
        return None
    stamp = stamp_file.read_text(encoding="utf-8-sig").strip()
    npz_path = ROOT / "artifacts" / "replay" / run_id / stamp / "trades.npz"
    if not npz_path.exists():
        return None
    d = np.load(npz_path, allow_pickle=True)
    if "trades" not in d.files:
        return None
    df = pd.DataFrame(d["trades"])
    if df.empty:
        return df
    df["entry_ts"] = pd.to_datetime(df["entry_ts"], utc=True)
    df["exit_ts"] = pd.to_datetime(df["exit_ts"], utc=True)
    return df


def _match_bt(bt_df, pair: str, direction: int, sig_bar_dt: datetime):
    """Pick the BT trade whose entry lands in the 15-minute window AFTER
    the signal bar open (so it corresponds to the same M15 bar firing)."""
    if bt_df is None or len(bt_df) == 0:
        return None
    import pandas as pd
    sig_dt = pd.Timestamp(sig_bar_dt.astimezone(timezone.utc))
    cand = bt_df[
        (bt_df["pair"] == pair)
        & (bt_df["direction"].astype(float).astype(int) == direction)
        & (bt_df["entry_ts"] >= sig_dt)
        & (bt_df["entry_ts"] < sig_dt + pd.Timedelta(minutes=16))
    ]
    if len(cand) == 0:
        return None
    return cand.iloc[0]


def _ea_summary(cfg: dict) -> dict:
    """Pick out the human-friendly knob values for this deployment."""
    bt = cfg.get("best_trial") or {}
    engine = bt.get("engine") or {}
    sl = engine.get("stop_loss", {})
    tp = engine.get("take_profit", {})
    stops = []
    if sl.get("selector") == "atr":
        stops.append(f"SL: ATR × {sl['atr']['mult']:.1f}")
    elif sl.get("selector") == "fixed":
        stops.append(f"SL: fixed {sl['fixed']['pips']:.1f} pips")
    if tp.get("selector") == "rr":
        stops.append(f"TP: RR × {tp['rr']['ratio']:.1f}")
    for name in ("trailing", "chandelier", "breakeven", "partial",
                 "stale", "session", "max_bars"):
        blk = engine.get(name, {}) or {}
        if blk.get("test"):
            on = blk.get("when_on") or {}
            if on:
                params = ", ".join(f"{k}={v:.2f}" if isinstance(v, float) else f"{k}={v}"
                                   for k, v in on.items())
                stops.append(f"{name}: ON ({params})")
            else:
                stops.append(f"{name}: ON")
    return {
        "signal_family": bt.get("signal_family"),
        "signal_variant": bt.get("signal_variant"),
        "signal_params": bt.get("signal_params", {}),
        "stops": stops,
    }


def _build_trade(inst: dict, ticket: dict, plan: dict,
                 deals: list[dict], bt_duka, bt_mt5) -> dict:
    pair = plan["pair"]
    direction = int(plan["direction"])
    pip = _pip(pair)
    dir_word = "LONG" if direction > 0 else "SHORT"

    open_d, close_d = deals[0], deals[-1]
    sig_bar = _iso(plan["signal_bar_ts"])
    bar_close = sig_bar + timedelta(minutes=15) if sig_bar else None
    fired_at = _iso(plan["fired_at_ts"])
    filled_at = _iso(ticket.get("filled_at"))

    plan_entry = float(plan.get("entry_ref_price") or 0.0)
    fill_px = float(ticket.get("fill_price") or 0.0) or float(open_d["price"])
    live_open = float(open_d["price"])
    live_close = float(close_d["price"])
    live_sl = _sl_price_from_close_comment(close_d.get("comment", ""))
    plan_sl = float(plan.get("sl_price") or 0.0)
    plan_tp = float(plan.get("tp_price") or 0.0)

    slippage_entry_pips = round((fill_px - plan_entry) / pip * direction, 2) if plan_entry else None
    fire_latency_sec = (fired_at - bar_close).total_seconds() if (fired_at and bar_close) else None
    exec_latency_ms = int((filled_at - fired_at).total_seconds() * 1000) if (fired_at and filled_at) else None

    pnl_pips_live = (live_close - live_open) / pip * direction
    live_close_dt = _iso(close_d.get("time"))

    def _bt_block(bt_row, label: str):
        if bt_row is None:
            return {"label": label, "matched": False}
        ent = float(bt_row["entry_price"])
        xt = float(bt_row["exit_price"])
        spread = float(bt_row.get("spread_entry_pips", 0.0) or 0.0)
        reason = str(bt_row.get("exit_reason_name", ""))
        xit_ts = bt_row["exit_ts"].to_pydatetime() if bt_row["exit_ts"] is not None else None
        exit_sub_bar = int(bt_row.get("exit_sub_bar_index", -1) or -1)
        # Δentry: live price was better or worse than BT's entry price?
        entry_delta = (live_open - ent) / pip * direction
        exit_delta = (live_close - xt) / pip * direction
        pnl_delta = pnl_pips_live - float(bt_row["pnl_pips"])
        exit_delta_sec = ((live_close_dt - xit_ts).total_seconds()
                          if (live_close_dt and xit_ts) else None)
        return {
            "label": label, "matched": True,
            "bt_entry": ent, "bt_exit": xt, "bt_pnl": float(bt_row["pnl_pips"]),
            "bt_reason": reason, "bt_exit_ts": xit_ts,
            "bt_spread_pips": spread,
            "entry_delta_pips": round(entry_delta, 2),
            "exit_delta_pips": round(exit_delta, 2),
            "pnl_delta_pips": round(pnl_delta, 2),
            "exit_delta_sec": round(exit_delta_sec, 1) if exit_delta_sec is not None else None,
            "exit_sub_bar_index": exit_sub_bar,
        }

    return {
        "position_id": ticket["ticket"],
        "pair": pair, "direction": dir_word, "direction_int": direction,
        "instance": inst["name"],
        "signal": _signal_from_comment(open_d.get("comment", "")),
        "signal_bar": sig_bar, "bar_close": bar_close,
        "fired_at": fired_at, "filled_at": filled_at,
        "live_open_ts": _iso(open_d.get("time")), "live_close_ts": live_close_dt,
        "plan_entry": plan_entry, "plan_sl": plan_sl, "plan_tp": plan_tp,
        "fill_px": fill_px, "live_open": live_open, "live_close": live_close,
        "live_sl_hit_price": live_sl,
        "live_pnl_pips": round(pnl_pips_live, 2),
        "profit_gbp": round(float(close_d.get("profit", 0.0))
                            + float(open_d.get("commission", 0.0))
                            + float(close_d.get("commission", 0.0)), 2),
        "slippage_entry_pips": slippage_entry_pips,
        "fire_latency_sec": round(fire_latency_sec, 1) if fire_latency_sec is not None else None,
        "exec_latency_ms": exec_latency_ms,
        "duka": _bt_block(bt_duka, "Dukascopy"),
        "mt5":  _bt_block(bt_mt5,  "MT5"),
    }


def _round_trips(inst: dict, bt_duka, bt_mt5) -> list[dict]:
    deals_by_pos: dict[int, list[dict]] = defaultdict(list)
    for d in inst["deals"]:
        deals_by_pos[d.get("position_id")].append(d)
    for lst in deals_by_pos.values():
        lst.sort(key=lambda d: d.get("time", ""))
    plan_by_id = {p["plan_id"]: p for p in inst["plans"]}

    out: list[dict] = []
    for t in inst["tickets"]:
        pos = t.get("ticket")
        dlist = deals_by_pos.get(pos, [])
        if len(dlist) < 2:
            continue
        plan = plan_by_id.get(t.get("plan_id"))
        if plan is None:
            continue
        sig_bar = _iso(plan["signal_bar_ts"])
        direction = int(plan["direction"])
        pair = plan["pair"]
        duka_row = _match_bt(bt_duka, pair, direction, sig_bar) if sig_bar else None
        mt5_row = _match_bt(bt_mt5, pair, direction, sig_bar) if sig_bar else None
        out.append(_build_trade(inst, t, plan, dlist, duka_row, mt5_row))
    return out


def _narrative(t: dict) -> list[str]:
    """Plain-English explanation of the drivers for this trade."""
    bits: list[str] = []

    # Fire timing
    fls = t.get("fire_latency_sec")
    if fls is not None:
        if fls < -5:
            bits.append(
                f"🕑 <b>Fire timing:</b> fired <b>{-fls:.0f}s BEFORE</b> the "
                f"M15 bar closed — provisional-candle trade "
                "(pre-forming-candle fix)."
            )
        elif fls < 30:
            bits.append(
                f"🕑 <b>Fire timing:</b> fired {fls:.1f}s after bar close — healthy."
            )
        else:
            bits.append(f"🕑 <b>Fire timing:</b> {fls:.0f}s after bar close.")
    el = t.get("exec_latency_ms")
    if el is not None:
        bits.append(f"⚡ <b>Broker fill latency:</b> {el} ms from fire to fill.")

    # Slippage
    sl = t.get("slippage_entry_pips")
    if sl is not None:
        bits.append(
            f"🎯 <b>Entry slippage:</b> broker filled at {t['fill_px']:.5f} vs "
            f"plan ref {t['plan_entry']:.5f} → "
            f"{abs(sl):.1f} pips {'worse' if sl > 0 else 'better'} than plan."
        )

    # SL price: live vs plan
    if t.get("live_sl_hit_price") is not None:
        slp = t["live_sl_hit_price"]
        diff = (slp - t["plan_sl"]) / _pip(t["pair"])
        bits.append(
            f"🛑 <b>Live SL hit:</b> broker closed at {slp:.5f} "
            f"(plan SL was {t['plan_sl']:.5f}, "
            f"{'+' if diff > 0 else ''}{diff:.1f} pips)."
        )

    for blk in (t["duka"], t["mt5"]):
        if not blk.get("matched"):
            bits.append(f"❓ <b>{blk['label']} replay:</b> no matching trade "
                        "in backtest (cannot reproduce this signal).")
            continue

        # Entry deltas — explained by spread assumption
        eds = blk.get("entry_delta_pips")
        spr = blk.get("bt_spread_pips", 0.0)
        if eds is not None:
            bits.append(
                f"🔵 <b>{blk['label']} entry:</b> BT entry "
                f"{blk['bt_entry']:.5f} vs live open {t['live_open']:.5f} "
                f"→ {abs(eds):.1f} pips "
                f"{'higher' if eds > 0 else 'lower'} in live "
                f"(BT assumed spread {spr:.2f} pips at entry)."
            )

        # Exit deltas
        xds = blk.get("exit_delta_pips")
        rs = blk.get("bt_reason", "")
        xsec = blk.get("exit_delta_sec")
        reason = "both hit SL" if rs == "SL" else f"BT exit reason: {rs}"
        if xsec is None:
            bits.append(f"🔴 <b>{blk['label']} exit:</b> {reason}; "
                        f"Δprice {abs(xds):.1f} pips.")
        elif abs(xsec) < 90:
            bits.append(
                f"🔴 <b>{blk['label']} exit:</b> {reason}. Live closed "
                f"{abs(xsec):.0f}s {'later' if xsec > 0 else 'earlier'} "
                f"than BT; Δprice {abs(xds):.1f} pips."
            )
        elif rs == "NONE":
            bits.append(
                f"🔴 <b>{blk['label']} exit:</b> <b>BT data ran out</b> while "
                f"the trade was still open — BT exit is the data edge, not a real close. "
                f"Live closed {abs(xsec) / 60.0:.0f}m "
                f"{'after' if xsec > 0 else 'before'} the data edge."
            )
        else:
            bits.append(
                f"🔴 <b>{blk['label']} exit:</b> {reason} BUT live closed "
                f"<b>{abs(xsec) / 60.0:.0f}m "
                f"{'later' if xsec > 0 else 'earlier'}</b> than BT — "
                f"broker and {blk['label']} ticker saw different price paths "
                f"(broker hit SL first vs {blk['label']} hit SL first)."
            )

    return bits


def _fmt_ts(dt, fmt="%H:%M:%S") -> str:
    if dt is None:
        return ""
    if hasattr(dt, "strftime"):
        return dt.strftime(fmt)
    return str(dt)


def _render_html(trades: list[dict], instances: list[dict], stamp: str) -> str:
    def esc(x):
        return html.escape(str(x)) if x is not None else ""

    def fmt_px(x, pair):
        if x is None or x == "":
            return "—"
        return f"{x:.3f}" if pair in JPY else f"{x:.5f}"

    # Top cards
    total_gbp = sum(t["profit_gbp"] for t in trades)
    duka_ok = sum(1 for t in trades if t["duka"].get("matched"))
    mt5_ok = sum(1 for t in trades if t["mt5"].get("matched"))
    avg_slip = (sum(abs(t["slippage_entry_pips"]) for t in trades
                    if t["slippage_entry_pips"] is not None)
                / max(1, sum(1 for t in trades
                             if t["slippage_entry_pips"] is not None)))
    cards = [
        ("Closed trades", f"{len(trades)}", "good"),
        ("Total P&L", f"£{total_gbp:+.2f}", "good" if total_gbp >= 0 else "bad"),
        ("Dukascopy BT match", f"{duka_ok}/{len(trades)}", "good" if duka_ok == len(trades) else "warn"),
        ("MT5 BT match", f"{mt5_ok}/{len(trades)}", "good" if mt5_ok == len(trades) else "warn"),
        ("Avg entry slippage", f"{avg_slip:.2f} pips", "warn" if avg_slip > 1 else "good"),
    ]
    card_html = "".join(
        f'<div class="card {cls}"><div>{esc(lbl)}</div><strong>{esc(val)}</strong></div>'
        for lbl, val, cls in cards
    )

    # EA knob summary per instance
    ea_html = []
    for inst in instances:
        ea = _ea_summary(inst["cfg"])
        name = inst["name"][-50:]
        stops = " · ".join(ea["stops"]) if ea["stops"] else "(no exit management)"
        sp = ea.get("signal_params", {})
        spstr = ", ".join(f"{k}={v}" for k, v in sp.items())
        ea_html.append(f"""
          <div class="ea">
            <div class="ea-name">{esc(name)}</div>
            <div class="ea-detail"><b>{esc(ea['signal_family'])}</b> (variant {esc(ea.get('signal_variant'))}) · {esc(spstr)}</div>
            <div class="ea-detail">{esc(stops)}</div>
          </div>""")

    # Per-trade cards
    trade_cards = []
    for t in trades:
        nlines = _narrative(t)
        nar = "".join(f"<li>{bit}</li>" for bit in nlines)

        sig_close = _fmt_ts(t["bar_close"])
        fire_at = _fmt_ts(t["fired_at"])
        fill_at = _fmt_ts(t["filled_at"])
        live_close = _fmt_ts(t["live_close_ts"])
        dir_color = "#0c7a43" if t["direction_int"] > 0 else "#b42318"
        dir_sign = "↑" if t["direction_int"] > 0 else "↓"
        verdict_cls = "bad" if t["live_pnl_pips"] < -2 else "good"

        duka = t["duka"]
        mt5 = t["mt5"]

        def _bt_box(blk, pair):
            if not blk.get("matched"):
                return f'<div class="bt-box nomatch">{esc(blk["label"])} replay: no match</div>'
            reason_cls = "bad" if blk["bt_reason"] == "NONE" else ""
            xsec = blk.get("exit_delta_sec")
            xsec_txt = (f"{xsec:+.0f}s" if xsec is not None and abs(xsec) < 120
                        else f"{xsec/60:+.1f} min" if xsec is not None
                        else "—")
            return f"""
              <div class="bt-box">
                <div class="bt-head">{esc(blk['label'])} backtest</div>
                <div class="kv"><span>BT entry</span><span>{fmt_px(blk['bt_entry'], pair)}</span></div>
                <div class="kv"><span>BT exit</span><span>{fmt_px(blk['bt_exit'], pair)}</span></div>
                <div class="kv"><span>BT pnl</span><span>{blk['bt_pnl']:+.1f} pips</span></div>
                <div class="kv"><span>BT close reason</span><span class="{reason_cls}">{esc(blk['bt_reason'])}</span></div>
                <div class="kv"><span>BT spread @ entry</span><span>{blk['bt_spread_pips']:.2f} pips</span></div>
                <div class="delta">
                  <div>Δ entry <b>{blk['entry_delta_pips']:+.2f}</b> pips</div>
                  <div>Δ exit <b>{blk['exit_delta_pips']:+.2f}</b> pips</div>
                  <div>Δ pnl <b>{blk['pnl_delta_pips']:+.2f}</b> pips</div>
                  <div>Δ time <b>{esc(xsec_txt)}</b></div>
                </div>
              </div>"""

        slip = t["slippage_entry_pips"]
        slip_txt = (f"{slip:+.2f} pips" if slip is not None else "—")
        fls = t["fire_latency_sec"]
        fls_txt = f"{fls:+.1f}s" if fls is not None else "—"

        trade_cards.append(f"""
          <section class="trade">
            <header class="trade-head {verdict_cls}">
              <div>
                <span class="pos">#{esc(t['position_id'])}</span>
                <span class="pair">{esc(t['pair'])}</span>
                <span class="signal">{esc(t['signal'])}</span>
                <span class="dir" style="color:{dir_color}">{dir_sign} {esc(t['direction'])}</span>
              </div>
              <div class="pnl">
                live pnl <b>{t['live_pnl_pips']:+.1f} pips</b>
                &nbsp;·&nbsp; <b>£{t['profit_gbp']:+.2f}</b>
              </div>
            </header>

            <div class="timeline">
              <div><div>bar close</div><b>{esc(sig_close)}</b></div>
              <div><div>fired</div><b>{esc(fire_at)}</b></div>
              <div><div>filled</div><b>{esc(fill_at)}</b></div>
              <div><div>live close</div><b>{esc(live_close)}</b></div>
            </div>

            <div class="grid">
              <div class="live-box">
                <div class="box-head">Live trade</div>
                <div class="kv"><span>plan entry ref</span><span>{fmt_px(t['plan_entry'], t['pair'])}</span></div>
                <div class="kv"><span>broker fill</span><span>{fmt_px(t['fill_px'], t['pair'])}</span></div>
                <div class="kv"><span>live open</span><span>{fmt_px(t['live_open'], t['pair'])}</span></div>
                <div class="kv"><span>live close</span><span>{fmt_px(t['live_close'], t['pair'])}</span></div>
                <div class="kv"><span>plan SL / TP</span><span>{fmt_px(t['plan_sl'], t['pair'])} / {fmt_px(t['plan_tp'], t['pair'])}</span></div>
                <div class="kv"><span>live SL hit price</span><span>{fmt_px(t['live_sl_hit_price'], t['pair'])}</span></div>
                <div class="kv"><span>entry slippage</span><span>{esc(slip_txt)}</span></div>
                <div class="kv"><span>fire latency</span><span>{esc(fls_txt)}</span></div>
                <div class="kv"><span>exec latency</span><span>{t['exec_latency_ms'] if t['exec_latency_ms'] is not None else '—'} ms</span></div>
              </div>
              {_bt_box(duka, t['pair'])}
              {_bt_box(mt5, t['pair'])}
            </div>

            <ul class="narrative">{nar}</ul>
          </section>""")

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Fire Forex forensic reconciliation</title>
<style>
:root {{
  --good:#0c7a43; --warn:#9a6700; --bad:#b42318;
  --line:#d8dee8; --ink:#17202c; --muted:#566273; --bg:#f6f8fb;
}}
* {{ box-sizing: border-box; }}
body {{ font-family: Segoe UI, Arial, sans-serif; margin:24px; color:var(--ink); background:white; }}
h1 {{ margin:0 0 4px; font-size:24px; }}
.note {{ color:var(--muted); max-width:1200px; line-height:1.45; }}
.cards {{ display:grid; grid-template-columns:repeat(5, minmax(150px, 1fr)); gap:10px; margin:18px 0; }}
.card {{ border:1px solid var(--line); border-left-width:5px; border-radius:8px; padding:10px 14px; background:var(--bg); }}
.card div {{ color:var(--muted); font-size:12px; }}
.card strong {{ display:block; font-size:21px; margin-top:4px; }}
.good {{ border-left-color:var(--good); }}
.warn {{ border-left-color:var(--warn); }}
.bad  {{ border-left-color:var(--bad); }}
.ea {{ border:1px solid var(--line); border-radius:6px; padding:8px 12px; background:#fbfcfe; margin-bottom:6px; font-size:13px; }}
.ea-name {{ font-family: ui-monospace, Consolas, monospace; color:var(--muted); font-size:11px; }}
.ea-detail {{ margin-top:2px; }}
section.trade {{ border:1px solid var(--line); border-radius:10px; padding:14px 18px; margin:14px 0; background:white; }}
.trade-head {{ display:flex; justify-content:space-between; padding-bottom:10px; border-bottom:1px solid var(--line); }}
.trade-head .pos {{ font-family: ui-monospace, Consolas, monospace; color:var(--muted); margin-right:10px; }}
.trade-head .pair {{ font-weight:600; font-size:16px; margin-right:10px; }}
.trade-head .signal {{ color:var(--muted); margin-right:10px; }}
.trade-head .dir {{ font-weight:700; }}
.trade-head .pnl {{ color:var(--muted); }}
.trade-head.bad .pnl b {{ color:var(--bad); }}
.trade-head.good .pnl b {{ color:var(--good); }}
.timeline {{ display:grid; grid-template-columns:repeat(4, 1fr); gap:12px; margin:10px 0 14px; }}
.timeline > div {{ background:var(--bg); border-radius:6px; padding:6px 10px; font-size:12px; }}
.timeline b {{ font-family:ui-monospace, Consolas, monospace; font-size:14px; color:var(--ink); display:block; margin-top:2px; }}
.grid {{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:12px; }}
.live-box, .bt-box {{ border:1px solid var(--line); border-radius:8px; padding:10px 12px; background:#fbfcfe; }}
.box-head, .bt-head {{ font-weight:600; margin-bottom:6px; color:var(--ink); }}
.bt-box.nomatch {{ color:var(--muted); font-style:italic; text-align:center; padding:20px 0; }}
.kv {{ display:flex; justify-content:space-between; font-size:13px; padding:2px 0; border-bottom:1px dotted #eef2f7; }}
.kv span:first-child {{ color:var(--muted); }}
.kv span.bad {{ color:var(--bad); font-weight:600; }}
.delta {{ margin-top:8px; padding-top:8px; border-top:1px solid var(--line); font-size:12px; display:grid; grid-template-columns:1fr 1fr; gap:4px 12px; }}
.narrative {{ margin:14px 0 0; padding:0 0 0 0; list-style:none; background:var(--bg); border-radius:6px; padding:10px 14px; }}
.narrative li {{ padding:4px 0; font-size:13.5px; line-height:1.5; }}
</style></head>
<body>
  <h1>Fire Forex reconciliation — forensic view</h1>
  <p class="note">Every closed MT5 live trade, decomposed into fire-timing, entry, and exit components — with the numerical delta against both Dukascopy and MT5 backtests, and a plain-English narrative of what the drift means. Generated {esc(stamp)}.</p>
  <div class="cards">{card_html}</div>
  <div class="note"><b>Deployed strategies (knob snapshot):</b></div>
  {"".join(ea_html)}
  {"".join(trade_cards)}
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stamp", default=None)
    ap.add_argument("--out-dir", type=Path, default=RECONCILE)
    args = ap.parse_args(argv)

    stamp = args.stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    all_trades: list[dict] = []
    instances: list[dict] = []
    for inst_dir in sorted(p for p in LIVE.glob("complexity_*") if p.is_dir()):
        data = _load_instance(inst_dir)
        instances.append(data)
        bt_duka = _load_replay_df(inst_dir, "dukascopy")
        bt_mt5 = _load_replay_df(inst_dir, "mt5")
        all_trades.extend(_round_trips(data, bt_duka, bt_mt5))

    all_trades.sort(key=lambda t: t.get("live_open_ts") or datetime.min.replace(tzinfo=timezone.utc))

    html_text = _render_html(all_trades, instances, stamp)
    out_html = args.out_dir / f"{stamp}_forensic.html"
    out_html.write_text(html_text, encoding="utf-8")
    (args.out_dir / "forensic.html").write_text(html_text, encoding="utf-8")
    print(f"[forensic] closed trades: {len(all_trades)}")
    print(f"[forensic] html: {out_html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
