"""decide_specialists decision function — pure, synchronous (SPEC §8.12)."""

from __future__ import annotations

import pathspec

from src.domain.types import (
    CONVERGE_REVIEW_BASE,
    PARALLEL_SPECIALIST_CAP,
    SPECIALIST_ROUTING,
)


def _matches(pattern: str, paths: list[str]) -> bool:
    """True if any path matches the glob using gitignore/pathspec semantics (SPEC §7)."""
    spec = pathspec.PathSpec.from_lines("gitignore", [pattern])
    return any(spec.match_file(path) for path in paths)


def decide_specialists(changed_paths: list[str], round: int) -> list[str]:
    """Select the specialist allow-set for a converge round.

    Base set (always included, ordered) plus routing additions in SPECIALIST_ROUTING
    definition order, capped at PARALLEL_SPECIALIST_CAP with the base always retained.
    Deterministic: same inputs → same output. `round` is reserved for future per-round
    suppression and is currently unused.
    """
    base = list(CONVERGE_REVIEW_BASE)
    extras: list[str] = []
    for entry in SPECIALIST_ROUTING:
        if any(_matches(pattern, changed_paths) for pattern in entry.patterns):
            for ref in entry.agent_refs:
                if ref not in base and ref not in extras:
                    extras.append(ref)

    cap = PARALLEL_SPECIALIST_CAP
    assert len(base) <= cap, "CONVERGE_REVIEW_BASE exceeds PARALLEL_SPECIALIST_CAP"
    return base + extras[: cap - len(base)]
