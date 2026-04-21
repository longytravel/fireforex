"""Archive today's live logs + flatten Fire Forex MT5 positions.

One-command "clean slate" for debugging. Used when you want to start
fresh and not have old plans/tickets/state pollute the reconciler.

Steps:
    1. Stop the ff-live-runner Scheduled Task so nothing fires mid-wipe.
    2. Close every MT5 position whose magic number matches service_config.
    3. Archive artifacts/live/{plans, tickets.jsonl, state.json, errors.jsonl,
       crashes.jsonl} → artifacts/live/archive/<YYYYMMDD_HHMMSS>/.
    4. Delete the originals so the runner starts cold.
    5. Re-arm the Scheduled Task.

Everything is archived — nothing is destroyed. To recover: copy the
archive dir back into artifacts/live/.

Runs on the VPS. Requires MetaTrader5 package + .env.live credentials.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = REPO_ROOT / "artifacts" / "live"
ARCHIVE_ROOT = LIVE_DIR / "archive"
SERVICE_CONFIG = LIVE_DIR / "service_config.json"
SCHEDULED_TASK = "ff-live-runner"

ARCHIVE_TARGETS = [
    "plans",                 # whole directory
    "tickets.jsonl",
    "state.json",
    "errors.jsonl",
    "crashes.jsonl",
]


def _stop_task() -> None:
    print(f"[reset] stopping Scheduled Task {SCHEDULED_TASK}...")
    subprocess.run(
        ["schtasks", "/End", "/TN", SCHEDULED_TASK],
        capture_output=True, check=False,
    )


def _start_task() -> None:
    print(f"[reset] starting Scheduled Task {SCHEDULED_TASK}...")
    subprocess.run(
        ["schtasks", "/Run", "/TN", SCHEDULED_TASK],
        capture_output=True, check=False,
    )


def _flatten_positions() -> int:
    """Close every Fire Forex position via MT5. Returns count closed."""
    if not SERVICE_CONFIG.exists():
        print("[reset] no service_config.json — skipping flatten")
        return 0
    cfg = json.loads(SERVICE_CONFIG.read_text(encoding="utf-8"))
    magic = int(cfg.get("magic_number", 20260420))

    # Load MT5 credentials same path the runner uses.
    from ff.live import broker_mt5 as _b
    env_file = REPO_ROOT / ".env.live"
    if env_file.exists():
        _b.load_broker_cfg_from_env(env_file)
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError:
        print("[reset] MetaTrader5 package not available — skipping flatten")
        return 0

    if not mt5.initialize(
        login=int(os.environ["MT5_LOGIN"]),
        password=os.environ["MT5_PASSWORD"],
        server=os.environ["MT5_SERVER"],
        path=os.environ.get("MT5_TERMINAL_PATH") or None,
    ):
        print(f"[reset] mt5.initialize failed: {mt5.last_error()}")
        return 0

    closed = 0
    try:
        positions = mt5.positions_get() or []
        ours = [p for p in positions if int(p.magic) == magic]
        print(f"[reset] {len(ours)} position(s) with magic={magic} to flatten")

        for p in ours:
            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None:
                print(f"[reset]  {p.symbol} no tick — SKIP")
                continue
            opposite_type = (
                mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY
                else mt5.ORDER_TYPE_BUY
            )
            price = tick.bid if opposite_type == mt5.ORDER_TYPE_SELL else tick.ask
            req = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": p.symbol,
                "position": int(p.ticket),
                "volume": float(p.volume),
                "type": opposite_type,
                "price": float(price),
                "deviation": 20,
                "magic": magic,
                "comment": "fireforex-reset",
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            r = mt5.order_send(req)
            if r is not None and r.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"[reset]  closed #{p.ticket} {p.symbol}")
                closed += 1
            else:
                rc = r.retcode if r is not None else "None"
                print(f"[reset]  close #{p.ticket} {p.symbol} FAILED retcode={rc}")
    finally:
        mt5.shutdown()
    return closed


def _archive_and_wipe() -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = ARCHIVE_ROOT / stamp
    dst.mkdir(parents=True, exist_ok=True)

    print(f"[reset] archiving today's live files → {dst}")
    for name in ARCHIVE_TARGETS:
        src = LIVE_DIR / name
        if not src.exists():
            continue
        target = dst / name
        if src.is_dir():
            shutil.move(str(src), str(target))
        else:
            shutil.move(str(src), str(target))
        print(f"[reset]  moved {name}")
    # Recreate empty plans dir so the runner writes into it on boot.
    (LIVE_DIR / "plans").mkdir(exist_ok=True)
    return dst


def main() -> int:
    print("=" * 60)
    print(f"Fire Forex · reset live day  ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    print("=" * 60)

    _stop_task()
    time.sleep(2)  # let the runner notice stop + release file handles

    try:
        closed = _flatten_positions()
        print(f"[reset] flattened {closed} position(s)")
    except Exception as exc:  # noqa: BLE001
        print(f"[reset] flatten failed: {exc!r} — continuing to archive anyway")

    archive_dir = _archive_and_wipe()
    print(f"[reset] live state archived at {archive_dir}")

    _start_task()
    print("=" * 60)
    print("Done. Runner back up, state.json will rebuild from MT5 snapshot.")
    print("Archived data is in:", archive_dir)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    # Ensure `import ff.live.broker_mt5` works.
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
