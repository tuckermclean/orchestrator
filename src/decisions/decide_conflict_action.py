"""decide_conflict_action — pure, synchronous (SPEC §8.7).

For all open PRs, decides whether to escalate due to merge conflict (RC-2).
"""

from __future__ import annotations

from typing import Literal

ConflictAction = Literal["escalate", "skip"]


def decide_conflict_action(
    mergeable: str,
    already_needs_human: bool,
) -> ConflictAction:
    """Return the conflict action for an open PR (SPEC §8.7 truth table).

    | # | Condition                                          | Output   |
    |---|----------------------------------------------------|----------|
    | 1 | mergeable == "CONFLICTING" and not already_needs_human | escalate |
    | 2 | else                                               | skip     |
    """
    if mergeable == "CONFLICTING" and not already_needs_human:
        return "escalate"
    return "skip"
