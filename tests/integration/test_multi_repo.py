"""Integration tests for multi-repo registry (issue #49).

Tests cover:
- Per-repo allowlist routing: event for repo A uses A's allowlist
- Reconciler iterates all enabled repos
- Single-repo backward-compat: service without registry behaves as before
- Registry-aware handle_event routing (unknown/disabled repo → not handled)
- run_intake uses per-repo config when registry is set
"""

from __future__ import annotations

import pytest

from src.db.audit import AuditLog
from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    LABEL_IMPLEMENTING,
    IssueRef,
    PRRef,
    RepoRef,
)
from src.ports.fakes import (
    FakeConvergeStateStore,
    FakeCounterStore,
    FakeForgePort,
    FakeHarnessPort,
    FakeSessionPort,
)
from src.service.orchestrator import OrchestratorService
from src.service.registry import FakeRepoRegistry, RepoConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_A = RepoRef(owner="acme", name="api")
_REPO_B = RepoRef(owner="acme", name="ui")


async def _fresh_audit() -> AuditLog:
    audit = AuditLog()
    await audit.init()
    return audit


def _issue_payload(repo: RepoRef, issue_number: int = 1) -> dict[str, object]:
    """Build a minimal GitHub issues event payload for the given repo."""
    return {
        "repository": {
            "name": repo.name,
            "owner": {"login": repo.owner},
        },
        "issue": {"number": issue_number},
    }


def _make_service(
    forge: FakeForgePort | None = None,
    harness: FakeHarnessPort | None = None,
    registry: FakeRepoRegistry | None = None,
    audit: AuditLog | None = None,
) -> OrchestratorService:
    forge = forge or FakeForgePort()
    session = FakeSessionPort()
    harness = harness or FakeHarnessPort(session=session)
    return OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        audit=audit,
        allowlist=[],
        owner="acme",
        registry=registry,
    )


# ---------------------------------------------------------------------------
# Per-repo allowlist routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_per_repo_allowlist_routing_admits_listed_author() -> None:
    """Event for repo A uses repo A's allowlist — listed author admitted."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO_A, number=1)
    forge.seed_issue(issue_ref, author="alice", labels=[])

    registry = FakeRepoRegistry([
        RepoConfig(repo=_REPO_A, allowlist=["alice"]),
    ])
    service = _make_service(forge=forge, harness=harness, registry=registry, audit=audit)

    payload = _issue_payload(_REPO_A, issue_number=1)
    result = await service.handle_event("issues", payload)

    assert result["handled"] is True
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in issue.labels, "alice should be admitted (allowlisted)"
    assert LABEL_AWAITING_PROMOTION not in issue.labels


@pytest.mark.asyncio
async def test_per_repo_allowlist_routing_queues_unlisted_author() -> None:
    """Event for repo A uses repo A's allowlist — unlisted author queued."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO_A, number=1)
    forge.seed_issue(issue_ref, author="unknown-user", labels=[])

    registry = FakeRepoRegistry([
        RepoConfig(repo=_REPO_A, allowlist=["alice"]),  # unknown-user not in list
    ])
    service = _make_service(forge=forge, harness=harness, registry=registry, audit=audit)

    payload = _issue_payload(_REPO_A, issue_number=1)
    result = await service.handle_event("issues", payload)

    assert result["handled"] is True
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AWAITING_PROMOTION in issue.labels
    assert LABEL_AGENT_WORK not in issue.labels


@pytest.mark.asyncio
async def test_per_repo_allowlist_routing_repo_b_different_allowlist() -> None:
    """Repo A and repo B have independent allowlists — author admitted in B but not A."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    audit = await _fresh_audit()

    issue_a = IssueRef(repo=_REPO_A, number=1)
    issue_b = IssueRef(repo=_REPO_B, number=2)
    forge.seed_issue(issue_a, author="bob", labels=[])
    forge.seed_issue(issue_b, author="bob", labels=[])

    registry = FakeRepoRegistry([
        RepoConfig(repo=_REPO_A, allowlist=["alice"]),   # bob NOT in repo A's list
        RepoConfig(repo=_REPO_B, allowlist=["alice", "bob"]),  # bob IS in repo B's list
    ])
    service = _make_service(forge=forge, harness=harness, registry=registry, audit=audit)

    # Event for repo A — bob should be queued
    await service.handle_event("issues", _issue_payload(_REPO_A, issue_number=1))
    a_issue = await forge.get_issue(issue_a)
    assert LABEL_AWAITING_PROMOTION in a_issue.labels

    # Event for repo B — bob should be admitted
    await service.handle_event("issues", _issue_payload(_REPO_B, issue_number=2))
    b_issue = await forge.get_issue(issue_b)
    assert LABEL_AGENT_WORK in b_issue.labels


@pytest.mark.asyncio
async def test_owner_always_admitted_per_repo() -> None:
    """Repo owner is admitted even with an empty allowlist (default-deny, owner-always-in)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO_A, number=1)
    forge.seed_issue(issue_ref, author="acme", labels=[])  # owner == repo.owner

    registry = FakeRepoRegistry([
        RepoConfig(repo=_REPO_A, allowlist=[]),  # empty — owner-only
    ])
    service = _make_service(forge=forge, harness=harness, registry=registry, audit=audit)

    payload = _issue_payload(_REPO_A, issue_number=1)
    await service.handle_event("issues", payload)

    issue = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in issue.labels


# ---------------------------------------------------------------------------
# Unknown / disabled repo routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_for_unknown_repo_not_handled() -> None:
    """handle_event returns not handled when the repo is not registered."""
    registry = FakeRepoRegistry([RepoConfig(repo=_REPO_A)])
    service = _make_service(registry=registry)

    # Payload for _REPO_B which is not registered
    payload = _issue_payload(_REPO_B, issue_number=1)
    result = await service.handle_event("issues", payload)

    assert result["handled"] is False
    assert result.get("reason") == "repo_not_registered"


@pytest.mark.asyncio
async def test_event_for_disabled_repo_not_handled() -> None:
    """handle_event returns not handled when the repo is disabled (enabled=False)."""
    forge = FakeForgePort()
    issue_ref = IssueRef(repo=_REPO_A, number=1)
    forge.seed_issue(issue_ref, author="alice", labels=[])

    registry = FakeRepoRegistry([
        RepoConfig(repo=_REPO_A, enabled=False),
    ])
    service = OrchestratorService(
        forge=forge,
        harness=FakeHarnessPort(),
        session=FakeSessionPort(),
        allowlist=[],
        owner="acme",
        registry=registry,
    )
    payload = _issue_payload(_REPO_A, issue_number=1)
    result = await service.handle_event("issues", payload)

    assert result["handled"] is False
    assert result.get("reason") == "repo_not_registered"


# ---------------------------------------------------------------------------
# Reconciler iterates all enabled repos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_now_iterates_all_enabled_repos() -> None:
    """reconcile_now() with registry reconciles all enabled repos, returns per-repo reports."""
    forge = FakeForgePort()

    # Seed stale implementing PRs in both repos so reconciler has something to act on.
    pr_a = PRRef(repo=_REPO_A, number=10)
    pr_b = PRRef(repo=_REPO_B, number=20)
    forge.seed_pr(
        pr_a,
        title="PR A",
        labels=[LABEL_IMPLEMENTING],
        draft=True,
        changed_files=1,
    )
    forge.seed_pr(
        pr_b,
        title="PR B",
        labels=[LABEL_IMPLEMENTING],
        draft=True,
        changed_files=1,
    )

    registry = FakeRepoRegistry([
        RepoConfig(repo=_REPO_A, enabled=True),
        RepoConfig(repo=_REPO_B, enabled=True),
    ])

    service = OrchestratorService(
        forge=forge,
        harness=FakeHarnessPort(),
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
        allowlist=[],
        owner="acme",
        registry=registry,
    )

    reports = await service.reconcile_now()
    # Two enabled repos → two reports
    assert len(reports) == 2


@pytest.mark.asyncio
async def test_reconcile_now_skips_disabled_repos() -> None:
    """reconcile_now() does not reconcile disabled repos."""
    registry = FakeRepoRegistry([
        RepoConfig(repo=_REPO_A, enabled=True),
        RepoConfig(repo=_REPO_B, enabled=False),
    ])
    service = _make_service(registry=registry)

    reports = await service.reconcile_now()
    # Only one enabled repo → one report
    assert len(reports) == 1


@pytest.mark.asyncio
async def test_reconcile_now_explicit_repo_bypasses_registry() -> None:
    """reconcile_now(repo=X) scopes to that single repo even with a multi-repo registry."""
    registry = FakeRepoRegistry([
        RepoConfig(repo=_REPO_A),
        RepoConfig(repo=_REPO_B),
    ])
    service = _make_service(registry=registry)

    reports = await service.reconcile_now(repo=_REPO_A)
    assert len(reports) == 1


@pytest.mark.asyncio
async def test_reconcile_now_empty_registry_returns_empty() -> None:
    """reconcile_now() with an empty registry returns an empty list."""
    registry = FakeRepoRegistry([])
    service = _make_service(registry=registry)

    reports = await service.reconcile_now()
    assert reports == []


# ---------------------------------------------------------------------------
# Single-repo backward compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_registry_backward_compat_handle_event() -> None:
    """Without a registry, handle_event works as before (uses global allowlist/owner)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO_A, number=1)
    forge.seed_issue(issue_ref, author="alice", labels=[])

    # No registry — single-repo mode using global allowlist
    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        audit=audit,
        allowlist=["alice"],
        owner="acme",
        registry=None,
    )

    payload = _issue_payload(_REPO_A, issue_number=1)
    result = await service.handle_event("issues", payload)

    assert result["handled"] is True
    issue = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in issue.labels


@pytest.mark.asyncio
async def test_no_registry_reconcile_fallback() -> None:
    """Without a registry, reconcile_now() falls back to the demo/repo default."""
    service = OrchestratorService(
        forge=FakeForgePort(),
        harness=FakeHarnessPort(),
        session=FakeSessionPort(),
        allowlist=[],
        owner="demo",
        registry=None,
    )
    # Should not raise — returns one report for the fallback demo/repo
    reports = await service.reconcile_now()
    assert len(reports) == 1


# ---------------------------------------------------------------------------
# run_intake per-repo config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_intake_uses_per_repo_allowlist() -> None:
    """run_intake routes through per-repo allowlist when registry is set."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO_A, number=1)
    forge.seed_issue(issue_ref, author="carol", labels=[])

    registry = FakeRepoRegistry([
        RepoConfig(repo=_REPO_A, allowlist=["carol"]),
    ])
    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        audit=audit,
        allowlist=[],   # global: no one admitted
        owner="acme",
        registry=registry,
    )

    handle = await service.run_intake(issue_ref)
    assert handle is not None

    issue = await forge.get_issue(issue_ref)
    # carol is in repo A's allowlist — should be admitted despite empty global list
    assert LABEL_AGENT_WORK in issue.labels


@pytest.mark.asyncio
async def test_run_intake_fallback_without_registry() -> None:
    """run_intake uses global allowlist when no registry is set."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    audit = await _fresh_audit()

    issue_ref = IssueRef(repo=_REPO_A, number=1)
    forge.seed_issue(issue_ref, author="alice", labels=[])

    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        audit=audit,
        allowlist=["alice"],
        owner="acme",
        registry=None,
    )

    handle = await service.run_intake(issue_ref)
    assert handle is not None

    issue = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in issue.labels
