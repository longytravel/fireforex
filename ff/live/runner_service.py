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
import time
from pathlib import Path
from threading import Event
from typing import Any

import pandas as pd

LOG = logging.getLogger(__name__)


_ROOT = Path(__file__).resolve().parent.parent.parent
_LIVE_DIR = _ROOT / "artifacts" / "live"
_SERVICE_CONFIG = _LIVE_DIR / "service_config.json"
_CRASHES_FILE = _LIVE_DIR / "crashes.jsonl"
_ENV_FILE = _ROOT / ".env.live"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


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


def _distribute_deploy_configs() -> None:
    """Copy active ``deploy/instances/<id>.json`` files into
    ``artifacts/live/<id>/config.json``. Registers each in
    ``instances.json``. Runs on every boot so a VPS pull-then-restart
    picks up newly-deployed instances without any bat changes.

    Source of truth for "which instances should run" is
    ``deploy/instances/active.json`` — a committed manifest updated by
    the laptop UI's Deploy button. If it's missing (older repo state),
    falls back to "every config file" but logs a warning so the user
    knows to upgrade the deploy flow.

    ``artifacts/live/`` is gitignored; ``deploy/instances/`` is the
    committed source.
    """
    deploy_dir = _ROOT / "deploy" / "instances"
    if not deploy_dir.exists():
        return

    active_manifest = deploy_dir / "active.json"
    active_ids: set[str] | None
    if active_manifest.exists():
        try:
            manifest = _read_json(active_manifest)
            active_ids = set(manifest.get("active") or [])
        except (json.JSONDecodeError, TypeError):
            LOG.warning("[svc] deploy/instances/active.json unparseable; treating as empty")
            active_ids = set()
    else:
        LOG.warning("[svc] deploy/instances/active.json missing — distributing every config file (legacy behaviour)")
        active_ids = None  # None means "no filter"

    index_file = _LIVE_DIR / "instances.json"
    index: dict[str, Any] = {"magic_counter": 20260420, "instances": {}}
    if index_file.exists():
        try:
            index = _read_json(index_file)
            index.setdefault("magic_counter", 20260420)
            index.setdefault("instances", {})
        except (json.JSONDecodeError, TypeError):
            pass

    added = 0
    for deploy_cfg in sorted(deploy_dir.glob("*.json")):
        if deploy_cfg.name == "active.json":
            continue
        instance_id_from_filename = deploy_cfg.stem
        if active_ids is not None and instance_id_from_filename not in active_ids:
            continue
        inst_dir = _LIVE_DIR / instance_id_from_filename
        live_cfg = inst_dir / "config.json"
        if live_cfg.exists():
            continue
        try:
            cfg = _read_json(deploy_cfg)
        except (json.JSONDecodeError, OSError):
            LOG.warning("[svc] bad deploy config %s — skipping", deploy_cfg)
            continue

        # Force instance_id to the filename stem — prevents a drift
        # between on-disk identity and config-embedded identity.
        cfg_id = cfg.get("instance_id")
        if cfg_id and cfg_id != instance_id_from_filename:
            LOG.warning(
                "[svc] deploy file %s has instance_id=%r mismatch — forcing to filename stem %r",
                deploy_cfg.name,
                cfg_id,
                instance_id_from_filename,
            )
        cfg["instance_id"] = instance_id_from_filename

        inst_dir.mkdir(parents=True, exist_ok=True)
        live_cfg.write_text(json.dumps(cfg, indent=2, default=str), encoding="utf-8")

        # pinned_run.json for the auto-reconciler.
        run_id = cfg.get("source_run_id")
        if run_id:
            (inst_dir / "pinned_run.json").write_text(json.dumps({"run_id": run_id}, indent=2), encoding="utf-8")

        index["instances"][instance_id_from_filename] = {
            "active": True,
            "source_run_id": run_id,
            "magic": int(cfg.get("magic_number") or 0),
            "pairs": list(cfg.get("pairs") or []),
            "started_at": pd.Timestamp.now("UTC").isoformat(),
            "distributed_from_deploy": True,
        }
        added += 1
        LOG.info("[svc] distributed deploy instance -> %s", live_cfg)

    # Deactivate any instance that is present in instances.json but NOT
    # in active.json. Without this, removing an id from active.json only
    # stops future distribution — an existing artifacts/live/<id>/
    # directory would keep trading. Runtime source of truth is
    # instances.json.active (filtered by _discover_instance_configs).
    deactivated = 0
    if active_ids is not None:
        for iid, meta in list(index.get("instances", {}).items()):
            if iid not in active_ids and meta.get("active", True):
                meta["active"] = False
                meta["deactivated_at"] = pd.Timestamp.now("UTC").isoformat()
                deactivated += 1
                LOG.info("[svc] deactivated instance %s (removed from deploy/instances/active.json)", iid)

    if added or deactivated:
        index_file.write_text(json.dumps(index, indent=2), encoding="utf-8")


def _discover_instance_configs() -> list[Path]:
    """Find every active instance's config.json under artifacts/live/.

    Runtime source of truth is ``artifacts/live/instances.json``. The
    deploy-side ``deploy/instances/active.json`` is import/deactivate
    only — it's applied to instances.json by
    ``_distribute_deploy_configs()`` before this function runs.

    Skips the legacy top-level service_config.json (handled separately
    via auto-migration).
    """
    configs: list[Path] = []
    active_filter: dict[str, bool] = {}
    index_file = _LIVE_DIR / "instances.json"
    if index_file.exists():
        try:
            idx = _read_json(index_file)
            for iid, meta in (idx.get("instances") or {}).items():
                active_filter[iid] = bool(meta.get("active", True))
        except (json.JSONDecodeError, TypeError):
            pass

    for sub in sorted(_LIVE_DIR.glob("*/config.json")):
        iid = sub.parent.name
        if iid == "archive" or iid == "reconcile":
            continue
        if active_filter.get(iid, True):
            configs.append(sub)
    return configs


def _auto_migrate_legacy() -> Path | None:
    """If only the flat artifacts/live/service_config.json exists and no
    instance subdirs do, move today's files into
    artifacts/live/<instance_id>/. Returns the new config path, or None
    if there was nothing to migrate.

    Preserves the config's embedded ``instance_id`` when present so a
    legacy file written by the new Deploy endpoint (which carries an
    instance_id field) does NOT get a duplicate identity when combined
    with its ``deploy/instances/`` counterpart.

    Also skips migration when the same instance is already present as
    a ``deploy/instances/<id>.json`` — distribution will handle it.
    """
    if not _SERVICE_CONFIG.exists():
        return None
    if list(_LIVE_DIR.glob("*/config.json")):
        LOG.warning(
            "[svc] legacy %s exists alongside instance subdirs — ignoring; archive it manually",
            _SERVICE_CONFIG,
        )
        return None

    import shutil

    cfg = _read_json(_SERVICE_CONFIG)
    src_run = cfg.get("source_run_id") or "legacy"

    # If this config carries an instance_id AND a matching
    # deploy/instances/<id>.json exists, skip migration — the
    # distributor will create the instance dir from the deploy file and
    # double-migration would duplicate magic.
    embedded_id = cfg.get("instance_id")
    deploy_dir = _ROOT / "deploy" / "instances"
    if embedded_id and (deploy_dir / f"{embedded_id}.json").exists():
        LOG.info(
            "[svc] legacy service_config.json has instance_id=%r "
            "already in deploy/instances — skipping migrate; "
            "distribute_deploy_configs will handle it.",
            embedded_id,
        )
        return None

    stamp = time.strftime("%Y%m%d_%H%M%S")
    instance_id = embedded_id or f"{src_run}__{stamp}"
    cfg["instance_id"] = instance_id
    cfg.setdefault("magic_number", 20260420)

    inst_dir = _LIVE_DIR / instance_id
    inst_dir.mkdir(parents=True, exist_ok=True)
    (inst_dir / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    for name in (
        "plans",
        "tickets.jsonl",
        "state.json",
        "errors.jsonl",
        "pinned_run.json",
        "reconcile",
    ):
        src = _LIVE_DIR / name
        if src.exists():
            shutil.move(str(src), str(inst_dir / name))

    # Remove the legacy config from the flat path — it's now inside
    # the instance dir. Keep a copy under archive/ for safety.
    archive_dir = _LIVE_DIR / "archive" / f"legacy_migrate_{stamp}"
    archive_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_SERVICE_CONFIG, archive_dir / "service_config.json")
    _SERVICE_CONFIG.unlink()

    # Register in instances.json.
    index_file = _LIVE_DIR / "instances.json"
    idx: dict[str, Any] = {"magic_counter": int(cfg["magic_number"]) + 1, "instances": {}}
    if index_file.exists():
        try:
            idx = _read_json(index_file)
            idx.setdefault("magic_counter", int(cfg["magic_number"]) + 1)
            idx.setdefault("instances", {})
        except (json.JSONDecodeError, TypeError):
            pass
    idx["instances"][instance_id] = {
        "active": True,
        "source_run_id": src_run,
        "magic": int(cfg["magic_number"]),
        "pairs": list(cfg.get("pairs") or []),
        "started_at": pd.Timestamp.now("UTC").isoformat(),
        "migrated_from_legacy": True,
    }
    index_file.write_text(json.dumps(idx, indent=2), encoding="utf-8")

    LOG.info("[svc] MIGRATED legacy service_config.json -> %s", inst_dir / "config.json")
    return inst_dir / "config.json"


def _build_live_config(config_path: Path, creds: dict[str, Any]) -> "Any":
    """Read one instance config.json -> LiveConfig."""
    from ff.live import runner

    svc = _read_json(config_path)
    instance_id = svc.get("instance_id") or config_path.parent.name

    broker_profile = {
        **creds,
        "deviation_pips": svc.get("deviation_pips", 3.0),
        "magic_number": int(svc.get("magic_number", 20260420)),
        "symbol_map": svc.get("symbol_map", {}),
    }
    return runner.LiveConfig(
        instance_id=instance_id,
        recipe=svc["recipe"],
        overrides=svc.get("overrides") or {},
        pairs=list(svc["pairs"]),
        broker=runner.BrokerCfg(**broker_profile),
        poll_interval_sec=float(svc.get("poll_interval_sec", 10.0)),
        size_lots=float(svc.get("size_lots", 0.01)),
        best_trial=svc.get("best_trial"),
        max_open_per_pair=int(svc.get("max_open_per_pair", 1)),
    )


def main() -> int:
    _LIVE_DIR.mkdir(parents=True, exist_ok=True)
    _log_file = _LIVE_DIR / "runner.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(_log_file, mode="w", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    LOG.info("[svc] starting runner_service; log=%s", _log_file)

    try:
        from ff.live import broker_mt5, runner

        # Order matters: distribute first so the legacy migrator can
        # detect that the legacy file is already present in deploy/
        # instances and skip duplicate migration (which would produce
        # two dirs + a duplicate magic_number).
        _distribute_deploy_configs()
        _auto_migrate_legacy()

        config_paths = _discover_instance_configs()
        if not config_paths:
            LOG.error(
                "[svc] no active instance configs under %s — deploy one via the web UI first",
                _LIVE_DIR,
            )
            return 2

        creds = broker_mt5.load_broker_cfg_from_env(_ENV_FILE)
        instances = [_build_live_config(cp, creds) for cp in config_paths]
        LOG.info("[svc] loaded %d instance(s): %s", len(instances), [c.instance_id for c in instances])

        stop_event = Event()
        _install_signal_handlers(stop_event)
        runner.run(instances, stop_event)
        return 0
    except Exception as exc:  # noqa: BLE001
        LOG.exception("[svc] crash")
        _log_crash(exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
