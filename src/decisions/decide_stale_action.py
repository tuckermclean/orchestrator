"""decide_stale_action — pure, synchronous (SPEC §8.5).

Decides recovery action for a stale PR carrying ``agent:implementing``
(draft or non-draft; widened RC-1 scope per §4).
"""

from __future__ import annotations

from typing import Literal

from src.domain.types import RECONCILER_STALE_REDISPATCH_CAP

StaleAction = Literal[
    "escalate",
    "trigger-ci",
    "redispatch",
    "needs-human",
    "mark-ready",
    "mark-ready-and-converge",
]


def decide_stale_action(
    redispatch_count: int,
    ci_runs: int,
    has_converge: bool,
    failing_count: int,
    has_issue: bool,
    has_diff: bool,
    is_draft: bool,
) -> StaleAction:
    """Return the recovery action for a stale implementing PR (SPEC §8.5 truth table).

    Priority order (first match wins):

    | # | Condition                                              | Output                |
    |---|--------------------------------------------------------|-----------------------|
    | 1 | redispatch_count >= RECONCILER_STALE_REDISPATCH_CAP    | escalate              |
    | 2 | ci_runs == 0                                           | trigger-ci            |
    | 2.5a | not has_diff AND is_draft AND has_issue              | redispatch            |
    | 2.5b | not has_diff AND is_draft AND not has_issue          | needs-human           |
    | 2.5c | not has_diff AND not is_draft                       | needs-human           |
    | 3 | has_converge                                           | mark-ready            |
    | 4 | failing_count == 0                                     | mark-ready-and-converge |
    | 5 | has_issue                                              | redispatch            |
    | 6 | else (failing, no issue)                               | needs-human           |
    """
    # Row 1 — cap reached: escalate (E8/E9) regardless of all other conditions
    if redispatch_count >= RECONCILER_STALE_REDISPATCH_CAP:
        return "escalate"

    # Row 2 — no CI runs yet: trigger CI so we can assess state
    if ci_runs == 0:
        return "trigger-ci"

    # Rows 2.5a/b/c — zero-diff PR (D4)
    if not has_diff:
        if is_draft and has_issue:
            # 2.5a: crash-draft with closing issue → re-dispatch bounded by cap
            return "redispatch"
        if is_draft and not has_issue:
            # 2.5b: crash-draft with no issue → escalate to human (E9)
            return "needs-human"
        # 2.5c: non-draft 0-diff always escalates (D4: finished-empty)
        return "needs-human"

    # Row 3 — converge label present: PR needs to be marked ready (B8a window)
    if has_converge:
        return "mark-ready"

    # Row 4 — CI clean, no converge label: mark ready and trigger converge in one step
    if failing_count == 0:
        return "mark-ready-and-converge"

    # Row 5 — CI failing but has a closing issue: re-dispatch the implementer
    if has_issue:
        return "redispatch"

    # Row 6 — CI failing and no issue to re-dispatch against → human needed (E9)
    return "needs-human"
