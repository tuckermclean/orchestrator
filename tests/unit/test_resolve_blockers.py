"""Unit tests for resolve_blockers — SPEC §8.2 / TESTING.md §2."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.decisions.resolve_blockers import parse_comment_blockers, resolve_blockers
from src.domain.types import Comment, PRRef, RepoRef, Verdict
from src.ports.fakes import FakeForgePort

_REPO = RepoRef(owner="acme", name="service")
_PR = PRRef(repo=_REPO, number=5)


def _seed_verdict(forge: FakeForgePort, blockers: int, sigs: list[str]) -> None:
    forge.seed_pr(_PR)
    verdict = Verdict(blockers=blockers, suggestions=0, nits=[], blocker_signatures=sigs)
    forge.seed_file(_PR, ".converge-verdict.json", verdict.model_dump_json().encode())


def _footer(n: int) -> str:
    return f"## Converge Review\n🔴 {n} blockers | 🟡 0 suggestions | 💬 0 nits"


# ---------------------------------------------------------------------------
# Row 1 — file present and not sentinel → .blockers from JSON
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.2", "row-1-file-present-not-sentinel")
async def test_resolve_blockers_row1_from_json() -> None:
    forge = FakeForgePort()
    _seed_verdict(forge, 3, ["a:x", "b:y", "c:z"])
    assert await resolve_blockers(forge, _PR, 1, None) == 3


@pytest.mark.covers("§8.2", "row-1-file-present-not-sentinel")
async def test_resolve_blockers_row1_zero() -> None:
    forge = FakeForgePort()
    _seed_verdict(forge, 0, [])
    assert await resolve_blockers(forge, _PR, 1, None) == 0


@pytest.mark.covers("§8.2", "row-1-file-present-not-sentinel")
async def test_resolve_blockers_row1_non_numeric_blockers_unknown() -> None:
    """File present, not sentinel, but .blockers missing/non-numeric → unknown."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    bad = json.dumps({"blocker_signatures": ["a:x"], "suggestions": 0, "nits": []})
    forge.seed_file(_PR, ".converge-verdict.json", bad.encode())
    assert await resolve_blockers(forge, _PR, 1, None) == "unknown"


# ---------------------------------------------------------------------------
# Row 2 — sentinel/absent + round_started → most-recent footer after round_started
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.2", "row-2-sentinel-round-started")
async def test_resolve_blockers_row2_footer_in_round() -> None:
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    sentinel = Verdict(
        blockers=1, suggestions=0, nits=[], blocker_signatures=["verdict-file-not-written"]
    )
    forge.seed_file(_PR, ".converge-verdict.json", sentinel.model_dump_json().encode())
    round_started = datetime.now(tz=UTC)
    await forge.post_comment(_PR, _footer(2))
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
# Row 3 — sentinel/absent + round_started is None → most-recent footer any age
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.2", "row-3-sentinel-no-round-started")
async def test_resolve_blockers_row3_footer_any_age() -> None:
    forge = FakeForgePort()
    forge.seed_pr(_PR)  # no verdict file at all
    await forge.post_comment(_PR, _footer(4))
    assert await resolve_blockers(forge, _PR, 1, None) == 4


# ---------------------------------------------------------------------------
# Row 4 — no footer resolved → unknown
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.2", "row-4-no-footer-unknown")
async def test_resolve_blockers_row4_no_footer_unknown() -> None:
    forge = FakeForgePort()
    forge.seed_pr(_PR)  # no verdict file, no comments
    assert await resolve_blockers(forge, _PR, 1, None) == "unknown"


@pytest.mark.covers("§8.2", "row-4-no-footer-unknown")
async def test_resolve_blockers_row4_comment_without_footer() -> None:
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    await forge.post_comment(_PR, "just a chat comment, no footer")
    assert await resolve_blockers(forge, _PR, 1, None) == "unknown"


# ---------------------------------------------------------------------------
# parse_comment_blockers helper
# ---------------------------------------------------------------------------


def test_parse_comment_blockers_extracts_count() -> None:
    assert parse_comment_blockers(_footer(7)) == 7


def test_parse_comment_blockers_none_when_absent() -> None:
    assert parse_comment_blockers("no footer here") is None


# ---------------------------------------------------------------------------
# Row 1 — non-numeric .blockers values (null / string / false) → unknown (§2.3 floor)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.2", "row-1-file-present-not-sentinel")
async def test_resolve_blockers_json_blockers_null() -> None:
    """Non-sentinel verdict with `"blockers": null` → unknown (non-numeric)."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    bad = json.dumps(
        {"blockers": None, "suggestions": 0, "nits": [], "blocker_signatures": ["a:x"]}
    )
    forge.seed_file(_PR, ".converge-verdict.json", bad.encode())
    assert await resolve_blockers(forge, _PR, 1, None) == "unknown"


@pytest.mark.covers("§8.2", "row-1-file-present-not-sentinel")
async def test_resolve_blockers_json_blockers_string() -> None:
    """Non-sentinel verdict with `"blockers": "bad"` → unknown (non-numeric)."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    bad = json.dumps(
        {"blockers": "bad", "suggestions": 0, "nits": [], "blocker_signatures": ["a:x"]}
    )
    forge.seed_file(_PR, ".converge-verdict.json", bad.encode())
    assert await resolve_blockers(forge, _PR, 1, None) == "unknown"


@pytest.mark.covers("§8.2", "row-1-file-present-not-sentinel")
async def test_resolve_blockers_json_blockers_false() -> None:
    """Non-sentinel verdict with `"blockers": false` → unknown (bool is not a real int)."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    bad = json.dumps(
        {"blockers": False, "suggestions": 0, "nits": [], "blocker_signatures": ["a:x"]}
    )
    forge.seed_file(_PR, ".converge-verdict.json", bad.encode())
    assert await resolve_blockers(forge, _PR, 1, None) == "unknown"


@pytest.mark.covers("§8.2", "row-3-sentinel-no-round-started")
async def test_resolve_blockers_sentinel_unscoped_fallback() -> None:
    """Sentinel + only a stale footer + round_started=None → stale footer count (row 3)."""
    forge = FakeForgePort()
    forge.seed_pr(_PR)
    sentinel = Verdict(
        blockers=1, suggestions=0, nits=[], blocker_signatures=["verdict-file-not-written"]
    )
    forge.seed_file(_PR, ".converge-verdict.json", sentinel.model_dump_json().encode())
    stale = datetime.now(tz=UTC) - timedelta(hours=2)
    forge._comments.setdefault(forge._entity_key(_PR), []).append(
        Comment(id="1", body=_footer(1), created_at=stale, author="reviewer")
    )
    # No round scoping → the stale footer is honoured regardless of age.
    assert await resolve_blockers(forge, _PR, 1, None) == 1
