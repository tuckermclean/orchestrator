"""Tests for tools/check_coverage_map.py — the coverage_map validator.

Tests the public API:
  load_coverage_map        — YAML parsing and validation
  parse_spec_functions     — SPEC.md §8 header extraction
  collect_markers          — AST-based @covers marker collection
  check_spec_completeness  — §8 function → coverage_map completeness
  validate                 — dangling / uncovered logic (map → suite)
  check_node_id_collisions — collision detection for bare test names
  check_markers_to_map     — @covers marker → coverage_map direction
  check_map_to_markers     — coverage_map → @covers marker direction
  run                      — end-to-end with the real suite (integration smoke)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tools.check_coverage_map import (
    check_map_to_markers,
    check_markers_to_map,
    check_node_id_collisions,
    check_spec_completeness,
    collect_markers,
    load_coverage_map,
    parse_spec_functions,
    run,
    validate,
)

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
# parse_spec_functions — unit tests
# ---------------------------------------------------------------------------


def test_parse_spec_functions_extracts_section_ids(tmp_path: Path) -> None:
    """SPEC.md with §8.x headers yields section IDs in document order."""
    spec = tmp_path / "SPEC.md"
    spec.write_text(
        "# Header\n"
        "## §8 Decision Functions\n"
        "### §8.1 `route_entry`\n"
        "some text\n"
        "### §8.2 `resolve_blockers`\n"
        "### §8.2a `CounterStore` — counter reads\n"
        "### §8.3 `decide_round`\n"
    )
    sections = parse_spec_functions(spec)
    assert sections == ["§8.1", "§8.2", "§8.2a", "§8.3"]


def test_parse_spec_functions_deduplicates(tmp_path: Path) -> None:
    """Duplicate §8.x headers produce a single entry each."""
    spec = tmp_path / "SPEC.md"
    spec.write_text("### §8.1 `route_entry`\n### §8.1 `route_entry` (again)\n")
    sections = parse_spec_functions(spec)
    assert sections.count("§8.1") == 1


def test_parse_spec_functions_missing_file(tmp_path: Path) -> None:
    """Missing SPEC.md raises FileNotFoundError."""
    with pytest.raises(FileNotFoundError):
        parse_spec_functions(tmp_path / "no_spec.md")


def test_parse_spec_functions_ignores_non_section8(tmp_path: Path) -> None:
    """Non-§8 headers are not included in the output."""
    spec = tmp_path / "SPEC.md"
    spec.write_text(
        "### §7 Constants\n"
        "### §8.1 `route_entry`\n"
        "### §9 Ports\n"
        "### §8.12 `decide_specialists`\n"
    )
    sections = parse_spec_functions(spec)
    assert sections == ["§8.1", "§8.12"]
    assert "§7" not in sections
    assert "§9" not in sections


# ---------------------------------------------------------------------------
# check_spec_completeness — unit tests
# ---------------------------------------------------------------------------


def test_check_spec_completeness_all_present() -> None:
    """All SPEC §8 functions present in coverage_map → no errors."""
    spec_functions = ["§8.1", "§8.2", "§8.3"]
    coverage_map = {
        "§8.1": {"r1": ["test_a"]},
        "§8.2": {"r1": ["test_b"]},
        "§8.3": {"r1": ["test_c"]},
    }
    errors = check_spec_completeness(spec_functions, coverage_map)
    assert errors == []


def test_check_spec_completeness_missing_function() -> None:
    """A §8.x section in SPEC.md with no coverage_map entry → error reported."""
    spec_functions = ["§8.1", "§8.2a", "§8.3"]
    coverage_map = {
        "§8.1": {"r1": ["test_a"]},
        "§8.3": {"r1": ["test_b"]},
        # §8.2a deliberately absent
    }
    errors = check_spec_completeness(spec_functions, coverage_map)
    assert len(errors) == 1
    assert "§8.2a" in errors[0]


def test_check_spec_completeness_dash_suffix_satisfies() -> None:
    """A key like '§8.3-integration' satisfies the §8.3 requirement."""
    spec_functions = ["§8.3"]
    coverage_map = {"§8.3-integration": {"r1": ["test_a"]}}
    errors = check_spec_completeness(spec_functions, coverage_map)
    assert errors == []


def test_check_spec_completeness_dash_suffix_no_prefix_match() -> None:
    """'§8.30' does not satisfy §8.3 (prefix match must be followed by '-' or EOF)."""
    spec_functions = ["§8.3"]
    coverage_map = {"§8.30": {"r1": ["test_a"]}}
    errors = check_spec_completeness(spec_functions, coverage_map)
    assert len(errors) == 1
    assert "§8.3" in errors[0]


def test_check_spec_completeness_multiple_missing() -> None:
    """Multiple absent §8 functions are all reported."""
    spec_functions = ["§8.5", "§8.6", "§8.7"]
    coverage_map: dict[str, dict[str, list[str]]] = {}
    errors = check_spec_completeness(spec_functions, coverage_map)
    assert len(errors) == 3
    reported = " ".join(errors)
    assert "§8.5" in reported
    assert "§8.6" in reported
    assert "§8.7" in reported


# ---------------------------------------------------------------------------
# collect_markers — unit tests
# ---------------------------------------------------------------------------


def test_collect_markers_finds_covers_decorator(tmp_path: Path) -> None:
    """@pytest.mark.covers(section, row) is detected and indexed."""
    test_file = tmp_path / "test_example.py"
    test_file.write_text(
        "import pytest\n\n"
        "@pytest.mark.covers('§8.1', 'row-1')\n"
        "def test_something() -> None:\n"
        "    pass\n"
    )
    markers = collect_markers(tmp_path)
    assert ("§8.1", "row-1") in markers
    assert "test_something" in markers[("§8.1", "row-1")]


def test_collect_markers_multiple_on_one_function(tmp_path: Path) -> None:
    """A function carrying two @covers decorators produces two entries."""
    test_file = tmp_path / "test_multi.py"
    test_file.write_text(
        "import pytest\n\n"
        "@pytest.mark.covers('§8.3', 'row-1-approve')\n"
        "@pytest.mark.covers('§8.3', 'row-2-fix')\n"
        "def test_both_rows() -> None:\n"
        "    pass\n"
    )
    markers = collect_markers(tmp_path)
    assert "test_both_rows" in markers[("§8.3", "row-1-approve")]
    assert "test_both_rows" in markers[("§8.3", "row-2-fix")]


def test_collect_markers_no_markers_returns_empty(tmp_path: Path) -> None:
    """A test file with no @covers markers → empty mapping."""
    test_file = tmp_path / "test_plain.py"
    test_file.write_text("def test_something() -> None:\n    pass\n")
    markers = collect_markers(tmp_path)
    assert markers == {}


def test_collect_markers_deduplicates_same_function_same_key(tmp_path: Path) -> None:
    """The same function name appearing twice under the same key is listed once."""
    test_file = tmp_path / "test_dup.py"
    # Unlikely in real code but guard against it anyway.
    test_file.write_text(
        "import pytest\n\n"
        "@pytest.mark.covers('§8.1', 'row-1')\n"
        "def test_once() -> None:\n"
        "    pass\n"
    )
    # Simulate a second file also defining test_once with the same marker.
    test_file2 = tmp_path / "test_dup2.py"
    test_file2.write_text(
        "import pytest\n\n"
        "@pytest.mark.covers('§8.1', 'row-1')\n"
        "def test_once() -> None:\n"
        "    pass\n"
    )
    markers = collect_markers(tmp_path)
    # Both files define test_once; the list should deduplicate.
    assert markers[("§8.1", "row-1")].count("test_once") == 1


# ---------------------------------------------------------------------------
# check_node_id_collisions — unit tests
# ---------------------------------------------------------------------------


def test_check_node_id_collisions_no_collision() -> None:
    """Unique bare names → no collision errors."""
    coverage_map = {"§8.1": {"r1": ["test_foo", "test_bar"]}}
    name_to_node_ids = {
        "test_foo": ["tests/unit/test_foo.py::test_foo"],
        "test_bar": ["tests/unit/test_bar.py::test_bar"],
    }
    errors = check_node_id_collisions(coverage_map, name_to_node_ids)
    assert errors == []


def test_check_node_id_collisions_detects_collision() -> None:
    """A bare name that maps to two distinct node IDs → collision error."""
    coverage_map = {"§8.1": {"r1": ["test_alpha"]}}
    name_to_node_ids = {
        "test_alpha": [
            "tests/unit/test_a.py::test_alpha",
            "tests/integration/test_b.py::test_alpha",
        ]
    }
    errors = check_node_id_collisions(coverage_map, name_to_node_ids)
    assert len(errors) == 1
    assert "test_alpha" in errors[0]
    assert "ambiguous" in errors[0]


def test_check_node_id_collisions_only_checks_map_entries() -> None:
    """Collisions in the suite that are NOT referenced by the map are not reported."""
    coverage_map = {"§8.1": {"r1": ["test_unique"]}}
    name_to_node_ids = {
        "test_unique": ["tests/a.py::test_unique"],
        # test_colliding is in the suite but not in the map
        "test_colliding": ["tests/a.py::test_colliding", "tests/b.py::test_colliding"],
    }
    errors = check_node_id_collisions(coverage_map, name_to_node_ids)
    assert errors == []


# ---------------------------------------------------------------------------
# check_markers_to_map — unit tests (direction D: marker → map)
# ---------------------------------------------------------------------------


def test_check_markers_to_map_all_present() -> None:
    """All marker (section, row) pairs exist in coverage_map → no errors."""
    markers = {("§8.3", "row-1"): ["test_foo"]}
    coverage_map = {"§8.3": {"row-1": ["test_foo"]}}
    errors = check_markers_to_map(markers, coverage_map)
    assert errors == []


def test_check_markers_to_map_missing_section() -> None:
    """A marker references a section absent from the map → error."""
    markers = {("§8.99", "row-x"): ["test_orphan"]}
    coverage_map = {"§8.1": {"row-1": ["test_foo"]}}
    errors = check_markers_to_map(markers, coverage_map)
    assert len(errors) == 1
    assert "§8.99" in errors[0]
    assert "row-x" in errors[0]


def test_check_markers_to_map_section_present_row_missing() -> None:
    """Section present in map but the specific row is absent → error."""
    markers = {("§8.3", "row-phantom"): ["test_bar"]}
    coverage_map = {"§8.3": {"row-1": ["test_real"]}}
    errors = check_markers_to_map(markers, coverage_map)
    assert len(errors) == 1
    assert "row-phantom" in errors[0]


def test_check_markers_to_map_empty_markers() -> None:
    """No markers in the suite → no errors (nothing to check)."""
    errors = check_markers_to_map({}, {"§8.1": {"r1": ["test_foo"]}})
    assert errors == []


# ---------------------------------------------------------------------------
# check_map_to_markers — unit tests (direction E: map → marker)
# ---------------------------------------------------------------------------


def test_check_map_to_markers_all_marked() -> None:
    """All §8 tests in the map carry the corresponding marker → no errors."""
    coverage_map = {"§8.3": {"row-1": ["test_foo"]}}
    markers = {("§8.3", "row-1"): ["test_foo"]}
    errors = check_map_to_markers(coverage_map, markers)
    assert errors == []


def test_check_map_to_markers_missing_marker() -> None:
    """A §8 test listed in the map without the corresponding marker → error."""
    coverage_map = {"§8.3": {"row-1": ["test_no_marker"]}}
    markers: dict[tuple[str, str], list[str]] = {}
    errors = check_map_to_markers(coverage_map, markers)
    assert len(errors) == 1
    assert "test_no_marker" in errors[0]
    assert "§8.3" in errors[0]
    assert "row-1" in errors[0]


def test_check_map_to_markers_non_section8_excluded() -> None:
    """Non-§8 sections (§4.2, §6-escalations) are NOT checked in the map→marker direction."""
    coverage_map = {
        "§4.2": {"row-1": ["test_dispatch_opens_draft_pr"]},
        "§6-escalations": {"E2-no-progress": ["test_converge_no_progress_e2"]},
    }
    markers: dict[tuple[str, str], list[str]] = {}
    errors = check_map_to_markers(coverage_map, markers)
    assert errors == []


def test_check_map_to_markers_section8_integration_checked() -> None:
    """§8.x sections (including §8.3-integration) ARE checked."""
    coverage_map = {"§8.3-integration": {"row-1": ["test_wired"]}}
    markers: dict[tuple[str, str], list[str]] = {}
    errors = check_map_to_markers(coverage_map, markers)
    # §8.3-integration starts with §8. → must be checked
    assert len(errors) == 1
    assert "test_wired" in errors[0]


def test_check_map_to_markers_both_directions_independent() -> None:
    """Marker present in map but wrong row id → both directions report errors."""
    coverage_map = {"§8.3": {"row-1": ["test_foo"]}}
    # test_foo has a marker but for the WRONG row
    markers = {("§8.3", "row-WRONG"): ["test_foo"]}
    map_to_marker = check_map_to_markers(coverage_map, markers)
    marker_to_map = check_markers_to_map(markers, coverage_map)
    # map says test_foo covers row-1 but test_foo has marker for row-WRONG → both fail
    assert len(map_to_marker) == 1  # row-1 not in markers
    assert len(marker_to_map) == 1  # row-WRONG not in map


# ---------------------------------------------------------------------------
# validate — unit tests (existing + backward-compat)
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
    result = run(
        map_path=repo_root / "coverage_map.yaml",
        spec_path=repo_root / "SPEC.md",
        rootdir=repo_root,
    )
    assert result == 0, "Validator failed on the real suite — check FAIL lines above"


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
    result = run(map_path=p, spec_path=repo_root / "SPEC.md", rootdir=repo_root)
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
    result = run(map_path=p, spec_path=repo_root / "SPEC.md", rootdir=repo_root)
    assert result == 1, "Expected exit 1 for uncovered row"


def test_run_fails_on_missing_spec_section(tmp_path: Path) -> None:
    """A SPEC.md listing §8.99 with no matching coverage_map entry → exit code 1."""
    # Create a minimal SPEC.md that lists a §8 function not in the map.
    spec = tmp_path / "SPEC.md"
    spec.write_text("### §8.99 `missing_function`\n")
    # Map has entries for real tests but nothing for §8.99.
    map_content = """
"§8.1":
  "row-1":
    tests: []
"""
    map_path = tmp_path / "coverage_map.yaml"
    map_path.write_text(map_content)
    repo_root = Path(__file__).parent.parent.parent
    result = run(map_path=map_path, spec_path=spec, rootdir=repo_root)
    assert result == 1, "Expected exit 1 for SPEC §8 section absent from coverage_map"


def test_run_fails_on_marker_references_absent_row(tmp_path: Path) -> None:
    """A test file whose @covers marker points to a row absent from the map → exit 1."""
    # Create a tiny test file that has a @covers for a nonexistent (section, row).
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_orphan_marker.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.covers('§8.PHANTOM', 'row-ghost')\n"
        "def test_with_orphan_marker() -> None:\n"
        "    pass\n"
    )
    # Minimal map with no §8.PHANTOM entry.
    map_path = tmp_path / "coverage_map.yaml"
    map_path.write_text('"§8.1":\n  "row-1":\n    tests: []\n')
    spec = tmp_path / "SPEC.md"
    spec.write_text("")  # empty SPEC so no spec-completeness failures
    result = run(map_path=map_path, spec_path=spec, rootdir=tmp_path)
    assert result == 1, "Expected exit 1: marker references section/row absent from map"


def test_run_fails_map_test_lacks_marker(tmp_path: Path) -> None:
    """A coverage_map §8 test listed without the corresponding @covers marker → exit 1."""
    # Test file has the test function but no @covers decorator.
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_unmarked.py").write_text(
        "def test_no_covers_decorator() -> None:\n"
        "    pass\n"
    )
    map_path = tmp_path / "coverage_map.yaml"
    map_path.write_text(
        '"§8.1":\n'
        '  "row-1":\n'
        '    tests: ["test_no_covers_decorator"]\n'
    )
    spec = tmp_path / "SPEC.md"
    spec.write_text("### §8.1 `route_entry`\n")
    result = run(map_path=map_path, spec_path=spec, rootdir=tmp_path)
    assert result == 1, "Expected exit 1: map entry for §8 test missing @covers marker"


def test_run_missing_map_returns_2(tmp_path: Path) -> None:
    """A missing coverage_map.yaml → exit code 2 (I/O error, not validation failure)."""
    result = run(map_path=tmp_path / "no_such_file.yaml")
    assert result == 2


def test_run_missing_spec_returns_2(tmp_path: Path) -> None:
    """A missing SPEC.md → exit code 2."""
    map_path = tmp_path / "coverage_map.yaml"
    map_path.write_text('"§8.1":\n  "r1":\n    tests: []\n')
    result = run(map_path=map_path, spec_path=tmp_path / "no_spec.md")
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

    spec = tmp_path / "SPEC.md"
    spec.write_text("")

    result = run(map_path=map_path, spec_path=spec, rootdir=tmp_path)
    assert result == 2, (
        f"Expected exit 2 (collection error) but got {result}. "
        "The validator must not proceed against a partial node list."
    )
