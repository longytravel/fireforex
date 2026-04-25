"""Fire Forex CLI entry point.

Usage:
    python run.py eas/complex01.py --layer complex01_random --trials 2000
    python run.py eas/complex01.py --layer complex01_seed43 --trials 2000 --seed 43
    python run.py eas/complex01.py --no-preflight --no-browser
    python run.py web                     # start the local web UI on :8000
    python run.py web --port 8765         # custom port
    python run.py replay                  # replay artifacts/live/service_config.json
    python run.py replay path/to/config.json
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

# Windows console default codepage can't encode the box-drawing chars we use.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Ensure the project root is on sys.path so `import ff` works.
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ff import harness
from ff import preflight as pre


def load_ea_module(path: str):
    p = Path(path).resolve()
    if not p.exists():
        raise FileNotFoundError(f"EA config not found: {p}")
    spec = importlib.util.spec_from_file_location(p.stem, p)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {p}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "EA"):
        raise AttributeError(f"{p} does not define a top-level EA dict")
    return module.EA


def run_web(argv: list[str]) -> int:
    """Launch the local FastAPI web UI via uvicorn. Binds to 127.0.0.1."""
    ap = argparse.ArgumentParser(prog="run.py web")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    # Auto-reload is ON by default — Fire Forex is a local-only dev tool,
    # and the common friction point is editing a Python file (VERSION bump,
    # new route, UI fix) and not seeing it because the old process is
    # still serving. Pass --no-reload for a steady-state run.
    ap.add_argument("--no-reload", action="store_true", help="disable auto-reload on code changes")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args(argv)
    reload_enabled = not args.no_reload

    try:
        import uvicorn  # type: ignore
    except ModuleNotFoundError:
        print(
            "uvicorn is not installed. Run:\n    .venv\\Scripts\\python.exe -m pip install -r requirements-web.txt",
            file=sys.stderr,
        )
        return 1

    # Port-in-use guard. Stacking a second uvicorn on top of a stale
    # one is how the M1-download-serves-retired-code bug recurred.
    # Fail loud instead.
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as _probe:
        _probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            _probe.bind((args.host, args.port))
        except OSError:
            print(
                f"Port {args.port} is already in use — another Fire Forex "
                f"server is running. To restart cleanly, run:\n"
                f"    scripts\\ff_restart_server.ps1\n"
                f"Or stop the existing server first.",
                file=sys.stderr,
            )
            return 1

    url = f"http://{args.host}:{args.port}/"
    print(f"Fire Forex web UI → {url}")
    print("Ctrl-C to stop.")
    if not args.no_browser:
        import threading
        import time
        import webbrowser

        def _open() -> None:
            time.sleep(1.2)
            try:
                webbrowser.open(url)
            except Exception:
                pass

        threading.Thread(target=_open, daemon=True).start()

    if reload_enabled:
        print(
            "Auto-reload: ON (pass --no-reload to disable). Python file "
            "changes reload automatically. Rust engine edits still need "
            "'maturin develop --release' + a manual Ctrl-C restart."
        )
    uvicorn.run(
        "app.api:api",
        host=args.host,
        port=args.port,
        reload=reload_enabled,
        log_level="info",
    )
    return 0


def run_replay(argv: list[str]) -> int:
    """Replay a deployed live config as a single-trial backtest per pair."""
    ap = argparse.ArgumentParser(prog="run.py replay")
    ap.add_argument(
        "config",
        nargs="?",
        default=None,
        help="path to service_config.json (default: artifacts/live/service_config.json)",
    )
    ap.add_argument(
        "--data-source",
        choices=("dukascopy", "mt5"),
        default="dukascopy",
        help="parquet root to backtest against (default: dukascopy)",
    )
    args = ap.parse_args(argv)

    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from ff import replay as _replay

    config_path = Path(args.config) if args.config else _replay.LIVE_DIR / "service_config.json"
    summary = _replay.replay_service_config(config_path, data_source=args.data_source)

    print()
    print(f"replay complete — {summary['n_trades_total']} trades, {summary['total_pips_all']:+.1f} pips in {summary['elapsed_sec']}s")
    print(f"output → artifacts/replay/{summary['source_run_id']}/{summary['stamp']}/")
    for row in summary["pairs"]:
        status = row["status"]
        if status == "ok":
            print(f"  {row['pair']:<10} {row['n_trades']:>4} trades  {row['total_pips']:>+8.1f} pips")
        else:
            print(f"  {row['pair']:<10} [{status}] {row.get('error', '')}")
    return 0


def main() -> int:
    # Subcommand dispatch (``web``, ``replay``). Positional-first CLI
    # preserved for backward-compat.
    if len(sys.argv) >= 2 and sys.argv[1] == "web":
        return run_web(sys.argv[2:])
    if len(sys.argv) >= 2 and sys.argv[1] == "replay":
        return run_replay(sys.argv[2:])

    ap = argparse.ArgumentParser()
    ap.add_argument("ea_path", help="path to EA module (e.g. eas/complex01.py)")
    ap.add_argument("--layer", default=None, help="layer label for history.csv; defaults to '{ea_name}_random'")
    ap.add_argument("--optimizer", default="random", choices=["random"])
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--trials", type=int, default=2000)
    ap.add_argument(
        "--no-preflight",
        action="store_true",
        help="skip the preflight report (no interactive pause)",
    )
    ap.add_argument("--no-pause", action="store_true", help="print preflight but don't wait for Enter")
    ap.add_argument(
        "--no-browser",
        action="store_true",
        help="don't open comparison.html in the browser on finish",
    )
    ap.add_argument(
        "--inspect",
        action="store_true",
        help="just print every parameter in the EA and exit (no run)",
    )
    args = ap.parse_args()

    ea = load_ea_module(args.ea_path)
    layer = args.layer or f"{ea['name']}_{args.optimizer}"

    if args.inspect:
        from ff import inspect as insp

        print(insp.inspect_report(ea, ea_path=args.ea_path))
        return 0

    if not args.no_preflight:
        print(pre.preflight_report(ea, n_trials=args.trials))
        if not args.no_pause:
            try:
                input("Press Enter to continue, Ctrl-C to abort: ")
            except KeyboardInterrupt:
                print("\naborted.")
                return 1

    harness.run(
        ea,
        layer_name=layer,
        optimizer=args.optimizer,
        seed=args.seed,
        n_trials=args.trials,
        open_browser=not args.no_browser,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
