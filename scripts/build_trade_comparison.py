"""Build a live-vs-backtest trade comparison CSV and clear-view HTML.

Inputs (read from ``artifacts/live/<instance>/``):
  * ``plans/<pair>.jsonl``   — every plan the runner proposed.
  * ``tickets.jsonl``        — what MT5 actually accepted (+ fill price).
  * ``deals.jsonl``          — every MT5 deal, including the closing one.
  * ``reconcile/*_dukascopy_live_vs_dukascopy.json`` (latest)
  * ``reconcile/*_mt5_A_live_vs_duka.json`` (latest, optional)

Output:
  * ``artifacts/live/reconcile/<stamp>_trade_comparison.csv`` — dealfix schema.
  * ``artifacts/live/reconcile/<stamp>_trade_comparison.html`` — clear view.
  * ``artifacts/live/reconcile/latest.html`` — symlink-like copy.

Run:
    .\\.venv\\Scripts\\python.exe scripts\\build_trade_comparison.py
"""
from __future__ import annotations

import argparse
import csv
import html
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "artifacts" / "live"
RECONCILE = LIVE / "reconcile"

JPY_PAIRS = {"USD_JPY", "EUR_JPY", "GBP_JPY", "AUD_JPY", "NZD_JPY", "CAD_JPY", "CHF_JPY"}


def _pip(pair: str) -> float:
    return 0.01 if pair in JPY_PAIRS else 0.0001


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_instance(inst: Path) -> dict:
    tickets = _read_jsonl(inst / "tickets.jsonl")
    deals = _read_jsonl(inst / "deals.jsonl")
    plans: list[dict] = []
    for pf in sorted((inst / "plans").glob("*.jsonl")):
        plans.extend(_read_jsonl(pf))
    return {"name": inst.name, "dir": inst, "tickets": tickets,
            "deals": deals, "plans": plans}


def _latest_reconcile(inst_dir: Path, tag: str) -> dict | None:
    recdir = inst_dir / "reconcile"
    if not recdir.exists():
        return None
    files = sorted(recdir.glob(f"*_{tag}.json"))
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8-sig"))


def _plan_by_id(plans: list[dict]) -> dict[str, dict]:
    return {p["plan_id"]: p for p in plans}


def _pair_to_sym(pair: str) -> str:
    return pair.replace("_", "")


def _sym_to_pair(sym: str) -> str:
    return f"{sym[:3]}_{sym[3:]}"


def _ticket_row(ticket: dict, plan: dict, open_deal: dict, close_deal: dict) -> dict:
    pair = plan["pair"]
    pip = _pip(pair)
    direction = int(plan["direction"])
    open_px = float(open_deal["price"])
    close_px = float(close_deal["price"])
    pnl_pips = (close_px - open_px) / pip * direction
    open_time = open_deal["time"]
    close_time = close_deal["time"]
    return {
        "position_id": ticket["ticket"],
        "pair": pair,
        "direction": "LONG" if direction > 0 else "SHORT",
        "signal": _signal_from_comment(open_deal.get("comment", "")),
        "signal_bar": plan.get("signal_bar_ts"),
        "fired_at_utc": plan.get("fired_at_ts"),
        "open_time_utc": open_time,
        "close_time_utc": close_time,
        "open_price": open_px,
        "close_price": close_px,
        "report_pnl_pips": round(pnl_pips, 2),
        "profit_gbp": round(float(close_deal.get("profit", 0.0)), 2),
        "commission_gbp": round(float(open_deal.get("commission", 0.0))
                                + float(close_deal.get("commission", 0.0)), 2),
        "sl_price": plan.get("sl_price"),
        "tp_price": plan.get("tp_price"),
        "close_reason": close_deal.get("comment", ""),
        "plan_id": plan["plan_id"],
    }


def _signal_from_comment(comment: str) -> str:
    if not comment:
        return ""
    c = comment.lower()
    if "ema" in c:
        return "ema_cross"
    if "macd" in c:
        return "macd_cross"
    if "donch" in c:
        return "donchian"
    return c


def _round_trips(inst: dict) -> list[dict]:
    """Return comparison rows for every ticket that has a closing deal."""
    deals_by_pos: dict[int, list[dict]] = defaultdict(list)
    for d in inst["deals"]:
        deals_by_pos[d.get("position_id")].append(d)
    for lst in deals_by_pos.values():
        lst.sort(key=lambda d: d.get("time", ""))
    plan_by_id = _plan_by_id(inst["plans"])

    out: list[dict] = []
    for t in inst["tickets"]:
        pos_id = t.get("ticket")
        dlist = deals_by_pos.get(pos_id, [])
        if len(dlist) < 2:
            continue
        plan = plan_by_id.get(t.get("plan_id"))
        if plan is None:
            continue
        open_d, close_d = dlist[0], dlist[-1]
        row = _ticket_row(t, plan, open_d, close_d)
        row["instance"] = inst["name"]
        out.append(row)
    return out


def _attach_replay(row: dict, reconcile: dict | None, source: str) -> None:
    """Fill in ``{source}_*`` fields from the reconcile report's matched list."""
    prefix = source
    fields = (f"{prefix}_match_status", f"{prefix}_bt_entry",
              f"{prefix}_bt_exit", f"{prefix}_bt_pnl_pips",
              f"{prefix}_bt_exit_ts", f"{prefix}_bt_close_reason",
              f"{prefix}_entry_delta_pips", f"{prefix}_exit_delta_pips",
              f"{prefix}_pnl_delta_pips", f"{prefix}_exit_delta_min",
              f"{prefix}_bt_spread_pips")
    for f in fields:
        row.setdefault(f, "")
    if reconcile is None:
        row[f"{prefix}_match_status"] = "no_replay"
        return
    pair = row["pair"]
    direction = 1 if row["direction"] == "LONG" else -1
    sig_bar = str(row["signal_bar"] or "").replace("T", " ")[:19]
    match = None
    for m in reconcile.get("matched", []):
        m_sig = str(m.get("signal_bar_ts", "")).replace("T", " ")[:19]
        if (m.get("pair") == pair and int(m.get("direction", 0)) == direction
                and m_sig == sig_bar):
            match = m
            break
    if match is None:
        row[f"{prefix}_match_status"] = "no_match"
        return
    pip = _pip(pair)
    row[f"{prefix}_match_status"] = "exact_signal_bar"
    row[f"{prefix}_bt_entry"] = round(float(match.get("bt_entry_price", 0.0)), 5)
    row[f"{prefix}_bt_exit"] = round(float(match.get("bt_exit_price", 0.0)), 5)
    row[f"{prefix}_bt_pnl_pips"] = _nan_safe(match.get("bt_pnl_pips"))
    row[f"{prefix}_bt_close_reason"] = match.get("bt_close_reason", "")
    row[f"{prefix}_bt_spread_pips"] = _nan_safe(match.get("bt_spread_pips"))

    live_px_in = row["open_price"]
    live_px_out = row["close_price"]
    bt_in = match.get("bt_entry_price")
    bt_out = match.get("bt_exit_price")
    if bt_in is not None and not _isnan(bt_in):
        row[f"{prefix}_entry_delta_pips"] = round(
            (live_px_in - bt_in) / pip * direction, 2)
    if bt_out is not None and not _isnan(bt_out):
        row[f"{prefix}_exit_delta_pips"] = round(
            (live_px_out - bt_out) / pip * direction, 2)
    live_pnl = row["report_pnl_pips"]
    bt_pnl = match.get("bt_pnl_pips")
    if bt_pnl is not None and not _isnan(bt_pnl):
        row[f"{prefix}_pnl_delta_pips"] = round(live_pnl - bt_pnl, 2)
    # exit time delta in minutes
    bt_exit_ts = match.get("bt_exit_ts")
    if bt_exit_ts:
        try:
            bt_dt = datetime.fromisoformat(str(bt_exit_ts).replace("Z", "+00:00"))
            live_dt = datetime.fromisoformat(row["close_time_utc"].replace("Z", "+00:00"))
            if bt_dt.tzinfo is None:
                bt_dt = bt_dt.replace(tzinfo=timezone.utc)
            if live_dt.tzinfo is None:
                live_dt = live_dt.replace(tzinfo=timezone.utc)
            delta_min = (live_dt - bt_dt).total_seconds() / 60.0
            row[f"{prefix}_exit_delta_min"] = round(delta_min, 2)
            row[f"{prefix}_bt_exit_ts"] = bt_exit_ts
        except Exception:
            pass


def _isnan(x) -> bool:
    try:
        return math.isnan(float(x))
    except Exception:
        return False


def _nan_safe(x):
    if x is None:
        return ""
    try:
        f = float(x)
        if math.isnan(f):
            return ""
        return round(f, 2)
    except Exception:
        return x


def _verdict(row: dict) -> tuple[str, str]:
    pnl_d = row.get("duka_pnl_delta_pips", "")
    exit_m = row.get("duka_exit_delta_min", "")
    entry_d = row.get("duka_entry_delta_pips", "")
    exit_d = row.get("duka_exit_delta_pips", "")
    try:
        pnl_f = float(pnl_d) if pnl_d != "" else None
        t = abs(float(exit_m)) if exit_m != "" else 0.0
        ed = abs(float(entry_d)) if entry_d != "" else 0.0
        xd = abs(float(exit_d)) if exit_d != "" else 0.0
    except (TypeError, ValueError):
        return ("No replay match", "bad")
    if pnl_f is None:
        return ("No replay match", "bad")
    if abs(pnl_f) <= 2 and t <= 2 and ed <= 2 and xd <= 2:
        return ("Good match", "good")
    if abs(pnl_f) <= 5 and t <= 10:
        return ("Close enough, review", "warn")
    return ("Material difference", "bad")


def _write_csv(rows: list[dict], out: Path) -> None:
    if not rows:
        out.write_text("position_id\n", encoding="utf-8")
        return
    cols = list(rows[0].keys())
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def _fmt_pips(x, nd=1) -> str:
    if x in ("", None):
        return ""
    try:
        v = float(x)
        if math.isnan(v):
            return ""
        return f"{v:.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_signed(x, nd=1) -> str:
    if x in ("", None):
        return ""
    try:
        v = float(x)
        if math.isnan(v):
            return ""
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.{nd}f}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_time_delta(x) -> str:
    if x in ("", None):
        return ""
    try:
        v = float(x)
        if math.isnan(v):
            return ""
        sec = int(round(abs(v) * 60))
        if sec == 0:
            return "same minute"
        m, s = divmod(sec, 60)
        direction = "later" if v > 0 else "earlier"
        if m:
            return f"{m}m {s:02d}s {direction}"
        return f"{s}s {direction}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_price_impact(delta, kind: str) -> str:
    if delta in ("", None):
        return ""
    try:
        v = float(delta)
        if math.isnan(v):
            return ""
    except (TypeError, ValueError):
        return str(delta)
    if abs(v) < 0.05:
        return "same"
    # entry: positive delta = paid more = worse; exit: positive = exited higher = better for long
    if kind == "entry":
        word = "better" if v < 0 else "worse"
    else:  # exit
        word = "better" if v > 0 else "worse"
    return f"{abs(v):.1f} pips {word}"


def _write_html(rows: list[dict], out: Path, stamp: str) -> None:
    closed = len(rows)
    complete = sum(1 for r in rows if r.get("profit_gbp") not in ("", None))
    duka_matches = sum(1 for r in rows
                       if r.get("duka_match_status") == "exact_signal_bar")
    mt5_matches = sum(1 for r in rows
                      if r.get("mt5_match_status") == "exact_signal_bar")
    good = warn = bad = 0
    for r in rows:
        _, cls = _verdict(r)
        if cls == "good":
            good += 1
        elif cls == "warn":
            warn += 1
        else:
            bad += 1

    cards = [
        ("Closed trades checked", str(closed), "good"),
        ("Ticket + plan + deals found", f"{complete}/{closed}", "good"),
        ("Dukascopy reproduced signal bar", f"{duka_matches}/{closed}", "good"),
        ("MT5 replay reproduced signal bar", f"{mt5_matches}/{closed}",
         "good" if mt5_matches else "warn"),
        ("Good / review / material", f"{good} / {warn} / {bad}",
         "warn" if bad else "good"),
    ]

    def esc(x):
        return html.escape(str(x)) if x is not None else ""

    card_html = "".join(
        f'<div class="card {cls}"><div>{esc(label)}</div>'
        f"<strong>{esc(value)}</strong></div>"
        for label, value, cls in cards
    )

    rows_html = []
    for r in rows:
        verdict, cls = _verdict(r)
        live_p = _fmt_pips(r.get("report_pnl_pips"))
        bt_p = _fmt_pips(r.get("duka_bt_pnl_pips"))
        pnl_d = r.get("duka_pnl_delta_pips", "")
        live_vs = "same"
        try:
            v = float(pnl_d) if pnl_d != "" else None
            if v is not None and abs(v) >= 0.05:
                live_vs = f"{abs(v):.1f} pips {'better' if v > 0 else 'worse'}"
        except (TypeError, ValueError):
            live_vs = ""
        mt5_match = ("yes" if r.get("mt5_match_status") == "exact_signal_bar"
                     else "no")
        why_bits = []
        if mt5_match == "no":
            why_bits.append("MT5 replay did not produce this signal")
        if r.get("duka_bt_close_reason") and r.get("duka_bt_close_reason") != "SL":
            why_bits.append(
                f"backtest exited via {r['duka_bt_close_reason']}")
        try:
            if abs(float(pnl_d or 0)) > 5:
                why_bits.append("pnl differs by more than 5 pips")
        except (TypeError, ValueError):
            pass
        try:
            if abs(float(r.get("duka_exit_delta_min", 0) or 0)) > 10:
                why_bits.append("exit time differs by more than 10 minutes")
        except (TypeError, ValueError):
            pass
        if not why_bits:
            why_bits.append("prices and timing are close")

        rows_html.append(f"""
        <tr class="{cls}">
          <td>{esc(r['position_id'])}</td>
          <td>{esc(r['signal'])}</td>
          <td>{esc(r['pair'])}</td>
          <td>{esc(r['direction'])}</td>
          <td>{esc(str(r['open_time_utc']).replace('T', ' ')[5:19])}</td>
          <td>{esc(str(r['close_time_utc']).replace('T', ' ')[5:19])}</td>
          <td>{live_p}</td>
          <td>{bt_p}</td>
          <td><b>{esc(live_vs)}</b></td>
          <td>{esc(_fmt_price_impact(r.get('duka_entry_delta_pips'), 'entry'))}</td>
          <td>{esc(_fmt_price_impact(r.get('duka_exit_delta_pips'), 'exit'))}</td>
          <td>{esc(_fmt_time_delta(r.get('duka_exit_delta_min')))}</td>
          <td>{esc('yes' if r.get('duka_match_status') == 'exact_signal_bar' else 'no')}</td>
          <td>{esc(mt5_match)}</td>
          <td><span class="pill {cls}">{esc(verdict)}</span>
              <br><small>{esc('; '.join(why_bits))}</small></td>
        </tr>""")

    html_text = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Fire Forex reconciliation — clear view</title>
<style>
:root {{
  --good:#0c7a43; --warn:#9a6700; --bad:#b42318;
  --line:#d8dee8; --ink:#17202c; --muted:#566273; --bg:#f6f8fb;
}}
body {{ font-family: Segoe UI, Arial, sans-serif; margin:24px; color:var(--ink); background:white; }}
h1 {{ margin:0 0 4px; font-size:24px; }}
.note {{ color:var(--muted); margin:0 0 18px; max-width:1100px; line-height:1.45; }}
.cards {{ display:grid; grid-template-columns:repeat(5, minmax(150px, 1fr)); gap:10px; margin:18px 0; }}
.card {{ border:1px solid var(--line); border-left-width:5px; border-radius:8px; padding:10px 12px; background:var(--bg); }}
.card div {{ color:var(--muted); font-size:12px; }}
.card strong {{ display:block; font-size:21px; margin-top:4px; }}
.good {{ border-left-color:var(--good); }} .warn {{ border-left-color:var(--warn); }} .bad {{ border-left-color:var(--bad); }}
.explain {{ background:#fbfcfe; border:1px solid var(--line); border-radius:8px; padding:12px 16px; margin:16px 0; }}
.explain ul {{ margin:8px 0 0 18px; padding:0; }}
.explain li {{ margin:4px 0; }}
table {{ border-collapse:collapse; width:100%; font-size:12.5px; }}
th,td {{ border:1px solid var(--line); padding:7px 8px; text-align:right; vertical-align:top; }}
th {{ background:#eef2f7; position:sticky; top:0; }}
td:nth-child(-n+6), th:nth-child(-n+6),
td:nth-child(15), th:nth-child(15) {{ text-align:left; }}
tr.good {{ background:#f4fbf7; }}
tr.warn {{ background:#fffaf0; }}
tr.bad  {{ background:#fff7f6; }}
.pill {{ display:inline-block; border-radius:999px; padding:2px 8px; color:white; font-weight:600; font-size:11px; }}
.pill.good {{ background:var(--good); }}
.pill.warn {{ background:var(--warn); }}
.pill.bad  {{ background:var(--bad); }}
small {{ color:var(--muted); display:block; margin-top:4px; line-height:1.35; }}
</style></head>
<body>
  <h1>Fire Forex reconciliation &mdash; clear view</h1>
  <p class="note">Compares every closed MT5 live trade against Dukascopy and MT5 replay backtests. Generated {esc(stamp)}.</p>
  <div class="cards">{card_html}</div>
  <div class="explain">
    <b>How to read this</b>
    <ul>
      <li><b>Live pips</b>: what the trade actually made on MT5.</li>
      <li><b>Dukascopy pips</b>: what the same signal bar produced in the replay.</li>
      <li><b>Live vs Dukascopy</b>: positive means live beat the replay; negative means live underperformed.</li>
      <li><b>Entry / exit price</b>: shown as &ldquo;better / worse&rdquo; from the live trade's point of view.</li>
      <li><b>Exit time</b>: how far the live close was from the replay close.</li>
      <li><b>Overall reading</b>: Good match = close on price and time. Review = still close. Material = needs investigation.</li>
    </ul>
  </div>
  <table><thead><tr>
    <th>position</th><th>signal</th><th>pair</th><th>dir</th>
    <th>live open UTC</th><th>live close UTC</th>
    <th>live pips</th><th>Dukascopy pips</th><th>live vs Dukascopy</th>
    <th>entry price</th><th>exit price</th><th>exit time</th>
    <th>Dukascopy signal?</th><th>MT5 signal?</th><th>reading</th>
  </tr></thead><tbody>
{''.join(rows_html)}
  </tbody></table>
</body></html>
"""
    out.write_text(html_text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stamp", default=None,
                    help="Timestamp tag for the output files. Defaults to now.")
    ap.add_argument("--live-dir", type=Path, default=LIVE)
    ap.add_argument("--out-dir", type=Path, default=RECONCILE)
    args = ap.parse_args(argv)

    stamp = args.stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for inst in sorted(p for p in args.live_dir.glob("complexity_*")
                       if p.is_dir()):
        data = _load_instance(inst)
        duka = _latest_reconcile(inst, "dukascopy_live_vs_dukascopy")
        mt5 = _latest_reconcile(inst, "mt5_A_live_vs_duka")
        if mt5 is None:
            mt5 = _latest_reconcile(inst, "mt5_live_vs_mt5")
        for row in _round_trips(data):
            _attach_replay(row, duka, "duka")
            _attach_replay(row, mt5, "mt5")
            rows.append(row)

    rows.sort(key=lambda r: r.get("open_time_utc", ""))
    csv_path = args.out_dir / f"{stamp}_trade_comparison.csv"
    html_path = args.out_dir / f"{stamp}_trade_comparison.html"
    _write_csv(rows, csv_path)
    _write_html(rows, html_path, stamp)
    (args.out_dir / "latest.html").write_text(
        html_path.read_text(encoding="utf-8"), encoding="utf-8")

    good = sum(1 for r in rows if _verdict(r)[1] == "good")
    warn = sum(1 for r in rows if _verdict(r)[1] == "warn")
    bad = sum(1 for r in rows if _verdict(r)[1] == "bad")
    duka_m = sum(1 for r in rows if r.get("duka_match_status") == "exact_signal_bar")
    mt5_m = sum(1 for r in rows if r.get("mt5_match_status") == "exact_signal_bar")
    print(f"[build_trade_comparison] closed={len(rows)} "
          f"duka_match={duka_m} mt5_match={mt5_m} "
          f"good={good} review={warn} material={bad}")
    print(f"[build_trade_comparison] csv:  {csv_path}")
    print(f"[build_trade_comparison] html: {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
