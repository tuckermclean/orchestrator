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
    """
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
