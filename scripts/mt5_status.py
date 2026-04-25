"""Live MT5 status dump — what's open RIGHT NOW + account snapshot.

Hits the running MT5 terminal directly (same connection model as
``import_mt5_report.py``) and prints:

  * account balance / equity / margin / free margin / floating P&L
  * every currently-open position with unrealised P&L and SL/TP
  * every pending order (limit/stop) waiting to fire
  * symbol-by-symbol live spread for each pair the active config trades

Run::

    .\\.venv\\Scripts\\python.exe scripts/mt5_status.py
    .\\.venv\\Scripts\\python.exe scripts/mt5_status.py --pairs EURUSD GBPJPY

The MT5 terminal must be open and logged in.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reconfigure stdout to UTF-8 on Windows (cp1252 default chokes on plus/minus signs).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "artifacts" / "live" / "incoming"


def _connect_mt5() -> tuple[Any, int]:
    """Connect to MT5; return (mt5 module, broker-to-UTC offset in seconds).

    Same pattern as `ff/live/broker_mt5.py` — MT5 timestamps are broker-local
    (e.g. IC Markets = GMT+2/+3); always apply the offset before formatting.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError as e:
        raise RuntimeError("MetaTrader5 not installed (`pip install MetaTrader5`).") from e
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize() failed: {mt5.last_error()}")

    import time as _time

    broker_to_utc_sec = 0
    tick = mt5.symbol_info_tick("EURUSD")
    if tick is not None and tick.time:
        broker_to_utc_sec = -(int(tick.time) - int(_time.time()))
    return mt5, broker_to_utc_sec


def _format_account(info: Any) -> str:
    return (
        f"Account #{info.login} ({info.server})\n"
        f"  Balance:        {info.balance:>10.2f} {info.currency}\n"
        f"  Equity:         {info.equity:>10.2f} {info.currency}\n"
        f"  Floating P&L:   {info.profit:>+10.2f} {info.currency}\n"
        f"  Margin used:    {info.margin:>10.2f} {info.currency}\n"
        f"  Free margin:    {info.margin_free:>10.2f} {info.currency}\n"
        f"  Leverage:       1:{info.leverage}"
    )


def _format_positions(positions: tuple, mt5: Any, offset_sec: int) -> str:
    if not positions:
        return "Open positions: none."
    lines = [f"Open positions: {len(positions)}"]
    for p in positions:
        side = "BUY " if p.type == mt5.POSITION_TYPE_BUY else "SELL"
        opened = datetime.fromtimestamp(p.time + offset_sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        lines.append(
            f"  {side} {p.symbol:<8} vol={p.volume:.2f} @ {p.price_open:.5f}"
            f" -> cur {p.price_current:.5f}  P&L {p.profit:+.2f}"
            f"  SL={p.sl or '-':<8} TP={p.tp or '-':<8}  opened {opened} UTC  ({p.comment or 'no comment'})"
        )
    return "\n".join(lines)


def _format_orders(orders: tuple, mt5: Any) -> str:
    if not orders:
        return "Pending orders: none."
    lines = [f"Pending orders: {len(orders)}"]
    for o in orders:
        type_name = {
            mt5.ORDER_TYPE_BUY_LIMIT: "BUY_LIMIT",
            mt5.ORDER_TYPE_SELL_LIMIT: "SELL_LIMIT",
            mt5.ORDER_TYPE_BUY_STOP: "BUY_STOP",
            mt5.ORDER_TYPE_SELL_STOP: "SELL_STOP",
        }.get(o.type, str(o.type))
        lines.append(f"  {type_name:<10} {o.symbol:<8} vol={o.volume_initial:.2f} @ {o.price_open:.5f}  ({o.comment or 'no comment'})")
    return "\n".join(lines)


def _format_spreads(symbols: list[str], mt5: Any) -> str:
    if not symbols:
        return ""
    lines = ["Live spreads:"]
    for sym in symbols:
        info = mt5.symbol_info(sym)
        tick = mt5.symbol_info_tick(sym)
        if info is None or tick is None:
            lines.append(f"  {sym:<8} (no data — symbol not in market-watch?)")
            continue
        # 5-digit / 3-digit FX brokers quote in fractional pips (1 pip = 10 points);
        # 4-digit / 2-digit symbols (some Gold / Index) quote whole pips (1 pip = 1 point).
        pip_factor = 10 if info.digits in (3, 5) else 1
        spread_pips = (tick.ask - tick.bid) / info.point / pip_factor
        lines.append(
            f"  {sym:<8} bid={tick.bid:.5f} ask={tick.ask:.5f} spread={spread_pips:.1f} pips  "
            f"swap_long={info.swap_long:+.2f} swap_short={info.swap_short:+.2f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pairs",
        nargs="*",
        default=None,
        help="Symbols to show live spreads for (default: union of open positions + pending orders).",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help=f"Also write the snapshot to {DEFAULT_OUT}/mt5_status_<stamp>.json",
    )
    args = parser.parse_args(argv)

    try:
        mt5, offset_sec = _connect_mt5()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    try:
        info = mt5.account_info()
        positions = mt5.positions_get() or ()
        orders = mt5.orders_get() or ()

        if args.pairs is None:
            symbols = sorted({p.symbol for p in positions} | {o.symbol for o in orders})
        else:
            symbols = args.pairs

        sections = [
            f"=== MT5 status @ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')} ===",
            "",
            _format_account(info),
            "",
            _format_positions(positions, mt5, offset_sec),
            "",
            _format_orders(orders, mt5),
            "",
            _format_spreads(symbols, mt5),
        ]
        report = "\n".join(s for s in sections if s != "")
        print(report)

        if args.save:
            DEFAULT_OUT.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
            out = DEFAULT_OUT / f"mt5_status_{stamp}.json"
            payload = {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "account": {
                    "login": info.login,
                    "server": info.server,
                    "currency": info.currency,
                    "balance": info.balance,
                    "equity": info.equity,
                    "profit": info.profit,
                    "margin": info.margin,
                    "margin_free": info.margin_free,
                    "leverage": info.leverage,
                },
                "positions": [
                    {
                        "ticket": p.ticket,
                        "symbol": p.symbol,
                        "type": "buy" if p.type == mt5.POSITION_TYPE_BUY else "sell",
                        "volume": p.volume,
                        "price_open": p.price_open,
                        "price_current": p.price_current,
                        "sl": p.sl,
                        "tp": p.tp,
                        "profit": p.profit,
                        "comment": p.comment,
                        "time_open": datetime.fromtimestamp(p.time + offset_sec, tz=timezone.utc).isoformat(),
                    }
                    for p in positions
                ],
                "pending_orders": [
                    {
                        "ticket": o.ticket,
                        "symbol": o.symbol,
                        "type": int(o.type),
                        "volume": o.volume_initial,
                        "price_open": o.price_open,
                        "comment": o.comment,
                    }
                    for o in orders
                ],
            }
            out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"\nSaved snapshot: {out}")
        return 0
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    sys.exit(main())
