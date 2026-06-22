"""decide_cap_action decision function — pure, synchronous (SPEC §8.4).

When the converge cap is reached with blockers remaining, always escalate.
D3: the redispatch branch is removed; a stuck converge is always a human problem.
MAX_REDISPATCHES is retained as a named constant for tests; never hardcode 2.
"""

from __future__ import annotations

from typing import Literal

from src.domain.types import MAX_REDISPATCHES  # noqa: F401  # re-exported for tests

CapAction = Literal["escalate"]


def decide_cap_action(redispatch_count: int, has_issue: bool) -> CapAction:
    """Return the action when the converge cap is reached (SPEC §8.4 truth table).

    D3: always returns ``"escalate"`` regardless of ``redispatch_count`` or ``has_issue``.
    ``MAX_REDISPATCHES`` is imported from domain constants and re-exported here so tests
    can reference it without hardcoding the value 2.

    | # | Condition | Output   |
    |---|-----------|----------|
    | 1 | always    | escalate |
    """
    # Inputs accepted to match the specified signature; values are unused per D3.
    _ = redispatch_count
    _ = has_issue
    return "escalate"
