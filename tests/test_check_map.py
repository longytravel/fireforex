"""Tests for scripts.check_map — the architecture-map completeness checker."""

from pathlib import Path

from scripts.check_map import find_unmapped_files, load_map_text, load_tracked_files


def test_unmapped_when_file_missing_from_map() -> None:
    tracked = {"ff/x.py", "ff/y.py"}
    map_text = "## Stage\n- `ff/x.py`\n"

    assert find_unmapped_files(tracked, map_text) == {"ff/y.py"}


def test_no_unmapped_when_all_files_referenced() -> None:
    tracked = {"ff/x.py", "ff/y.py"}
    map_text = "Stage 1: `ff/x.py` and `ff/y.py`."

    assert find_unmapped_files(tracked, map_text) == set()


def test_path_match_is_substring_not_exact() -> None:
    """A path appearing inside backticks-with-suffix should still count as referenced."""
    tracked = {"app/routes.py"}
    map_text = "See `app/routes.py:123-145` for the issue."

    assert find_unmapped_files(tracked, map_text) == set()


def test_wildcard_dir_reference_covers_files_under_it() -> None:
    """A `dir/*` reference in the map covers any tracked file under that dir."""
    tracked = {
        "docs/builds/2026-04-19-chandelier-stop/01-mechanics-brief.md",
        "docs/builds/2026-04-19-chandelier-stop/02-slot-map.md",
    }
    map_text = "See `docs/builds/2026-04-19-chandelier-stop/*` (6 files) for evidence."

    assert find_unmapped_files(tracked, map_text) == set()


def test_wildcard_does_not_over_match_unrelated_dirs() -> None:
    """A wildcard for one dir doesn't accidentally cover sibling dirs."""
    tracked = {"docs/other/file.md"}
    map_text = "See `docs/builds/2026-04-19-chandelier-stop/*` for evidence."

    assert find_unmapped_files(tracked, map_text) == {"docs/other/file.md"}


def test_self_paths_are_excluded() -> None:
    """The map and the checker itself don't have to reference themselves."""
    tracked = {
        "docs/ARCHITECTURE_MAP.md",
        "scripts/check_map.py",
        "tests/test_check_map.py",
        "ff/x.py",
    }
    map_text = "Talks about `ff/x.py` only."

    assert find_unmapped_files(tracked, map_text) == set()


def test_load_map_text_reads_real_file(tmp_path: Path) -> None:
    map_file = tmp_path / "MAP.md"
    map_file.write_text("hello world\n", encoding="utf-8")

    assert load_map_text(map_file) == "hello world\n"


def test_load_tracked_files_runs_against_real_repo() -> None:
    """Smoke — load_tracked_files invokes git ls-files and returns at least one expected file."""
    repo_root = Path(__file__).resolve().parent.parent
    tracked = load_tracked_files(repo_root)

    assert "docs/ARCHITECTURE_MAP.md" in tracked
