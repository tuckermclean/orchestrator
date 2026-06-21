"""Integration tests for Engine.intake and OrchestratorService triage (SPEC §10.4, §11.3)."""

from __future__ import annotations

from src.db.audit import AuditLog
from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    LABEL_TRIAGE,
    IssueRef,
    RepoRef,
)
from src.engine.intake import IntakeEngine
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService

_REPO = RepoRef(owner="acme", name="repo")


def _make_engine(
    forge: FakeForgePort,
    harness: FakeHarnessPort,
    session: FakeSessionPort,
    audit: AuditLog,
    allowlist: list[str],
) -> IntakeEngine:
    return IntakeEngine(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=allowlist,
    )


async def _fresh_audit() -> AuditLog:
    audit = AuditLog()
    await audit.init()
    return audit


# ---------------------------------------------------------------------------
# test_intake_admit_path
# ---------------------------------------------------------------------------


async def test_intake_admit_path() -> None:
    """Allowlisted author → LABEL_AGENT_WORK set + dispatch occurs."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=1)
    forge.seed_issue(issue_ref, author="alice", labels=[])

    engine = _make_engine(forge, harness, session, audit, allowlist=["alice"])
    handle = await engine.intake(issue_ref)

    assert handle is not None
    # Verify labels were set atomically to [LABEL_TRIAGE, LABEL_AGENT_WORK]
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in issue.labels
    assert LABEL_TRIAGE in issue.labels
    assert LABEL_AWAITING_PROMOTION not in issue.labels
    # Dispatch occurred
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
    handle = await engine.intake(issue_ref)

    assert handle is not None
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
