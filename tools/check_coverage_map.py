"""check_coverage_map — coverage_map.yaml validator.

Validates five invariants:
  A) Every test name listed in coverage_map.yaml resolves to a collected pytest node.
  B) Every (section, row-id) entry in coverage_map.yaml has at least one test listed.
  C) Every SPEC.md §8 decision-function section has a corresponding coverage_map entry.
  D) Every @pytest.mark.covers(section, row) marker references a (section, row) that
     exists in coverage_map.yaml (marker → map direction).
  E) Every (section, row) entry in coverage_map.yaml that lists a test has that test
     decorated with @covers(section, row) (map → marker direction).
  F) Node-id collision detection: a bare test name that appears in multiple test modules
     is flagged so a deleted test cannot be masked by a same-named test elsewhere.

Run as:
  python tools/check_coverage_map.py            # exits 0 on success
  python -m tools.check_coverage_map            # same

Exit codes:
  0 — all checks passed
  1 — one or more validation failures
  2 — I/O or usage error (file not found, YAML parse error, pytest collection error)
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml  # type: ignore[import-untyped]
except ImportError:
    print(
        "ERROR: PyYAML not installed — run: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(2)

# ---------------------------------------------------------------------------
# Public API used by tests
# ---------------------------------------------------------------------------

_DEFAULT_MAP_PATH = Path("coverage_map.yaml")
_DEFAULT_SPEC_PATH = Path("SPEC.md")

# Regex that matches SPEC.md §8 decision-function section headers.
# Captures the section ID (e.g. "§8.1", "§8.2a", "§8.10").
_SPEC_SECTION_RE = re.compile(r"^### (§8\.\d+[a-z]*)\b")


def load_coverage_map(path: Path = _DEFAULT_MAP_PATH) -> dict[str, dict[str, list[str]]]:
    """Parse coverage_map.yaml into {section: {row_id: [test_name, ...]}}."""
    if not path.exists():
        raise FileNotFoundError(f"coverage_map.yaml not found at {path}")
    with path.open() as fh:
        raw: Any = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"coverage_map.yaml must be a YAML mapping, got {type(raw)}")
    result: dict[str, dict[str, list[str]]] = {}
    for section, rows in raw.items():
        if not isinstance(rows, dict):
            raise ValueError(
                f"Section {section!r} must be a mapping of row_id → {{tests: [...]}}"
            )
        result[str(section)] = {}
        for row_id, row_value in rows.items():
            if isinstance(row_value, dict):
                tests = row_value.get("tests", [])
            elif isinstance(row_value, list):
                # Legacy format: row_id: [test1, test2]
                tests = row_value
            else:
                tests = []
            if not isinstance(tests, list):
                raise ValueError(
                    f"Section {section!r} row {row_id!r}: 'tests' must be a list, "
                    f"got {type(tests)}"
                )
            result[str(section)][str(row_id)] = [str(t) for t in tests]
    return result


def parse_spec_functions(spec_path: Path = _DEFAULT_SPEC_PATH) -> list[str]:
    """Parse SPEC.md and return the list of §8.x section IDs in document order.

    Matches headers of the form ``### §8.1 `route_entry``` (with any suffix).
    Returns e.g. ["§8.1", "§8.2", "§8.2a", "§8.3", ..., "§8.12"].
    """
    if not spec_path.exists():
        raise FileNotFoundError(f"SPEC.md not found at {spec_path}")
    sections: list[str] = []
    for line in spec_path.read_text(encoding="utf-8").splitlines():
        m = _SPEC_SECTION_RE.match(line)
        if m:
            section_id = m.group(1)
            if section_id not in sections:
                sections.append(section_id)
    return sections


class CollectionError(RuntimeError):
    """Raised when pytest --collect-only exits with a collection error (returncode != 0, 5)."""


def collect_node_ids(rootdir: Path | None = None) -> tuple[set[str], dict[str, list[str]]]:
    """Run pytest --collect-only -q and return collected node IDs.

    Returns:
        (node_ids, name_to_node_ids) where:
        - node_ids: the full set of node IDs, e.g.
          {"tests/unit/test_foo.py::test_bar", ...}
        - name_to_node_ids: mapping from bare function name to all node IDs that
          share that name; entries with >1 values indicate a collision.

    Parametrized tests are de-duplicated: ``test_foo[param]`` → ``tests/.../test_foo``.

    Raises:
        CollectionError: if pytest exits with a returncode other than 0 or 5.
    """
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "--tb=no"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(rootdir) if rootdir else None,
    )
    if result.returncode not in (0, 5):
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        raise CollectionError(
            f"pytest --collect-only exited {result.returncode} (collection error); "
            "stderr shown above. Fix the import/syntax error before re-running."
        )
    if result.returncode == 5:
        return set(), {}

    node_ids: set[str] = set()
    name_to_node_ids: dict[str, list[str]] = {}

    for line in result.stdout.splitlines():
        line = line.strip()
        if "::" not in line or line.startswith("="):
            continue
        # Strip parametrize suffix: tests/unit/test_foo.py::test_bar[param] →
        #   tests/unit/test_foo.py::test_bar
        if "[" in line:
            line = line[: line.index("[")]
        node_ids.add(line)
        bare = line.split("::")[-1]
        name_to_node_ids.setdefault(bare, [])
        if line not in name_to_node_ids[bare]:
            name_to_node_ids[bare].append(line)

    return node_ids, name_to_node_ids


def collect_test_names(rootdir: Path | None = None) -> set[str]:
    """Return the set of collected bare function names (backward-compatible helper).

    Delegates to collect_node_ids; callers that only need bare names continue
    to work.  Raises CollectionError on pytest collection failure.
    """
    _, name_to_node_ids = collect_node_ids(rootdir)
    return set(name_to_node_ids.keys())


# ---------------------------------------------------------------------------
# Marker collection — AST-based (no pytest execution required)
# ---------------------------------------------------------------------------


def _extract_covers_from_node(
    node: ast.AST, source_path: Path
) -> list[tuple[str, str, str]]:
    """Walk a single AST node and yield (func_name, section, row) triples.

    Inspects function-level decorators for ``@pytest.mark.covers(section, row)``.
    Returns a list so that one function may carry multiple markers.
    """
    results: list[tuple[str, str, str]] = []
    for item in ast.walk(node):
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        func_name = item.name
        for deco in item.decorator_list:
            # Accept both @pytest.mark.covers(...) and @covers(...) forms.
            if not isinstance(deco, ast.Call):
                continue
            func = deco.func
            # Match: pytest.mark.covers OR mark.covers OR covers
            if isinstance(func, ast.Attribute) and func.attr == "covers":
                pass  # accept any .covers(...) call
            elif isinstance(func, ast.Name) and func.id == "covers":
                pass
            else:
                continue
            # Extract the two positional string arguments.
            if len(deco.args) >= 2:
                section_node = deco.args[0]
                row_node = deco.args[1]
                if isinstance(section_node, ast.Constant) and isinstance(
                    row_node, ast.Constant
                ):
                    section = str(section_node.value)
                    row = str(row_node.value)
                    results.append((func_name, section, row))
    return results


def collect_markers(
    rootdir: Path | None = None,
) -> dict[tuple[str, str], list[str]]:
    """Scan all test_*.py files under rootdir and collect @covers markers.

    Returns a mapping from (section, row_id) → list of bare function names
    that carry that marker.  One function may appear in multiple entries.

    The scan is purely AST-based; it does not import or execute any test code.
    """
    base = rootdir if rootdir is not None else Path(".")
    # Scan only the project's own tests/ tree (mirrors pyproject testpaths and
    # the `pytest --collect-only` scope used by collect_node_ids). Never descend
    # into .venv or .claude/worktrees, which hold unrelated test_*.py copies
    # (vendored packages, other branches) that would corrupt the marker set.
    search_root = base / "tests" if (base / "tests").is_dir() else base
    skip_dirs = {".venv", ".claude", "__pycache__", "node_modules", ".git"}
    markers: dict[tuple[str, str], list[str]] = {}
    for py_file in sorted(search_root.rglob("test_*.py")):
        # Check relative parts only — the absolute path may contain skip_dir
        # components (e.g. when the worktree lives inside .claude/worktrees/).
        try:
            rel_parts = py_file.relative_to(search_root).parts
        except ValueError:
            rel_parts = py_file.parts
        if any(part in skip_dirs for part in rel_parts):
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, OSError):
            continue
        for func_name, section, row in _extract_covers_from_node(tree, py_file):
            key = (section, row)
            markers.setdefault(key, [])
            if func_name not in markers[key]:
                markers[key].append(func_name)
    return markers


# ---------------------------------------------------------------------------
# Validation functions
# ---------------------------------------------------------------------------


def check_spec_completeness(
    spec_functions: list[str],
    coverage_map: dict[str, dict[str, list[str]]],
) -> list[str]:
    """Return errors for §8 functions that have no coverage_map section.

    A SPEC.md §8 section is considered "covered" when the coverage_map contains
    at least one key that starts with the section ID.  For example ``§8.10``
    matches both ``"§8.10"`` and ``"§8.10-something"`` but not ``"§8.1"``.
    """
    errors: list[str] = []
    map_keys = list(coverage_map.keys())
    for spec_section in spec_functions:
        # A key matches if it equals the section id, or starts with "<id>-"
        # (e.g. "§8.3-integration" satisfies §8.3).
        covered = any(
            k == spec_section or k.startswith(spec_section + "-")
            for k in map_keys
        )
        if not covered:
            errors.append(
                f"SPEC.md {spec_section} has no coverage_map section "
                f"(expected key {spec_section!r} or {spec_section + '-...'!r})"
            )
    return errors


def validate(
    coverage_map: dict[str, dict[str, list[str]]],
    collected: set[str],
) -> tuple[list[str], list[str]]:
    """Return (dangling_errors, uncovered_errors).

    dangling_errors: test names in coverage_map that are NOT in collected tests.
    uncovered_errors: (section, row) entries that have zero listed tests.

    This function operates on bare names for backward compatibility.  The caller
    is responsible for passing ``collected`` as the set of bare names.
    """
    dangling: list[str] = []
    uncovered: list[str] = []

    for section, rows in coverage_map.items():
        for row_id, tests in rows.items():
            if not tests:
                uncovered.append(f"{section}/{row_id}: no tests listed")
                continue
            for name in tests:
                if name not in collected:
                    dangling.append(
                        f"{section}/{row_id}: test {name!r} not found in collected suite"
                    )

    return dangling, uncovered


def check_node_id_collisions(
    coverage_map: dict[str, dict[str, list[str]]],
    name_to_node_ids: dict[str, list[str]],
) -> list[str]:
    """Return errors for bare test names in coverage_map that collide across modules.

    A collision means two or more test modules define a function with the same
    name.  The coverage_map cannot unambiguously resolve which one is meant, so
    each colliding entry is flagged.  The fix is to rename one of the colliding
    functions or update the map entry to use a unique name.
    """
    errors: list[str] = []
    for section, rows in coverage_map.items():
        for row_id, tests in rows.items():
            for name in tests:
                node_ids = name_to_node_ids.get(name, [])
                if len(node_ids) > 1:
                    colliders = ", ".join(node_ids)
                    errors.append(
                        f"{section}/{row_id}: test name {name!r} is ambiguous — "
                        f"found in multiple modules: {colliders}"
                    )
    return errors


def check_markers_to_map(
    markers: dict[tuple[str, str], list[str]],
    coverage_map: dict[str, dict[str, list[str]]],
) -> list[str]:
    """Direction D: marker → map.

    Returns errors when a @covers(section, row) marker references a (section,
    row) pair that does not exist in coverage_map.yaml.
    """
    errors: list[str] = []
    for (section, row), funcs in sorted(markers.items()):
        map_section = coverage_map.get(section)
        if map_section is None or row not in map_section:
            func_list = ", ".join(funcs)
            errors.append(
                f"@covers({section!r}, {row!r}) used in [{func_list}] "
                f"but {section!r}/{row!r} is absent from coverage_map.yaml"
            )
    return errors


def check_map_to_markers(
    coverage_map: dict[str, dict[str, list[str]]],
    markers: dict[tuple[str, str], list[str]],
) -> list[str]:
    """Direction E: map → marker.

    Returns errors when a coverage_map entry lists a test that does NOT carry
    the corresponding @covers(section, row) marker.

    Note: only (section, row) pairs where the section ID matches the §8.x
    pattern are checked.  Non-§8 sections (e.g. "§4.2", "§6-escalations") are
    excluded from this direction of the check because those sections track
    integration/engine tests that may be annotated differently.
    """
    errors: list[str] = []
    for section, rows in coverage_map.items():
        # Only enforce for §8.x sections (decision-function truth tables).
        if not re.match(r"^§8\.", section):
            continue
        for row_id, tests in rows.items():
            marked_funcs = markers.get((section, row_id), [])
            for test_name in tests:
                if test_name not in marked_funcs:
                    errors.append(
                        f"{section}/{row_id}: test {test_name!r} listed in "
                        f"coverage_map but lacks @covers({section!r}, {row_id!r}) marker"
                    )
    return errors


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run(
    map_path: Path = _DEFAULT_MAP_PATH,
    spec_path: Path = _DEFAULT_SPEC_PATH,
    rootdir: Path | None = None,
) -> int:
    """Main entry point. Returns exit code (0=ok, 1=failures, 2=error)."""
    try:
        coverage_map = load_coverage_map(map_path)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR loading {map_path}: {exc}", file=sys.stderr)
        return 2

    try:
        spec_functions = parse_spec_functions(spec_path)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        node_ids, name_to_node_ids = collect_node_ids(rootdir)
    except CollectionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    collected_names = set(name_to_node_ids.keys())

    # Resolve rootdir for marker scanning: use rootdir if given, else cwd.
    marker_root = rootdir if rootdir is not None else Path(".")
    markers = collect_markers(marker_root)

    # --- Run all checks ---
    spec_errors = check_spec_completeness(spec_functions, coverage_map)
    dangling, uncovered = validate(coverage_map, collected_names)
    collision_errors = check_node_id_collisions(coverage_map, name_to_node_ids)
    marker_to_map_errors = check_markers_to_map(markers, coverage_map)
    map_to_marker_errors = check_map_to_markers(coverage_map, markers)

    # --- Summary line ---
    total_sections = len(coverage_map)
    total_rows = sum(len(rows) for rows in coverage_map.values())
    total_listed = sum(
        len(tests) for rows in coverage_map.values() for tests in rows.values()
    )
    total_markers = sum(len(funcs) for funcs in markers.values())

    print(
        f"coverage_map: {total_sections} sections, {total_rows} rows, "
        f"{total_listed} listed test names"
    )
    print(f"collected suite: {len(node_ids)} node IDs ({len(collected_names)} unique names)")
    print(f"SPEC.md §8 functions: {len(spec_functions)}")
    print(f"@covers markers found: {total_markers} across {len(markers)} (section,row) pairs")

    all_errors = (
        spec_errors
        + dangling
        + uncovered
        + collision_errors
        + marker_to_map_errors
        + map_to_marker_errors
    )

    if not all_errors:
        print("OK — all checks passed.")
        return 0

    if spec_errors:
        print(f"\nFAIL — {len(spec_errors)} SPEC §8 section(s) missing from coverage_map:")
        for msg in spec_errors:
            print(f"  SPEC-MISSING  {msg}")

    if uncovered:
        print(f"\nFAIL — {len(uncovered)} row(s) with no tests listed:")
        for msg in uncovered:
            print(f"  UNCOVERED     {msg}")

    if dangling:
        print(f"\nFAIL — {len(dangling)} dangling test name(s) in coverage_map:")
        for msg in dangling:
            print(f"  DANGLING      {msg}")

    if collision_errors:
        print(f"\nFAIL — {len(collision_errors)} node-id collision(s) in coverage_map:")
        for msg in collision_errors:
            print(f"  COLLISION     {msg}")

    if marker_to_map_errors:
        print(
            f"\nFAIL — {len(marker_to_map_errors)} @covers marker(s) with no "
            f"matching coverage_map entry:"
        )
        for msg in marker_to_map_errors:
            print(f"  MARKER→MAP    {msg}")

    if map_to_marker_errors:
        print(
            f"\nFAIL — {len(map_to_marker_errors)} coverage_map test(s) lacking "
            f"the corresponding @covers marker:"
        )
        for msg in map_to_marker_errors:
            print(f"  MAP→MARKER    {msg}")

    return 1


def main() -> None:
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate coverage_map.yaml against the collected pytest suite."
    )
    parser.add_argument(
        "--map",
        type=Path,
        default=_DEFAULT_MAP_PATH,
        help="Path to coverage_map.yaml (default: coverage_map.yaml)",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        default=_DEFAULT_SPEC_PATH,
        help="Path to SPEC.md (default: SPEC.md)",
    )
    parser.add_argument(
        "--rootdir",
        type=Path,
        default=None,
        help="Working directory for pytest --collect-only and marker scan (default: cwd)",
    )
    args = parser.parse_args()
    sys.exit(run(map_path=args.map, spec_path=args.spec, rootdir=args.rootdir))


if __name__ == "__main__":
    main()
