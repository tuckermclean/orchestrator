"""Unit tests for decide_intake — SPEC §8.11 / TESTING.md §2.1.

Issue #48 — default-deny: empty allowlist admits ONLY the repo owner (fail-closed).
"""

from __future__ import annotations

import pytest

from src.decisions.intake import decide_intake
from src.domain.types import Issue, IssueRef, RepoRef

_REPO = RepoRef(owner="acme", name="repo")
_OWNER = "acme"


def _make_issue(author: str, number: int = 1) -> Issue:
    return Issue(
        ref=IssueRef(repo=_REPO, number=number),
        title="Test issue",
        body="Test body",
        labels=[],
        closed=False,
        author=author,
    )


# ---------------------------------------------------------------------------
# Empty allowlist + owner → admit  (default-deny / fail-closed — issue #48)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.11", "row-empty-allowlist-owner-admit")
def test_intake_empty_allowlist_owner_admitted() -> None:
    """Empty allowlist + author == owner → 'admit' (owner-only default-deny)."""
    assert decide_intake(_make_issue(_OWNER), allowlist=[], owner=_OWNER) == "admit"


@pytest.mark.covers("§8.11", "row-empty-allowlist-owner-admit")
def test_intake_empty_allowlist_owner_admitted_matches_repo_owner() -> None:
    """Owner string derived from RepoRef.owner → 'admit'."""
    repo = RepoRef(owner="myorg", name="myrepo")
    issue = Issue(
        ref=IssueRef(repo=repo, number=1),
        title="T",
        body="B",
        labels=[],
        closed=False,
        author="myorg",
    )
    assert decide_intake(issue, allowlist=[], owner="myorg") == "admit"


# ---------------------------------------------------------------------------
# Empty allowlist + non-owner → queue  (fail-closed default-deny)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.11", "row-empty-allowlist-nonowner-queue")
def test_intake_empty_allowlist_nonowner_queued() -> None:
    """Empty allowlist + author != owner → 'queue' (fail-closed, not gate-disabled)."""
    assert decide_intake(_make_issue("anyone"), allowlist=[], owner=_OWNER) == "queue"


@pytest.mark.covers("§8.11", "row-empty-allowlist-nonowner-queue")
def test_intake_empty_allowlist_empty_string_author_queued() -> None:
    """Empty allowlist + empty-string author (not owner) → 'queue'."""
    assert decide_intake(_make_issue(""), allowlist=[], owner=_OWNER) == "queue"


@pytest.mark.covers("§8.11", "row-empty-allowlist-nonowner-queue")
def test_intake_empty_allowlist_random_actor_queued() -> None:
    """Empty allowlist + random actor → 'queue' (fail-closed default-deny)."""
    assert decide_intake(_make_issue("random-actor"), allowlist=[], owner=_OWNER) == "queue"


# ---------------------------------------------------------------------------
# Non-empty allowlist, author IN list → admit
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.11", "row-allowlisted-admit")
def test_intake_allowlisted_first() -> None:
    """First entry in allowlist → 'admit'."""
    assert decide_intake(_make_issue("alice"), allowlist=["alice", "bob"], owner=_OWNER) == "admit"


@pytest.mark.covers("§8.11", "row-allowlisted-admit")
def test_intake_allowlisted_second() -> None:
    """Second entry in allowlist → 'admit'."""
    assert decide_intake(_make_issue("bob"), allowlist=["alice", "bob"], owner=_OWNER) == "admit"


@pytest.mark.covers("§8.11", "row-allowlisted-admit")
def test_intake_single_match() -> None:
    """Single-entry allowlist, author matches → 'admit'."""
    assert decide_intake(_make_issue("solo"), allowlist=["solo"], owner=_OWNER) == "admit"


# ---------------------------------------------------------------------------
# Non-empty allowlist, author == owner → admit (owner implicitly allowed)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.11", "row-nonempty-owner-admit")
def test_intake_owner_always_admitted_nonempty_allowlist() -> None:
    """Owner is admitted even when not explicitly listed in a non-empty allowlist."""
    assert decide_intake(_make_issue(_OWNER), allowlist=["alice", "bob"], owner=_OWNER) == "admit"


@pytest.mark.covers("§8.11", "row-nonempty-owner-admit")
def test_intake_owner_admitted_single_entry_allowlist() -> None:
    """Owner admitted from a single-entry allowlist that does not list them."""
    assert decide_intake(_make_issue(_OWNER), allowlist=["other"], owner=_OWNER) == "admit"


# ---------------------------------------------------------------------------
# Non-empty allowlist, author NOT in list AND not owner → queue
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.11", "row-unlisted-queue")
def test_intake_unlisted() -> None:
    """Author not in allowlist and not owner → 'queue'."""
    assert decide_intake(_make_issue("eve"), allowlist=["alice", "bob"], owner=_OWNER) == "queue"


@pytest.mark.covers("§8.11", "row-unlisted-queue")
def test_intake_single_nomatch() -> None:
    """Single-entry allowlist, author doesn't match and not owner → 'queue'."""
    assert decide_intake(_make_issue("other"), allowlist=["solo"], owner=_OWNER) == "queue"


# ---------------------------------------------------------------------------
# Case sensitivity — SPEC §8.11: exact string equality, no case-folding
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.11", "row-unlisted-queue")
def test_intake_case_sensitive_upper_in_list() -> None:
    """'Alice' in list but 'alice' submitted → 'queue' (no case-folding)."""
    assert decide_intake(_make_issue("alice"), allowlist=["Alice"], owner=_OWNER) == "queue"


@pytest.mark.covers("§8.11", "row-unlisted-queue")
def test_intake_case_sensitive_lower_in_list() -> None:
    """'alice' in list but 'Alice' submitted → 'queue' (no case-folding)."""
    assert decide_intake(_make_issue("Alice"), allowlist=["alice"], owner=_OWNER) == "queue"


# ---------------------------------------------------------------------------
# Return value type sanity
# ---------------------------------------------------------------------------


def test_intake_returns_string() -> None:
    """decide_intake always returns a str."""
    result = decide_intake(_make_issue("x"), allowlist=["x"], owner=_OWNER)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Purity: calling twice with same args gives same result
# ---------------------------------------------------------------------------


def test_intake_pure_idempotent() -> None:
    """Same inputs always produce same output (pure function, I4)."""
    issue = _make_issue("alice")
    allowlist = ["alice", "bob"]
    r1 = decide_intake(issue, allowlist, owner=_OWNER)
    r2 = decide_intake(issue, allowlist, owner=_OWNER)
    assert r1 == r2 == "admit"


def test_intake_empty_string_author_with_allowlist() -> None:
    """Empty-string author with non-empty allowlist → 'queue' (not in list, not owner)."""
    assert decide_intake(_make_issue(""), allowlist=["alice"], owner=_OWNER) == "queue"
