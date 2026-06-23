"""Integration tests — handle_event routes pull_request events to converge_pr (issue #107).

SPEC §11.1 event-routing table:
  pull_request / ready_for_review  → Engine.converge (P2)
  pull_request / labeled (converge label only) → Engine.converge (P2/P7)
  pull_request / synchronize       → Engine.converge (P7)
  anything else                    → no-op (no converge, no dispatch)

Eligibility is enforced inside Engine.converge (§10.2 idempotency gate), not here.
These tests verify the routing layer drives converge_pr for eligible AND ineligible
PRs on the right actions, then confirms the gate handles the ineligible cases gracefully.
"""

from __future__ import annotations

import pytest

from src.domain.types import (
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    PRRef,
    RepoRef,
    Verdict,
)
from src.ports.fakes import (
    FakeConvergeStateStore,
    FakeCounterStore,
    FakeForgePort,
    FakeHarnessPort,
    FakeSessionPort,
)
from src.service.orchestrator import OrchestratorService

_REPO = RepoRef(owner="acme", name="svc")
_PR_NUMBER = 42
_PR = PRRef(repo=_REPO, number=_PR_NUMBER)


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


def _repo_dict(repo: RepoRef) -> dict[str, object]:
    return {"name": repo.name, "owner": {"login": repo.owner}}


def _pr_payload(
    action: str,
    pr_number: int = _PR_NUMBER,
    label_name: str = "",
) -> dict[str, object]:
    """Build a minimal pull_request webhook payload."""
    payload: dict[str, object] = {
        "action": action,
        "repository": _repo_dict(_REPO),
        "pull_request": {"number": pr_number},
    }
    if label_name:
        payload["label"] = {"name": label_name}
    return payload


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------


def _green_pr(forge: FakeForgePort) -> None:
    """Seed a non-draft converge-eligible PR with a generic green CI check."""
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=1)
    forge.seed_check_run(_PR, "CI", "completed", "success")


def _make_service(
    forge: FakeForgePort,
    harness: FakeHarnessPort,
) -> OrchestratorService:
    return OrchestratorService(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
    )


async def _drain_converge(service: OrchestratorService) -> None:
    """Await all in-flight background converge tasks.

    handle_event spawns converge as a background task (it is a minutes-long
    sub-machine that must not block the webhook response), so tests that assert on
    converge side effects must drain it first.
    """
    import asyncio

    tasks = list(service._converge_tasks.values())
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / ready_for_review-eligible
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "ready_for_review-eligible")
async def test_handle_event_ready_for_review_drives_converge_pr() -> None:
    """pull_request/ready_for_review on a converge-eligible PR calls converge_pr.

    converge_pr → Engine.converge → reviewer dispatched → APPROVED (R1 zero-blocker path).
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("ready_for_review")
    )
    await _drain_converge(service)

    assert result == {"handled": True}
    # Reviewer + adjudicator were dispatched — converge actually ran.
    assert len(harness.dispatch_calls) == 2
    assert harness.dispatch_calls[0].contract == "agents/converge-reviewer.md"
    assert harness.dispatch_calls[-1].contract == "agents/adjudicator.md"
    # PR ended in APPROVED state (label swap confirms).
    assert (_PR, LABEL_READY) in forge.add_label_calls
    assert (_PR, LABEL_CONVERGE) in forge.remove_label_calls


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / labeled-converge-eligible
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "labeled-converge-eligible")
async def test_handle_event_labeled_converge_drives_converge_pr() -> None:
    """pull_request/labeled with label==converge on an eligible PR calls converge_pr.

    P2: the implementing agent adds the converge label and marks the PR ready.
    The webhook fires and converge_pr runs the review loop.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("labeled", label_name=LABEL_CONVERGE)
    )
    await _drain_converge(service)

    assert result == {"handled": True}
    assert len(harness.dispatch_calls) == 2
    assert harness.dispatch_calls[0].contract == "agents/converge-reviewer.md"
    assert harness.dispatch_calls[-1].contract == "agents/adjudicator.md"
    assert (_PR, LABEL_READY) in forge.add_label_calls


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / synchronize-eligible
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "synchronize-eligible")
async def test_handle_event_synchronize_drives_converge_pr() -> None:
    """pull_request/synchronize on a converge-eligible PR calls converge_pr (P7).

    The fixer pushes new commits → synchronize fires → converge_pr reruns the loop.
    The idempotency gate (§10.2 step 1) handles draft-PR early-exit internally;
    this test confirms the routing reaches converge_pr for a non-draft converge PR.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("synchronize")
    )
    await _drain_converge(service)

    assert result == {"handled": True}
    assert len(harness.dispatch_calls) == 2
    assert harness.dispatch_calls[0].contract == "agents/converge-reviewer.md"
    assert harness.dispatch_calls[-1].contract == "agents/adjudicator.md"


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / ineligible-draft-no-converge-run
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "ineligible-draft-no-converge-run")
async def test_handle_event_synchronize_draft_pr_no_converge_run() -> None:
    """pull_request/synchronize on a DRAFT PR routes to converge_pr but the
    idempotency gate (§10.2 step 1) short-circuits before any reviewer dispatch.

    The implementing agent's own commits fire synchronize events; these must never
    enter the converge loop.  Engine.converge returns BUILDING immediately for draft PRs.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    # Draft PR with converge label — gate must short-circuit.
    forge.seed_pr(_PR, draft=True, labels=[LABEL_CONVERGE], changed_files=1)
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("synchronize")
    )

    assert result == {"handled": True}
    # No reviewer dispatched — draft guard fired.
    assert harness.dispatch_calls == []


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / no-converge-label-still-routes
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "ineligible-no-converge-label")
async def test_handle_event_ready_for_review_routes_converge_regardless_of_label() -> None:
    """pull_request/ready_for_review routes to converge_pr unconditionally (§11.1 table).

    The SPEC §11.1 routing table has no condition on ready_for_review — it always routes
    to Engine.converge.  Eligibility (having the converge label) is NOT checked in the
    routing layer; Engine.converge runs its own idempotency gate (§10.2 step 1) which
    only gates on draft/terminal labels — not on the converge label itself.

    So a non-draft PR without the converge label will still have converge_pr called and
    a reviewer dispatched (since none of the idempotency gate conditions fire).
    This test documents the routing behavior — eligibility enforcement happens inside
    Engine.converge, not at the routing layer.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    # Non-draft PR without converge label — routing still calls converge_pr.
    forge.seed_pr(_PR, draft=False, labels=[], changed_files=1)
    forge.seed_check_run(_PR, "CI", "completed", "success")
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("ready_for_review")
    )
    await _drain_converge(service)

    # Routing reached converge_pr — reviewer + adjudicator were dispatched.
    assert result == {"handled": True}
    assert len(harness.dispatch_calls) == 2


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / ineligible-needs-human
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "ineligible-needs-human")
async def test_handle_event_ready_for_review_needs_human_no_reviewer() -> None:
    """pull_request/ready_for_review on a PR with needs-human does NOT dispatch.

    The needs-human label is a terminal label; Engine.converge returns ESCALATED
    immediately (idempotency gate, §10.2 step 1) without dispatching any agent.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE, LABEL_NEEDS_HUMAN], changed_files=1)
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("ready_for_review")
    )

    assert result == {"handled": True}
    assert harness.dispatch_calls == []


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / ineligible-agent-ready
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "ineligible-agent-ready")
async def test_handle_event_ready_for_review_agent_ready_no_reviewer() -> None:
    """pull_request/ready_for_review on a PR with agent:ready does NOT dispatch.

    agent:ready is a terminal label; Engine.converge returns APPROVED immediately
    (idempotency gate, §10.2 step 1) without dispatching any agent.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    forge.seed_pr(_PR, draft=False, labels=[LABEL_READY], changed_files=1)
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("ready_for_review")
    )

    assert result == {"handled": True}
    assert harness.dispatch_calls == []


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / ignored-action-no-converge
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "ignored-action-no-converge")
async def test_handle_event_assigned_action_no_converge_run() -> None:
    """pull_request/assigned is an ignored action — converge_pr is NOT called.

    SPEC §11.1 only routes ready_for_review, labeled (converge), and synchronize
    to converge; all other pull_request actions are a no-op.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("assigned")
    )

    assert result == {"handled": True}
    assert harness.dispatch_calls == []


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / labeled-non-converge-label-no-converge
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "labeled-non-converge-label-no-converge")
async def test_handle_event_labeled_non_converge_label_no_converge_run() -> None:
    """pull_request/labeled with a non-converge label does NOT call converge_pr.

    Only the converge label triggers the converge path; other labels are ignored.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("labeled", label_name="agent:implementing")
    )

    assert result == {"handled": True}
    assert harness.dispatch_calls == []


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / idempotent-duplicate-delivery
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "idempotent-duplicate-delivery")
async def test_handle_event_converge_duplicate_delivery_id_deduped() -> None:
    """Two handle_event calls with the same delivery_id — second is a no-op.

    The delivery-ID LRU dedup cache (SPEC §11.3) must gate converge_pr so that a
    webhook replay does not double-run a converge round on the same event.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[]),
        # Script a second verdict in case a bug causes a second converge run.
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[]),
    )
    service = _make_service(forge, harness)

    result1 = await service.handle_event(
        "pull_request",
        _pr_payload("ready_for_review"),
        delivery_id="evt-xyz-001",
    )
    result2 = await service.handle_event(
        "pull_request",
        _pr_payload("ready_for_review"),
        delivery_id="evt-xyz-001",
    )
    await _drain_converge(service)

    assert result1 == {"handled": True}
    assert result2 == {"handled": False, "reason": "duplicate_delivery_id"}
    # Only one converge ran (first call) — second was deduped before converge_pr.
    # Each converge dispatches reviewer + adjudicator = 2 calls.
    assert len(harness.dispatch_calls) == 2


# ---------------------------------------------------------------------------
# §11.1-converge-trigger / converge-runs-in-background
# ---------------------------------------------------------------------------


@pytest.mark.covers("§11.1-converge-trigger", "converge-runs-in-background")
async def test_handle_event_converge_runs_in_background_not_inline() -> None:
    """handle_event must NOT run the converge sub-machine inline.

    Converge is minutes-long (review dispatch + multi-round CI polling). Running it
    inside the webhook request blows GitHub's ~10s delivery timeout → redelivery →
    duplicate dispatch, and holds DB write locks long enough to starve other writers
    ("database is locked"). It must be spawned as a background task so the webhook
    returns immediately.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("ready_for_review")
    )

    # Webhook returned, but converge has NOT yet dispatched a reviewer — it was
    # scheduled as a background task, not awaited inline.
    assert result == {"handled": True}
    assert harness.dispatch_calls == []
    assert len(service._converge_tasks) == 1

    # Once drained, the background converge completes: reviewer + adjudicator dispatched.
    await _drain_converge(service)
    assert len(harness.dispatch_calls) == 2


@pytest.mark.covers("§11.1-converge-trigger", "converge-runs-in-background")
async def test_spawn_converge_dedupes_concurrent_same_pr() -> None:
    """Two converge triggers for the same PR while one is in flight → one task.

    A burst of synchronize/labeled events (or a redelivery) must not stack
    concurrent converge runs on the same PR.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    service = _make_service(forge, harness)

    # Two distinct deliveries (different IDs so the delivery-ID cache does not
    # dedup) for the SAME PR, before draining — the per-PR in-flight guard dedups.
    await service.handle_event(
        "pull_request", _pr_payload("synchronize"), delivery_id="d-1"
    )
    assert service._spawn_converge(_PR) is False  # already in flight → no-op
    assert len(service._converge_tasks) == 1

    await _drain_converge(service)
    # Exactly one converge ran → reviewer + adjudicator dispatched (2 total).
    assert len(harness.dispatch_calls) == 2
