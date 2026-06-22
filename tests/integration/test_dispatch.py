"""Integration tests for §4.2 Dispatch Lifecycle."""

from __future__ import annotations

from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_CONVERGE,
    LABEL_IMPLEMENTING,
    LABEL_READY,
    LABEL_TRIAGE,
    IssueRef,
    PRRef,
    RepoRef,
)
from src.engine.dispatch import Engine
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort


def _engine(
    forge: FakeForgePort,
    harness: FakeHarnessPort,
    session: FakeSessionPort,
) -> Engine:
    return Engine(forge=forge, harness=harness, session=session)


# ---------------------------------------------------------------------------
# §4.2 row 1 — issues:labeled agent-work → draft PR created + LABEL_IMPLEMENTING
# ---------------------------------------------------------------------------


async def test_dispatch_opens_draft_pr() -> None:
    """issues:labeled with agent-work → draft PR with Closes #N; LABEL_IMPLEMENTING added."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="acme", name="service")
    issue_ref = IssueRef(repo=repo, number=7)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    engine = _engine(forge, harness, session)
    handle = await engine.dispatch("issues", issue_ref=issue_ref)

    assert handle is not None

    # Draft PR was created
    assert len(forge.create_pr_calls) == 1
    _repo, title, body, head, base, draft = forge.create_pr_calls[0]
    assert draft is True
    assert "Closes #7" in body
    assert "7" in title  # e.g. "Fix #7"

    # LABEL_IMPLEMENTING was added to the issue
    add_calls = forge.add_label_calls
    assert any(
        (ref == issue_ref or ref.number == issue_ref.number) and label == LABEL_IMPLEMENTING
        for ref, label in add_calls
    )

    # The draft PR was created in-store with draft=True
    pr_ref = PRRef(repo=repo, number=1)  # auto-incremented to 1
    pr = await forge.get_pr(pr_ref)
    assert pr.draft is True
    assert "Closes #7" in pr.body


# ---------------------------------------------------------------------------
# §4.2 row 2 — issues:labeled → harness called with claude-opus-4-8 / 40 turns
# ---------------------------------------------------------------------------


async def test_dispatch_calls_harness() -> None:
    """issues:labeled → harness.dispatch called with model=claude-opus-4-8, max_turns=40."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="acme", name="service")
    issue_ref = IssueRef(repo=repo, number=3)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    engine = _engine(forge, harness, session)
    await engine.dispatch("issues", issue_ref=issue_ref)

    assert len(harness.dispatch_calls) == 1
    ctx = harness.dispatch_calls[0]
    assert ctx.model == "claude-opus-4-8"
    assert ctx.max_turns == 40
    assert ctx.issue_ref == issue_ref
    assert ctx.forge_token_scope == "repo-branch"


# ---------------------------------------------------------------------------
# §4.2 row 3 — issue_comment @claude → Sonnet/30; harness called
# ---------------------------------------------------------------------------


async def test_dispatch_comment_uses_sonnet() -> None:
    """issue_comment event dispatches with Sonnet/30 params when issue has LABEL_AGENT_WORK."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="acme", name="service")
    issue_ref = IssueRef(repo=repo, number=10)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    engine = _engine(forge, harness, session)
    handle = await engine.dispatch(
        "issue_comment", issue_ref=issue_ref, comment_body="@claude please fix"
    )

    assert handle is not None
    assert len(harness.dispatch_calls) == 1
    ctx = harness.dispatch_calls[0]
    assert ctx.model == "claude-sonnet-4-6"
    assert ctx.max_turns == 30


# ---------------------------------------------------------------------------
# §4.2 row 4 — PR in BUILDING; @claude on issue → second harness.dispatch
# ---------------------------------------------------------------------------


async def test_dispatch_redispatch_via_comment() -> None:
    """PR in BUILDING state; @claude comment on issue → second harness.dispatch call."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="acme", name="service")
    issue_ref = IssueRef(repo=repo, number=15)
    pr_ref = PRRef(repo=repo, number=5)

    # Seed issue with agent-work, and seed an implementing (BUILDING) PR
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], draft=True, body="Closes #15")

    engine = _engine(forge, harness, session)

    # Initial dispatch via issues event is dedup-guarded (PR already exists)
    dedup_handle = await engine.dispatch("issues", issue_ref=issue_ref)
    assert dedup_handle is None
    assert len(harness.dispatch_calls) == 0

    # Second dispatch via issue_comment should succeed (different path, H5 passes)
    handle = await engine.dispatch(
        "issue_comment",
        issue_ref=issue_ref,
        comment_body="@claude also add tests please",
    )
    assert handle is not None
    assert len(harness.dispatch_calls) == 1
    ctx = harness.dispatch_calls[0]
    assert ctx.model == "claude-sonnet-4-6"
    assert ctx.max_turns == 30


# ---------------------------------------------------------------------------
# §4.2 row 5 — Full lifecycle: QUEUED → dispatch → PR CONVERGING → APPROVED → merged
# ---------------------------------------------------------------------------


async def test_dispatch_full_lifecycle() -> None:
    """Full lifecycle: issue dispatched; PR transitions through label states."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="acme", name="service")
    issue_ref = IssueRef(repo=repo, number=20)

    # Issue starts as QUEUED (has agent-work label)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    engine = _engine(forge, harness, session)

    # Dispatch → opens draft PR + harness called exactly once
    handle = await engine.dispatch("issues", issue_ref=issue_ref)
    assert handle is not None
    assert len(harness.dispatch_calls) == 1

    # Draft PR was created in store
    assert len(forge.create_pr_calls) == 1
    pr_ref = PRRef(repo=repo, number=1)
    pr = await forge.get_pr(pr_ref)
    assert pr.draft is True  # BUILDING state

    # Simulate implementer finishing → mark PR as converging
    await forge.add_label(pr_ref, LABEL_CONVERGE)
    pr_converging = await forge.get_pr(pr_ref)
    assert LABEL_CONVERGE in pr_converging.labels  # PR is now CONVERGING

    # Simulate converge approval → add LABEL_READY
    await forge.add_label(pr_ref, LABEL_READY)
    pr_approved = await forge.get_pr(pr_ref)
    assert LABEL_READY in pr_approved.labels  # PR is now APPROVED

    # Only one harness.dispatch call in the full lifecycle
    assert len(harness.dispatch_calls) == 1


# ---------------------------------------------------------------------------
# §4.2 row 6 — pull_request_review_comment @claude, PR has LABEL_IMPLEMENTING → dispatch
# ---------------------------------------------------------------------------


async def test_dispatch_pr_review_comment_triggers_dispatch() -> None:
    """pull_request_review_comment on PR with LABEL_IMPLEMENTING → Sonnet/30 dispatch."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="acme", name="service")
    pr_ref = PRRef(repo=repo, number=8)

    # PR carries LABEL_IMPLEMENTING (BUILDING state)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], draft=True)

    engine = _engine(forge, harness, session)
    handle = await engine.dispatch(
        "pull_request_review_comment",
        pr_ref=pr_ref,
        comment_body="@claude please address this nit",
    )

    assert handle is not None
    assert len(harness.dispatch_calls) == 1
    ctx = harness.dispatch_calls[0]
    assert ctx.model == "claude-sonnet-4-6"
    assert ctx.max_turns == 30
    assert ctx.pr_ref == pr_ref


# ---------------------------------------------------------------------------
# §4.2 row 7 — issue_comment @claude; issue has only LABEL_TRIAGE → no dispatch (H5 guard)
# ---------------------------------------------------------------------------


async def test_dispatch_no_dispatch_without_agent_work_label() -> None:
    """issue_comment @claude; issue has only LABEL_TRIAGE (no LABEL_AGENT_WORK) → no dispatch."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    session = FakeSessionPort()
    repo = RepoRef(owner="acme", name="service")
    issue_ref = IssueRef(repo=repo, number=30)

    # Issue has only LABEL_TRIAGE — no LABEL_AGENT_WORK
    forge.seed_issue(issue_ref, labels=[LABEL_TRIAGE])

    engine = _engine(forge, harness, session)
    handle = await engine.dispatch(
        "issue_comment",
        issue_ref=issue_ref,
        comment_body="@claude can you look at this?",
    )

    # H5 guard fires: no harness dispatch
    assert handle is None
    assert len(harness.dispatch_calls) == 0
