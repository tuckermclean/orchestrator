"""Unit tests for decide_intake — SPEC §8.11 / TESTING.md §2.1."""

from __future__ import annotations

from src.decisions.intake import decide_intake
from src.domain.types import Issue, IssueRef, RepoRef

_REPO = RepoRef(owner="acme", name="repo")


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
# Empty allowlist → gate disabled → always admit
# ---------------------------------------------------------------------------


def test_intake_gate_disabled() -> None:
    """Empty allowlist → 'admit' regardless of author."""
    assert decide_intake(_make_issue("anyone"), allowlist=[]) == "admit"


def test_intake_gate_disabled_empty_string_author() -> None:
    """Empty allowlist + empty-string author → 'admit'."""
    assert decide_intake(_make_issue(""), allowlist=[]) == "admit"


# ---------------------------------------------------------------------------
# Non-empty allowlist, author IN list → admit
# ---------------------------------------------------------------------------


def test_intake_allowlisted_first() -> None:
    """First entry in allowlist → 'admit'."""
    assert decide_intake(_make_issue("alice"), allowlist=["alice", "bob"]) == "admit"


def test_intake_allowlisted_second() -> None:
    """Second entry in allowlist → 'admit'."""
    assert decide_intake(_make_issue("bob"), allowlist=["alice", "bob"]) == "admit"


def test_intake_single_match() -> None:
    """Single-entry allowlist, author matches → 'admit'."""
    assert decide_intake(_make_issue("solo"), allowlist=["solo"]) == "admit"


# ---------------------------------------------------------------------------
# Non-empty allowlist, author NOT in list → queue
# ---------------------------------------------------------------------------


def test_intake_unlisted() -> None:
    """Author not in allowlist → 'queue'."""
    assert decide_intake(_make_issue("eve"), allowlist=["alice", "bob"]) == "queue"


def test_intake_single_nomatch() -> None:
    """Single-entry allowlist, author doesn't match → 'queue'."""
    assert decide_intake(_make_issue("other"), allowlist=["solo"]) == "queue"


# ---------------------------------------------------------------------------
# Case sensitivity — SPEC §8.11: exact string equality, no case-folding
# ---------------------------------------------------------------------------


def test_intake_case_sensitive_upper_in_list() -> None:
    """'Alice' in list but 'alice' submitted → 'queue' (no case-folding)."""
    assert decide_intake(_make_issue("alice"), allowlist=["Alice"]) == "queue"


def test_intake_case_sensitive_lower_in_list() -> None:
    """'alice' in list but 'Alice' submitted → 'queue' (no case-folding)."""
    assert decide_intake(_make_issue("Alice"), allowlist=["alice"]) == "queue"


# ---------------------------------------------------------------------------
# Return value type sanity
# ---------------------------------------------------------------------------


def test_intake_returns_string() -> None:
    """decide_intake always returns a str."""
    result = decide_intake(_make_issue("x"), allowlist=["x"])
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Purity: calling twice with same args gives same result
# ---------------------------------------------------------------------------


def test_intake_pure_idempotent() -> None:
    """Same inputs always produce same output (pure function, I4)."""
    issue = _make_issue("alice")
    allowlist = ["alice", "bob"]
    r1 = decide_intake(issue, allowlist)
    r2 = decide_intake(issue, allowlist)
    assert r1 == r2 == "admit"
