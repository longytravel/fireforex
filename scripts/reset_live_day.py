"""Archive today's live logs + flatten MT5 positions on the demo.

One-command "clean slate" for debugging. Used when you want to start
fresh and not have old plans/tickets/state pollute the reconciler.

Steps:
    1. Stop the ff-live-runner Scheduled Task so nothing fires mid-wipe.
    2. Close MT5 positions. By default: EVERY open position on the
       account (accumulated across past deploys with different magic
       numbers tends to leave orphans). Pass ``--magic-only`` to revert
       to the conservative magic-number filter.
    3. Archive artifacts/live/{plans, tickets.jsonl, state.json, errors.jsonl,
       crashes.jsonl} → artifacts/live/archive/<YYYYMMDD_HHMMSS>/.
    4. Delete the originals so the runner starts cold.

Runner stays stopped. Re-arming is the Deploy button's job — a reset
without a deliberate redeploy should not resume trading.

Everything is archived — nothing is destroyed. To recover: copy the
archive dir back into artifacts/live/.

Runs on the VPS. Requires MetaTrader5 package + .env.live credentials.
"""
from __future__ import annotations

import argparse
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
    "service_config.json",   # legacy pre-multi-instance config
    "instances.json",        # multi-instance index
    "runner.log",            # shared process log
    "state_sync_errors.jsonl",
]

# In multi-instance layout, every artifacts/live/<instance_id>/ subdir is
# archived in full. ARCHIVE_TARGETS above covers the flat top-level files.


def _stop_task() -> None:
    """End the current run AND disable future triggers.

    ``schtasks /End`` only stops the active instance — the task stays in
    Ready state and any auto-trigger (onLogon, interval) re-launches it,
    which reappeared after reset as "trading resumed with the old
    service_config.json". Pair ``/End`` with ``/Change /DISABLE`` so the
    runner truly stays down until Deploy re-enables it.
    """
    print(f"[reset] ending + disabling Scheduled Task {SCHEDULED_TASK}...")
    subprocess.run(
        ["schtasks", "/End", "/TN", SCHEDULED_TASK],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["schtasks", "/Change", "/TN", SCHEDULED_TASK, "/DISABLE"],
        capture_output=True, check=False,
    )


def _start_task() -> None:
    print(f"[reset] enabling + starting Scheduled Task {SCHEDULED_TASK}...")
    subprocess.run(
        ["schtasks", "/Change", "/TN", SCHEDULED_TASK, "/ENABLE"],
        capture_output=True, check=False,
    )
    subprocess.run(
        ["schtasks", "/Run", "/TN", SCHEDULED_TASK],
        capture_output=True, check=False,
    )


def _flatten_positions(magic_only: bool = False) -> int:
    """Close open MT5 positions. Returns count closed.

    ``magic_only=False`` (default): closes every open position on the
    account, regardless of who placed it. Matches the user's expectation
    that "reset clears the demo" when orphan positions from old deploys
    or from a different magic linger.

    ``magic_only=True``: close only positions matching the Fire Forex
    magic number in ``service_config.json``. Use this on accounts shared
    with other EAs so manual/third-party trades survive the reset.
    """
    cfg: dict = {}
    if SERVICE_CONFIG.exists():
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
        print("[reset] MetaTrader5 package not available -- skipping flatten")
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
        if magic_only:
            ours = [p for p in positions if int(p.magic) == magic]
            print(f"[reset] {len(ours)} position(s) with magic={magic} to flatten "
                  f"(leaving {len(positions) - len(ours)} other positions untouched)")
        else:
            ours = list(positions)
            print(f"[reset] {len(ours)} open position(s) on the account -- "
                  f"closing ALL (pass --magic-only to filter by Fire Forex magic)")

        for p in ours:
            tick = mt5.symbol_info_tick(p.symbol)
            if tick is None:
                print(f"[reset]  {p.symbol} no tick -- SKIP")
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


def _archive_and_wipe(only_instance: str | None = None) -> Path:
    """Move today's live artifacts into a stamped archive dir.

    Multi-instance layout: every ``artifacts/live/<instance_id>/`` subdir
    (that is not ``archive`` itself) moves under
    ``archive/<stamp>/<instance_id>/``. Top-level flat files in
    ARCHIVE_TARGETS also move.

    ``only_instance`` restricts archival to one instance_id; top-level
    files stay where they are. Use for per-instance pause/cleanup.
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = ARCHIVE_ROOT / stamp
    dst.mkdir(parents=True, exist_ok=True)

    print(f"[reset] archiving live files -> {dst}"
          f"{' (instance=' + only_instance + ')' if only_instance else ''}")

    # 1. Per-instance subdirs.
    for sub in sorted(LIVE_DIR.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name == "archive":
            continue
        if sub.name == "plans" and only_instance is None:
            # legacy flat plans dir — handled via ARCHIVE_TARGETS below
            continue
        if only_instance is not None and sub.name != only_instance:
            continue
        target = dst / sub.name
        shutil.move(str(sub), str(target))
        print(f"[reset]  moved instance dir {sub.name}/")

    # 2. Flat top-level files (only when archiving everything).
    if only_instance is None:
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
        # Recreate empty plans dir so any legacy-path writer survives.
        (LIVE_DIR / "plans").mkdir(exist_ok=True)

    return dst


def main() -> int:
    parser = argparse.ArgumentParser(description="Fire Forex VPS reset")
    parser.add_argument(
        "--magic-only", action="store_true",
        help="Only close positions matching the Fire Forex magic number. "
             "Default is to close EVERY open position on the account.",
    )
    parser.add_argument(
        "--restart", action="store_true",
        help="Re-arm the ff-live-runner Scheduled Task after the wipe. "
             "Default leaves it stopped -- Deploy from the laptop is the "
             "intended way to resume trading.",
    )
    parser.add_argument(
        "--instance", type=str, default=None,
        help="Archive + flatten only this instance_id. "
             "Omit to reset everything.",
    )
    args = parser.parse_args()

    print("=" * 60)
    print(f"Fire Forex -- reset live day  ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    print("=" * 60)

    _stop_task()
    time.sleep(2)  # let the runner notice stop + release file handles

    try:
        closed = _flatten_positions(magic_only=args.magic_only)
        print(f"[reset] flattened {closed} position(s)")
    except Exception as exc:  # noqa: BLE001
        print(f"[reset] flatten failed: {exc!r} -- continuing to archive anyway")

    archive_dir = _archive_and_wipe(only_instance=args.instance)
    print(f"[reset] live state archived at {archive_dir}")

    if args.restart:
        _start_task()
        tail = "Runner restarted."
    else:
        tail = "Runner STOPPED. Deploy from the laptop UI to resume trading."

    print("=" * 60)
    print(f"Done. {tail}")
    print("Archived data is in:", archive_dir)
    print("=" * 60)
    return 0


if __name__ == "__main__":
    # Ensure `import ff.live.broker_mt5` works.
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
