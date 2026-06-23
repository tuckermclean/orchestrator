"""triager_reconcile — pure helpers for detecting intake/triager recommendation divergence.

When ``decide_intake`` auto-admits an issue (trust axis: owner or allowlist) but the
triager's structured comment recommends "queue for human review" (scope/risk axis), the
two assessments diverge.  This module provides a pure function to parse the triager's
recommendation from its comment body, and a sentinel constant for detecting the divergence.

See SPEC §10.4 (reconciliation step) and ``agents/triager.md`` for the triager comment
format.  The divergence is surfaced by ``IntakeEngine.reconcile_triager_divergence``.
"""

from __future__ import annotations

import re

# Header line that identifies a comment as a triager triage summary.
_TRIAGE_HEADER = "## Triage Summary"

# Regex to extract the Recommended action line from the structured triager comment.
# Matches: **Recommended action**: <value>
_REC_ACTION_RE = re.compile(
    r"^\*\*Recommended action\*\*:\s*(.+)$",
    re.MULTILINE,
)

# Canonical recommended-action values from agents/triager.md
TRIAGER_REC_ADMIT = "admit for autonomous dispatch"
TRIAGER_REC_QUEUE = "queue for human review"
TRIAGER_REC_CLOSE = "close as duplicate/out-of-scope"


def parse_triager_recommendation(body: str) -> str | None:
    """Extract the triager's **Recommended action** from a triager comment body.

    Returns the recommendation string (lowercased, stripped) if the comment
    looks like a triager triage summary and contains a Recommended action line,
    or ``None`` if the comment is not a triager comment or the field is absent.

    Pure, synchronous, no I/O.  Used by ``IntakeEngine.reconcile_triager_divergence``
    and by tests.

    >>> body = "## Triage Summary\\n**Recommended action**: queue for human review"
    >>> parse_triager_recommendation(body)
    'queue for human review'
    >>> parse_triager_recommendation("some other comment") is None
    True
    """
    if _TRIAGE_HEADER not in body:
        return None
    m = _REC_ACTION_RE.search(body)
    if m is None:
        return None
    return m.group(1).strip().lower()


def is_triager_comment(body: str) -> bool:
    """Return True if ``body`` looks like a triager triage summary comment.

    Pure, synchronous.  A comment is a triager comment iff it contains
    the ``## Triage Summary`` header (verbatim).
    """
    return _TRIAGE_HEADER in body
