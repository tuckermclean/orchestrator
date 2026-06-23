"""Unit tests for src/decisions/triager_reconcile.py — pure helpers.

Covers: parse_triager_verdict (gate verdict), parse_triager_recommendation,
is_triager_comment.
SPEC §10.4 two-gate flow: verdict = machine-readable gate signal.
"""

from __future__ import annotations

from src.decisions.triager_reconcile import (
    TRIAGER_REC_ADMIT,
    TRIAGER_REC_CLOSE,
    TRIAGER_REC_QUEUE,
    TRIAGER_VERDICT_ACTIONABLE,
    TRIAGER_VERDICT_NOT_ACTIONABLE,
    is_triager_comment,
    parse_triager_recommendation,
    parse_triager_verdict,
)

# ---------------------------------------------------------------------------
# Helpers — canonical triager comment bodies
# ---------------------------------------------------------------------------

_FULL_TRIAGE_QUEUE = (
    "## Triage Summary\n\n"
    "**Author**: @bob (queue — not in allowlist)\n"
    "**Issue type**: feature\n"
    "**Scope estimate**: large\n"
    "**Risk flags**: scope-unclear\n"
    "**Summary**: The issue is ambiguous.\n"
    "**Files likely affected**: unknown\n"
    "**Recommended action**: queue for human review\n"
)

_FULL_TRIAGE_ADMIT = (
    "## Triage Summary\n\n"
    "**Author**: @alice (admit — in allowlist)\n"
    "**Issue type**: bug\n"
    "**Scope estimate**: small\n"
    "**Risk flags**: none\n"
    "**Summary**: A clear small bug.\n"
    "**Files likely affected**: src/foo.py\n"
    "**Recommended action**: admit for autonomous dispatch\n"
)

_FULL_TRIAGE_CLOSE = (
    "## Triage Summary\n\n"
    "**Author**: @spam (queue — not in allowlist)\n"
    "**Issue type**: unclear\n"
    "**Scope estimate**: unclear\n"
    "**Risk flags**: none\n"
    "**Summary**: Duplicate of #12.\n"
    "**Files likely affected**: unknown\n"
    "**Recommended action**: close as duplicate/out-of-scope\n"
)


# ---------------------------------------------------------------------------
# parse_triager_verdict — machine-readable gate verdict
# ---------------------------------------------------------------------------

_FULL_TRIAGE_ADMIT_WITH_VERDICT = (
    "## Triage Summary\n\n"
    "**Author**: @alice (admit — in allowlist)\n"
    "**Issue type**: bug\n"
    "**Scope estimate**: small\n"
    "**Risk flags**: none\n"
    "**Summary**: A clear small bug.\n"
    "**Files likely affected**: src/foo.py\n"
    "**Recommended action**: admit for autonomous dispatch\n"
    "\n<!-- triager-verdict: actionable -->"
)

_FULL_TRIAGE_QUEUE_WITH_VERDICT = (
    "## Triage Summary\n\n"
    "**Author**: @bob (queue — not in allowlist)\n"
    "**Issue type**: feature\n"
    "**Scope estimate**: large\n"
    "**Risk flags**: scope-unclear\n"
    "**Summary**: The issue is ambiguous.\n"
    "**Files likely affected**: unknown\n"
    "**Recommended action**: queue for human review\n"
    "\n<!-- triager-verdict: not-actionable -->"
)


def test_parse_triager_verdict_actionable() -> None:
    """Extracts 'actionable' from a triage comment with actionable verdict."""
    verdict = parse_triager_verdict(_FULL_TRIAGE_ADMIT_WITH_VERDICT)
    assert verdict == TRIAGER_VERDICT_ACTIONABLE


def test_parse_triager_verdict_not_actionable() -> None:
    """Extracts 'not-actionable' from a triage comment with not-actionable verdict."""
    verdict = parse_triager_verdict(_FULL_TRIAGE_QUEUE_WITH_VERDICT)
    assert verdict == TRIAGER_VERDICT_NOT_ACTIONABLE


def test_parse_triager_verdict_none_when_no_marker() -> None:
    """Returns None when the triage comment has no verdict marker."""
    body = (
        "## Triage Summary\n"
        "**Recommended action**: admit for autonomous dispatch\n"
    )
    assert parse_triager_verdict(body) is None


def test_parse_triager_verdict_none_for_non_triage_comment() -> None:
    """Returns None for a comment that is not a triager triage summary."""
    assert parse_triager_verdict("just a regular comment") is None


def test_parse_triager_verdict_none_for_empty_string() -> None:
    """Returns None for an empty comment body."""
    assert parse_triager_verdict("") is None


def test_parse_triager_verdict_case_insensitive() -> None:
    """Verdict value is returned lowercased regardless of marker case."""
    body = "## Triage Summary\n<!-- triager-verdict: ACTIONABLE -->"
    assert parse_triager_verdict(body) == "actionable"


def test_parse_triager_verdict_strips_whitespace() -> None:
    """Verdict value is stripped of leading/trailing whitespace."""
    body = "## Triage Summary\n<!--  triager-verdict:  not-actionable  -->"
    assert parse_triager_verdict(body) == "not-actionable"


def test_parse_triager_verdict_rejects_unknown_value() -> None:
    """Returns None when the verdict marker contains an unrecognized value."""
    body = "## Triage Summary\n<!-- triager-verdict: maybe -->"
    assert parse_triager_verdict(body) is None


def test_parse_triager_verdict_no_header_no_match() -> None:
    """Returns None when the verdict marker is present but header is absent."""
    body = "<!-- triager-verdict: actionable -->"
    assert parse_triager_verdict(body) is None


def test_triager_verdict_constants_are_distinct() -> None:
    """TRIAGER_VERDICT_ACTIONABLE and TRIAGER_VERDICT_NOT_ACTIONABLE are distinct."""
    assert TRIAGER_VERDICT_ACTIONABLE != TRIAGER_VERDICT_NOT_ACTIONABLE
    assert TRIAGER_VERDICT_ACTIONABLE == "actionable"
    assert TRIAGER_VERDICT_NOT_ACTIONABLE == "not-actionable"


# ---------------------------------------------------------------------------
# parse_triager_recommendation
# ---------------------------------------------------------------------------


def test_parse_triager_rec_queue() -> None:
    """Extracts 'queue for human review' from a triage summary."""
    rec = parse_triager_recommendation(_FULL_TRIAGE_QUEUE)
    assert rec == TRIAGER_REC_QUEUE


def test_parse_triager_rec_admit() -> None:
    """Extracts 'admit for autonomous dispatch' from a triage summary."""
    rec = parse_triager_recommendation(_FULL_TRIAGE_ADMIT)
    assert rec == TRIAGER_REC_ADMIT


def test_parse_triager_rec_close() -> None:
    """Extracts 'close as duplicate/out-of-scope' from a triage summary."""
    rec = parse_triager_recommendation(_FULL_TRIAGE_CLOSE)
    assert rec == TRIAGER_REC_CLOSE


def test_parse_triager_rec_none_for_non_triage_comment() -> None:
    """Returns None when the comment is not a triager triage summary."""
    assert parse_triager_recommendation("just a regular comment") is None


def test_parse_triager_rec_none_for_empty_string() -> None:
    """Returns None for an empty comment body."""
    assert parse_triager_recommendation("") is None


def test_parse_triager_rec_none_when_field_absent() -> None:
    """Returns None when the Recommended action field is missing from the summary."""
    body = "## Triage Summary\n\n**Author**: @alice\n**Issue type**: bug\n"
    assert parse_triager_recommendation(body) is None


def test_parse_triager_rec_case_insensitive_value() -> None:
    """Recommended action value is returned lowercased and stripped."""
    body = (
        "## Triage Summary\n"
        "**Recommended action**: Queue For Human Review  \n"
    )
    rec = parse_triager_recommendation(body)
    assert rec == "queue for human review"


def test_parse_triager_rec_strips_trailing_whitespace() -> None:
    """Recommended action value is stripped of leading/trailing whitespace."""
    body = (
        "## Triage Summary\n"
        "**Recommended action**:   admit for autonomous dispatch   \n"
    )
    rec = parse_triager_recommendation(body)
    assert rec == "admit for autonomous dispatch"


def test_parse_triager_rec_handles_no_trailing_newline() -> None:
    """Parses recommendation when comment body has no trailing newline."""
    body = "## Triage Summary\n**Recommended action**: queue for human review"
    rec = parse_triager_recommendation(body)
    assert rec == TRIAGER_REC_QUEUE


# ---------------------------------------------------------------------------
# is_triager_comment
# ---------------------------------------------------------------------------


def test_is_triager_comment_true_for_triage_summary() -> None:
    """Returns True for a comment containing the triage header."""
    assert is_triager_comment(_FULL_TRIAGE_QUEUE) is True


def test_is_triager_comment_false_for_regular_comment() -> None:
    """Returns False for a comment that lacks the triage header."""
    assert is_triager_comment("lgtm") is False


def test_is_triager_comment_false_for_empty_string() -> None:
    """Returns False for an empty string."""
    assert is_triager_comment("") is False


def test_is_triager_comment_false_for_partial_header() -> None:
    """Returns False when only part of the triage header is present."""
    assert is_triager_comment("## Triage") is False
    assert is_triager_comment("Triage Summary") is False


def test_is_triager_comment_true_minimal_header() -> None:
    """Returns True even when the body is just the header line."""
    assert is_triager_comment("## Triage Summary") is True
