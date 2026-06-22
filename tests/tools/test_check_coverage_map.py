"""Tests for tools/check_coverage_map.py — the coverage_map validator.

Tests the three public functions:
  load_coverage_map  — YAML parsing and validation
  validate           — dangling / uncovered logic
  run                — end-to-end with the real suite (integration smoke)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.check_coverage_map import load_coverage_map, run, validate

# ---------------------------------------------------------------------------
# load_coverage_map — unit tests
# ---------------------------------------------------------------------------


def test_load_coverage_map_parses_valid_yaml(tmp_path: Path) -> None:
    """Valid coverage_map.yaml is parsed into section → row_id → test list."""
    content = """
"§8.1":
  "row-1":
    tests: ["test_foo", "test_bar"]
  "row-2":
    tests: ["test_baz"]
"""
    p = tmp_path / "coverage_map.yaml"
    p.write_text(content)
    result = load_coverage_map(p)
    assert result == {
        "§8.1": {
            "row-1": ["test_foo", "test_bar"],
            "row-2": ["test_baz"],
        }
    }


def test_load_coverage_map_empty_tests_list(tmp_path: Path) -> None:
    """Rows with an empty tests list are preserved (uncovered rows fail in validate)."""
    content = """
"§8.2":
  "row-1":
    tests: []
"""
    p = tmp_path / "coverage_map.yaml"
    p.write_text(content)
    result = load_coverage_map(p)
    assert result["§8.2"]["row-1"] == []


def test_load_coverage_map_missing_file(tmp_path: Path) -> None:
    """Missing file raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        load_coverage_map(tmp_path / "nonexistent.yaml")


def test_load_coverage_map_invalid_yaml(tmp_path: Path) -> None:
    """Invalid YAML content raises yaml.YAMLError."""
    p = tmp_path / "bad.yaml"
    p.write_text("{ unclosed: yaml: mapping\n  broken indent\n")
    with pytest.raises((yaml.YAMLError, ValueError)):
        load_coverage_map(p)


def test_load_coverage_map_top_level_not_mapping(tmp_path: Path) -> None:
    """YAML file that is a list at the top level raises ValueError."""
    p = tmp_path / "list.yaml"
    p.write_text("- foo\n- bar\n")
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_coverage_map(p)


# ---------------------------------------------------------------------------
# validate — unit tests
# ---------------------------------------------------------------------------


def test_validate_all_resolve() -> None:
    """No dangling, no uncovered → both error lists are empty."""
    coverage_map = {
        "§8.1": {
            "row-1": ["test_alpha", "test_beta"],
        }
    }
    collected = {"test_alpha", "test_beta", "test_gamma"}
    dangling, uncovered = validate(coverage_map, collected)
    assert dangling == []
    assert uncovered == []


def test_validate_dangling_name() -> None:
    """A test name in the map that is not in collected → dangling error."""
    coverage_map = {
        "§8.1": {
            "row-1": ["test_real", "test_ghost"],
        }
    }
    collected = {"test_real"}
    dangling, uncovered = validate(coverage_map, collected)
    assert len(dangling) == 1
    assert "test_ghost" in dangling[0]
    assert "§8.1/row-1" in dangling[0]
    assert uncovered == []


def test_validate_uncovered_row() -> None:
    """A row with an empty test list → uncovered error."""
    coverage_map = {
        "§8.2": {
            "row-empty": [],
        }
    }
    collected = {"test_something"}
    dangling, uncovered = validate(coverage_map, collected)
    assert dangling == []
    assert len(uncovered) == 1
    assert "§8.2/row-empty" in uncovered[0]


def test_validate_multiple_dangling_across_sections() -> None:
    """Dangling names in different sections are all reported."""
    coverage_map = {
        "§8.1": {"r1": ["test_ok", "test_missing_a"]},
        "§8.2": {"r1": ["test_missing_b"]},
    }
    collected = {"test_ok"}
    dangling, uncovered = validate(coverage_map, collected)
    assert len(dangling) == 2
    names_in_errors = " ".join(dangling)
    assert "test_missing_a" in names_in_errors
    assert "test_missing_b" in names_in_errors


def test_validate_empty_map() -> None:
    """Empty coverage_map → no errors."""
    dangling, uncovered = validate({}, {"test_something"})
    assert dangling == []
    assert uncovered == []


def test_validate_empty_collected() -> None:
    """If collected is empty and map has entries, everything is dangling."""
    coverage_map = {"§8.1": {"r1": ["test_foo"]}}
    dangling, uncovered = validate(coverage_map, set())
    assert len(dangling) == 1
    assert uncovered == []


# ---------------------------------------------------------------------------
# run — integration smoke tests against the real suite
# ---------------------------------------------------------------------------


def test_run_passes_on_real_suite() -> None:
    """run() exits 0 on the real coverage_map + real collected suite."""
    repo_root = Path(__file__).parent.parent.parent
    result = run(map_path=repo_root / "coverage_map.yaml", rootdir=repo_root)
    assert result == 0, "Validator failed on the real suite — dangling or uncovered rows"


def test_run_fails_on_dangling_name(tmp_path: Path) -> None:
    """A coverage_map.yaml with a test name that does not exist → exit code 1."""
    content = """
"§8.1":
  "row-1":
    tests: ["test_this_test_does_not_exist_anywhere_12345"]
"""
    p = tmp_path / "coverage_map.yaml"
    p.write_text(content)
    repo_root = Path(__file__).parent.parent.parent
    result = run(map_path=p, rootdir=repo_root)
    assert result == 1, "Expected exit 1 for dangling test name"


def test_run_fails_on_uncovered_row(tmp_path: Path) -> None:
    """A coverage_map.yaml with an empty test list → exit code 1."""
    content = """
"§8.99":
  "row-no-tests":
    tests: []
"""
    p = tmp_path / "coverage_map.yaml"
    p.write_text(content)
    repo_root = Path(__file__).parent.parent.parent
    result = run(map_path=p, rootdir=repo_root)
    assert result == 1, "Expected exit 1 for uncovered row"


def test_run_missing_map_returns_2(tmp_path: Path) -> None:
    """A missing coverage_map.yaml → exit code 2 (I/O error, not validation failure)."""
    result = run(map_path=tmp_path / "no_such_file.yaml")
    assert result == 2


def test_run_collection_error_exits_2(tmp_path: Path) -> None:
    """A test tree with an import error causes pytest --collect-only to exit non-zero.

    run() must return 2 (I/O/usage error) rather than 0 or 1 — it must NOT validate
    against the partial node list that pytest still emits before aborting.
    """
    # Write a conftest-free test module that fails at import time.
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_broken.py").write_text(
        "import nonexistent_xyz_module_that_does_not_exist\n\ndef test_dummy() -> None:\n    pass\n"
    )

    # Write a minimal but valid coverage_map.yaml so the map-load step succeeds.
    map_path = tmp_path / "coverage_map.yaml"
    map_path.write_text('"§8.1":\n  "row-1":\n    tests: ["test_dummy"]\n')

    result = run(map_path=map_path, rootdir=tmp_path)
    assert result == 2, (
        f"Expected exit 2 (collection error) but got {result}. "
        "The validator must not proceed against a partial node list."
    )
