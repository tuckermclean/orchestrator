"""decide_rearm_action — pure, synchronous (SPEC §8.6).

For a non-draft converge PR, decides whether to trigger CI, re-arm, or skip.
"""

from __future__ import annotations

from typing import Literal

from src.domain.types import REARM_RECENT_GUARD_S, RunStatus

RearmAction = Literal[
    "trigger-ci",
    "rearm",
    "skip-escalated",
    "skip-in-progress",
    "skip-done",
    "skip-recent",
]


def decide_rearm_action(
    ci_runs: int,
    run: RunStatus | None,
    has_terminal_label: bool,
    seconds_since_last_run: int | None,
    has_needs_human: bool,
) -> RearmAction:
    """Return the re-arm action for a converge PR (SPEC §8.6 truth table).

    Row 0: has_needs_human                              → skip-escalated
    Row 1: ci_runs == 0                                 → trigger-ci
    Row 2: run active (queued/in_progress)              → skip-in-progress
    Row 3: run completed+success AND has_terminal_label → skip-done
    Row 4: seconds_since_last_run < REARM_RECENT_GUARD_S → skip-recent
    Row 5: else                                          → rearm
    """
    # Row 0 — belt-and-suspenders: RC-3 scope already excludes needs-human PRs,
    # but the explicit check prevents silent regression if scope filter is relaxed.
    if has_needs_human:
        return "skip-escalated"

    # Row 1 — no CI runs at all: trigger CI before attempting rearm
    if ci_runs == 0:
        return "trigger-ci"

    # Row 2 — run is active: skip to prevent duplicate dispatch
    if run is not None and run.state in ("queued", "in_progress"):
        return "skip-in-progress"

    # Row 3 — run completed successfully and we already have a terminal label: done
    if (
        run is not None
        and run.state == "completed"
        and run.conclusion == "success"
        and has_terminal_label
    ):
        return "skip-done"

    # Row 4 — last run was too recent: skip-recent guard (strict <)
    # Note: exactly REARM_RECENT_GUARD_S = NOT recent (boundary is stale)
    if seconds_since_last_run is not None and seconds_since_last_run < REARM_RECENT_GUARD_S:
        return "skip-recent"

    # Row 5 — else: rearm (covers completed non-success, or no active run and old enough)
    return "rearm"
