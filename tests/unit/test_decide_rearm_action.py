"""Unit tests — decide_rearm_action (SPEC §8.6 truth table, full branch coverage).

15 required test cases covering all rows including skip-escalated guard.
"""

from __future__ import annotations

import pytest

from src.decisions.decide_rearm_action import decide_rearm_action
from src.domain.types import REARM_RECENT_GUARD_S, RunStatus

# ---------------------------------------------------------------------------
# Row 0 — has_needs_human: skip-escalated (SPEC §8.6 row 0)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.6", "row-0-skip-escalated")
def test_rearm_skip_escalated_needs_human() -> None:
    """has_needs_human=True → skip-escalated (belt-and-suspenders guard)."""
    assert decide_rearm_action(1, None, False, None, True) == "skip-escalated"


@pytest.mark.covers("§8.6", "row-0-skip-escalated")
def test_rearm_skip_escalated_needs_human_even_with_old_run() -> None:
    """has_needs_human=True beats all other conditions."""
    run = RunStatus(state="completed", conclusion="failure")
    assert (
        decide_rearm_action(5, run, False, REARM_RECENT_GUARD_S + 100, True)
        == "skip-escalated"
    )


# ---------------------------------------------------------------------------
# Row 1 — ci_runs == 0: trigger-ci (SPEC §8.6 row 1)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.6", "row-1-trigger-ci")
def test_rearm_trigger_ci_no_runs() -> None:
    """ci_runs == 0 → trigger-ci."""
    assert decide_rearm_action(0, None, False, None, False) == "trigger-ci"


@pytest.mark.covers("§8.6", "row-1-trigger-ci")
def test_rearm_trigger_ci_no_runs_with_terminal_label() -> None:
    """ci_runs == 0 even with has_terminal_label=True → trigger-ci (row 1 beats row 3)."""
    assert decide_rearm_action(0, None, True, None, False) == "trigger-ci"


# ---------------------------------------------------------------------------
# Row 2 — run in progress/queued: skip-in-progress (SPEC §8.6 row 2)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.6", "row-2-skip-in-progress")
def test_rearm_skip_in_progress_queued() -> None:
    """run.state == "queued" → skip-in-progress."""
    run = RunStatus(state="queued")
    assert decide_rearm_action(1, run, False, None, False) == "skip-in-progress"


@pytest.mark.covers("§8.6", "row-2-skip-in-progress")
def test_rearm_skip_in_progress_active() -> None:
    """run.state == "in_progress" → skip-in-progress."""
    run = RunStatus(state="in_progress")
    assert decide_rearm_action(2, run, False, 600, False) == "skip-in-progress"


# ---------------------------------------------------------------------------
# Row 3 — run completed success + terminal label: skip-done (SPEC §8.6 row 3)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.6", "row-3-skip-done")
def test_rearm_skip_done_success_terminal() -> None:
    """completed:success + has_terminal_label → skip-done."""
    run = RunStatus(state="completed", conclusion="success")
    assert decide_rearm_action(1, run, True, 100, False) == "skip-done"


@pytest.mark.covers("§8.6", "row-3-skip-done")
def test_rearm_not_skip_done_success_no_terminal_label() -> None:
    """completed:success but has_terminal_label=False → NOT skip-done (falls through to rearm)."""
    run = RunStatus(state="completed", conclusion="success")
    result = decide_rearm_action(1, run, False, REARM_RECENT_GUARD_S + 1, False)
    assert result != "skip-done"


@pytest.mark.covers("§8.6", "row-3-skip-done")
def test_rearm_not_skip_done_completed_failure() -> None:
    """completed:failure with terminal label → NOT skip-done (any non-success → rearm)."""
    run = RunStatus(state="completed", conclusion="failure")
    result = decide_rearm_action(1, run, True, REARM_RECENT_GUARD_S + 1, False)
    assert result != "skip-done"


# ---------------------------------------------------------------------------
# Row 4 — seconds_since_last_run < REARM_RECENT_GUARD_S: skip-recent (SPEC §8.6 row 4)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.6", "row-4-skip-recent")
def test_rearm_skip_recent_below_guard() -> None:
    """seconds_since_last_run < REARM_RECENT_GUARD_S → skip-recent."""
    run = RunStatus(state="completed", conclusion="failure")
    assert (
        decide_rearm_action(1, run, False, REARM_RECENT_GUARD_S - 1, False)
        == "skip-recent"
    )


@pytest.mark.covers("§8.6", "row-4-skip-recent")
def test_rearm_skip_recent_zero_seconds() -> None:
    """seconds_since_last_run == 0 → skip-recent."""
    assert decide_rearm_action(1, None, False, 0, False) == "skip-recent"


@pytest.mark.covers("§8.6", "row-4-skip-recent")
def test_rearm_not_skip_recent_exactly_guard() -> None:
    """seconds_since_last_run == REARM_RECENT_GUARD_S → NOT recent (strict <); falls through."""
    run = RunStatus(state="completed", conclusion="failure")
    result = decide_rearm_action(1, run, False, REARM_RECENT_GUARD_S, False)
    assert result != "skip-recent"


@pytest.mark.covers("§8.6", "row-4-skip-recent")
def test_rearm_skip_recent_none_skips_guard() -> None:
    """seconds_since_last_run == None → skip recency guard; falls through to rearm."""
    run = RunStatus(state="completed", conclusion="failure")
    assert decide_rearm_action(1, run, False, None, False) == "rearm"


# ---------------------------------------------------------------------------
# Row 5 — else: rearm (SPEC §8.6 row 5)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.6", "row-5-rearm")
def test_rearm_else_rearm_old_run() -> None:
    """run completed non-success, old enough → rearm."""
    run = RunStatus(state="completed", conclusion="failure")
    assert (
        decide_rearm_action(1, run, False, REARM_RECENT_GUARD_S + 10, False) == "rearm"
    )


@pytest.mark.covers("§8.6", "row-5-rearm")
def test_rearm_else_rearm_no_run_ci_ran() -> None:
    """ci_runs > 0, run is None, no recency value → rearm."""
    assert decide_rearm_action(2, None, False, None, False) == "rearm"


# ---------------------------------------------------------------------------
# Constant integrity
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.6", "runtime-guard")
def test_rearm_recent_guard_is_named_constant() -> None:
    """REARM_RECENT_GUARD_S is sourced from domain types."""
    from src.domain.types import REARM_RECENT_GUARD_S as domain_guard

    assert REARM_RECENT_GUARD_S == domain_guard
    assert REARM_RECENT_GUARD_S > 0


# ---------------------------------------------------------------------------
# Arity guard
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.6", "runtime-guard")
def test_rearm_arity() -> None:
    """Missing argument raises TypeError."""
    with pytest.raises(TypeError):
        decide_rearm_action(0, None, False)  # type: ignore[call-arg]
