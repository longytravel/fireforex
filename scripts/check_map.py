"""Architecture-map completeness checker.

Walks every tracked file in the repo and warns if any aren't referenced in
``docs/ARCHITECTURE_MAP.md``. Exits non-zero on miss so CI can fail.
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = REPO_ROOT / "docs" / "ARCHITECTURE_MAP.md"

SELF_PATHS = {
    "docs/ARCHITECTURE_MAP.md",
    "scripts/check_map.py",
    "tests/test_check_map.py",
}


def load_tracked_files(root: Path) -> set[str]:
    """Return all tracked files (POSIX paths relative to ``root``)."""
    out = subprocess.run(
        ["git", "ls-files"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return {line.strip() for line in out.stdout.splitlines() if line.strip()}


def load_map_text(map_path: Path = MAP_PATH) -> str:
    return map_path.read_text(encoding="utf-8")


_GLOB_PATTERN_RE = re.compile(r"`([A-Za-z0-9_./*-]+\*[A-Za-z0-9_./*-]*)`")


def _extract_glob_patterns(map_text: str) -> list[str]:
    """Pull every backtick-wrapped path containing ``*`` out of the map.

    Examples matched: ``docs/builds/2026-04-19-chandelier-stop/*``,
    ``docs/validation/2026-04-19-*/``.
    """
    return _GLOB_PATTERN_RE.findall(map_text)


def _path_is_wildcarded(path: str, patterns: list[str]) -> bool:
    """True if any glob pattern from the map matches ``path`` (or its directory prefix)."""
    for pat in patterns:
        # Trailing slash means "any file under this dir" — fnmatch needs `/*` for that.
        normalised = f"{pat}*" if pat.endswith("/") else pat
        if fnmatch.fnmatchcase(path, normalised):
            return True
        # Also match against the path's parent directory (covers `dir/*` patterns
        # written without `**` against arbitrarily deep tracked files).
        parent = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
        if parent and fnmatch.fnmatchcase(parent, normalised + "/"):
            return True
    return False


def find_unmapped_files(tracked: set[str], map_text: str) -> set[str]:
    """Files whose path does not appear (anywhere — backticks, prose, or glob pattern) in the map."""
    patterns = _extract_glob_patterns(map_text)
    return {p for p in tracked if p not in SELF_PATHS and p not in map_text and not _path_is_wildcarded(p, patterns)}


def main() -> int:
    if not MAP_PATH.exists():
        print(f"ERROR: {MAP_PATH} does not exist", file=sys.stderr)
        return 2

    tracked = load_tracked_files(REPO_ROOT)
    map_text = load_map_text(MAP_PATH)
    unmapped = find_unmapped_files(tracked, map_text)

    if not unmapped:
        print(f"OK: all {len(tracked)} tracked files referenced in ARCHITECTURE_MAP.md")
        return 0

    print(f"FAIL: {len(unmapped)} tracked files are not referenced in ARCHITECTURE_MAP.md:")
    for path in sorted(unmapped):
        print(f"  {path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
