"""triager_reconcile — pure helpers for the triager gate and recommendation parsing.

The triager is the **content gate** for the intake pipeline (SPEC §10.4).  After the
triager posts its structured comment, the control plane reads its machine-readable
``<!-- triager-verdict: ... -->`` marker and decides whether to apply ``agent-work``
(actionable → orchestrator fires) or leave the issue in ``awaiting-promotion`` (not
actionable → human review required).

``parse_triager_verdict``  — reads the machine-readable gate verdict.
``parse_triager_recommendation`` — reads the human-readable recommendation field
    (kept for audit / divergence-surfacing; not used for gating).
``is_triager_comment`` — fast check that a comment body is a triager summary.

See SPEC §10.4 and ``agents/triager.md`` for the full comment format.
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

# Regex to extract the machine-readable verdict from the HTML comment marker.
# Matches: <!-- triager-verdict: actionable --> or <!-- triager-verdict: not-actionable -->
# The marker is the last line of the triager comment (agents/triager.md §What You Produce).
_VERDICT_RE = re.compile(
    r"<!--\s*triager-verdict:\s*(actionable|not-actionable)\s*-->",
    re.IGNORECASE,
)

# Canonical recommended-action values from agents/triager.md
TRIAGER_REC_ADMIT = "admit for autonomous dispatch"
TRIAGER_REC_QUEUE = "queue for human review"
TRIAGER_REC_CLOSE = "close as duplicate/out-of-scope"

# Machine-readable verdict constants (agents/triager.md §What You Produce)
TRIAGER_VERDICT_ACTIONABLE = "actionable"
TRIAGER_VERDICT_NOT_ACTIONABLE = "not-actionable"


def parse_triager_verdict(body: str) -> str | None:
    """Extract the machine-readable gate verdict from a triager comment body.

    The triager embeds a ``<!-- triager-verdict: actionable|not-actionable -->``
    marker as the last line of its structured comment (agents/triager.md).  This
    function returns that verdict string (lowercased) or ``None`` when:
      - the comment does not contain the ``## Triage Summary`` header, or
      - the verdict marker is absent or malformed.

    The gate falls back safely to ``None`` when the verdict is absent — the
    control plane treats ``None`` as "no verdict available" and leaves the issue
    in ``[LABEL_TRIAGE]`` only (awaiting human), preserving I1.

    Pure, synchronous, no I/O.

    >>> parse_triager_verdict("## Triage Summary\\n<!-- triager-verdict: actionable -->")
    'actionable'
    >>> parse_triager_verdict("## Triage Summary\\n<!-- triager-verdict: not-actionable -->")
    'not-actionable'
    >>> parse_triager_verdict(  # doctest: +ELLIPSIS
    ...     "## Triage Summary\\n**Recommended action**: admit for autonomous dispatch"
    ... ) is None
    True
    >>> parse_triager_verdict("some other comment") is None
    True
    """
    if _TRIAGE_HEADER not in body:
        return None
    m = _VERDICT_RE.search(body)
    if m is None:
        return None
    return m.group(1).strip().lower()


def parse_triager_recommendation(body: str) -> str | None:
    """Extract the triager's **Recommended action** from a triager comment body.

    Returns the recommendation string (lowercased, stripped) if the comment
    looks like a triager triage summary and contains a Recommended action line,
    or ``None`` if the comment is not a triager comment or the field is absent.

    Used for audit / divergence-surfacing (human-readable).  The gate itself
    uses ``parse_triager_verdict`` (machine-readable).

    Pure, synchronous, no I/O.

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
