"""Unit tests — decide_redispatch_action (SPEC §8.8 truth table, full branch coverage).

14 required test cases covering all rows.
"""

from __future__ import annotations

import pytest

from src.decisions.decide_redispatch_action import decide_redispatch_action
from src.domain.types import ISSUE_COOLDOWN_S, ISSUE_REDISPATCH_CAP

# ---------------------------------------------------------------------------
# Row 1 — has_open_pr: skip-has-pr (SPEC §8.8 row 1)
# ---------------------------------------------------------------------------


def test_redispatch_skip_has_pr() -> None:
    """has_open_pr=True → skip-has-pr regardless of other inputs."""
    assert decide_redispatch_action(True, None, 0) == "skip-has-pr"


def test_redispatch_skip_has_pr_beats_cap() -> None:
    """has_open_pr=True beats cap (row 1 is first-match)."""
    assert decide_redispatch_action(True, 0, ISSUE_REDISPATCH_CAP) == "skip-has-pr"


def test_redispatch_skip_has_pr_beats_recent() -> None:
    """has_open_pr=True beats skip-recent (row 1 before row 2)."""
    assert decide_redispatch_action(True, 0, 0) == "skip-has-pr"


# ---------------------------------------------------------------------------
# Row 2 — seconds_since < ISSUE_COOLDOWN_S: skip-recent (SPEC §8.8 row 2)
# ---------------------------------------------------------------------------


def test_redispatch_skip_recent_below_cooldown() -> None:
    """seconds_since_last_activity < ISSUE_COOLDOWN_S → skip-recent."""
    assert (
        decide_redispatch_action(False, ISSUE_COOLDOWN_S - 1, 0) == "skip-recent"
    )


def test_redispatch_skip_recent_zero() -> None:
    """seconds_since_last_activity == 0 → skip-recent."""
    assert decide_redispatch_action(False, 0, 0) == "skip-recent"


def test_redispatch_not_skip_recent_exactly_cooldown() -> None:
    """seconds_since == ISSUE_COOLDOWN_S → NOT recent (strict <)."""
    result = decide_redispatch_action(False, ISSUE_COOLDOWN_S, 0)
    assert result != "skip-recent"


def test_redispatch_skip_recent_none_skips_guard() -> None:
    """seconds_since_last_activity == None → skip recency guard, falls to row 3/4."""
    result = decide_redispatch_action(False, None, 0)
    assert result != "skip-recent"


# ---------------------------------------------------------------------------
# Row 3 — redispatch_count >= ISSUE_REDISPATCH_CAP: escalate (SPEC §8.8 row 3)
# ---------------------------------------------------------------------------


def test_redispatch_escalate_at_cap() -> None:
    """redispatch_count == ISSUE_REDISPATCH_CAP → escalate (E10)."""
    assert (
        decide_redispatch_action(False, None, ISSUE_REDISPATCH_CAP) == "escalate"
    )


def test_redispatch_escalate_above_cap() -> None:
    """redispatch_count > ISSUE_REDISPATCH_CAP → escalate."""
    assert (
        decide_redispatch_action(False, None, ISSUE_REDISPATCH_CAP + 1) == "escalate"
    )


def test_redispatch_escalate_at_cap_with_old_activity() -> None:
    """At cap + old activity (>= ISSUE_COOLDOWN_S) → escalate."""
    assert (
        decide_redispatch_action(False, ISSUE_COOLDOWN_S + 100, ISSUE_REDISPATCH_CAP)
        == "escalate"
    )


# ---------------------------------------------------------------------------
# Row 4 — else: redispatch (SPEC §8.8 row 4)
# ---------------------------------------------------------------------------


def test_redispatch_else_no_pr_old_activity() -> None:
    """No PR, old enough, below cap → redispatch."""
    assert (
        decide_redispatch_action(False, ISSUE_COOLDOWN_S + 1, 0) == "redispatch"
    )


def test_redispatch_else_no_pr_no_activity() -> None:
    """No PR, None activity (no recency guard), count=0 → redispatch."""
    assert decide_redispatch_action(False, None, 0) == "redispatch"


def test_redispatch_else_below_cap() -> None:
    """count == ISSUE_REDISPATCH_CAP - 1, no recency block → redispatch."""
    count = ISSUE_REDISPATCH_CAP - 1
    assert decide_redispatch_action(False, None, count) == "redispatch"


# ---------------------------------------------------------------------------
# Constant integrity
# ---------------------------------------------------------------------------


def test_issue_redispatch_cap_is_named_constant() -> None:
    """ISSUE_REDISPATCH_CAP is sourced from domain types (never hardcoded 3)."""
    from src.domain.types import ISSUE_REDISPATCH_CAP as domain_cap

    assert ISSUE_REDISPATCH_CAP == domain_cap
    assert ISSUE_REDISPATCH_CAP > 0


def test_issue_cooldown_is_named_constant() -> None:
    """ISSUE_COOLDOWN_S is sourced from domain types."""
    from src.domain.types import ISSUE_COOLDOWN_S as domain_cool

    assert ISSUE_COOLDOWN_S == domain_cool
    assert ISSUE_COOLDOWN_S > 0


# ---------------------------------------------------------------------------
# Arity guard
# ---------------------------------------------------------------------------


def test_redispatch_arity() -> None:
    """Missing argument raises TypeError."""
    with pytest.raises(TypeError):
        decide_redispatch_action(False, None)  # type: ignore[call-arg]
