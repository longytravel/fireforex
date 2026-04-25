"""One-shot VPS diagnostic dumper.

Double-click ``Diagnose Fire Forex.bat`` on the VPS desktop. Writes a
single text blob to ``artifacts/live/diag_<stamp>.txt`` AND echoes it to
stdout so the pause window shows it. Meant for when laptop Claude has
no SSH yet and the user needs a one-paste "what is this VPS actually
doing" report.

Collects (best-effort, survives partial failures):
    - git HEAD + any uncommitted changes
    - schtasks state for ff-live-runner + ff-web
    - service_config.json shape (pairs, max_open_per_pair, best_trial
      group on/off)
    - MT5 open positions (pair, type, magic, comment, volume, ticket,
      time)
    - MT5 last 20 deals (time, ticket, magic, symbol, type, volume,
      price, profit)
    - artifacts/live/ directory listing + sizes
    - Tail of crashes.jsonl + errors.jsonl (last 10 each)
    - Tail of the scheduled task stdout (if we can find it — Windows
      task output lives in Event Viewer, we note the command here)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DIR = REPO_ROOT / "artifacts" / "live"
DIAG_PATH = LIVE_DIR / f"diag_{time.strftime('%Y%m%d_%H%M%S')}.txt"


def _run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = (r.stdout or "") + (("\n-- stderr --\n" + r.stderr) if r.stderr else "")
        return out.strip() or "(empty)"
    except Exception as e:  # noqa: BLE001
        return f"(subprocess failed: {e!r})"


def _section(title: str) -> str:
    bar = "=" * 60
    return f"\n{bar}\n{title}\n{bar}\n"


def _git_state() -> str:
    out = _section("git")
    out += "git log -1 --oneline:\n" + _run(["git", "log", "-1", "--oneline"])
    out += "\n\ngit status --short:\n" + _run(["git", "status", "--short"])
    out += "\n\nremote live-state ref (expected after first state_sync push):\n"
    out += _run(["git", "ls-remote", "origin", "live-state"])
    return out


def _tasks() -> str:
    out = _section("scheduled tasks")
    for tn in ("ff-live-runner", "ff-web"):
        out += f"\n--- {tn} ---\n"
        out += _run(["schtasks", "/Query", "/TN", tn, "/V", "/FO", "LIST"])
    return out


def _config_one(cfg: dict, origin: str) -> dict:
    summary: dict[str, object] = {
        "origin": origin,
        "instance_id": cfg.get("instance_id"),
        "source_run_id": cfg.get("source_run_id"),
        "pairs": cfg.get("pairs"),
        "max_open_per_pair": cfg.get("max_open_per_pair"),
        "magic_number": cfg.get("magic_number"),
        "poll_interval_sec": cfg.get("poll_interval_sec"),
    }
    bt = cfg.get("best_trial") or {}
    engine = bt.get("engine") or {}
    summary["best_trial.signal_variant"] = bt.get("signal_variant")
    summary["best_trial.groups_on"] = {
        name: bool((engine.get(name) or {}).get("test"))
        for name in (
            "trailing",
            "breakeven",
            "chandelier",
            "partial",
            "session",
            "stale",
            "max_bars",
        )
    }
    return summary


def _config() -> str:
    out = _section("instances")
    configs: list[dict] = []

    # Prefer the multi-instance layout.
    for sub in sorted(LIVE_DIR.glob("*/config.json")):
        if sub.parent.name in ("archive", "reconcile"):
            continue
        try:
            configs.append(
                _config_one(
                    json.loads(sub.read_text(encoding="utf-8")),
                    origin=str(sub.relative_to(LIVE_DIR.parent.parent)),
                )
            )
        except Exception as e:  # noqa: BLE001
            configs.append({"origin": str(sub), "error": repr(e)})

    # Legacy flat config (pre-migration) — still show if present.
    legacy = LIVE_DIR / "service_config.json"
    if legacy.exists():
        try:
            configs.append(
                _config_one(
                    json.loads(legacy.read_text(encoding="utf-8")),
                    origin="service_config.json (legacy)",
                )
            )
        except Exception as e:  # noqa: BLE001
            configs.append({"origin": str(legacy), "error": repr(e)})

    if not configs:
        return out + "(no configs found)\n"

    out += json.dumps(configs, indent=2, default=str)

    # Also surface instances.json if present (active/inactive registry).
    index_file = LIVE_DIR / "instances.json"
    if index_file.exists():
        try:
            out += "\n\ninstances.json:\n" + json.dumps(
                json.loads(index_file.read_text(encoding="utf-8")),
                indent=2,
                default=str,
            )
        except Exception as e:  # noqa: BLE001
            out += f"\n\ninstances.json parse error: {e!r}"
    return out


def _mt5() -> str:
    out = _section("MT5")
    try:
        from ff.live import broker_mt5 as _b

        env_file = REPO_ROOT / ".env.live"
        if env_file.exists():
            _b.load_broker_cfg_from_env(env_file)
        else:
            return out + f"(no .env.live at {env_file})\n"
        try:
            import MetaTrader5 as mt5  # type: ignore
        except ImportError:
            return out + "(MetaTrader5 package not installed)\n"

        if not mt5.initialize(
            login=int(os.environ["MT5_LOGIN"]),
            password=os.environ["MT5_PASSWORD"],
            server=os.environ["MT5_SERVER"],
            path=os.environ.get("MT5_TERMINAL_PATH") or None,
        ):
            return out + f"mt5.initialize FAILED: {mt5.last_error()!r}\n"

        try:
            out += "account:\n"
            ai = mt5.account_info()
            if ai is not None:
                out += f"  login={ai.login} balance={ai.balance} equity={ai.equity} server={ai.server}\n"
            positions = mt5.positions_get() or []
            out += f"\nopen positions: {len(positions)}\n"
            for p in positions:
                side = "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL"
                out += (
                    f"  ticket={p.ticket} {p.symbol:<8} {side:<4} "
                    f"vol={p.volume} magic={p.magic} "
                    f"comment='{p.comment}' open_price={p.price_open} "
                    f"sl={p.sl} tp={p.tp} "
                    f"time={time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(p.time))}\n"
                )
            since = int(time.time()) - 24 * 3600
            deals = mt5.history_deals_get(since, int(time.time())) or []
            deals = list(deals)[-20:]
            out += f"\nlast {len(deals)} deals (24h):\n"
            for d in deals:
                out += (
                    f"  ticket={d.ticket} pos={d.position_id} {d.symbol:<8} "
                    f"type={d.type} vol={d.volume} price={d.price} "
                    f"magic={d.magic} comment='{d.comment}' profit={d.profit} "
                    f"time={time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(d.time))}\n"
                )
        finally:
            mt5.shutdown()
    except Exception as e:  # noqa: BLE001
        out += f"(MT5 section crashed: {e!r})\n{traceback.format_exc()}"
    return out


def _artifacts_listing() -> str:
    out = _section("artifacts/live/ listing")
    if not LIVE_DIR.exists():
        return out + "(dir missing)\n"
    for entry in sorted(LIVE_DIR.iterdir()):
        if entry.is_dir():
            size = sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
            count = sum(1 for _ in entry.rglob("*") if _.is_file())
            out += f"  [dir]  {entry.name:<30} {count} files, {size} bytes\n"
        else:
            out += f"  [file] {entry.name:<30} {entry.stat().st_size} bytes\n"
    return out


def _tail(path: Path, n: int = 10) -> str:
    if not path.exists():
        return "(file missing)"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:  # noqa: BLE001
        return f"(read failed: {e!r})"
    tail = lines[-n:]
    return "\n".join(tail) if tail else "(empty)"


def _log_tails() -> str:
    out = _section("log tails (last 10 lines each)")
    out += "--- crashes.jsonl ---\n" + _tail(LIVE_DIR / "crashes.jsonl") + "\n"
    out += "\n--- errors.jsonl ---\n" + _tail(LIVE_DIR / "errors.jsonl") + "\n"
    return out


def main() -> int:
    parts: list[str] = []
    parts.append(f"Fire Forex -- VPS diagnostic  ({time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())})")
    parts.append(f"Report path: {DIAG_PATH}")
    for fn in (_git_state, _tasks, _config, _artifacts_listing, _log_tails, _mt5):
        try:
            parts.append(fn())
        except Exception as e:  # noqa: BLE001
            parts.append(_section(fn.__name__) + f"(section crashed: {e!r})\n{traceback.format_exc()}")
    blob = "\n".join(parts)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    DIAG_PATH.write_text(blob, encoding="utf-8")
    print(blob)
    print()
    print(f"[diag] wrote {DIAG_PATH}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(REPO_ROOT))
    sys.exit(main())
