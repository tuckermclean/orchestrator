"""resolve_blockers — async; reads verdict from run result or comment footer (SPEC §8.2).

The verdict channel is now the reviewer's structured output (a fenced JSON block in
the run's final message), captured by the harness and read by the engine via
``harness.get_run_verdict(reviewer_handle)``.  This avoids committing scratch state
to the PR branch (SPEC §5 anti-pattern fix).

Fallback order (SPEC §8.2):
  Row 1 — verdict passed in is not None and not sentinel → ``.blockers`` from Verdict.
  Rows 2–3 — verdict is None (reviewer crashed / omitted output) → most-recent comment footer.
  Row 4 — no footer resolved → ``"unknown"``.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from src.domain.types import SENTINEL_SIGNATURE, PRRef, Verdict
from src.ports.base import ForgePort

# Extracts the blocker count from a reviewer comment footer: "🔴 <N> blockers".
_FOOTER_RE = re.compile(r"🔴\s*(\d+)\s+blockers")


def parse_comment_blockers(body: str) -> int | None:
    """Extract the 🔴 <N> blockers count from a comment footer, or None if absent."""
    match = _FOOTER_RE.search(body)
    if match is None:
        return None
    return int(match.group(1))


def _is_sentinel(verdict: Verdict) -> bool:
    return verdict.blocker_signatures == [SENTINEL_SIGNATURE]


async def resolve_blockers(
    forge: ForgePort,
    pr_ref: PRRef,
    round: int,
    round_started: datetime | None,
    verdict: Verdict | None = None,
) -> int | Literal["unknown"]:
    """Resolve the effective blocker count for one converge round (SPEC §8.2).

    The primary source is the structured ``verdict`` extracted from the reviewer
    run's output by the harness (``harness.get_run_verdict``).  When ``verdict``
    is ``None`` (reviewer crashed or omitted structured output), falls back to
    the comment-footer heuristic so a human-readable review comment can still
    drive the decision.

    Priority table (SPEC §8.2):
      Row 1 — verdict present and not sentinel → ``.blockers`` from Verdict.
      Rows 2–3 — verdict absent; ``round_started`` present/absent → most-recent footer.
      Row 4 — no footer resolved → ``"unknown"``.
    """
    # Row 1 — verdict present and not sentinel: take .blockers from Verdict.
    if verdict is not None and not _is_sentinel(verdict):
        raw_count: object = verdict.blockers
        if isinstance(raw_count, int) and not isinstance(raw_count, bool):
            return raw_count
        return "unknown"

    # Rows 2–3 — verdict absent (crash): fall back to the most-recent comment footer.
    comments = await forge.list_comments(pr_ref)
    # Row 2 scopes to the current round when round_started is provided.
    if round_started is not None:
        comments = [c for c in comments if c.created_at >= round_started]
    for comment in sorted(comments, key=lambda c: c.created_at, reverse=True):
        count = parse_comment_blockers(comment.body)
        if count is not None:
            return count

    # Row 4 — no footer resolved.
    return "unknown"
