"""Unit tests for decision functions: route_entry, pipeline_health, derive_state."""

from __future__ import annotations

import pytest

from src.decisions.derive_state import derive_issue_state, derive_pr_state
from src.decisions.pipeline_health import pipeline_health
from src.decisions.route_entry import route_entry
from src.domain.types import (
    ADJUDICATION_MODEL,
    DEFAULT_SWARM_MODEL,
    LABEL_AWAITING_PROMOTION,
    LABEL_CONVERGE,
    LABEL_IMPLEMENTING,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    PRRef,
    RepoRef,
)
from src.ports.fakes import FakeForgePort

# ---------------------------------------------------------------------------
# route_entry tests
# ---------------------------------------------------------------------------

_CONTRACT = "agents/orchestrator.md"


@pytest.mark.covers("§8.1", "row-1-issues")
def test_route_entry_issues() -> None:
    result = route_entry("issues")
    assert result.model == ADJUDICATION_MODEL
    assert result.max_turns == 40
    assert result.contract == _CONTRACT


@pytest.mark.covers("§8.1", "row-2-issue_comment")
def test_route_entry_issue_comment() -> None:
    result = route_entry("issue_comment")
    assert result.model == DEFAULT_SWARM_MODEL
    assert result.max_turns == 30
    assert result.contract == _CONTRACT


@pytest.mark.covers("§8.1", "row-2-pr_review_comment")
def test_route_entry_pr_review_comment() -> None:
    result = route_entry("pull_request_review_comment")
    assert result.model == DEFAULT_SWARM_MODEL
    assert result.max_turns == 30
    assert result.contract == _CONTRACT


@pytest.mark.covers("§8.1", "row-3-unknown")
def test_route_entry_unknown() -> None:
    result = route_entry("pull_request")
    assert result.model == DEFAULT_SWARM_MODEL
    assert result.max_turns == 30
    assert result.contract == _CONTRACT


@pytest.mark.covers("§8.1", "row-3-empty")
def test_route_entry_empty_string() -> None:
    result = route_entry("")
    assert result.model == DEFAULT_SWARM_MODEL
    assert result.max_turns == 30
    assert result.contract == _CONTRACT


def test_route_entry_contract_invariant() -> None:
    """All events return the same contract path."""
    events = ["issues", "issue_comment", "pull_request_review_comment", "pull_request", ""]
    contracts = {route_entry(e).contract for e in events}
    assert contracts == {_CONTRACT}


# ---------------------------------------------------------------------------
# pipeline_health tests
# ---------------------------------------------------------------------------


def _make_repo() -> RepoRef:
    return RepoRef(owner="acme", name="repo")


@pytest.mark.covers("§8.9", "row-3-on_track")
async def test_health_on_track_empty() -> None:
    forge = FakeForgePort()
    repo = _make_repo()
    report = await pipeline_health(repo, forge)
    assert report.implementing == 0
    assert report.converge == 0
    assert report.ready == 0
    assert report.needs_human == 0
    assert report.stale_drafts == 0
    assert report.in_flight == 0
    assert report.verdict == "ON_TRACK"
    assert "ON_TRACK" in report.report_md


@pytest.mark.covers("§8.9", "row-3-on_track")
async def test_health_on_track_mixed() -> None:
    forge = FakeForgePort()
    repo = _make_repo()
    # 1 impl + 1 conv + 2 ready → in_flight=2
    forge.seed_pr(PRRef(repo=repo, number=1), labels=[LABEL_IMPLEMENTING])
    forge.seed_pr(PRRef(repo=repo, number=2), labels=[LABEL_CONVERGE])
    forge.seed_pr(PRRef(repo=repo, number=3), labels=[LABEL_READY])
    forge.seed_pr(PRRef(repo=repo, number=4), labels=[LABEL_READY])

    report = await pipeline_health(repo, forge)
    assert report.implementing == 1
    assert report.converge == 1
    assert report.ready == 2
    assert report.needs_human == 0
    assert report.in_flight == 2
    assert report.verdict == "ON_TRACK"


@pytest.mark.covers("§8.9", "row-1-blocked")
async def test_health_blocked() -> None:
    forge = FakeForgePort()
    repo = _make_repo()
    forge.seed_pr(PRRef(repo=repo, number=1), labels=[LABEL_NEEDS_HUMAN])
    forge.seed_pr(PRRef(repo=repo, number=2), labels=[LABEL_READY])

    report = await pipeline_health(repo, forge)
    assert report.needs_human == 1
    assert report.ready == 1
    assert report.verdict == "BLOCKED"
    assert "BLOCKED" in report.report_md


@pytest.mark.covers("§8.9", "row-2-at_risk")
async def test_health_at_risk_3_plus_2() -> None:
    forge = FakeForgePort()
    repo = _make_repo()
    for i in range(1, 4):
        forge.seed_pr(PRRef(repo=repo, number=i), labels=[LABEL_IMPLEMENTING])
    for i in range(4, 6):
        forge.seed_pr(PRRef(repo=repo, number=i), labels=[LABEL_CONVERGE])

    report = await pipeline_health(repo, forge)
    assert report.implementing == 3
    assert report.converge == 2
    assert report.in_flight == 5
    assert report.verdict == "AT_RISK"


@pytest.mark.covers("§8.9", "row-1-blocked")
async def test_health_blocked_beats_at_risk() -> None:
    forge = FakeForgePort()
    repo = _make_repo()
    forge.seed_pr(PRRef(repo=repo, number=1), labels=[LABEL_NEEDS_HUMAN])
    for i in range(2, 5):
        forge.seed_pr(PRRef(repo=repo, number=i), labels=[LABEL_IMPLEMENTING])
    for i in range(5, 7):
        forge.seed_pr(PRRef(repo=repo, number=i), labels=[LABEL_CONVERGE])

    report = await pipeline_health(repo, forge)
    assert report.needs_human == 1
    assert report.in_flight == 5  # 3 impl + 2 conv
    assert report.verdict == "BLOCKED"


@pytest.mark.covers("§8.9", "row-2-at_risk")
async def test_health_at_risk_4_plus_1() -> None:
    forge = FakeForgePort()
    repo = _make_repo()
    for i in range(1, 5):
        forge.seed_pr(PRRef(repo=repo, number=i), labels=[LABEL_IMPLEMENTING])
    forge.seed_pr(PRRef(repo=repo, number=5), labels=[LABEL_CONVERGE])

    report = await pipeline_health(repo, forge)
    assert report.implementing == 4
    assert report.converge == 1
    assert report.in_flight == 5
    assert report.verdict == "AT_RISK"


@pytest.mark.covers("§8.9", "row-3-on_track")
async def test_health_on_track_four() -> None:
    forge = FakeForgePort()
    repo = _make_repo()
    for i in range(1, 5):
        forge.seed_pr(PRRef(repo=repo, number=i), labels=[LABEL_IMPLEMENTING])

    report = await pipeline_health(repo, forge)
    assert report.implementing == 4
    assert report.converge == 0
    assert report.in_flight == 4
    assert report.verdict == "ON_TRACK"


async def test_health_in_flight_no_double_count() -> None:
    """3 PRs each with BOTH implementing AND converge → in_flight=3."""
    forge = FakeForgePort()
    repo = _make_repo()
    for i in range(1, 4):
        forge.seed_pr(
            PRRef(repo=repo, number=i),
            labels=[LABEL_IMPLEMENTING, LABEL_CONVERGE],
        )

    report = await pipeline_health(repo, forge)
    assert report.implementing == 3
    assert report.converge == 3
    assert report.in_flight == 3  # no double-counting
    assert report.verdict == "ON_TRACK"


async def test_health_usage_error() -> None:
    """Missing required arguments → TypeError."""
    forge = FakeForgePort()
    with pytest.raises(TypeError):
        await pipeline_health(forge)  # type: ignore[call-arg]


async def test_health_stale_drafts() -> None:
    """Draft PRs with implementing label count as stale_drafts."""
    forge = FakeForgePort()
    repo = _make_repo()
    forge.seed_pr(PRRef(repo=repo, number=1), labels=[LABEL_IMPLEMENTING], draft=True)
    forge.seed_pr(PRRef(repo=repo, number=2), labels=[LABEL_IMPLEMENTING], draft=False)

    report = await pipeline_health(repo, forge)
    assert report.stale_drafts == 1
    assert report.implementing == 2


# ---------------------------------------------------------------------------
# derive_issue_state tests
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.10", "derive_issue_state-closed")
def test_derive_issue_closed() -> None:
    assert derive_issue_state([], closed=True) == "CLOSED"


@pytest.mark.covers("§8.10", "derive_issue_state-escalated")
def test_derive_issue_escalated() -> None:
    assert derive_issue_state([LABEL_NEEDS_HUMAN], closed=False) == "ESCALATED"


@pytest.mark.covers("§8.10", "derive_issue_state-queued")
def test_derive_issue_queued_agent_work() -> None:
    assert derive_issue_state(["agent-work"], closed=False) == "QUEUED"


@pytest.mark.covers("§8.10", "derive_issue_state-queued")
def test_derive_issue_queued_default() -> None:
    assert derive_issue_state([], closed=False) == "QUEUED"


@pytest.mark.covers("§8.10", "derive_issue_state-closed")
def test_derive_issue_closed_beats_needs_human() -> None:
    assert derive_issue_state([LABEL_NEEDS_HUMAN], closed=True) == "CLOSED"


@pytest.mark.covers("§8.10", "derive_issue_state-closed")
def test_derive_issue_closed_beats_agent_work() -> None:
    assert derive_issue_state(["agent-work"], closed=True) == "CLOSED"


@pytest.mark.covers("§8.10", "derive_issue_state-pending")
def test_derive_issue_pending() -> None:
    """awaiting-promotion label without closed/needs-human → PENDING (SPEC §8.10)."""
    assert derive_issue_state([LABEL_AWAITING_PROMOTION], closed=False) == "PENDING"


@pytest.mark.covers("§8.10", "derive_issue_state-closed")
def test_derive_issue_closed_beats_awaiting_promotion() -> None:
    """closed=True beats awaiting-promotion — priority ordering."""
    assert derive_issue_state([LABEL_AWAITING_PROMOTION], closed=True) == "CLOSED"


@pytest.mark.covers("§8.10", "derive_issue_state-escalated")
def test_derive_issue_needs_human_beats_awaiting_promotion() -> None:
    """needs-human beats awaiting-promotion — priority ordering."""
    assert (
        derive_issue_state([LABEL_NEEDS_HUMAN, LABEL_AWAITING_PROMOTION], closed=False)
        == "ESCALATED"
    )


# ---------------------------------------------------------------------------
# derive_pr_state tests
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.10", "derive_pr_state-merged")
def test_derive_pr_merged() -> None:
    assert derive_pr_state([], draft=False, merged=True, changed_files=5) == "MERGED"


@pytest.mark.covers("§8.10", "derive_pr_state-escalated")
def test_derive_pr_escalated() -> None:
    assert (
        derive_pr_state([LABEL_NEEDS_HUMAN], draft=False, merged=False, changed_files=5)
        == "ESCALATED"
    )


@pytest.mark.covers("§8.10", "derive_pr_state-approved")
def test_derive_pr_approved() -> None:
    assert (
        derive_pr_state([LABEL_READY], draft=False, merged=False, changed_files=5) == "APPROVED"
    )


@pytest.mark.covers("§8.10", "derive_pr_state-empty")
def test_derive_pr_empty() -> None:
    assert derive_pr_state([], draft=False, merged=False, changed_files=0) == "EMPTY"


@pytest.mark.covers("§8.10", "derive_pr_state-converging")
def test_derive_pr_converging() -> None:
    assert (
        derive_pr_state([LABEL_CONVERGE], draft=False, merged=False, changed_files=3)
        == "CONVERGING"
    )


@pytest.mark.covers("§8.10", "derive_pr_state-building")
def test_derive_pr_building_implementing() -> None:
    assert (
        derive_pr_state([LABEL_IMPLEMENTING], draft=False, merged=False, changed_files=3)
        == "BUILDING"
    )


@pytest.mark.covers("§8.10", "derive_pr_state-building")
def test_derive_pr_building_default() -> None:
    assert derive_pr_state([], draft=False, merged=False, changed_files=3) == "BUILDING"


@pytest.mark.covers("§8.10", "derive_pr_state-merged")
def test_derive_pr_merged_beats_needs_human() -> None:
    assert (
        derive_pr_state([LABEL_NEEDS_HUMAN], draft=False, merged=True, changed_files=5) == "MERGED"
    )


@pytest.mark.covers("§8.10", "derive_pr_state-building")
def test_derive_pr_converging_requires_non_draft() -> None:
    """Draft PR with converge label → BUILDING, not CONVERGING."""
    assert (
        derive_pr_state([LABEL_CONVERGE], draft=True, merged=False, changed_files=3) == "BUILDING"
    )


@pytest.mark.covers("§8.10", "derive_pr_state-building")
def test_derive_pr_draft_empty_is_building() -> None:
    """Draft PR with 0 changed files → BUILDING (not EMPTY, because draft)."""
    assert derive_pr_state([], draft=True, merged=False, changed_files=0) == "BUILDING"


@pytest.mark.covers("§8.10", "derive_pr_state-empty")
def test_derive_pr_non_draft_empty_is_empty() -> None:
    """Non-draft PR with 0 changed files → EMPTY."""
    assert derive_pr_state([], draft=False, merged=False, changed_files=0) == "EMPTY"


@pytest.mark.covers("§8.10", "derive_pr_state-empty")
def test_derive_pr_empty_before_converging() -> None:
    """EMPTY check comes before CONVERGING — 0 files + converge label → EMPTY."""
    assert (
        derive_pr_state([LABEL_CONVERGE], draft=False, merged=False, changed_files=0) == "EMPTY"
    )
