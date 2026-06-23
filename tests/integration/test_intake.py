"""Integration tests for Engine.intake and OrchestratorService triage (SPEC §10.4, §11.3)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from src.db.audit import AuditLog
from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    LABEL_TRIAGE,
    IssueRef,
    RepoRef,
)
from src.engine.intake import IntakeEngine, IntakeResult
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService
from src.service.registry import FakeRepoRegistry, RepoConfig

_REPO = RepoRef(owner="acme", name="repo")

# Triager comment templates used in gate tests (SPEC §10.4 two-gate flow)
# These include the machine-readable <!-- triager-verdict: ... --> marker.
_TRIAGER_COMMENT_QUEUE = (
    "## Triage Summary\n\n"
    "**Author**: @owner (admit — in allowlist)\n"
    "**Issue type**: feature\n"
    "**Scope estimate**: large\n"
    "**Risk flags**: scope-unclear\n"
    "**Summary**: The issue is ambiguous.\n"
    "**Files likely affected**: unknown\n"
    "**Recommended action**: queue for human review\n"
    "\n<!-- triager-verdict: not-actionable -->"
)

_TRIAGER_COMMENT_ADMIT = (
    "## Triage Summary\n\n"
    "**Author**: @owner (admit — in allowlist)\n"
    "**Issue type**: bug\n"
    "**Scope estimate**: small\n"
    "**Risk flags**: none\n"
    "**Summary**: A clear small bug.\n"
    "**Files likely affected**: src/foo.py\n"
    "**Recommended action**: admit for autonomous dispatch\n"
    "\n<!-- triager-verdict: actionable -->"
)


def _make_engine(
    forge: FakeForgePort,
    harness: FakeHarnessPort,
    session: FakeSessionPort,
    audit: AuditLog,
    allowlist: list[str],
    owner: str = _REPO.owner,
) -> IntakeEngine:
    return IntakeEngine(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=allowlist,
        owner=owner,
    )


@pytest.fixture
async def fresh_audit() -> AsyncGenerator[AuditLog, None]:
    """Yield an initialised in-memory AuditLog and close it on teardown.

    Closing the connection prevents the aiosqlite 'Event loop is closed'
    RuntimeWarning that occurs when an open connection is garbage-collected
    after the event loop shuts down.
    """
    audit = AuditLog()
    await audit.init()
    try:
        yield audit
    finally:
        await audit.close()


async def _fresh_audit() -> AuditLog:
    """Legacy helper for tests that call _fresh_audit() directly.

    .. deprecated::
        Prefer the ``fresh_audit`` fixture which closes the connection on
        teardown.  This helper is kept to avoid a large test refactor; it
        opens a connection that will be closed when the AuditLog is
        garbage-collected (no leak in practice for in-memory DBs, but may
        emit a teardown warning in some environments).
    """
    audit = AuditLog()
    await audit.init()
    return audit


# ---------------------------------------------------------------------------
# test_intake_admit_path
# ---------------------------------------------------------------------------


async def test_intake_admit_path() -> None:
    """Allowlisted author → Gate 1 sets only LABEL_TRIAGE; triager dispatched.

    Two-gate flow (SPEC §10.4): Gate 1 (trust) sets [LABEL_TRIAGE] only.
    LABEL_AGENT_WORK is NOT applied yet — orchestrator must NOT fire.
    Gate 2 (apply_triager_gate) applies LABEL_AGENT_WORK only after the triager
    classifies the issue as actionable.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=1)
    forge.seed_issue(issue_ref, author="alice", labels=[])

    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])
    result = await engine.intake(issue_ref)

    assert isinstance(result, IntakeResult)
    assert result.handle is not None
    assert result.decision == "admit"
    # Gate 1 sets ONLY [LABEL_TRIAGE] — NOT LABEL_AGENT_WORK (two-gate flow)
    issue = await forge.get_issue(issue_ref)
    assert LABEL_TRIAGE in issue.labels
    assert LABEL_AGENT_WORK not in issue.labels, (
        "LABEL_AGENT_WORK must NOT be set by Gate 1 — orchestrator fires only after Gate 2"
    )
    assert LABEL_AWAITING_PROMOTION not in issue.labels
    # Triager dispatch occurred (Gate 1 dispatches triager; Gate 2 reads verdict)
    assert len(harness.dispatch_calls) == 1


# ---------------------------------------------------------------------------
# test_intake_queue_path
# ---------------------------------------------------------------------------


async def test_intake_queue_path() -> None:
    """Unlisted author → LABEL_AWAITING_PROMOTION set + triager dispatched."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=2)
    forge.seed_issue(issue_ref, author="eve", labels=[])

    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])
    result = await engine.intake(issue_ref)

    assert isinstance(result, IntakeResult)
    assert result.handle is not None
    assert result.decision == "queue"
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AWAITING_PROMOTION in issue.labels
    assert LABEL_TRIAGE in issue.labels
    assert LABEL_AGENT_WORK not in issue.labels
    # Triager dispatch occurred
    assert len(harness.dispatch_calls) == 1


# ---------------------------------------------------------------------------
# test_intake_triager_read_only
# ---------------------------------------------------------------------------


async def test_intake_triager_read_only() -> None:
    """Dispatched triager context always has forge_token_scope='repo-comment' (I5)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=3)
    forge.seed_issue(issue_ref, author="external", labels=[])

    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])
    await engine.intake(issue_ref)

    assert len(harness.dispatch_calls) == 1
    ctx = harness.dispatch_calls[0]
    assert ctx.forge_token_scope == "repo-comment"


# ---------------------------------------------------------------------------
# test_promote_dispatches
# ---------------------------------------------------------------------------


async def test_promote_dispatches() -> None:
    """OrchestratorService.promote swaps label to LABEL_AGENT_WORK and returns RunHandle."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=10)
    forge.seed_issue(issue_ref, author="external", labels=[LABEL_AWAITING_PROMOTION])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )
    handle = await service.promote(issue_ref, operator="admin")

    assert handle is not None
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in issue.labels
    assert LABEL_TRIAGE in issue.labels
    assert LABEL_AWAITING_PROMOTION not in issue.labels


# ---------------------------------------------------------------------------
# test_decline_closes
# ---------------------------------------------------------------------------


async def test_decline_closes() -> None:
    """OrchestratorService.decline removes AWAITING_PROMOTION label."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=11)
    forge.seed_issue(issue_ref, author="external", labels=[LABEL_AWAITING_PROMOTION])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )
    await service.decline(issue_ref, operator="admin")

    issue = await forge.get_issue(issue_ref)
    assert LABEL_AWAITING_PROMOTION not in issue.labels


# ---------------------------------------------------------------------------
# test_decline_audit_logged
# ---------------------------------------------------------------------------


async def test_decline_audit_logged() -> None:
    """OrchestratorService.decline writes an audit record with action='decline' (I6)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=12)
    forge.seed_issue(issue_ref, author="external", labels=[LABEL_AWAITING_PROMOTION])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )
    await service.decline(issue_ref, operator="admin")

    entries = await audit.list_entries(_REPO, issue_ref)
    decline_entries = [e for e in entries if e["action"] == "decline"]
    assert len(decline_entries) == 1
    assert decline_entries[0]["operator"] == "admin"


# ---------------------------------------------------------------------------
# test_audit_log_records_intake
# ---------------------------------------------------------------------------


async def test_audit_log_records_intake() -> None:
    """Both intake paths (admit + queue) write audit records (I6)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    # admit path
    admit_ref = IssueRef(repo=_REPO, number=20)
    forge.seed_issue(admit_ref, author="alice", labels=[])
    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])
    await engine.intake(admit_ref)

    # queue path
    queue_ref = IssueRef(repo=_REPO, number=21)
    forge.seed_issue(queue_ref, author="eve", labels=[])
    await engine.intake(queue_ref)

    entries_admit = await audit.list_entries(_REPO, admit_ref)
    entries_queue = await audit.list_entries(_REPO, queue_ref)

    assert any(e["action"] == "intake:admit" for e in entries_admit)
    assert any(e["action"] == "intake:queue" for e in entries_queue)


# ---------------------------------------------------------------------------
# test_audit_log_records_promote
# ---------------------------------------------------------------------------


async def test_audit_log_records_promote() -> None:
    """OrchestratorService.promote writes an audit record with the operator (I6 + I7)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=30)
    forge.seed_issue(issue_ref, author="external", labels=[LABEL_AWAITING_PROMOTION])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )
    await service.promote(issue_ref, operator="human-admin")

    entries = await audit.list_entries(_REPO, issue_ref)
    promote_entries = [e for e in entries if e["action"] == "promote"]
    assert len(promote_entries) == 1
    assert promote_entries[0]["operator"] == "human-admin"


# ---------------------------------------------------------------------------
# test_handle_event_issues_routes_through_intake (Blocker 1)
# ---------------------------------------------------------------------------


async def test_handle_event_issues_routes_through_intake() -> None:
    """handle_event('issues', ...) calls run_intake(), not engine.dispatch().

    I1 — non-allowlisted issues must be queued (AWAITING_PROMOTION), not dispatched
    directly. Previously handle_event bypassed intake and called engine.dispatch().
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=50)
    forge.seed_issue(issue_ref, author="external-user", labels=[])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )

    payload: dict[str, object] = {
        "action": "opened",  # SPEC §11.1: intake only on opened/reopened
        "issue": {"number": 50},
        "repository": {"owner": {"login": "acme"}, "name": "repo"},
    }
    await service.handle_event("issues", payload)

    # external-user is not allowlisted → intake must set AWAITING_PROMOTION
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AWAITING_PROMOTION in issue.labels
    assert LABEL_AGENT_WORK not in issue.labels

    # Audit record must be written (I6)
    entries = await audit.list_entries(_REPO, issue_ref)
    assert any(e["action"] == "intake:queue" for e in entries)


# ---------------------------------------------------------------------------
# SPEC §11.1 routing: opened → intake (when intake_enabled == True)
# ---------------------------------------------------------------------------


async def test_handle_event_issues_opened_runs_intake() -> None:
    """issues:opened → Gate 1 runs, sets [LABEL_TRIAGE] only for admitted author.

    Two-gate flow (SPEC §10.4): after Gate 1, the issue has LABEL_TRIAGE only.
    LABEL_AGENT_WORK is applied later by Gate 2 (apply_triager_gate).
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=60)
    forge.seed_issue(issue_ref, author="alice", labels=[])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )
    payload: dict[str, object] = {
        "action": "opened",
        "issue": {"number": 60},
        "repository": {"owner": {"login": "acme"}, "name": "repo"},
    }
    result = await service.handle_event("issues", payload)

    assert result["handled"] is True
    issue = await forge.get_issue(issue_ref)
    # alice is allowlisted → admitted by Gate 1 → [LABEL_TRIAGE] only
    assert LABEL_TRIAGE in issue.labels
    assert LABEL_AGENT_WORK not in issue.labels, (
        "Gate 1 must not apply LABEL_AGENT_WORK — Gate 2 does that after triager verdict"
    )


# ---------------------------------------------------------------------------
# SPEC §11.1 routing: opened → NO intake when intake_enabled == False
# ---------------------------------------------------------------------------


async def test_handle_event_issues_opened_no_intake_when_disabled() -> None:
    """issues:opened with intake_enabled=False → no intake run, no labels set."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=61)
    forge.seed_issue(issue_ref, author="alice", labels=[])

    registry = FakeRepoRegistry([
        RepoConfig(repo=_REPO, intake_enabled=False, allowlist=["alice"]),
    ])
    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=["alice"],
        registry=registry,
    )
    payload: dict[str, object] = {
        "action": "opened",
        "issue": {"number": 61},
        "repository": {"owner": {"login": "acme"}, "name": "repo"},
    }
    result = await service.handle_event("issues", payload)

    assert result["handled"] is True
    issue = await forge.get_issue(issue_ref)
    # intake was skipped — no labels written, no dispatch
    assert LABEL_TRIAGE not in issue.labels
    assert LABEL_AGENT_WORK not in issue.labels
    assert LABEL_AWAITING_PROMOTION not in issue.labels
    assert len(harness.dispatch_calls) == 0


# ---------------------------------------------------------------------------
# SPEC §11.1 routing: labeled:agent-work → Engine.dispatch (not intake)
# ---------------------------------------------------------------------------


async def test_handle_event_issues_labeled_agent_work_dispatches() -> None:
    """issues:labeled with label==LABEL_AGENT_WORK → Engine.dispatch, not intake."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=62)
    # Issue already has LABEL_TRIAGE + LABEL_AGENT_WORK (set by prior intake run)
    forge.seed_issue(issue_ref, author="alice", labels=[LABEL_TRIAGE, LABEL_AGENT_WORK])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )
    payload: dict[str, object] = {
        "action": "labeled",
        "label": {"name": LABEL_AGENT_WORK},
        "issue": {"number": 62},
        "repository": {"owner": {"login": "acme"}, "name": "repo"},
    }
    result = await service.handle_event("issues", payload)

    assert result["handled"] is True
    # Engine.dispatch was called (implementer agent dispatched), not intake
    assert len(harness.dispatch_calls) == 1


# ---------------------------------------------------------------------------
# SPEC §11.1 routing: labeled:other → no-op
# ---------------------------------------------------------------------------


async def test_handle_event_issues_labeled_other_noop() -> None:
    """issues:labeled with label != LABEL_AGENT_WORK → no-op, no dispatch."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=63)
    forge.seed_issue(issue_ref, author="alice", labels=[LABEL_TRIAGE])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )
    payload: dict[str, object] = {
        "action": "labeled",
        "label": {"name": LABEL_TRIAGE},  # not LABEL_AGENT_WORK
        "issue": {"number": 63},
        "repository": {"owner": {"login": "acme"}, "name": "repo"},
    }
    result = await service.handle_event("issues", payload)

    assert result["handled"] is True
    # No dispatch — triage label does not trigger intake or dispatch
    assert len(harness.dispatch_calls) == 0


# ---------------------------------------------------------------------------
# SPEC §11.1 routing: other actions (edited, assigned, closed) → no-op
# ---------------------------------------------------------------------------


async def test_handle_event_issues_other_actions_noop() -> None:
    """issues:edited, issues:assigned, issues:closed → no-op, no dispatch."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=64)
    forge.seed_issue(issue_ref, author="alice", labels=[LABEL_TRIAGE, LABEL_AGENT_WORK])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )

    for action in ("edited", "assigned", "closed"):
        payload: dict[str, object] = {
            "action": action,
            "issue": {"number": 64},
            "repository": {"owner": {"login": "acme"}, "name": "repo"},
        }
        await service.handle_event("issues", payload)

    # None of edited/assigned/closed trigger intake or dispatch
    assert len(harness.dispatch_calls) == 0


# ---------------------------------------------------------------------------
# SPEC §11.1 feedback-loop guard: labeled:agent-work does NOT re-run intake
# ---------------------------------------------------------------------------


async def test_handle_event_labeled_agent_work_does_not_re_run_intake() -> None:
    """Simulate the #108 feedback loop: labeled:agent-work → dispatch, not intake.

    Previously the issues branch ran intake for every action, so a newly
    labeled LABEL_AGENT_WORK event would re-run intake (dispatching a second
    triager + setting labels again).  This test confirms that labeled:agent-work
    triggers Engine.dispatch and that the intake idempotency guard prevents
    double-triager even if intake were somehow reached.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=65)
    # Issue already has LABEL_TRIAGE from the first (real) intake run
    forge.seed_issue(issue_ref, author="alice", labels=[LABEL_TRIAGE, LABEL_AGENT_WORK])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )

    # Simulate GitHub re-delivering the labeled:agent-work webhook
    payload: dict[str, object] = {
        "action": "labeled",
        "label": {"name": LABEL_AGENT_WORK},
        "issue": {"number": 65},
        "repository": {"owner": {"login": "acme"}, "name": "repo"},
    }
    await service.handle_event("issues", payload)

    # Should call dispatch exactly once (the implementer), NOT re-run intake
    assert len(harness.dispatch_calls) == 1
    # No audit intake:admit or intake:queue from this re-delivery
    entries = await audit.list_entries(_REPO, issue_ref)
    intake_entries = [e for e in entries if e["action"].startswith("intake:")]
    assert len(intake_entries) == 0


# ---------------------------------------------------------------------------
# SPEC §10 idempotency guard: intake skips when LABEL_TRIAGE already present
# ---------------------------------------------------------------------------


async def test_intake_idempotency_guard_skips_if_triage_label_present() -> None:
    """If the issue already carries LABEL_TRIAGE, intake returns None (no dispatch).

    Defence-in-depth guard: set_labels in step 4 is atomic, so LABEL_TRIAGE
    presence means intake already completed.  A second intake call (re-delivery,
    reconciler re-trigger, or labeled-feedback loop) must be a no-op.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=70)
    # Seed issue with LABEL_TRIAGE already present (simulates post-intake state)
    forge.seed_issue(issue_ref, author="alice", labels=[LABEL_TRIAGE, LABEL_AGENT_WORK])

    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])
    result = await engine.intake(issue_ref)

    # Guard fired: no dispatch, no label changes; handle and decision are None
    assert isinstance(result, IntakeResult)
    assert result.handle is None
    assert result.decision is None
    assert len(harness.dispatch_calls) == 0
    # No audit record written (guard returns before audit step)
    entries = await audit.list_entries(_REPO, issue_ref)
    assert len(entries) == 0


# ---------------------------------------------------------------------------
# SPEC §10.4 two-gate flow — Gate 2: apply_triager_gate
# ---------------------------------------------------------------------------


async def test_gate_actionable_applies_agent_work() -> None:
    """Gate 2: triager verdict 'actionable' → LABEL_AGENT_WORK applied (I2 → orchestrator fires).

    This is the actionable path of the two-gate flow (SPEC §10.4 Gate 2).
    The control plane (not the triager) applies the work label, preserving I5.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=80)
    # After Gate 1: issue has [LABEL_TRIAGE] only
    forge.seed_issue(issue_ref, author="alice", labels=[LABEL_TRIAGE])

    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])

    # Triager posted its comment with actionable verdict
    await forge.post_comment(issue_ref, _TRIAGER_COMMENT_ADMIT)

    result = await engine.apply_triager_gate(issue_ref, "admit")

    assert result == "applied-agent-work"

    issue = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in issue.labels, (
        "Gate 2 must apply LABEL_AGENT_WORK when verdict is actionable"
    )
    assert LABEL_AWAITING_PROMOTION not in issue.labels

    # Audit record written (I6)
    entries = await audit.list_entries(_REPO, issue_ref)
    assert any(e["action"] == "intake:gate-actionable" for e in entries)


async def test_gate_not_actionable_applies_awaiting_promotion() -> None:
    """Gate 2: triager verdict 'not-actionable' → LABEL_AWAITING_PROMOTION + comment.

    Triager classifies issue as requiring human review (scope-unclear, risk flags, etc.).
    Gate 2 must NOT apply LABEL_AGENT_WORK; orchestrator must NOT fire.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=81)
    # After Gate 1: issue has [LABEL_TRIAGE] only
    forge.seed_issue(issue_ref, author="alice", labels=[LABEL_TRIAGE])

    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])

    # Triager posted its comment with not-actionable verdict
    await forge.post_comment(issue_ref, _TRIAGER_COMMENT_QUEUE)

    result = await engine.apply_triager_gate(issue_ref, "admit")

    assert result == "applied-awaiting-promotion"

    issue = await forge.get_issue(issue_ref)
    assert LABEL_AWAITING_PROMOTION in issue.labels
    assert LABEL_AGENT_WORK not in issue.labels, (
        "Gate 2 must not apply LABEL_AGENT_WORK when verdict is not-actionable"
    )

    # A human-visible comment posted
    comments = await forge.list_comments(issue_ref)
    gate_comments = [c for c in comments if "orchestrator:intake-not-actionable" in c.body]
    assert len(gate_comments) == 1

    # Audit record written (I6)
    entries = await audit.list_entries(_REPO, issue_ref)
    assert any(e["action"] == "intake:gate-not-actionable" for e in entries)


async def test_gate_no_verdict_safe_fallback() -> None:
    """Gate 2: no triager verdict in window → safe fallback to AWAITING_PROMOTION.

    When the triager did not post a machine-readable verdict (comment absent or
    malformed), Gate 2 must NOT auto-admit.  Safe fallback: LABEL_AWAITING_PROMOTION
    + explanation comment.  This is the critical property: the triager gates dispatch.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=83)
    # After Gate 1: issue has [LABEL_TRIAGE] only
    forge.seed_issue(issue_ref, author="alice", labels=[LABEL_TRIAGE])

    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])

    # No triager comment — triager hasn't completed yet (or failed)
    result = await engine.apply_triager_gate(issue_ref, "admit")

    assert result == "applied-awaiting-promotion"

    issue = await forge.get_issue(issue_ref)
    assert LABEL_AWAITING_PROMOTION in issue.labels
    assert LABEL_AGENT_WORK not in issue.labels, (
        "Gate 2 must not auto-admit when verdict is absent"
    )

    # Fallback comment posted
    comments = await forge.list_comments(issue_ref)
    fallback_comments = [c for c in comments if "orchestrator:intake-no-verdict" in c.body]
    assert len(fallback_comments) == 1

    # Audit record written (I6)
    entries = await audit.list_entries(_REPO, issue_ref)
    assert any(e["action"] == "intake:gate-no-verdict" for e in entries)


async def test_gate_noop_when_decision_is_queue() -> None:
    """Gate 2 is a no-op when Gate 1 queued the issue — already conservative.

    Non-allowlisted author → Gate 1 sets [LABEL_TRIAGE, LABEL_AWAITING_PROMOTION].
    Gate 2 is skipped entirely; no label changes, no comments.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=82)
    forge.seed_issue(issue_ref, author="external", labels=[LABEL_TRIAGE, LABEL_AWAITING_PROMOTION])

    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])

    await forge.post_comment(issue_ref, _TRIAGER_COMMENT_QUEUE)

    result = await engine.apply_triager_gate(issue_ref, "queue")

    assert result == "no-op"
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AWAITING_PROMOTION in issue.labels
    assert LABEL_AGENT_WORK not in issue.labels


async def test_gate_idempotent_when_agent_work_already_present() -> None:
    """Gate 2 is idempotent: if LABEL_AGENT_WORK is already present, skip.

    Protects against re-delivery: a second Gate 2 run sees the label already
    set and returns 'no-op' without applying a duplicate.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=84)
    # Simulate Gate 2 already ran: LABEL_AGENT_WORK is present
    forge.seed_issue(issue_ref, author="alice", labels=[LABEL_TRIAGE, LABEL_AGENT_WORK])

    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])
    await forge.post_comment(issue_ref, _TRIAGER_COMMENT_ADMIT)

    result = await engine.apply_triager_gate(issue_ref, "admit")

    assert result == "no-op"
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in issue.labels  # unchanged
    # No audit record written (no-op)
    entries = await audit.list_entries(_REPO, issue_ref)
    assert len(entries) == 0


async def test_intake_result_carries_decision() -> None:
    """IntakeResult.decision matches the actual intake decision (admit or queue)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    # Admit path
    admit_ref = IssueRef(repo=_REPO, number=85)
    forge.seed_issue(admit_ref, author="alice", labels=[])
    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])
    admit_result = await engine.intake(admit_ref)
    assert admit_result.decision == "admit"

    # Queue path
    queue_ref = IssueRef(repo=_REPO, number=86)
    forge.seed_issue(queue_ref, author="external", labels=[])
    queue_result = await engine.intake(queue_ref)
    assert queue_result.decision == "queue"
