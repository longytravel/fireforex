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

# Characters that count as "part of a file path" for boundary checks. A path
# only matches when the surrounding characters are NOT in this set — so
# `api.py` does not match inside `app/api.py`.
_PATH_CHAR = "A-Za-z0-9_./-"

_GLOB_PATTERN_RE = re.compile(r"`([A-Za-z0-9_./*-]+\*[A-Za-z0-9_./*-]*)`")


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


def _extract_glob_patterns(map_text: str) -> list[str]:
    """Pull every backtick-wrapped path containing ``*`` out of the map."""
    return _GLOB_PATTERN_RE.findall(map_text)


def _path_is_referenced(path: str, map_text: str) -> bool:
    """True if ``path`` appears in ``map_text`` as a delimited token.

    Avoids false positives where one path is a substring of another (e.g.
    ``api.py`` should NOT match inside ``app/api.py``). The path must be
    bounded on both sides by characters that aren't path-identifier chars.
    """
    pattern = rf"(?<![{_PATH_CHAR}]){re.escape(path)}(?![{_PATH_CHAR}])"
    return re.search(pattern, map_text) is not None


def _path_is_wildcarded(path: str, patterns: list[str]) -> bool:
    """True if any backtick-wrapped glob from the map matches ``path``."""
    for pat in patterns:
        normalised = f"{pat}*" if pat.endswith("/") else pat
        if fnmatch.fnmatchcase(path, normalised):
            return True
    return False


def find_unmapped_files(tracked: set[str], map_text: str) -> set[str]:
    """Files whose path does not appear (as a delimited token or glob match) in the map."""
    patterns = _extract_glob_patterns(map_text)
    return {p for p in tracked if p not in SELF_PATHS and not _path_is_referenced(p, map_text) and not _path_is_wildcarded(p, patterns)}


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
