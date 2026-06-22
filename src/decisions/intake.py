"""decide_intake — pure, synchronous (AGENTS.md §5)."""

from __future__ import annotations

from src.domain.types import Issue


def decide_intake(issue: Issue, allowlist: list[str], owner: str) -> str:
    """Return 'admit' or 'queue'.

    Truth table (SPEC §8.11, default-deny / fail-closed — issue #48):
      allowlist empty, author == owner                  → 'admit'  (owner-only default)
      allowlist empty, author != owner                  → 'queue'  (fail-closed)
      allowlist non-empty, author in allowlist          → 'admit'
      allowlist non-empty, author == owner (implicit)   → 'admit'
      allowlist non-empty, author not in list/owner     → 'queue'

    An empty allowlist is the SAFE DEFAULT: it admits ONLY the repo owner and
    queues everyone else.  This is fail-closed / default-deny (not gate-disabled).

    Pure, synchronous, no forge calls, no side effects (I4).
    Exact string equality — no case-folding, no fuzzy match.
    """
    # Owner is always admitted regardless of allowlist content.
    if issue.author == owner:
        return "admit"
    if allowlist and issue.author in allowlist:
        return "admit"
    return "queue"
