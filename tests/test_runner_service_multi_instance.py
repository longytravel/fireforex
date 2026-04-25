"""Runner service multi-instance deploy pipeline tests.

Covers the service-level flow that lives between the laptop UI and the
live runner loop:

- ``deploy/instances/active.json`` filters which committed deploy
  configs get distributed.
- Distribution happens before legacy migration so a deploy + legacy
  service_config don't double-create the same instance.
- Instances removed from ``active.json`` get deactivated in
  ``instances.json`` so they stop trading on the next boot, even when
  their ``artifacts/live/<id>/`` dir already exists.
- Filename stem and embedded ``instance_id`` are reconciled on import.

These tests patch the module's ``_LIVE_DIR`` / ``_ROOT`` / related
paths so the filesystem stays under tmp.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _write_json_bom(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8-sig")


def _svc(instance_id: str, magic: int = 20260420, pairs=("EUR_USD",)) -> dict:
    return {
        "instance_id": instance_id,
        "source_run_id": "run_X",
        "recipe": {"pair": pairs[0], "main_tf": "M15", "sub_tf": "M1"},
        "overrides": {},
        "pairs": list(pairs),
        "best_trial": {"signal_variant": 1, "engine": {}},
        "magic_number": magic,
        "max_open_per_pair": 1,
    }


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    """Patch runner_service paths to a fresh tmp repo layout."""
    from ff.live import runner_service as rs

    root = tmp_path / "repo"
    live = root / "artifacts" / "live"
    live.mkdir(parents=True)
    (root / "deploy" / "instances").mkdir(parents=True)

    monkeypatch.setattr(rs, "_ROOT", root)
    monkeypatch.setattr(rs, "_LIVE_DIR", live)
    monkeypatch.setattr(rs, "_SERVICE_CONFIG", live / "service_config.json")
    monkeypatch.setattr(rs, "_CRASHES_FILE", live / "crashes.jsonl")
    monkeypatch.setattr(rs, "_ENV_FILE", root / ".env.live")
    return root


def test_distribute_honours_active_manifest(tmp_root):
    from ff.live import runner_service as rs

    deploy_dir = tmp_root / "deploy" / "instances"
    _write_json(deploy_dir / "alpha.json", _svc("alpha", magic=100))
    _write_json(deploy_dir / "beta.json", _svc("beta", magic=101))
    _write_json(deploy_dir / "active.json", {"active": ["alpha"]})

    rs._distribute_deploy_configs()

    assert (rs._LIVE_DIR / "alpha" / "config.json").exists()
    assert not (rs._LIVE_DIR / "beta" / "config.json").exists(), "beta is not in active.json and must not be imported"


def test_active_removal_deactivates_existing_instance(tmp_root):
    from ff.live import runner_service as rs

    deploy_dir = tmp_root / "deploy" / "instances"
    # Instance gamma was deployed + distributed earlier, then removed
    # from active.json. Its artifacts dir already exists.
    (rs._LIVE_DIR / "gamma").mkdir()
    _write_json(rs._LIVE_DIR / "gamma" / "config.json", _svc("gamma"))
    _write_json(
        rs._LIVE_DIR / "instances.json",
        {
            "magic_counter": 20260421,
            "instances": {
                "gamma": {"active": True, "magic": 20260420, "pairs": ["EUR_USD"]},
            },
        },
    )
    # Now active.json drops gamma and promotes delta.
    _write_json(deploy_dir / "gamma.json", _svc("gamma"))
    _write_json(deploy_dir / "delta.json", _svc("delta", magic=102))
    _write_json(deploy_dir / "active.json", {"active": ["delta"]})

    rs._distribute_deploy_configs()

    idx = json.loads((rs._LIVE_DIR / "instances.json").read_text(encoding="utf-8"))
    assert idx["instances"]["gamma"]["active"] is False, "gamma removed from active.json must be flipped to inactive"
    assert idx["instances"]["delta"]["active"] is True
    # Discovery skips inactive.
    configs = rs._discover_instance_configs()
    active_dirs = {p.parent.name for p in configs}
    assert "delta" in active_dirs
    assert "gamma" not in active_dirs


def test_distribute_forces_filename_identity(tmp_root):
    """If a deploy file's embedded instance_id does not match the
    filename stem, the filename wins so runtime state cannot drift."""
    from ff.live import runner_service as rs

    deploy_dir = tmp_root / "deploy" / "instances"
    cfg = _svc("claimed_id", magic=103)
    _write_json(deploy_dir / "true_id.json", cfg)
    _write_json(deploy_dir / "active.json", {"active": ["true_id"]})

    rs._distribute_deploy_configs()

    loaded = json.loads((rs._LIVE_DIR / "true_id" / "config.json").read_text(encoding="utf-8"))
    assert loaded["instance_id"] == "true_id"


def test_distribute_accepts_windows_bom_json(tmp_root):
    """Windows tooling can write UTF-8 with a BOM; the VPS service must
    still parse those configs on boot.
    """
    from ff.live import runner_service as rs

    deploy_dir = tmp_root / "deploy" / "instances"
    _write_json_bom(deploy_dir / "alpha.json", _svc("alpha", magic=100))
    _write_json_bom(deploy_dir / "active.json", {"active": ["alpha"]})

    rs._distribute_deploy_configs()

    assert (rs._LIVE_DIR / "alpha" / "config.json").exists()


def test_runner_interval_read_accepts_windows_bom_json(tmp_root, monkeypatch):
    from ff.live import runner

    monkeypatch.setattr(runner, "LIVE_DIR", tmp_root / "artifacts" / "live")
    _write_json_bom(
        runner.LIVE_DIR / "alpha" / "config.json",
        {**_svc("alpha"), "auto_reconcile_interval_min": 0},
    )

    assert runner._read_auto_reconcile_interval_min() == 0


def test_migration_skipped_when_deploy_already_has_same_instance(tmp_root):
    """Legacy service_config.json carrying an instance_id that also
    exists as deploy/instances/<id>.json must NOT double-migrate — else
    we end up with two dirs sharing one magic and run() crashes."""
    from ff.live import runner_service as rs

    deploy_dir = tmp_root / "deploy" / "instances"
    _write_json(deploy_dir / "shared.json", _svc("shared", magic=104))
    _write_json(deploy_dir / "active.json", {"active": ["shared"]})
    # Legacy file carrying the same instance_id.
    _write_json(rs._SERVICE_CONFIG, _svc("shared", magic=104))

    rs._distribute_deploy_configs()
    rs._auto_migrate_legacy()

    # Only one instance dir should exist.
    dirs = [p for p in rs._LIVE_DIR.iterdir() if p.is_dir() and p.name not in ("archive", "reconcile")]
    assert len(dirs) == 1, f"expected 1 instance dir, got {[d.name for d in dirs]}"
    assert dirs[0].name == "shared"
