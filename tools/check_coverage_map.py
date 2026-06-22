"""check_coverage_map — coverage_map.yaml validator.

Validates two invariants:
  A) Every test name listed in coverage_map.yaml resolves to a collected pytest node.
  B) Every (section, row-id) entry in coverage_map.yaml has at least one test listed.

Run as:
  python tools/check_coverage_map.py            # exits 0 on success
  python -m tools.check_coverage_map            # same

Exit codes:
  0 — all checks passed
  1 — one or more validation failures (dangling names or uncovered rows)
  2 — usage / I/O error (file not found, YAML parse error, etc.)
"""

from __future__ import annotations

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


def collect_test_names(rootdir: Path | None = None) -> set[str]:
    """Run pytest --collect-only -q and return the set of collected test node IDs
    (bare function names, without module path)."""
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q", "--tb=no"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(rootdir) if rootdir else None,
    )
    names: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        # Lines look like: tests/unit/test_foo.py::test_bar_baz
        if "::" in line and not line.startswith("="):
            bare = line.split("::")[-1]
            # Strip parametrize suffix: test_foo[param] → test_foo
            if "[" in bare:
                bare = bare[: bare.index("[")]
            names.add(bare)
    return names


def validate(
    coverage_map: dict[str, dict[str, list[str]]],
    collected: set[str],
) -> tuple[list[str], list[str]]:
    """Return (dangling_errors, uncovered_errors).

    dangling_errors: test names in coverage_map that are NOT in collected tests.
    uncovered_errors: (section, row) entries that have zero listed tests.
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


def run(
    map_path: Path = _DEFAULT_MAP_PATH,
    rootdir: Path | None = None,
) -> int:
    """Main entry point. Returns exit code (0=ok, 1=failures, 2=error)."""
    try:
        coverage_map = load_coverage_map(map_path)
    except (FileNotFoundError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR loading {map_path}: {exc}", file=sys.stderr)
        return 2

    collected = collect_test_names(rootdir)
    dangling, uncovered = validate(coverage_map, collected)

    total_sections = len(coverage_map)
    total_rows = sum(len(rows) for rows in coverage_map.values())
    total_listed = sum(
        len(tests) for rows in coverage_map.values() for tests in rows.values()
    )

    print(
        f"coverage_map: {total_sections} sections, {total_rows} rows, "
        f"{total_listed} listed test names"
    )
    print(f"collected suite: {len(collected)} unique test names")

    if not dangling and not uncovered:
        print("OK — all coverage_map entries resolve to collected tests.")
        return 0

    if uncovered:
        print(f"\nFAIL — {len(uncovered)} row(s) with no tests listed:")
        for msg in uncovered:
            print(f"  UNCOVERED  {msg}")

    if dangling:
        print(f"\nFAIL — {len(dangling)} dangling test name(s) in coverage_map:")
        for msg in dangling:
            print(f"  DANGLING   {msg}")

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
        "--rootdir",
        type=Path,
        default=None,
        help="Working directory for pytest --collect-only (default: cwd)",
    )
    args = parser.parse_args()
    sys.exit(run(map_path=args.map, rootdir=args.rootdir))


if __name__ == "__main__":
    main()
