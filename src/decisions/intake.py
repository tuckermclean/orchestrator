"""decide_intake — pure, synchronous (AGENTS.md §5)."""

from __future__ import annotations

from src.domain.types import Issue


def decide_intake(issue: Issue, allowlist: list[str]) -> str:
    """Return 'admit' or 'queue'.

    Truth table (SPEC §8.11):
      allowlist empty                           → 'admit' (gate disabled)
      allowlist non-empty, author in allowlist  → 'admit'
      allowlist non-empty, author not in list   → 'queue'

    Pure, synchronous, no forge calls, no side effects (I4).
    Exact string equality — no case-folding, no fuzzy match.
    """
    if not allowlist:
        return "admit"
    if issue.author in allowlist:
        return "admit"
    return "queue"
