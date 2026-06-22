"""Idempotency and crash-only tests (SPEC §11.3, TESTING.md §6).

Tests delivery-ID dedup, reconcile_now idempotency, and deescalate_pr recovery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.db.audit import AuditLog
from src.domain.types import (
    ISSUE_COOLDOWN_S,
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    LABEL_CONVERGE,
    LABEL_IMPLEMENTING,
    LABEL_NEEDS_HUMAN,
    LABEL_TRIAGE,
    IssueRef,
    PRRef,
    RepoRef,
)
from src.engine.dispatch import Engine
from src.ports.fakes import (
    FakeConvergeStateStore,
    FakeCounterStore,
    FakeForgePort,
    FakeHarnessPort,
    FakeSessionPort,
)
from src.service.orchestrator import OrchestratorService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO = RepoRef(owner="acme", name="service")


def _make_service(
    forge: FakeForgePort | None = None,
    harness: FakeHarnessPort | None = None,
    counter: FakeCounterStore | None = None,
    converge_state: FakeConvergeStateStore | None = None,
    audit: AuditLog | None = None,
    dedup_window: int = 1000,
) -> OrchestratorService:
    forge = forge or FakeForgePort()
    harness = harness or FakeHarnessPort()
    svc = OrchestratorService(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        audit=audit or AuditLog(),
        counter=counter or FakeCounterStore(),
        converge_state=converge_state or FakeConvergeStateStore(),
        dedup_window=dedup_window,
    )
    return svc


def _pr(n: int) -> PRRef:
    return PRRef(repo=REPO, number=n)


def _issue(n: int) -> IssueRef:
    return IssueRef(repo=REPO, number=n)


# ---------------------------------------------------------------------------
# §6 — delivery-ID dedup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_duplicate_delivery_id() -> None:
    """Second handle_event with same delivery_id → handled=False; no duplicate label ops."""
    forge = FakeForgePort()
    issue_ref = _issue(1)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge)
    await svc._audit.init()

    # First delivery
    payload: dict[str, object] = {
        "repository": {"owner": {"login": "acme"}, "name": "service"},
        "issue": {"number": 1},
    }
    result1 = await svc.handle_event("issue_comment", payload, delivery_id="abc-123")
    assert result1["handled"] is True

    # Second delivery with same ID
    add_label_count_before = len(forge.add_label_calls)
    result2 = await svc.handle_event("issue_comment", payload, delivery_id="abc-123")
    assert result2["handled"] is False
    assert result2["reason"] == "duplicate_delivery_id"
    # No additional label ops
    assert len(forge.add_label_calls) == add_label_count_before


@pytest.mark.asyncio
async def test_handle_event_idempotent_two_distinct_deliveries() -> None:
    """Two handle_event calls with different delivery_ids both return handled=True."""
    forge = FakeForgePort()
    issue_ref = _issue(5)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge)
    await svc._audit.init()

    payload: dict[str, object] = {
        "repository": {"owner": {"login": "acme"}, "name": "service"},
        "issue": {"number": 5},
    }

    result1 = await svc.handle_event("issue_comment", payload, delivery_id="x-001")
    result2 = await svc.handle_event("issue_comment", payload, delivery_id="x-002")

    assert result1["handled"] is True
    assert result2["handled"] is True


@pytest.mark.asyncio
async def test_dedup_window_expiry_allows_reprocessing() -> None:
    """LRU window eviction: after window fills, oldest delivery_id can be reprocessed."""
    forge = FakeForgePort()
    issue_ref = _issue(2)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge, dedup_window=3)
    await svc._audit.init()

    payload: dict[str, object] = {
        "repository": {"owner": {"login": "acme"}, "name": "service"},
        "issue": {"number": 2},
    }

    # Fill the window with 3 unique IDs (abc-001 is oldest → evicted after 4th)
    await svc.handle_event("issue_comment", payload, delivery_id="abc-001")
    await svc.handle_event("issue_comment", payload, delivery_id="abc-002")
    await svc.handle_event("issue_comment", payload, delivery_id="abc-003")
    # 4th ID pushes abc-001 out of the LRU window
    await svc.handle_event("issue_comment", payload, delivery_id="abc-004")

    # abc-001 is evicted; re-delivering it returns handled=True
    result = await svc.handle_event("issue_comment", payload, delivery_id="abc-001")
    assert result["handled"] is True


# ---------------------------------------------------------------------------
# §6 — reconciler idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconciler_idempotent_two_sweeps() -> None:
    """Two successive Engine.reconcile calls do not double-act on any entity.

    An orphan issue with no PR should only be redispatched once; the second
    sweep sees the audit marker comment from the first and the counter at 1,
    which is still < cap, but within ISSUE_COOLDOWN_S window → skip-recent.
    """
    from src.domain.types import Comment

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(10)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])
    # Old initial comment to make first sweep redispatch
    old_time = datetime.now(tz=UTC) - timedelta(seconds=ISSUE_COOLDOWN_S + 100)
    entity_key = f"issue:acme/service#{issue_ref.number}"
    forge._comments[entity_key] = [
        Comment(id="1", body="initial", created_at=old_time, author="user")
    ]

    counter = FakeCounterStore()
    engine = Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=counter,
        converge_state=FakeConvergeStateStore(),
    )

    # First sweep → redispatch
    report1 = await engine.reconcile(REPO)
    assert report1.redispatched == 1
    dispatch_count_after_sweep1 = len(harness.dispatch_calls)

    # After the first sweep, the audit marker comment is posted (recent)
    # so the second sweep sees seconds_since_last_activity < ISSUE_COOLDOWN_S → skip-recent
    report2 = await engine.reconcile(REPO)
    assert report2.redispatched == 0
    assert len(harness.dispatch_calls) == dispatch_count_after_sweep1


@pytest.mark.asyncio
async def test_reconciler_idempotent_rc4_skip_recent() -> None:
    """After RC-4 re-dispatch, second reconcile within ISSUE_COOLDOWN_S → skip-recent."""
    from src.domain.types import Comment

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(11)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])
    # Pre-seed with a very old comment so first sweep redispatches
    old_time = datetime.now(tz=UTC) - timedelta(seconds=ISSUE_COOLDOWN_S + 200)
    entity_key = f"issue:acme/service#{issue_ref.number}"
    forge._comments[entity_key] = [
        Comment(id="1", body="initial old", created_at=old_time, author="user")
    ]

    counter = FakeCounterStore()
    engine = Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=counter,
        converge_state=FakeConvergeStateStore(),
    )

    # First sweep triggers redispatch and posts a recent comment
    r1 = await engine.reconcile(REPO)
    assert r1.redispatched == 1

    # Immediately sweep again — the audit marker comment is recent
    r2 = await engine.reconcile(REPO)
    assert r2.redispatched == 0  # skip-recent


@pytest.mark.asyncio
async def test_partial_state_recovery_building_pr() -> None:
    """PR in BUILDING (stale) + reconciler → RC-1 applies correct StaleAction."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    pr_ref = _pr(20)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], draft=True)
    stale_ago = datetime.now(tz=UTC) - timedelta(seconds=1500)
    forge.seed_dispatch_run_at(pr_ref, stale_ago)
    # ci_runs == 0 → trigger-ci

    engine = Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
    )
    report = await engine.reconcile(REPO)

    # Should have triggered CI on the stale PR without panic or label corruption
    assert report.stale_acted == 1
    pr_after = await forge.get_pr(pr_ref)
    assert LABEL_NEEDS_HUMAN not in pr_after.labels  # no false escalation


@pytest.mark.asyncio
async def test_redispatch_count_survives_crash() -> None:
    """Counter pre-seeded at 2 simulates 2 prior crashes; third cycle escalates (E10, I4)."""
    from src.domain.types import ISSUE_REDISPATCH_CAP, Comment

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(30)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])
    old_time = datetime.now(tz=UTC) - timedelta(seconds=ISSUE_COOLDOWN_S + 100)
    entity_key = f"issue:acme/service#{issue_ref.number}"
    forge._comments[entity_key] = [
        Comment(id="1", body="old", created_at=old_time, author="user")
    ]

    counter = FakeCounterStore()
    counter.seed_count(issue_ref, "orphan", ISSUE_REDISPATCH_CAP - 1)  # count == 2

    engine = Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=counter,
        converge_state=FakeConvergeStateStore(),
    )

    # Cycle at count=2: below cap → redispatch
    r1 = await engine.reconcile(REPO)
    assert r1.redispatched == 1
    # Counter is now 3
    assert await counter.get_count(issue_ref, "orphan") == ISSUE_REDISPATCH_CAP

    # Need a fresh old comment so second cycle doesn't see skip-recent
    old_time2 = datetime.now(tz=UTC) - timedelta(seconds=ISSUE_COOLDOWN_S + 100)
    forge._comments[entity_key].append(
        Comment(id="99", body="forced old", created_at=old_time2, author="bot")
    )
    # Overwrite comments with only the old one (skip the recent audit marker)
    forge._comments[entity_key] = [
        Comment(id="99", body="forced old override", created_at=old_time2, author="bot")
    ]

    # Cycle at count=3: at cap → escalate (E10)
    r2 = await engine.reconcile(REPO)
    assert r2.escalated >= 1
    issue_after = await forge.get_issue(issue_ref)
    assert LABEL_NEEDS_HUMAN in issue_after.labels


@pytest.mark.asyncio
async def test_counter_db_wins_over_comment_count() -> None:
    """CounterStore is authoritative; comment count is irrelevant."""
    from src.domain.types import Comment

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(40)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    # 3 audit marker comments (as if counter were 3)
    old_time = datetime.now(tz=UTC) - timedelta(seconds=ISSUE_COOLDOWN_S + 100)
    entity_key = f"issue:acme/service#{issue_ref.number}"
    forge._comments[entity_key] = [
        Comment(
            id=str(i),
            body=f"<!-- orchestrator:redispatch ch=orphan --> count={i}",
            created_at=old_time,
            author="bot",
        )
        for i in (1, 2, 3)
    ]

    # BUT counter says 1 (below cap=3)
    counter = FakeCounterStore()
    counter.seed_count(issue_ref, "orphan", 1)

    engine = Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=counter,
        converge_state=FakeConvergeStateStore(),
    )

    report = await engine.reconcile(REPO)
    # counter=1 < ISSUE_REDISPATCH_CAP → redispatch (not escalate)
    assert report.redispatched == 1
    assert report.escalated == 0


# ---------------------------------------------------------------------------
# deescalate_pr — P16/P17 recovery tests (TESTING.md §4.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deescalate_pr_removes_needs_human() -> None:
    """deescalate_pr removes LABEL_NEEDS_HUMAN from PR; converge label remains."""
    forge = FakeForgePort()
    pr_ref = _pr(50)
    forge.seed_pr(pr_ref, labels=[LABEL_NEEDS_HUMAN, LABEL_CONVERGE])

    audit = AuditLog()
    await audit.init()
    svc = _make_service(forge=forge, audit=audit)
    await svc.deescalate_pr(pr_ref, operator="alice")

    pr_after = await forge.get_pr(pr_ref)
    assert LABEL_NEEDS_HUMAN not in pr_after.labels
    assert LABEL_CONVERGE in pr_after.labels


@pytest.mark.asyncio
async def test_deescalate_pr_resets_counters() -> None:
    """deescalate_pr resets stale-pr and converge-retry counters to 0."""
    forge = FakeForgePort()
    pr_ref = _pr(51)
    forge.seed_pr(pr_ref, labels=[LABEL_NEEDS_HUMAN])
    counter = FakeCounterStore()
    counter.seed_count(pr_ref, "stale-pr", 2)
    counter.seed_count(pr_ref, "converge-retry", 2)

    audit = AuditLog()
    await audit.init()
    svc = _make_service(forge=forge, counter=counter, audit=audit)
    await svc.deescalate_pr(pr_ref, operator="alice")

    assert await counter.get_count(pr_ref, "stale-pr") == 0
    assert await counter.get_count(pr_ref, "converge-retry") == 0


@pytest.mark.asyncio
async def test_deescalate_pr_writes_audit_record() -> None:
    """deescalate_pr writes an audit record with event=deescalate_pr and operator."""
    forge = FakeForgePort()
    pr_ref = _pr(52)
    forge.seed_pr(pr_ref, labels=[LABEL_NEEDS_HUMAN])

    audit = AuditLog()
    await audit.init()
    svc = _make_service(forge=forge, audit=audit)
    await svc.deescalate_pr(pr_ref, operator="alice")

    entries = await audit.list_entries(pr_ref.repo, pr_ref)
    deesc_entries = [e for e in entries if e["action"] == "deescalate_pr"]
    assert len(deesc_entries) >= 1
    assert deesc_entries[0]["operator"] == "alice"


@pytest.mark.asyncio
async def test_deescalate_pr_clears_converge_state() -> None:
    """deescalate_pr clears converge state so next Engine.converge starts at R1."""
    forge = FakeForgePort()
    pr_ref = _pr(53)
    forge.seed_pr(pr_ref, labels=[LABEL_NEEDS_HUMAN])

    converge_state = FakeConvergeStateStore()
    now = datetime.now(tz=UTC)
    converge_state.seed_round(pr_ref, 2)
    converge_state.seed_round_started(pr_ref, now)

    audit = AuditLog()
    await audit.init()
    svc = _make_service(forge=forge, converge_state=converge_state, audit=audit)
    await svc.deescalate_pr(pr_ref, operator="alice")

    assert await converge_state.get_converge_round(pr_ref) == 0
    assert await converge_state.get_round_started(pr_ref) is None


@pytest.mark.asyncio
async def test_deescalate_pr_full_recovery_cycle() -> None:
    """Full P16 path: deescalate → converge completes in R1 → APPROVED."""
    from src.domain.types import Verdict
    from src.ports.fakes import FakeForgePort

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    pr_ref = _pr(54)
    forge.seed_pr(
        pr_ref,
        labels=[LABEL_NEEDS_HUMAN, LABEL_CONVERGE],
        draft=False,
        changed_files=2,
    )
    # Seed passing CI checks
    _ci_checks = [
        "Type Check", "Lint", "Integration Tests",
        "Docker Build & Scan", "Helm Lint", "Helm Kubeconform",
    ]
    for name in _ci_checks:
        forge.seed_check_run(pr_ref, name, "completed", "success")

    # Script a 0-blocker verdict for the reviewer dispatch
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )

    converge_state = FakeConvergeStateStore()
    # Simulate a prior converge having advanced to round 2 before escalation
    converge_state.seed_round(pr_ref, 2)

    audit = AuditLog()
    await audit.init()
    svc = OrchestratorService(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        audit=audit,
        counter=FakeCounterStore(),
        converge_state=converge_state,
    )

    # De-escalate: removes needs-human, clears converge state → R1 on next converge
    await svc.deescalate_pr(pr_ref, operator="alice")

    # Remove the needs-human label effect (de-escalation removes it from forge)
    # Add back converge label in case it was affected
    await forge.add_label(pr_ref, LABEL_CONVERGE)

    # Run converge — should start at R1 (converge_state cleared) and approve
    from src.engine.converge import converge as _converge
    state = await _converge(svc.engine, pr_ref)
    assert state == "APPROVED"


# ---------------------------------------------------------------------------
# TESTING.md §6 named idempotency tests — ALREADY-BUILT behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_idempotent_two_calls() -> None:
    """Two Engine.dispatch calls for the same issue do not create two PRs (TESTING.md §6).

    The dedup guard in Engine.dispatch checks for an existing open implementing PR
    (carrying LABEL_IMPLEMENTING) whose body closes the issue; the second dispatch
    is a no-op when such a PR already exists.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(60)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    engine = Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
    )

    # First dispatch — creates a draft PR
    await engine.dispatch("issues", issue_ref=issue_ref)
    prs_after_first = list(forge._prs.values())
    assert len(prs_after_first) == 1, "First dispatch should create exactly one PR"

    # The dedup guard looks for open PRs with LABEL_IMPLEMENTING.  Add that label to
    # the newly-created PR (simulating the forge labeling that happens in production
    # after the harness applies the implementing label to the PR).
    first_pr_ref = prs_after_first[0].ref
    await forge.add_label(first_pr_ref, LABEL_IMPLEMENTING)

    # Second dispatch — dedup guard sees the existing implementing PR, no-ops
    await engine.dispatch("issues", issue_ref=issue_ref)
    prs_after_second = list(forge._prs.values())
    assert len(prs_after_second) == 1, "Second dispatch must not create a duplicate PR"
    # Harness was called once (for the first dispatch only)
    assert len(harness.dispatch_calls) == 1


@pytest.mark.asyncio
async def test_intake_idempotent_triage_already_set() -> None:
    """issues:reopened on already-LABEL_TRIAGE issue does not re-run full intake (TESTING.md §6).

    When an issue already carries LABEL_TRIAGE, re-running intake still works but
    the atomic label swap is idempotent — the label set ends up the same.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(61)
    # Issue already has triage + awaiting-promotion (was previously processed)
    forge.seed_issue(issue_ref, labels=[LABEL_TRIAGE, LABEL_AWAITING_PROMOTION])

    audit = AuditLog()
    await audit.init()
    svc = _make_service(forge=forge, harness=harness, audit=audit)

    # Run intake (simulating issues:reopened)
    await svc.run_intake(issue_ref)

    # Label state reflects the intake decision (set_labels is idempotent via PUT semantics)
    issue_after = await forge.get_issue(issue_ref)
    # Triage label is always present after intake
    assert LABEL_TRIAGE in issue_after.labels
    # No duplicate triage label (labels are a set in spirit)
    assert issue_after.labels.count(LABEL_TRIAGE) == 1


@pytest.mark.asyncio
async def test_converge_idempotent_not_converging() -> None:
    """Engine.converge on a draft PR returns BUILDING; no reviewer dispatched (TESTING.md §6).

    The idempotency gate in Engine.converge returns immediately for draft PRs
    without dispatching any reviewer agent.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    pr_ref = _pr(62)
    # Draft PR — converge gate should short-circuit
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], draft=True, changed_files=2)

    engine = Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
    )

    from src.engine.converge import converge as _converge
    state = await _converge(engine, pr_ref)

    assert state == "BUILDING"
    assert len(harness.dispatch_calls) == 0, "No reviewer should be dispatched for a draft PR"


@pytest.mark.asyncio
async def test_engine_no_in_process_state() -> None:
    """Two sequential Engine.converge calls on the same PR share no state (TESTING.md §6).

    Each call reads fresh forge labels at the idempotency gate; no mutable
    in-process state leaks between calls. The second call sees APPROVED (added
    by the first) and returns immediately.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    pr_ref = _pr(63)
    forge.seed_pr(pr_ref, labels=[LABEL_CONVERGE], draft=False, changed_files=2)
    for ci_name in [
        "Type Check", "Lint", "Integration Tests",
        "Docker Build & Scan", "Helm Lint", "Helm Kubeconform",
    ]:
        forge.seed_check_run(pr_ref, ci_name, "completed", "success")

    from src.domain.types import Verdict
    # Script a 0-blocker verdict so the first converge approves
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )

    converge_state = FakeConvergeStateStore()
    engine = Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=converge_state,
    )

    from src.engine.converge import converge as _converge

    # First call: approves the PR
    state1 = await _converge(engine, pr_ref)
    assert state1 == "APPROVED"
    dispatch_count_after_first = len(harness.dispatch_calls)

    # Second call: idempotency gate reads LABEL_READY → returns APPROVED immediately
    state2 = await _converge(engine, pr_ref)
    assert state2 == "APPROVED"
    # No additional reviewer dispatches on the second call
    assert len(harness.dispatch_calls) == dispatch_count_after_first


@pytest.mark.asyncio
async def test_partial_state_recovery_converge_pr_no_workflow() -> None:
    """Non-draft converge PR, no recent run → RC-3 re-arms once (TESTING.md §6)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    pr_ref = _pr(64)
    forge.seed_pr(pr_ref, labels=[LABEL_CONVERGE], draft=False, changed_files=2)
    # At least one CI check run so ci_runs > 0 (avoids row-1 trigger-ci)
    forge.seed_check_run(pr_ref, "Type Check", "completed", "failure")
    # No workflow run seeded → last_workflow_run_at returns None → recency guard skipped → rearm
    # (Also no run handle in converge_state → run=None → rows 2/3 don't fire)

    engine = Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
    )
    report = await engine.reconcile(REPO)

    # RC-3 should call trigger_workflow exactly once (rearm action)
    assert len(harness.trigger_workflow_calls) == 1
    assert report.rearmed == 1
