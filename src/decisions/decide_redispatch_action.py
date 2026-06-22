"""decide_redispatch_action — pure, synchronous (SPEC §8.8).

For an ``agent-work`` issue with no open PR, decides whether to redispatch,
escalate, or skip (RC-4).
"""

from __future__ import annotations

from typing import Literal

from src.domain.types import ISSUE_COOLDOWN_S, ISSUE_REDISPATCH_CAP

RedispatchAction = Literal["skip-has-pr", "skip-recent", "escalate", "redispatch"]


def decide_redispatch_action(
    has_open_pr: bool,
    seconds_since_last_activity: int | None,
    redispatch_count: int,
) -> RedispatchAction:
    """Return the orphan-issue action (SPEC §8.8 truth table).

    | # | Condition                                                                               | Output       |
    |---|-----------------------------------------------------------------------------------------|--------------|
    | 1 | has_open_pr                                                                             | skip-has-pr  |
    | 2 | seconds_since_last_activity is not None and seconds_since_last_activity < ISSUE_COOLDOWN_S | skip-recent  |
    | 3 | redispatch_count >= ISSUE_REDISPATCH_CAP                                                | escalate     |
    | 4 | else                                                                                    | redispatch   |

    Exactly ``ISSUE_COOLDOWN_S`` = NOT recent. ``None`` skips the recency guard.
    """
    # Row 1 — PR already open: nothing to do
    if has_open_pr:
        return "skip-has-pr"

    # Row 2 — issue was touched too recently: skip (strict <; exactly ISSUE_COOLDOWN_S = NOT recent)
    if (
        seconds_since_last_activity is not None
        and seconds_since_last_activity < ISSUE_COOLDOWN_S
    ):
        return "skip-recent"

    # Row 3 — cap reached: escalate (E10)
    if redispatch_count >= ISSUE_REDISPATCH_CAP:
        return "escalate"

    # Row 4 — eligible for redispatch
    return "redispatch"
