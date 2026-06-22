"""Unit tests — decide_conflict_action (SPEC §8.7 truth table, full branch coverage).

7 required test cases covering both rows and edge conditions.
"""

from __future__ import annotations

import pytest

from src.decisions.decide_conflict_action import decide_conflict_action

# ---------------------------------------------------------------------------
# Row 1 — CONFLICTING + not already labeled: escalate (SPEC §8.7 row 1)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.7", "row-1-has-conflict-no-label")
def test_conflict_escalate_conflicting_not_labeled() -> None:
    """CONFLICTING, not already needs-human → escalate (E7)."""
    assert decide_conflict_action("CONFLICTING", False) == "escalate"


# ---------------------------------------------------------------------------
# Row 2 — else: skip (SPEC §8.7 row 2)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.7", "row-3-already-labeled")
def test_conflict_skip_already_labeled() -> None:
    """CONFLICTING but already has needs-human → skip (idempotent)."""
    assert decide_conflict_action("CONFLICTING", True) == "skip"


@pytest.mark.covers("§8.7", "row-2-no-conflict")
def test_conflict_skip_mergeable() -> None:
    """MERGEABLE → skip."""
    assert decide_conflict_action("MERGEABLE", False) == "skip"


@pytest.mark.covers("§8.7", "row-2-no-conflict")
def test_conflict_skip_unknown() -> None:
    """UNKNOWN mergeable state → skip."""
    assert decide_conflict_action("UNKNOWN", False) == "skip"


@pytest.mark.covers("§8.7", "row-3-already-labeled")
def test_conflict_skip_mergeable_already_labeled() -> None:
    """MERGEABLE + already labeled → skip."""
    assert decide_conflict_action("MERGEABLE", True) == "skip"


@pytest.mark.covers("§8.7", "row-2-no-conflict")
def test_conflict_skip_empty_string() -> None:
    """Empty string mergeable → skip."""
    assert decide_conflict_action("", False) == "skip"


@pytest.mark.covers("§8.7", "row-2-no-conflict")
def test_conflict_skip_random_value() -> None:
    """Any non-CONFLICTING value → skip."""
    assert decide_conflict_action("BEHIND", False) == "skip"


# ---------------------------------------------------------------------------
# Arity guard
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.7", "enum-exhaustiveness")
def test_conflict_action_arity() -> None:
    """Missing argument raises TypeError."""
    with pytest.raises(TypeError):
        decide_conflict_action("CONFLICTING")  # type: ignore[call-arg]
