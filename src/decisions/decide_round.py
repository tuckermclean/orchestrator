"""decide_round decision function — pure, synchronous (SPEC §8.3)."""

from __future__ import annotations

from typing import Literal

from src.domain.types import SENTINEL_SIGNATURE

ConvergeToken = Literal[
    "approve",
    "fix",
    "escalate:no-progress",
    "escalate:no-verdict",
    "escalate:ci-red",
    "escalate:cap-reached",
]


def _normalize(sigs: list[str]) -> list[str]:
    """Sentinel normalization: ['verdict-file-not-written'] → [] (SPEC §8.3)."""
    if sigs == [SENTINEL_SIGNATURE]:
        return []
    return sigs


def decide_round(
    round: Literal[1, 2, 3],
    blockers: int | Literal["unknown"],
    ci_green: bool,
    prev_sigs: list[str],
    curr_sigs: list[str],
) -> ConvergeToken:
    """Decide the convergence action for one round (SPEC §8.3 truth table).

    Priority table evaluated top-to-bottom; first match fires. Both signature lists are
    sentinel-normalized and sorted lexicographically before the no-progress comparison so
    detection is stable regardless of reviewer output order.

    `round` is `Literal[1, 2, 3]`; a value outside that set is a `TypeError` (SPEC §8.3).
    Implementations must not accept arbitrary integers. `ci_green` must be a `bool` and
    `blockers` must be an `int` or the literal `"unknown"`; wrong types raise `TypeError`.
    """
    # Runtime validation — the static Literal is not enforced at runtime, so guard the
    # call site explicitly (SPEC §8.3). bool is a subclass of int; reject it for `round`
    # and `blockers` but require it exactly for `ci_green`.
    if not isinstance(round, int) or isinstance(round, bool) or round not in (1, 2, 3):
        raise TypeError(f"round must be one of 1, 2, 3; got {round!r}")
    if not isinstance(ci_green, bool):
        raise TypeError(f"ci_green must be a bool; got {ci_green!r}")
    if blockers != "unknown" and (not isinstance(blockers, int) or isinstance(blockers, bool)):
        raise TypeError(f"blockers must be an int or 'unknown'; got {blockers!r}")

    # Row 1 — clean and green approves in any round. "unknown" never matches (int 0 only).
    if blockers == 0 and ci_green:
        return "approve"

    # Row 2 — R1 always advances to a fix step.
    if round == 1:
        return "fix"

    # Row 3 — no-progress: identical non-empty signatures two consecutive rounds.
    prev = sorted(_normalize(prev_sigs))
    curr = sorted(_normalize(curr_sigs))
    if curr == prev and curr != [] and blockers not in (0, "unknown"):
        return "escalate:no-progress"

    # Row 4 — R2 advances to a fix step (no-progress already handled above).
    if round == 2:
        return "fix"

    # Rows 5–7 — R3 terminal outcomes.
    if blockers == "unknown":
        return "escalate:no-verdict"
    if blockers == 0:
        # ci not green here (row 1 handled the green case).
        return "escalate:ci-red"
    return "escalate:cap-reached"
