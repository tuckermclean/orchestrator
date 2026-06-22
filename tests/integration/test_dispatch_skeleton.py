"""Integration tests for the dispatch skeleton."""

from __future__ import annotations

from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_IMPLEMENTING,
    IssueRef,
    PRRef,
    RepoRef,
)
from src.engine.dispatch import Engine
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.service.orchestrator import OrchestratorService


async def test_dispatch_skeleton_creates_run() -> None:
    """Engine.dispatch → FakeHarnessPort → RunHandle stored; SSE events deliverable."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="test", name="repo")
    issue_ref = IssueRef(repo=repo, number=42)
    # Seed issue so add_label(issue_ref, LABEL_IMPLEMENTING) can resolve it
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    engine = Engine(forge=forge, harness=harness, session=session)
    handle = await engine.dispatch("issues", issue_ref=issue_ref)

    assert handle is not None
    assert len(harness.dispatch_calls) == 1
    ctx = harness.dispatch_calls[0]
    assert ctx.model == "claude-opus-4-8"
    assert ctx.max_turns == 40
    assert ctx.forge_token_scope == "repo-branch"


async def test_dispatch_skeleton_dedup_guard() -> None:
    """Second dispatch for same issue is skipped if implementing PR exists."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="test", name="repo")
    issue_ref = IssueRef(repo=repo, number=42)

    # Seed an existing implementing PR with Closes #42
    pr_ref = PRRef(repo=repo, number=1)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], body="Closes #42")

    engine = Engine(forge=forge, harness=harness, session=session)
    handle = await engine.dispatch("issues", issue_ref=issue_ref)

    assert handle is None  # skipped
    assert len(harness.dispatch_calls) == 0


async def test_dispatch_skeleton_sse_events_delivered() -> None:
    """FakeHarnessPort emits SSE events that FakeSessionPort can stream."""
    forge = FakeForgePort()
    session = FakeSessionPort()
    harness = FakeHarnessPort(session=session)

    repo = RepoRef(owner="test", name="repo")

    service = OrchestratorService(forge=forge, harness=harness, session=session)
    handle = await service.dev_dispatch(repo)

    assert handle is not None
    events = []
    async for event in session.stream_events(handle.run_id):
        events.append(event)
        if event.event_type == "completed":
            break

    assert any(e.event_type == "queued" for e in events)
    assert any(e.event_type == "completed" for e in events)


async def test_dispatch_comment_event() -> None:
    """issue_comment events dispatch when issue carries LABEL_AGENT_WORK (H5 guard)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="test", name="repo")
    issue_ref = IssueRef(repo=repo, number=1)
    # Issue must carry LABEL_AGENT_WORK for H5 guard to pass
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    engine = Engine(forge=forge, harness=harness, session=session)
    handle = await engine.dispatch("issue_comment", issue_ref=issue_ref)

    assert handle is not None
    assert len(harness.dispatch_calls) == 1
    ctx = harness.dispatch_calls[0]
    assert ctx.model == "claude-sonnet-4-6"
    assert ctx.max_turns == 30


async def test_dispatch_unknown_event_returns_none() -> None:
    """Unknown events return None (not dispatched)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()

    engine = Engine(forge=forge, harness=harness, session=session)
    handle = await engine.dispatch("push", issue_ref=None, pr_ref=None)

    assert handle is None
    assert len(harness.dispatch_calls) == 0


async def test_dispatch_dedup_guard_fixed_keyword() -> None:
    """Dedup guard fires on 'fixed #N' (past-tense fix variant)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="test", name="repo")
    issue_ref = IssueRef(repo=repo, number=12)

    pr_ref = PRRef(repo=repo, number=2)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], body="fixed #12")

    engine = Engine(forge=forge, harness=harness, session=session)
    handle = await engine.dispatch("issues", issue_ref=issue_ref)

    assert handle is None  # skipped — PR already targets this issue
    assert len(harness.dispatch_calls) == 0


async def test_dispatch_dedup_guard_closed_keyword() -> None:
    """Dedup guard fires on 'closed #N' (past-tense close variant)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="test", name="repo")
    issue_ref = IssueRef(repo=repo, number=5)

    pr_ref = PRRef(repo=repo, number=3)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], body="closed #5")

    engine = Engine(forge=forge, harness=harness, session=session)
    handle = await engine.dispatch("issues", issue_ref=issue_ref)

    assert handle is None  # skipped — PR already targets this issue
    assert len(harness.dispatch_calls) == 0


async def test_dispatch_dedup_guard_resolved_keyword() -> None:
    """Dedup guard fires on 'resolved #N' (past-tense resolve variant)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="test", name="repo")
    issue_ref = IssueRef(repo=repo, number=99)

    pr_ref = PRRef(repo=repo, number=4)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], body="resolved #99")

    engine = Engine(forge=forge, harness=harness, session=session)
    handle = await engine.dispatch("issues", issue_ref=issue_ref)

    assert handle is None  # skipped — PR already targets this issue
    assert len(harness.dispatch_calls) == 0


async def test_dispatch_dedup_guard_close_keyword() -> None:
    """Dedup guard fires on bare 'close #N' keyword."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="test", name="repo")
    issue_ref = IssueRef(repo=repo, number=7)

    pr_ref = PRRef(repo=repo, number=5)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], body="close #7")

    engine = Engine(forge=forge, harness=harness, session=session)
    handle = await engine.dispatch("issues", issue_ref=issue_ref)

    assert handle is None  # skipped — PR already targets this issue
    assert len(harness.dispatch_calls) == 0


async def test_dispatch_dedup_guard_fix_keyword() -> None:
    """Dedup guard fires on bare 'fix #N' keyword."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="test", name="repo")
    issue_ref = IssueRef(repo=repo, number=8)

    pr_ref = PRRef(repo=repo, number=6)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], body="fix #8")

    engine = Engine(forge=forge, harness=harness, session=session)
    handle = await engine.dispatch("issues", issue_ref=issue_ref)

    assert handle is None  # skipped — PR already targets this issue
    assert len(harness.dispatch_calls) == 0


async def test_dispatch_dedup_guard_resolve_keyword() -> None:
    """Dedup guard fires on bare 'resolve #N' keyword."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="test", name="repo")
    issue_ref = IssueRef(repo=repo, number=9)

    pr_ref = PRRef(repo=repo, number=7)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], body="resolve #9")

    engine = Engine(forge=forge, harness=harness, session=session)
    handle = await engine.dispatch("issues", issue_ref=issue_ref)

    assert handle is None  # skipped — PR already targets this issue
    assert len(harness.dispatch_calls) == 0
