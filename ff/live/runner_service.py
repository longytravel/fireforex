"""Runner service entry point — what the VPS Scheduled Task executes.

Reads broker credentials from ``.env.live`` and runtime config from
``artifacts/live/service_config.json`` (written by the web UI's
``POST /api/live/start``). On uncaught exception writes a crash record and
exits non-zero; the Scheduled Task restarts on failure every 60s.

Not imported by tests — Windows-only and depends on ``MetaTrader5``.
"""
from __future__ import annotations

import json
import logging
import signal
import sys
from pathlib import Path
from threading import Event


LOG = logging.getLogger(__name__)


_ROOT = Path(__file__).resolve().parent.parent.parent
_LIVE_DIR = _ROOT / "artifacts" / "live"
_SERVICE_CONFIG = _LIVE_DIR / "service_config.json"
_CRASHES_FILE = _LIVE_DIR / "crashes.jsonl"
_ENV_FILE = _ROOT / ".env.live"


def _log_crash(exc: BaseException) -> None:
    import traceback
    import pandas as pd

    row = {
        "ts": pd.Timestamp.now("UTC").isoformat(),
        "error": repr(exc),
        "traceback": traceback.format_exc(),
    }
    _CRASHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _CRASHES_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")


def _install_signal_handlers(stop_event: Event) -> None:
    def _handler(signum, _frame):  # noqa: ANN001
        LOG.info("[svc] signal %s received — draining", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, AttributeError):
            # Windows doesn't support all signals on all contexts.
            pass


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not _SERVICE_CONFIG.exists():
        LOG.error("[svc] no service_config.json at %s — write one via the web UI first",
                  _SERVICE_CONFIG)
        return 2

    try:
        from ff.live import broker_mt5, runner

        service_cfg = json.loads(_SERVICE_CONFIG.read_text(encoding="utf-8"))
        creds = broker_mt5.load_broker_cfg_from_env(_ENV_FILE)

        broker_profile = {
            **creds,
            "deviation_pips": service_cfg.get("deviation_pips", 3.0),
            "magic_number": service_cfg.get("magic_number", 20260420),
            "symbol_map": service_cfg.get("symbol_map", {}),
        }

        cfg = runner.LiveConfig(
            recipe=service_cfg["recipe"],
            overrides=service_cfg.get("overrides") or {},
            pairs=list(service_cfg["pairs"]),
            broker=runner.BrokerCfg(**broker_profile),
            poll_interval_sec=float(service_cfg.get("poll_interval_sec", 10.0)),
            size_lots=float(service_cfg.get("size_lots", 0.01)),
            best_trial=service_cfg.get("best_trial"),
        )

        stop_event = Event()
        _install_signal_handlers(stop_event)
        runner.run(cfg, stop_event)
        return 0
    except Exception as exc:  # noqa: BLE001
        LOG.exception("[svc] crash")
        _log_crash(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
