"""Runtime bootstrap: pin to P-cores and set sane numba env BEFORE vectorbt/numba import.

Background:
    Intel 12th–14th gen hybrid CPUs (e.g. i9-14900HX: 16 P-cores + 16 E-cores)
    crash LLVM's JIT when compiled AVX/AVX2 code runs on an E-core. The fix is
    to restrict the process to P-core logical CPUs before numba starts.

    Logical CPU layout on a 14900HX (32 threads total when HT on across P+E):
        0..31  → P-core SMT threads (16 P × 2)
        32..47 → E-cores            (16 E × 1)

    We probe `psutil.cpu_count(logical=True)` and, if > 32, assume the first
    32 are the P-cores and pin there. On other CPUs the pin is a no-op:
    we pin to whatever is already affine.

Set NUMBA_NUM_THREADS to match the number of P-core physical cores (16)
to avoid SMT contention on numeric hot paths.
"""
from __future__ import annotations

import os
import sys


def _bootstrap_cpu_pin() -> dict:
    info = {"pinned": False, "numba_threads": None, "affinity": None, "skipped": None}

    # Only do this once per process
    if os.environ.get("FIRE_FOREX_BOOTSTRAPPED"):
        info["skipped"] = "already bootstrapped"
        return info
    os.environ["FIRE_FOREX_BOOTSTRAPPED"] = "1"

    # User can opt out
    if os.environ.get("FIRE_FOREX_NO_PIN") == "1":
        info["skipped"] = "FIRE_FOREX_NO_PIN=1"
        return info

    try:
        import psutil
    except ImportError:
        info["skipped"] = "psutil not installed"
        return info

    total = psutil.cpu_count(logical=True) or 0
    # Heuristic: 14900HX (hybrid) reports 32 (16P×2 HT + 16E×1 = 48?)
    # Raptor Lake-HX layout varies — use the conservative rule:
    #   if total >= 48 → likely hybrid with E-cores past index 31; pin to 0..31
    #   elif total >= 32 → pin to first 16 (assume HT off or P-only)
    #   else → do nothing, we're on a small machine
    if total >= 48:
        pin = list(range(32))
        numba_threads = 16
    elif total >= 32:
        pin = list(range(16))
        numba_threads = 16
    else:
        info["skipped"] = f"only {total} logical CPUs, no pin needed"
        # Still cap numba threads to physical cores if we can tell
        phys = psutil.cpu_count(logical=False)
        if phys:
            os.environ.setdefault("NUMBA_NUM_THREADS", str(max(1, phys)))
            info["numba_threads"] = phys
        return info

    try:
        psutil.Process().cpu_affinity(pin)
        info["pinned"] = True
        info["affinity"] = pin
    except Exception as e:
        info["skipped"] = f"cpu_affinity failed: {e}"
        return info

    os.environ.setdefault("NUMBA_NUM_THREADS", str(numba_threads))
    info["numba_threads"] = numba_threads

    # Defensive: avoid Intel SVML on hybrid (sometimes miscompiles)
    os.environ.setdefault("NUMBA_DISABLE_INTEL_SVML", "1")
    # Use workqueue threading; TBB can be missing on Windows
    os.environ.setdefault("NUMBA_THREADING_LAYER", "workqueue")

    return info


BOOT_INFO = _bootstrap_cpu_pin()


def print_boot_info() -> None:
    print(f"[bootstrap] {BOOT_INFO}", file=sys.stderr)
