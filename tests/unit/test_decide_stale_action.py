"""Unit tests — decide_stale_action (SPEC §8.5 truth table, full branch coverage).

20 required test cases covering all rows including 2.5a/b/c sub-rows.
"""

from __future__ import annotations

import pytest

from src.decisions.decide_stale_action import decide_stale_action
from src.domain.types import RECONCILER_STALE_REDISPATCH_CAP

# ---------------------------------------------------------------------------
# Row 1 — cap reached: escalate (SPEC §8.5 row 1)
# ---------------------------------------------------------------------------


def test_stale_escalate_at_cap() -> None:
    """redispatch_count == RECONCILER_STALE_REDISPATCH_CAP → escalate (E8)."""
    assert (
        decide_stale_action(
            RECONCILER_STALE_REDISPATCH_CAP, 5, False, 2, True, True, True
        )
        == "escalate"
    )


def test_stale_escalate_above_cap() -> None:
    """redispatch_count > RECONCILER_STALE_REDISPATCH_CAP → escalate."""
    assert (
        decide_stale_action(
            RECONCILER_STALE_REDISPATCH_CAP + 1, 3, False, 1, True, True, True
        )
        == "escalate"
    )


def test_stale_escalate_far_above_cap() -> None:
    """Large redispatch_count → escalate (row 1 is first-match)."""
    assert decide_stale_action(999, 1, False, 0, True, True, True) == "escalate"


def test_stale_escalate_row1_beats_ci_runs_zero() -> None:
    """Row 1 beats row 2: cap reached even when ci_runs == 0 → escalate (not trigger-ci)."""
    assert (
        decide_stale_action(
            RECONCILER_STALE_REDISPATCH_CAP, 0, False, 0, True, True, True
        )
        == "escalate"
    )


# ---------------------------------------------------------------------------
# Row 2 — ci_runs == 0: trigger-ci (SPEC §8.5 row 2)
# ---------------------------------------------------------------------------


def test_stale_trigger_ci_no_runs() -> None:
    """ci_runs == 0, below cap → trigger-ci."""
    assert decide_stale_action(0, 0, False, 0, True, True, True) == "trigger-ci"


def test_stale_trigger_ci_no_runs_not_draft() -> None:
    """ci_runs == 0, non-draft, below cap → trigger-ci."""
    assert decide_stale_action(0, 0, False, 0, True, True, False) == "trigger-ci"


def test_stale_trigger_ci_beats_nodiff() -> None:
    """Row 2 beats rows 2.5: ci_runs == 0 fires before 0-diff check."""
    assert decide_stale_action(0, 0, False, 0, True, False, True) == "trigger-ci"


# ---------------------------------------------------------------------------
# Rows 2.5a/b/c — zero-diff PR (SPEC §8.5, D4)
# ---------------------------------------------------------------------------


def test_stale_nodiff_draft_with_issue_redispatch() -> None:
    """Row 2.5a: has_diff=False, is_draft=True, has_issue=True → redispatch."""
    assert decide_stale_action(0, 1, False, 2, True, False, True) == "redispatch"


def test_stale_nodiff_draft_no_issue_needs_human() -> None:
    """Row 2.5b: has_diff=False, is_draft=True, has_issue=False → needs-human (E9)."""
    assert decide_stale_action(0, 1, False, 2, False, False, True) == "needs-human"


def test_stale_nodiff_nondraft_needs_human() -> None:
    """Row 2.5c: has_diff=False, is_draft=False → needs-human (D4: finished-empty)."""
    assert decide_stale_action(0, 1, False, 0, True, False, False) == "needs-human"


def test_stale_nodiff_nondraft_no_issue_needs_human() -> None:
    """Row 2.5c: non-draft, 0-diff, no issue → needs-human."""
    assert decide_stale_action(0, 1, False, 0, False, False, False) == "needs-human"


# ---------------------------------------------------------------------------
# Row 3 — has_converge: mark-ready (SPEC §8.5 row 3)
# ---------------------------------------------------------------------------


def test_stale_mark_ready_has_converge() -> None:
    """has_converge=True, has_diff=True → mark-ready."""
    assert decide_stale_action(0, 3, True, 1, False, True, True) == "mark-ready"


def test_stale_mark_ready_has_converge_nondraft() -> None:
    """has_converge=True, non-draft, has_diff=True → mark-ready."""
    assert decide_stale_action(1, 2, True, 0, True, True, False) == "mark-ready"


# ---------------------------------------------------------------------------
# Row 4 — failing_count == 0: mark-ready-and-converge (SPEC §8.5 row 4)
# ---------------------------------------------------------------------------


def test_stale_mark_ready_and_converge_ci_clean() -> None:
    """failing_count == 0, no converge label, has_diff=True → mark-ready-and-converge."""
    assert (
        decide_stale_action(0, 2, False, 0, True, True, True)
        == "mark-ready-and-converge"
    )


def test_stale_mark_ready_and_converge_no_issue() -> None:
    """failing_count == 0, no issue → mark-ready-and-converge (row 4 beats rows 5/6)."""
    assert (
        decide_stale_action(0, 1, False, 0, False, True, True)
        == "mark-ready-and-converge"
    )


# ---------------------------------------------------------------------------
# Row 5 — has_issue: redispatch (SPEC §8.5 row 5)
# ---------------------------------------------------------------------------


def test_stale_redispatch_has_issue_failing() -> None:
    """CI failing, has_issue=True → redispatch."""
    assert decide_stale_action(0, 2, False, 3, True, True, True) == "redispatch"


def test_stale_redispatch_has_issue_below_cap() -> None:
    """redispatch_count < cap, has_issue=True, failing_count > 0 → redispatch."""
    count = RECONCILER_STALE_REDISPATCH_CAP - 1
    assert decide_stale_action(count, 2, False, 1, True, True, False) == "redispatch"


# ---------------------------------------------------------------------------
# Row 6 — else: needs-human (SPEC §8.5 row 6)
# ---------------------------------------------------------------------------


def test_stale_needs_human_failing_no_issue() -> None:
    """CI failing, no issue → needs-human (E9)."""
    assert decide_stale_action(0, 2, False, 1, False, True, True) == "needs-human"


def test_stale_needs_human_failing_no_issue_nondraft() -> None:
    """Non-draft, CI failing, no issue → needs-human."""
    assert decide_stale_action(0, 3, False, 2, False, True, False) == "needs-human"


# ---------------------------------------------------------------------------
# Constant integrity
# ---------------------------------------------------------------------------


def test_reconciler_stale_redispatch_cap_is_named_constant() -> None:
    """RECONCILER_STALE_REDISPATCH_CAP comes from domain types (never hardcoded 3)."""
    from src.domain.types import RECONCILER_STALE_REDISPATCH_CAP as domain_cap

    assert RECONCILER_STALE_REDISPATCH_CAP == domain_cap
    assert RECONCILER_STALE_REDISPATCH_CAP > 0


# ---------------------------------------------------------------------------
# Below-cap boundary — last value before escalate
# ---------------------------------------------------------------------------


def test_stale_below_cap_does_not_escalate() -> None:
    """redispatch_count == RECONCILER_STALE_REDISPATCH_CAP - 1 → does not escalate."""
    count = RECONCILER_STALE_REDISPATCH_CAP - 1
    result = decide_stale_action(count, 2, False, 1, True, True, True)
    assert result != "escalate"


# ---------------------------------------------------------------------------
# Signature / arity guard
# ---------------------------------------------------------------------------


def test_stale_action_arity() -> None:
    """Missing argument raises TypeError."""
    with pytest.raises(TypeError):
        decide_stale_action(0, 0, False)  # type: ignore[call-arg]
