"""resolve_blockers — async; reads verdict file or comment footer (SPEC §8.2)."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal

from src.domain.types import SENTINEL_SIGNATURE, PRRef
from src.ports.base import ForgePort

_VERDICT_PATH = ".converge-verdict.json"

# Extracts the blocker count from a reviewer comment footer: "🔴 <N> blockers".
_FOOTER_RE = re.compile(r"🔴\s*(\d+)\s+blockers")


def parse_comment_blockers(body: str) -> int | None:
    """Extract the 🔴 <N> blockers count from a comment footer, or None if absent."""
    match = _FOOTER_RE.search(body)
    if match is None:
        return None
    return int(match.group(1))


def _is_sentinel(verdict: dict[str, object]) -> bool:
    sigs = verdict.get("blocker_signatures")
    return isinstance(sigs, list) and SENTINEL_SIGNATURE in sigs


async def resolve_blockers(
    forge: ForgePort,
    pr_ref: PRRef,
    round: int,
    round_started: datetime | None,
) -> int | Literal["unknown"]:
    """Resolve the effective blocker count for one converge round (SPEC §8.2).

    Falls back from the verdict JSON to the reviewer's comment footer when the sentinel
    survived. Returns the blocker count or "unknown" when nothing resolves.
    """
    raw = await forge.get_file_contents(pr_ref, _VERDICT_PATH)
    verdict: dict[str, object] | None = None
    if raw is not None:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            verdict = parsed

    # Row 1 — file present and not sentinel: take .blockers from JSON.
    if verdict is not None and not _is_sentinel(verdict):
        count = verdict.get("blockers")
        if isinstance(count, int) and not isinstance(count, bool):
            return count
        return "unknown"

    # Rows 2–3 — sentinel or file absent: fall back to the most-recent comment footer.
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
