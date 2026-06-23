"""Regression locks for SPEC §11.1 comment-dispatch gate.

Covers the 4-part gate introduced to stop the per-comment orchestrator spawn /
self-trigger loop:

  1. action == "created"
  2. comment body contains the configured @<GITHUB_BOT_LOGIN> mention
  3. author is NOT a bot / NOT the orchestrator itself  (loop-prevention)
  4. _try_claim_dispatch(issue_ref) guard passes (in-flight dedup)

Live bug: every issue_comment / pull_request_review_comment fired the unguarded
``else`` branch, spawning a fresh Sonnet orchestrator for each comment including
the bot's own replies — a self-amplifying loop.

GITHUB_BOT_LOGIN env-var controls the mention trigger:
  - Set → "@<GITHUB_BOT_LOGIN>"   (e.g. "@orecchiette1111")
  - Unset → "@claude"  (backward-compat fallback)
"""

from __future__ import annotations

import asyncio

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

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO = RepoRef(owner="acme", name="service")


def _make_service(
    forge: FakeForgePort | None = None,
    harness: FakeHarnessPort | None = None,
    allowlist: list[str] | None = None,
) -> OrchestratorService:
    forge = forge or FakeForgePort()
    harness = harness or FakeHarnessPort()
    svc = OrchestratorService(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        audit=AuditLog(),
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
        allowlist=allowlist,
    )
    return svc


def _issue(n: int) -> IssueRef:
    return IssueRef(repo=REPO, number=n)


def _pr(n: int) -> PRRef:
    return PRRef(repo=REPO, number=n)


def _issue_comment_payload(
    issue_n: int,
    body: str = "@orecchiette1111 please fix this",
    action: str = "created",
    author_login: str = "tuckermclean",
    author_type: str = "User",
    labels: list[str] | None = None,
) -> dict[str, object]:
    # Real GitHub issue_comment payloads carry the full issue object incl. labels;
    # the work-label gate reads them from the payload (no extra forge round-trip).
    label_objs = [{"name": n} for n in (labels if labels is not None else [LABEL_AGENT_WORK])]
    return {
        "repository": {"owner": {"login": "acme"}, "name": "service"},
        "issue": {"number": issue_n, "labels": label_objs},
        "action": action,
        "comment": {
            "body": body,
            "user": {"login": author_login, "type": author_type},
        },
    }


def _pr_review_comment_payload(
    pr_n: int,
    body: str = "@orecchiette1111 please fix this",
    action: str = "created",
    author_login: str = "tuckermclean",
    author_type: str = "User",
    labels: list[str] | None = None,
) -> dict[str, object]:
    label_objs = [{"name": n} for n in (labels if labels is not None else [LABEL_IMPLEMENTING])]
    return {
        "repository": {"owner": {"login": "acme"}, "name": "service"},
        "pull_request": {"number": pr_n, "labels": label_objs},
        "action": action,
        "comment": {
            "body": body,
            "user": {"login": author_login, "type": author_type},
        },
    }


# ---------------------------------------------------------------------------
# Gate test 1 — valid @mention from User author → dispatches exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_mention_from_user_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """issue_comment created, body has @orecchiette1111, author type==User → dispatches once.

    GITHUB_BOT_LOGIN=orecchiette1111 so the trigger is "@orecchiette1111".
    """
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(1)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=1,
        body="@orecchiette1111 please fix this",
        action="created",
        author_login="tuckermclean",
        author_type="User",
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-001")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 1, (
        "Valid @mention from User author must dispatch exactly once"
    )
    ctx = harness.dispatch_calls[0]
    # comment events route to Sonnet/30 (SPEC §8.1 row-2)
    assert ctx.model == "claude-sonnet-4-6"
    assert ctx.max_turns == 30


# ---------------------------------------------------------------------------
# Gate test 2 — BOT author → NO dispatch (loop prevention — critical lock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_from_bot_author_no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """issue_comment created, body has @orecchiette1111, author type==Bot → NO dispatch.

    This is the key regression lock for the self-trigger loop.  The orchestrator
    comments as orecchiette1111[bot] (type==Bot).  Its own comments MUST NOT
    spawn a new orchestrator run.
    """
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(2)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=2,
        body="@orecchiette1111 please fix this — posted by bot",
        action="created",
        author_login="orecchiette1111[bot]",
        author_type="Bot",  # orchestrator always comments as Bot
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-002")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, (
        "Bot author MUST NOT trigger dispatch — self-trigger loop prevention"
    )


# ---------------------------------------------------------------------------
# Gate test 3 — mention absent → no dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_without_mention_no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """issue_comment created, body does NOT contain the @mention → no dispatch."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(3)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=3,
        body="This is a regular comment with no bot mention",
        action="created",
        author_login="tuckermclean",
        author_type="User",
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-003")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, "Missing mention must produce no dispatch"


# ---------------------------------------------------------------------------
# Gate test 4 — action edited → no dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_edited_action_no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """issue_comment edited (not created) → no dispatch even with valid mention."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(4)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=4,
        body="@orecchiette1111 please also fix tests",
        action="edited",
        author_login="tuckermclean",
        author_type="User",
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-004")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, "action=edited must not dispatch"


# ---------------------------------------------------------------------------
# Gate test 5 — action deleted → no dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_deleted_action_no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """issue_comment deleted → no dispatch."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(5)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=5,
        body="@orecchiette1111 do this",
        action="deleted",
        author_login="tuckermclean",
        author_type="User",
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-005")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, "action=deleted must not dispatch"


# ---------------------------------------------------------------------------
# Gate test 6 — two rapid valid @mentions on same issue → second deduped by claim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_rapid_mentions_same_issue_deduped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two rapid valid @mention created comments on the same issue → exactly one dispatch.

    The second comment passes gates 1-3 but is rejected by gate 4
    (_try_claim_dispatch) because the first dispatch is still in-flight.
    """
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(6)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload1 = _issue_comment_payload(
        issue_n=6,
        body="@orecchiette1111 please fix this",
        action="created",
        author_login="tuckermclean",
        author_type="User",
    )
    payload2 = _issue_comment_payload(
        issue_n=6,
        body="@orecchiette1111 also add tests",
        action="created",
        author_login="tuckermclean",
        author_type="User",
    )

    result1 = await svc.handle_event("issue_comment", payload1, delivery_id="d-006a")
    result2 = await svc.handle_event("issue_comment", payload2, delivery_id="d-006b")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert result1["handled"] is True
    assert result2["handled"] is True
    assert len(harness.dispatch_calls) == 1, (
        "Second rapid @mention on same issue must be deduped by _try_claim_dispatch"
    )


# ---------------------------------------------------------------------------
# Gate test 7 — unknown / unrelated event → no-op (no dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_event_noop() -> None:
    """Unknown/unrelated event → no dispatch (SPEC §11.1 'anything else → no-op')."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload: dict[str, object] = {
        "repository": {"owner": {"login": "acme"}, "name": "service"},
        "action": "created",
    }

    result = await svc.handle_event("push", payload, delivery_id="d-007")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, "Unknown event must produce no dispatch"


@pytest.mark.asyncio
async def test_star_event_noop() -> None:
    """watch/star event → no dispatch (should not have ever dispatched)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload: dict[str, object] = {
        "repository": {"owner": {"login": "acme"}, "name": "service"},
        "action": "started",
    }

    result = await svc.handle_event("watch", payload, delivery_id="d-007b")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0


# ---------------------------------------------------------------------------
# Gate test 8 — GITHUB_BOT_LOGIN unset → "@claude" fallback works; bot still filtered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_at_claude_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """When GITHUB_BOT_LOGIN is unset, '@claude' is the fallback mention trigger."""
    monkeypatch.delenv("GITHUB_BOT_LOGIN", raising=False)

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(8)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=8,
        body="@claude please look at this",
        action="created",
        author_login="tuckermclean",
        author_type="User",
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-008")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 1, (
        "@claude fallback must dispatch when GITHUB_BOT_LOGIN is unset"
    )


@pytest.mark.asyncio
async def test_fallback_bot_author_still_filtered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even with GITHUB_BOT_LOGIN unset, a Bot author type is still filtered out."""
    monkeypatch.delenv("GITHUB_BOT_LOGIN", raising=False)

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(9)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=9,
        body="@claude I did the work",
        action="created",
        author_login="some-bot[bot]",
        author_type="Bot",
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-009")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, (
        "Bot author MUST be filtered even when GITHUB_BOT_LOGIN is unset"
    )


# ---------------------------------------------------------------------------
# Gate test 9 — case-insensitive @mention matching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mention_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """@ORECCHIETTE1111 (uppercase) matches trigger @orecchiette1111 (case-insensitive)."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(10)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=10,
        body="@ORECCHIETTE1111 please address this",  # uppercase
        action="created",
        author_login="tuckermclean",
        author_type="User",
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-010")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 1, "Mention matching must be case-insensitive"


# ---------------------------------------------------------------------------
# Gate test 10 — pull_request_review_comment with valid mention → dispatches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_review_comment_mention_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """pull_request_review_comment created, body has @mention, User author → dispatches."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    pr_ref = _pr(11)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], draft=True)

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _pr_review_comment_payload(
        pr_n=11,
        body="@orecchiette1111 please fix this nit",
        action="created",
        author_login="tuckermclean",
        author_type="User",
    )

    result = await svc.handle_event(
        "pull_request_review_comment", payload, delivery_id="d-011"
    )
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 1, (
        "Valid @mention from User on PR review comment must dispatch"
    )


# ---------------------------------------------------------------------------
# Gate test 11 — pull_request_review_comment from Bot → no dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_review_comment_from_bot_no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """pull_request_review_comment from Bot author → no dispatch (loop prevention)."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    pr_ref = _pr(12)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], draft=True)

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _pr_review_comment_payload(
        pr_n=12,
        body="@orecchiette1111 I've made the suggested changes",
        action="created",
        author_login="orecchiette1111[bot]",
        author_type="Bot",
    )

    result = await svc.handle_event(
        "pull_request_review_comment", payload, delivery_id="d-012"
    )
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, (
        "Bot author on PR review comment must NOT dispatch — self-trigger loop prevention"
    )


# ---------------------------------------------------------------------------
# Delivery-ID dedup still works with the new comment gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_delivery_id_dedup_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Duplicate delivery_id on a valid comment is rejected before routing.

    The delivery-ID dedup guard (SPEC §11.3 step 1) runs before the comment
    gate — a re-delivered valid @mention returns handled=False, no second dispatch.
    """
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(13)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=13,
        body="@orecchiette1111 please fix this",
        action="created",
        author_login="tuckermclean",
        author_type="User",
    )

    # First delivery
    result1 = await svc.handle_event("issue_comment", payload, delivery_id="dup-001")
    await asyncio.sleep(0)
    assert result1["handled"] is True
    assert len(harness.dispatch_calls) == 1

    # Same delivery_id → dedup rejects before even reaching comment gate
    result2 = await svc.handle_event("issue_comment", payload, delivery_id="dup-001")
    assert result2["handled"] is False
    assert result2.get("reason") == "duplicate_delivery_id"
    assert len(harness.dispatch_calls) == 1, "Duplicate delivery must not trigger second dispatch"


# ---------------------------------------------------------------------------
# Existing issues:opened and issues:labeled paths are unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_comment_promotes_non_work_issue_authorized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC §11.1 (Bug 2): an authorized @mention on a non-work issue PROMOTES it.

    An owner/allowlisted @mention on an issue NOT yet carrying LABEL_AGENT_WORK
    (e.g. an awaiting-promotion follow-up issue) acts as an explicit human
    promotion: agent-work is applied (swapping out awaiting-promotion per I7) and
    the orchestrator dispatches.  This intentionally bypasses the triager
    content-gate because the human explicitly asked.
    """
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(14)
    forge.seed_issue(issue_ref, labels=[LABEL_AWAITING_PROMOTION])
    svc = _make_service(forge=forge, harness=harness, allowlist=["tuckermclean"])
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=14,
        body="@orecchiette1111 please pick this up",
        action="created",
        author_login="tuckermclean",
        author_type="User",
        labels=[LABEL_AWAITING_PROMOTION],  # NOT agent-work yet
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-014")
    await asyncio.sleep(0)

    assert result["handled"] is True
    # Promotion dispatches the orchestrator exactly once.
    assert len(harness.dispatch_calls) == 1, (
        "Authorized @mention on a non-work issue must promote + dispatch"
    )
    # I7: agent-work applied, awaiting-promotion swapped out (PUT label semantics).
    final = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK in final.labels
    assert LABEL_AWAITING_PROMOTION not in final.labels


@pytest.mark.asyncio
async def test_comment_promote_non_work_issue_unauthorized_no_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC §11.1 (Bug 2): a NON-allowlisted @mention on a non-work issue → no promote.

    The promotion is a human override gated by the actor allowlist; a non-listed
    actor neither promotes nor dispatches.
    """
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(17)
    forge.seed_issue(issue_ref, labels=[LABEL_AWAITING_PROMOTION])
    # allowlist excludes the comment author.
    svc = _make_service(forge=forge, harness=harness, allowlist=["someone-else"])
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=17,
        body="@orecchiette1111 please pick this up",
        action="created",
        author_login="tuckermclean",
        author_type="User",
        labels=[LABEL_AWAITING_PROMOTION],
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-017")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, (
        "Non-allowlisted actor must not promote a non-work issue"
    )
    final = await forge.get_issue(issue_ref)
    assert LABEL_AGENT_WORK not in final.labels
    assert LABEL_AWAITING_PROMOTION in final.labels


@pytest.mark.asyncio
async def test_comment_no_promote_closed_issue(monkeypatch: pytest.MonkeyPatch) -> None:
    """SPEC §11.1 (Bug 2): a closed non-work issue is never auto-promoted."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(18)
    forge.seed_issue(issue_ref, labels=[LABEL_AWAITING_PROMOTION], closed=True)
    svc = _make_service(forge=forge, harness=harness, allowlist=["tuckermclean"])
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=18,
        body="@orecchiette1111 reopen and work this",
        action="created",
        author_login="tuckermclean",
        author_type="User",
        labels=[LABEL_AWAITING_PROMOTION],
    )
    # Mark the subject closed in the payload (GitHub sends issue.state).
    issue_obj = payload["issue"]
    assert isinstance(issue_obj, dict)
    issue_obj["state"] = "closed"

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-018")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, "Closed issue must not be auto-promoted"


# ---------------------------------------------------------------------------
# Bug 1 — issue_comment on a PR (payload.issue.pull_request present)
# ---------------------------------------------------------------------------


def _issue_comment_on_pr_payload(
    pr_n: int,
    body: str = "@orecchiette1111 address those suggestions",
    action: str = "created",
    author_login: str = "tuckermclean",
    author_type: str = "User",
    labels: list[str] | None = None,
) -> dict[str, object]:
    """GitHub delivers a comment on a PR conversation as an issue_comment event.

    The issue object carries a ``pull_request`` key and the PR's labels
    (e.g. agent:implementing) — NOT agent-work.
    """
    label_objs = [{"name": n} for n in (labels if labels is not None else [LABEL_IMPLEMENTING])]
    return {
        "repository": {"owner": {"login": "acme"}, "name": "service"},
        "issue": {
            "number": pr_n,
            "labels": label_objs,
            "pull_request": {"url": f"https://api.github.com/repos/acme/service/pulls/{pr_n}"},
        },
        "action": action,
        "comment": {
            "body": body,
            "user": {"login": author_login, "type": author_type},
        },
    }


@pytest.mark.asyncio
async def test_issue_comment_on_pr_dispatches_as_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 1 regression lock: @mention on a PR conversation dispatches, routed as PR.

    GitHub delivers it as an issue_comment with payload.issue.pull_request present
    and the PR's labels (agent:implementing).  The old gate required agent-work and
    rejected it.  The fix detects the PR and requires LABEL_IMPLEMENTING instead,
    routing the dispatch as a PR.
    """
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    pr_ref = _pr(45)
    forge.seed_pr(pr_ref, labels=[LABEL_IMPLEMENTING], draft=True)

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_on_pr_payload(
        pr_n=45,
        body="@orecchiette1111 please address those 7 suggestions",
        labels=[LABEL_IMPLEMENTING, "agent:ready"],
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-045")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 1, (
        "@mention on a PR conversation (issue_comment-on-PR) must dispatch"
    )
    ctx = harness.dispatch_calls[0]
    # Routed as a PR: the dispatch context carries pr_ref, not issue_ref.
    assert ctx.pr_ref == pr_ref
    assert ctx.issue_ref is None


@pytest.mark.asyncio
async def test_issue_comment_on_pr_without_implementing_no_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An issue_comment on a PR NOT carrying agent:implementing → no dispatch."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    pr_ref = _pr(46)
    forge.seed_pr(pr_ref, labels=["agent:ready"], draft=True)

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload = _issue_comment_on_pr_payload(
        pr_n=46,
        body="@orecchiette1111 do something",
        labels=["agent:ready"],  # no agent:implementing
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-046")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, (
        "issue_comment on a PR without agent:implementing must not dispatch"
    )


@pytest.mark.asyncio
async def test_comment_actor_not_in_allowlist_no_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SPEC §11.1: when the repo allowlist is non-empty, a non-listed actor → no dispatch."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    # allowlist excludes "tuckermclean"
    svc = _make_service(forge=forge, harness=harness, allowlist=["someone-else"])
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=15,
        body="@orecchiette1111 please fix this",
        action="created",
        author_login="tuckermclean",
        author_type="User",
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-015")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 0, (
        "Actor not in a non-empty allowlist must not dispatch"
    )


@pytest.mark.asyncio
async def test_comment_actor_in_allowlist_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the actor IS in the allowlist, a valid @mention dispatches."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    forge.seed_issue(_issue(16), labels=[LABEL_AGENT_WORK])
    svc = _make_service(forge=forge, harness=harness, allowlist=["tuckermclean"])
    await svc._audit.init()

    payload = _issue_comment_payload(
        issue_n=16,
        body="@orecchiette1111 please fix this",
        action="created",
        author_login="tuckermclean",
        author_type="User",
    )

    result = await svc.handle_event("issue_comment", payload, delivery_id="d-016")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 1, "Allowlisted actor with valid @mention must dispatch"


@pytest.mark.asyncio
async def test_issues_opened_intake_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Existing issues:opened path is unaffected by the comment gate change."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(20)
    forge.seed_issue(issue_ref, labels=[])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload: dict[str, object] = {
        "repository": {"owner": {"login": "acme"}, "name": "service"},
        "issue": {"number": 20},
        "action": "opened",
    }

    result = await svc.handle_event("issues", payload, delivery_id="intake-001")
    await asyncio.sleep(0)

    assert result["handled"] is True
    # Intake runs the triager (one dispatch)
    assert len(harness.dispatch_calls) == 1


@pytest.mark.asyncio
async def test_issues_labeled_agent_work_dispatch_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing issues:labeled agent-work dispatch is unaffected by the comment gate."""
    monkeypatch.setenv("GITHUB_BOT_LOGIN", "orecchiette1111")

    forge = FakeForgePort()
    harness = FakeHarnessPort()
    issue_ref = _issue(21)
    forge.seed_issue(issue_ref, labels=[LABEL_AGENT_WORK])

    svc = _make_service(forge=forge, harness=harness)
    await svc._audit.init()

    payload: dict[str, object] = {
        "repository": {"owner": {"login": "acme"}, "name": "service"},
        "issue": {"number": 21},
        "action": "labeled",
        "label": {"name": LABEL_AGENT_WORK},
    }

    result = await svc.handle_event("issues", payload, delivery_id="lbl-001")
    await asyncio.sleep(0)

    assert result["handled"] is True
    assert len(harness.dispatch_calls) == 1
    ctx = harness.dispatch_calls[0]
    assert ctx.model == "claude-opus-4-8"
    assert ctx.max_turns == 40
