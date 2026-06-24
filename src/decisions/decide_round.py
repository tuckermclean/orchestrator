"""decide_round decision function — pure, synchronous (SPEC §8.3).

Updated for the 3-tier adjudication model (SPEC §5/§8.3/§251):
- Reviewers (all rounds R1/R2/R3) run on DEFAULT_SWARM_MODEL (Sonnet).
- The ``adjudicate`` token routes to the adjudication phase (Haiku nitpicker →
  Opus adjudicator) rather than directly to APPROVED.
- Spotless early-exit: ``blockers == 0 AND suggestions == 0 AND ci_green`` at
  any round → ``adjudicate`` (skip remaining fix rounds).
- R2/R3 with 0 blockers + CI green (any suggestions) → ``adjudicate``: the R2/R3
  fixer is blockers-only and would be a no-op; residual suggestions go to the
  nitpicker in the adjudication phase.
"""

from __future__ import annotations

from typing import Literal

from src.domain.types import SENTINEL_SIGNATURE

ConvergeToken = Literal[
    "adjudicate",
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
    suggestions: int = 0,
) -> ConvergeToken:
    """Decide the convergence action for one round (SPEC §8.3 truth table, amended).

    Priority table evaluated top-to-bottom; first match fires.  Both signature lists are
    sentinel-normalized and sorted lexicographically before the no-progress comparison so
    detection is stable regardless of reviewer output order.

    ``round`` is ``Literal[1, 2, 3]``; a value outside that set is a ``TypeError``
    (SPEC §8.3).  ``ci_green`` must be a ``bool`` and ``blockers`` must be an ``int`` or
    the literal ``"unknown"``; wrong types raise ``TypeError``.

    ``suggestions`` is the count of reviewer suggestions (non-blocking findings).
    Defaults to 0 for callers that pass a ``Verdict`` with no suggestions field.

    **Amended truth table (SPEC §5/§8.3 — 3-tier model):**

    Row 1:  ``blockers==0 AND ci_green AND suggestions==0`` (any round) → ``adjudicate``
    Row 1b: ``round>=2 AND blockers==0 AND ci_green`` (suggestions may remain) → ``adjudicate``
            (R2/R3 fixer is blockers-only; dispatching it with 0 blockers is a no-op;
            residual suggestions go to the nitpicker in the adjudication phase)
    Row 2:  ``round==1`` → ``fix``
    Row 3:  ``curr_sigs==prev_sigs AND curr_sigs!=[] AND blockers not in (0,"unknown")``
            → ``escalate:no-progress``
    Row 4:  ``round==2`` → ``fix``
    Row 5:  ``round==3 AND blockers=="unknown"`` → ``escalate:no-verdict``
    Row 6:  ``round==3 AND blockers==0`` (ci not green) → ``escalate:ci-red``
    Row 7:  ``round==3`` else (blockers ≥ 1) → ``escalate:cap-reached``

    Key: ``"unknown"`` never produces ``adjudicate``. Spotless (row 1) takes priority
    over all other rows. Row 1b catches R2/R3 with 0 blockers regardless of suggestions —
    this prevents a no-op fixer run when only suggestions remain at R2.
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

    # Row 1 — spotless: 0 blockers, 0 suggestions, CI green (any round → early-exit).
    # "unknown" never matches (int 0 only).
    if blockers == 0 and ci_green and suggestions == 0:
        return "adjudicate"

    # Row 1b — R2/R3 with 0 blockers + CI green (suggestions may remain).
    # The R2/R3 fixer addresses blockers only; dispatching it with 0 blockers is a no-op.
    # Residual suggestions are handed to the nitpicker in the adjudication phase.
    if round >= 2 and blockers == 0 and ci_green:
        return "adjudicate"

    # Row 2 — R1 always advances to a fix step (when not adjudicate).
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

    # Rows 5–7 — R3 terminal outcomes (blockers remain or unknown).
    if blockers == "unknown":
        return "escalate:no-verdict"
    if blockers == 0:
        # ci not green here (rows 1/1b handled the green case).
        return "escalate:ci-red"
    return "escalate:cap-reached"
