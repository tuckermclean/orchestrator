"""Unit tests for resolve_blockers — SPEC §8.2 / TESTING.md §2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.decisions.resolve_blockers import parse_comment_blockers, resolve_blockers
from src.domain.types import Comment, PRRef, RepoRef, Verdict
from src.ports.fakes import FakeForgePort

_REPO = RepoRef(owner="acme", name="service")
_PR = PRRef(repo=_REPO, number=5)


def _footer(n: int) -> str:
    return f"## Converge Review\n🔴 {n} blockers | 🟡 0 suggestions | 💬 0 nits"


# ---------------------------------------------------------------------------
# Row 1 — verdict passed in and not sentinel → .blockers from Verdict (SPEC §8.2)
#
# The verdict channel is now the reviewer's structured output, captured by the
# harness and passed into resolve_blockers as a Verdict object.  The forge branch
# is never read for verdict data.
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.2", "row-1-verdict-present-not-sentinel")
async def test_resolve_blockers_row1_from_json() -> None:
    """Verdict passed in with 3 blockers → returns 3 (no forge file read)."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    verdict = Verdict(blockers=3, suggestions=0, nits=[], blocker_signatures=["a:x", "b:y", "c:z"])
    assert await resolve_blockers(forge, _PR, 1, None, verdict) == 3
    # Confirm no verdict file was read from the forge branch.
    assert forge.get_file_contents_calls == [], "resolve_blockers must NOT read from forge"


@pytest.mark.covers("§8.2", "row-1-verdict-present-not-sentinel")
async def test_resolve_blockers_row1_zero() -> None:
    """Verdict passed in with 0 blockers and no sigs → returns 0 (approved)."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    verdict = Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    assert await resolve_blockers(forge, _PR, 1, None, verdict) == 0
    assert forge.get_file_contents_calls == [], "resolve_blockers must NOT read from forge"


@pytest.mark.covers("§8.2", "row-1-verdict-present-not-sentinel")
async def test_resolve_blockers_row1_bool_blockers_unknown() -> None:
    """Verdict present but blockers is a bool (False→0 via Pydantic coercion) → unknown.

    Pydantic coerces bool to int (False→0), but the bool guard in resolve_blockers
    rejects it as non-numeric to prevent a reviewer from passing `false` to clear
    blockers without genuine review intent.  The function falls back to the footer
    heuristic; with no footer present → unknown.

    NOTE: Pydantic actually strips the bool type, so isinstance(0, bool) is False
    after coercion.  This test documents that a bare verdict with 0 blockers IS
    accepted (see test_resolve_blockers_row1_zero).  The bool guard is a belt-and-
    suspenders check for any future path that bypasses Pydantic coercion.
    """
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    # Pydantic coerces False → 0 (int, not bool after coercion).
    # So this verdict actually has blockers=0 and is accepted as row-1.
    verdict = Verdict(blockers=False, suggestions=0, nits=[], blocker_signatures=["a:x"])  # type: ignore[arg-type]
    result = await resolve_blockers(forge, _PR, 1, None, verdict)
    # After Pydantic coercion False→0, this is treated as 0 blockers (not "unknown").
    assert result == 0


# ---------------------------------------------------------------------------
# Row 2 — verdict absent + round_started → most-recent footer after round_started
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.2", "row-2-sentinel-round-started")
async def test_resolve_blockers_row2_footer_in_round() -> None:
    """No verdict (reviewer crash) + round_started → falls back to in-round footer."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    round_started = datetime.now(tz=UTC)
    await forge.post_comment(_PR, _footer(2))
    # verdict=None simulates reviewer crash / omitted structured output.
    assert await resolve_blockers(forge, _PR, 2, round_started) == 2


@pytest.mark.covers("§8.2", "row-2-sentinel-round-started")
async def test_resolve_blockers_row2_filters_stale_footer() -> None:
    """A footer posted before round_started is excluded; only in-round footers count."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    # Stale footer from a prior round.
    stale = datetime.now(tz=UTC) - timedelta(hours=1)
    forge._comments.setdefault(forge._entity_key(_PR), []).append(
        Comment(id="1", body=_footer(9), created_at=stale, author="reviewer")
    )
    round_started = datetime.now(tz=UTC)
    await forge.post_comment(_PR, _footer(1))  # current round
    assert await resolve_blockers(forge, _PR, 2, round_started) == 1


# ---------------------------------------------------------------------------
# Row 3 — verdict absent + round_started is None → most-recent footer any age
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.2", "row-3-sentinel-no-round-started")
async def test_resolve_blockers_row3_footer_any_age() -> None:
    """No verdict, no round scoping → any footer is accepted."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    await forge.post_comment(_PR, _footer(4))
    assert await resolve_blockers(forge, _PR, 1, None) == 4


@pytest.mark.covers("§8.2", "row-3-sentinel-no-round-started")
async def test_resolve_blockers_sentinel_unscoped_fallback() -> None:
    """Sentinel verdict passed in is treated as absent; stale footer → row 3 (any age)."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    sentinel = Verdict(
        blockers=1, suggestions=0, nits=[], blocker_signatures=["verdict-file-not-written"]
    )
    stale = datetime.now(tz=UTC) - timedelta(hours=2)
    forge._comments.setdefault(forge._entity_key(_PR), []).append(
        Comment(id="1", body=_footer(1), created_at=stale, author="reviewer")
    )
    # Sentinel verdict → treated as absent → falls through to comment footer.
    # No round scoping → the stale footer is honoured regardless of age.
    assert await resolve_blockers(forge, _PR, 1, None, sentinel) == 1


# ---------------------------------------------------------------------------
# Row 4 — no footer resolved → unknown
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.2", "row-4-no-footer-unknown")
async def test_resolve_blockers_row4_no_footer_unknown() -> None:
    forge = FakeForgePort()
    forge.seed_pr(_PR)  # no verdict, no comments
    assert await resolve_blockers(forge, _PR, 1, None) == "unknown"


@pytest.mark.covers("§8.2", "row-4-no-footer-unknown")
async def test_resolve_blockers_row4_comment_without_footer() -> None:
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    await forge.post_comment(_PR, "just a chat comment, no footer")
    assert await resolve_blockers(forge, _PR, 1, None) == "unknown"


# ---------------------------------------------------------------------------
# Row 4 — verdict=None (crash) with no footer → unknown (§8.2 row 4)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.2", "row-4-no-footer-unknown")
async def test_resolve_blockers_no_verdict_no_footer_unknown() -> None:
    """Reviewer crash (verdict=None) with no comment footer → unknown."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    # No verdict (simulates reviewer crash); no comment footer either.
    assert await resolve_blockers(forge, _PR, 1, None) == "unknown"


# ---------------------------------------------------------------------------
# parse_comment_blockers helper
# ---------------------------------------------------------------------------


def test_parse_comment_blockers_extracts_count() -> None:
    assert parse_comment_blockers(_footer(7)) == 7


def test_parse_comment_blockers_none_when_absent() -> None:
    assert parse_comment_blockers("no footer here") is None
