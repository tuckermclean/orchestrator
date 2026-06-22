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
    BLOCKING_CI_CHECKS,
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
    """Seed a non-draft converge-eligible PR with all CI checks green."""
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=1)
    for name in BLOCKING_CI_CHECKS:
        forge.seed_check_run(_PR, name, "completed", "success")


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

    assert result == {"handled": True}
    # Reviewer was dispatched — converge actually ran.
    assert len(harness.dispatch_calls) == 1
    assert harness.dispatch_calls[0].contract == "agents/converge-reviewer.md"
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

    assert result == {"handled": True}
    assert len(harness.dispatch_calls) == 1
    assert harness.dispatch_calls[0].contract == "agents/converge-reviewer.md"
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

    assert result == {"handled": True}
    assert len(harness.dispatch_calls) == 1
    assert harness.dispatch_calls[0].contract == "agents/converge-reviewer.md"


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
    for name in BLOCKING_CI_CHECKS:
        forge.seed_check_run(_PR, name, "completed", "success")
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    service = _make_service(forge, harness)

    result = await service.handle_event(
        "pull_request", _pr_payload("ready_for_review")
    )

    # Routing reached converge_pr — reviewer was dispatched.
    assert result == {"handled": True}
    assert len(harness.dispatch_calls) == 1


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

    assert result1 == {"handled": True}
    assert result2 == {"handled": False, "reason": "duplicate_delivery_id"}
    # Only one reviewer was dispatched (first call) — second was deduped before converge_pr.
    assert len(harness.dispatch_calls) == 1
