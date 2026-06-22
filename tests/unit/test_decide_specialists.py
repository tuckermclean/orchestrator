"""Unit tests for decide_specialists — SPEC §8.12 / TESTING.md §2."""

from __future__ import annotations

from src.decisions.decide_specialists import decide_specialists
from src.domain.types import CONVERGE_REVIEW_BASE, PARALLEL_SPECIALIST_CAP

_BASE = list(CONVERGE_REVIEW_BASE)


# ---------------------------------------------------------------------------
# Base set alone — no routing matches
# ---------------------------------------------------------------------------


def test_decide_specialists_base_set_only() -> None:
    """No path matches routing → exactly the base set, in order."""
    assert decide_specialists(["README.md", "src/foo.py"], 1) == _BASE


def test_decide_specialists_empty_paths_returns_base() -> None:
    assert decide_specialists([], 1) == _BASE


# ---------------------------------------------------------------------------
# Each routing pattern adds its specialist
# ---------------------------------------------------------------------------


def test_decide_specialists_db_migration() -> None:
    result = decide_specialists(["db/migrations/001_init.sql"], 1)
    assert "engineering-database-optimizer.md" in result


def test_decide_specialists_sql_file() -> None:
    result = decide_specialists(["queries/report.sql"], 1)
    assert "engineering-database-optimizer.md" in result


def test_decide_specialists_schema_file() -> None:
    result = decide_specialists(["src/schema.py"], 1)
    assert "engineering-database-optimizer.md" in result


def test_decide_specialists_ui_tsx() -> None:
    result = decide_specialists(["ui/src/App.tsx"], 1)
    assert "testing-accessibility-auditor.md" in result


def test_decide_specialists_css() -> None:
    result = decide_specialists(["ui/styles/main.css"], 1)
    assert "testing-accessibility-auditor.md" in result


def test_decide_specialists_components_dir() -> None:
    result = decide_specialists(["app/components/Button.jsx"], 1)
    assert "testing-accessibility-auditor.md" in result


def test_decide_specialists_api_dir() -> None:
    result = decide_specialists(["src/api/routes.py"], 1)
    assert "testing-api-tester.md" in result


def test_decide_specialists_routes_dir() -> None:
    result = decide_specialists(["src/routes/users.py"], 1)
    assert "testing-api-tester.md" in result


def test_decide_specialists_handlers_dir() -> None:
    result = decide_specialists(["pkg/handlers/auth.go"], 1)
    assert "testing-api-tester.md" in result


# ---------------------------------------------------------------------------
# Cap enforcement — base always retained, extras truncated
# ---------------------------------------------------------------------------


def test_decide_specialists_cap_enforced() -> None:
    """All three routing rows match → capped at PARALLEL_SPECIALIST_CAP, base retained."""
    paths = ["db/migrations/x.sql", "ui/App.tsx", "src/api/routes.py"]
    result = decide_specialists(paths, 1)
    assert len(result) <= PARALLEL_SPECIALIST_CAP
    assert result[: len(_BASE)] == _BASE
    # cap is 4, base is 2 → 2 extra slots: db + ui (api truncated by definition order)
    assert result == [
        *_BASE,
        "engineering-database-optimizer.md",
        "testing-accessibility-auditor.md",
    ]


# ---------------------------------------------------------------------------
# Determinism / definition order
# ---------------------------------------------------------------------------


def test_decide_specialists_definition_order() -> None:
    """Extras appear in SPECIALIST_ROUTING definition order regardless of path order."""
    paths = ["src/api/routes.py", "db/x.sql"]  # api listed first, but db routes first
    result = decide_specialists(paths, 1)
    assert result == [
        *_BASE,
        "engineering-database-optimizer.md",
        "testing-api-tester.md",
    ]


def test_decide_specialists_deterministic_repeat() -> None:
    paths = ["ui/App.tsx", "src/api/routes.py"]
    assert decide_specialists(paths, 1) == decide_specialists(paths, 3)


def test_decide_specialists_no_duplicate_refs() -> None:
    """Two paths hitting the same routing row do not duplicate the ref."""
    result = decide_specialists(["a.sql", "b.sql"], 1)
    assert result.count("engineering-database-optimizer.md") == 1
