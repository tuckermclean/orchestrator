"""Security tests for Step 2 — Intake & Triage.

Invariants tested:
  I1 — non-allowlisted authors never reach a code-writing agent without human promotion
  I5 — triage agent is always read-only (forge_token_scope == 'repo-comment')
  I6 — every intake decision and human promotion is audit-logged
  I7 — LABEL_AWAITING_PROMOTION and LABEL_AGENT_WORK never coexist

Per TESTING.md §5 and SECURITY.md §3.
"""

from __future__ import annotations

from src.db.audit import AuditLog
from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    IssueRef,
    RepoRef,
)
from src.engine.intake import IntakeEngine
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService

_REPO = RepoRef(owner="acme", name="repo")


async def _fresh_audit() -> AuditLog:
    audit = AuditLog()
    await audit.init()
    return audit


# ---------------------------------------------------------------------------
# I1 — Unlisted author never dispatches a code-writing agent
# ---------------------------------------------------------------------------


async def test_security_unlisted_never_dispatches() -> None:
    """Non-allowlisted author cannot trigger a repo-branch dispatch (I1).

    The triager IS dispatched (read-only), but no code-writing agent
    (forge_token_scope='repo-branch') is ever dispatched for an unlisted author.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=1)
    forge.seed_issue(issue_ref, author="attacker", labels=[])

    engine = IntakeEngine(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=["alice"],
        owner=_REPO.owner,
    )
    await engine.intake(issue_ref)

    # At most one dispatch (the triager), and it must be read-only
    branch_dispatches = [
        ctx for ctx in harness.dispatch_calls if ctx.forge_token_scope == "repo-branch"
    ]
    assert len(branch_dispatches) == 0, (
        "Non-allowlisted author triggered a repo-branch dispatch"
    )


# ---------------------------------------------------------------------------
# I1 — Human promotion is required before a code-writing agent runs
# ---------------------------------------------------------------------------


async def test_security_promotion_required() -> None:
    """LABEL_AGENT_WORK is only set after explicit promote() call (I1).

    After intake with a non-allowlisted author, the issue should carry
    LABEL_AWAITING_PROMOTION. Only after promote() should LABEL_AGENT_WORK appear.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=2)
    forge.seed_issue(issue_ref, author="external-user", labels=[])

    engine = IntakeEngine(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=["alice"],
        owner=_REPO.owner,
    )
    await engine.intake(issue_ref)

    # Before promotion: no LABEL_AGENT_WORK
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK not in issue.labels, "LABEL_AGENT_WORK must not appear before promotion"
    assert LABEL_AWAITING_PROMOTION in issue.labels, "Issue must be in triage queue"

    # Promote and verify LABEL_AGENT_WORK appears
    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )
    await service.promote(issue_ref, operator="human")

    issue_after = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in issue_after.labels, "LABEL_AGENT_WORK must appear after promotion"


# ---------------------------------------------------------------------------
# I5 — Triage agent is always read-only
# ---------------------------------------------------------------------------


async def test_security_triage_agent_read_only() -> None:
    """Triager dispatch always uses forge_token_scope='repo-comment' (I5).

    This applies to BOTH the admit path and the queue path — in both cases the
    triager posts a structured comment and must never have branch-write access.
    Gate 1 (trust) dispatches the triager; Gate 2 (apply_triager_gate) applies labels.
    The triager itself never applies any labels (I5 preserved).
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    # Test admit path
    admit_ref = IssueRef(repo=_REPO, number=10)
    forge.seed_issue(admit_ref, author="alice", labels=[])

    engine = IntakeEngine(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=["alice"],
        owner=_REPO.owner,
    )
    await engine.intake(admit_ref)

    for ctx in harness.dispatch_calls:
        assert ctx.forge_token_scope == "repo-comment", (
            f"Triager dispatched with scope {ctx.forge_token_scope!r} instead of 'repo-comment'"
        )

    harness.dispatch_calls.clear()

    # Test queue path
    queue_ref = IssueRef(repo=_REPO, number=11)
    forge.seed_issue(queue_ref, author="external", labels=[])
    await engine.intake(queue_ref)

    for ctx in harness.dispatch_calls:
        assert ctx.forge_token_scope == "repo-comment", (
            f"Triager dispatched with scope {ctx.forge_token_scope!r} instead of 'repo-comment'"
        )


async def test_security_gate2_applies_agent_work_not_triager() -> None:
    """I5 preserved: LABEL_AGENT_WORK is applied by the control plane (Gate 2), not the triager.

    The triager only posts a comment with a machine-readable verdict.  Gate 2
    (apply_triager_gate, running in the control plane) reads that verdict and
    applies the label.  Zero repo-branch dispatches happen during Gate 2.
    """
    from src.domain.types import LABEL_AGENT_WORK, LABEL_TRIAGE

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=12)
    # Post-Gate-1 state: [LABEL_TRIAGE] only
    forge.seed_issue(issue_ref, author="alice", labels=[LABEL_TRIAGE])

    engine = IntakeEngine(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=["alice"],
        owner=_REPO.owner,
    )

    # Seed actionable triager comment
    _ACTIONABLE = (
        "## Triage Summary\n"
        "**Recommended action**: admit for autonomous dispatch\n"
        "\n<!-- triager-verdict: actionable -->"
    )
    await forge.post_comment(issue_ref, _ACTIONABLE)

    # Gate 2 runs (no harness dispatch occurs — it only mutates forge labels)
    dispatch_count_before = len(harness.dispatch_calls)
    await engine.apply_triager_gate(issue_ref, "admit")

    # No new dispatch (Gate 2 applies labels directly, does not dispatch any agent)
    assert len(harness.dispatch_calls) == dispatch_count_before, (
        "Gate 2 must not dispatch any agent — it applies labels directly (I5)"
    )

    # LABEL_AGENT_WORK was applied by Gate 2 (control plane)
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in issue.labels


# ---------------------------------------------------------------------------
# I7 — LABEL_AWAITING_PROMOTION and LABEL_AGENT_WORK never coexist
# ---------------------------------------------------------------------------


async def test_security_awaiting_and_agent_work_never_coexist() -> None:
    """After any intake or promote action, both promotion labels never appear together (I7).

    set_labels() uses PUT semantics (atomic replace) — this invariant is guaranteed
    structurally, but we assert it explicitly on the post-action label state.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=20)
    forge.seed_issue(issue_ref, author="external", labels=[])

    engine = IntakeEngine(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=["alice"],
        owner=_REPO.owner,
    )

    # After intake (queue path)
    await engine.intake(issue_ref)
    issue = await forge.get_issue(issue_ref)
    _assert_no_coexistence(issue.labels)

    # After promote
    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )
    await service.promote(issue_ref, operator="admin")
    issue_after = await forge.get_issue(issue_ref)
    _assert_no_coexistence(issue_after.labels)


def _assert_no_coexistence(labels: list[str]) -> None:
    has_awaiting = LABEL_AWAITING_PROMOTION in labels
    has_agent_work = LABEL_AGENT_WORK in labels
    assert not (has_awaiting and has_agent_work), (
        f"I7 violated: both {LABEL_AWAITING_PROMOTION!r} and {LABEL_AGENT_WORK!r} are present"
    )


# ---------------------------------------------------------------------------
# I6 — Every intake decision is audit-logged
# ---------------------------------------------------------------------------


async def test_intake_audit_logged() -> None:
    """Every intake call writes an audit record (I6)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=30)
    forge.seed_issue(issue_ref, author="someone", labels=[])

    engine = IntakeEngine(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=[],   # empty allowlist → default-deny; "someone" is not owner → queue
        owner=_REPO.owner,
    )
    await engine.intake(issue_ref)

    entries = await audit.list_entries(_REPO, issue_ref)
    assert len(entries) >= 1, "Audit log must have at least one entry after intake"
    actions = {e["action"] for e in entries}
    assert any(a.startswith("intake:") for a in actions), (
        f"No intake action found in audit entries: {actions}"
    )


# ---------------------------------------------------------------------------
# I6 + I7 — Human promotion is audit-logged with operator identity
# ---------------------------------------------------------------------------


async def test_promote_audit_logged() -> None:
    """promote() writes an audit record that includes the operator identity (I6 + I7)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO, number=40)
    forge.seed_issue(issue_ref, author="external", labels=[LABEL_AWAITING_PROMOTION])

    service = OrchestratorService(
        forge=forge, harness=harness, session=session, audit=audit, allowlist=["alice"]
    )
    await service.promote(issue_ref, operator="ops-team")

    entries = await audit.list_entries(_REPO, issue_ref)
    promote_entries = [e for e in entries if e["action"] == "promote"]
    assert len(promote_entries) == 1, "Exactly one promote audit record expected"
    assert promote_entries[0]["operator"] == "ops-team", (
        "Promote audit record must include operator identity"
    )
