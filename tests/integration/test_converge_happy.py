"""Integration test — converge R1 approve happy path (SPEC §10.2 / TESTING.md §4.3)."""

from __future__ import annotations

import pytest

from src.decisions.decide_specialists import decide_specialists
from src.domain.types import (
    DEFAULT_SWARM_MODEL,
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    PRRef,
    RepoRef,
    Verdict,
)
from src.engine import dispatch as dispatch_mod
from src.engine.dispatch import Engine
from src.ports.fakes import (
    FakeConvergeStateStore,
    FakeCounterStore,
    FakeForgePort,
    FakeHarnessPort,
    FakeSessionPort,
)

_REPO = RepoRef(owner="acme", name="service")
_PR = PRRef(repo=_REPO, number=11)


def _green_pr(forge: FakeForgePort, *, changed_files: list[str]) -> None:
    """Seed a non-draft converge PR with a generic green CI check (no named allow-list)."""
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=len(changed_files))
    forge._changed_files[forge._pr_key(_PR)] = changed_files
    forge.seed_check_run(_PR, "CI", "completed", "success")


def _engine(
    forge: FakeForgePort,
    harness: FakeHarnessPort,
) -> Engine:
    return Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
    )


async def test_converge_approve_round1() -> None:
    """R1: reviewer emits 0-blocker verdict, CI green → APPROVED with label swap."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    # Reviewer dispatched at R1 with Sonnet / DEFAULT_SWARM_MODEL.
    assert len(harness.dispatch_calls) == 1
    reviewer_ctx = harness.dispatch_calls[0]
    assert reviewer_ctx.model == DEFAULT_SWARM_MODEL
    assert reviewer_ctx.contract == "agents/converge-reviewer.md"
    # allowed_agent_refs matches decide_specialists exactly (I9/D2).
    assert reviewer_ctx.allowed_agent_refs == decide_specialists(["src/foo.py"], 1)
    # Label swap.
    assert (_PR, LABEL_READY) in forge.add_label_calls
    assert (_PR, LABEL_CONVERGE) in forge.remove_label_calls
    # No formal GitHub APPROVE review — self-authored PRs 422 on APPROVE/REQUEST_CHANGES.
    # The agent:ready label IS the approval signal (SPEC §10.2 step 4c, amended).
    assert not any(event == "APPROVE" for _ref, event, _body in forge.create_review_calls), (
        "Engine must NOT post a formal GitHub APPROVE review on a self-authored PR "
        "(HTTP 422); approval is signaled by the agent:ready label"
    )


async def test_converge_no_sentinel_written_to_branch() -> None:
    """No sentinel commit is written to the PR branch (SPEC §5 anti-pattern fix).

    The verdict channel is now the reviewer's structured output via the harness
    RunEventStore — no file is committed to the PR branch before or after the round.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    # No put_file_on_branch calls — the sentinel seed is removed (SPEC §5).
    assert forge.put_file_on_branch_calls == [], (
        "Engine must NOT commit sentinel or verdict files to the PR branch"
    )
    # No copy_file_on_branch archival — per-round history is in the RunEventStore,
    # not on the PR branch (SPEC §10.2).
    assert forge.copy_file_on_branch_calls == [], (
        "Engine must NOT archive verdict files on the PR branch"
    )


async def test_converge_verdict_read_from_run_result() -> None:
    """The verdict is read from harness.get_run_verdict, not from a forge file (SPEC §5)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    # Engine reads verdict from the run result → approves (0 blockers, CI green).
    assert state == "APPROVED"
    # No verdict file was read from the forge branch.
    verdict_reads = [
        (pr, path)
        for pr, path in forge.get_file_contents_calls
        if "converge-verdict" in path
    ]
    assert verdict_reads == [], (
        "Engine must NOT read verdict from forge branch file"
    )


async def test_converge_clears_state_on_approve() -> None:
    """ConvergeStateStore is cleared and converge-retry counter reset on approve."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)
    assert isinstance(engine.converge_state, FakeConvergeStateStore)
    assert isinstance(engine.counter, FakeCounterStore)

    await engine.converge(_PR)

    assert _PR in engine.converge_state.clear_calls
    assert (_PR, "converge-retry") in engine.counter.reset_calls


async def test_converge_idempotency_gate_draft_pr() -> None:
    """A draft PR short-circuits before any reviewer dispatch (BUILDING)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    forge.seed_pr(_PR, draft=True, labels=[LABEL_CONVERGE])
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "BUILDING"
    assert harness.dispatch_calls == []


async def test_converge_idempotency_gate_approved() -> None:
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    forge.seed_pr(_PR, draft=False, labels=[LABEL_READY])
    engine = _engine(forge, harness)

    assert await engine.converge(_PR) == "APPROVED"
    assert harness.dispatch_calls == []


async def test_converge_nit_followup_issue() -> None:
    """Approve with nits opens a deduplicated follow-up issue."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=["nit-a", "nit-a"], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    assert len(forge.create_issue_calls) == 1
    _repo, _title, body = forge.create_issue_calls[0]
    assert body.count("nit-a") == 1


async def test_converge_reviewer_timeout_cancels_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On reviewer CI_WAIT_S timeout, the engine cancels the reviewer handle (SPEC §10.2 4b).

    A ghost reviewer must not complete later and overwrite the next round's sentinel/verdict.
    With never_completes=True the fixer also times out (R1 sentinel → fix → fixer timeout
    → E11) so there are 2 dispatches and 2 cancels; the final state is ESCALATED (E11).
    """
    # Force an immediate timeout in _await_run (deadline = now + 0).
    monkeypatch.setattr(dispatch_mod, "CI_WAIT_S", 0)

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    harness.never_completes = True  # all runs stay in_progress → _await_run times out
    _green_pr(forge, changed_files=["src/foo.py"])
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    # Reviewer dispatched + timed out; sentinel verdict (1 blocker) → fix → fixer dispatched
    # + timed out (E11).
    assert len(harness.dispatch_calls) == 2
    assert len(harness.cancel_calls) == 2
    reviewer_run_id = "fake-run-1"
    fixer_run_id = "fake-run-2"
    assert harness.cancel_calls[0].run_id == reviewer_run_id
    assert harness.cancel_calls[1].run_id == fixer_run_id
    # Fixer timeout → terminal_escalate(E11).
    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls

async def test_converge_pr_dispatch_uses_head_branch() -> None:
    """P0.4: reviewer and fixer DispatchContexts include head_branch from the PR.

    This ensures the harness checks out the actual PR branch rather than the
    default branch, so reviewers/fixers see the PR diff.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    # seed_pr sets head_branch="feature-branch" by default
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    assert harness.dispatch_calls, "No dispatch calls"
    reviewer_ctx = harness.dispatch_calls[0]
    assert reviewer_ctx.head_branch == "feature-branch", (
        f"P0.4: reviewer context must carry head_branch; got {reviewer_ctx.head_branch!r}"
    )


async def test_converge_approve_does_not_self_approve_via_github_review() -> None:
    """Regression: converge APPROVE path must NOT post a GitHub APPROVE review.

    The GitHub App is the PR author — posting {"event": "APPROVE"} on a self-authored PR
    returns HTTP 422, causing the converge task to raise, and the reconciler (RC-3) to
    re-arm the PR indefinitely (re-arm storm).  The fix: agent:ready label is the approval
    signal; no formal GitHub review is submitted (SPEC §10.2 step 4c, amended).

    This test locks in the fix: a FakeForgePort that raises on APPROVE is wired so any
    regression restores the 422 failure path.
    """
    class _Self422ForgePort(FakeForgePort):
        async def create_review(self, pr_ref: PRRef, event: str, body: str) -> None:
            if event == "APPROVE":
                raise RuntimeError(
                    "422 Unprocessable Entity: Can not approve your own pull request"
                )
            # Non-APPROVE events (e.g. COMMENT) are recorded normally.
            self.create_review_calls.append((pr_ref, event, body))
            self._reviews.append({"pr_ref": pr_ref, "event": event, "body": body})

    forge = _Self422ForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    # Must succeed (return APPROVED) without raising — the 422-raising forge port
    # proves that no APPROVE review is attempted.
    state = await engine.converge(_PR)

    assert state == "APPROVED", (
        "Converge must return APPROVED even when APPROVE reviews would 422 — "
        "the agent:ready label is the approval signal, not a GitHub review"
    )
    # Confirm the label swap happened (the real approval signal).
    assert (_PR, LABEL_READY) in forge.add_label_calls
    assert (_PR, LABEL_CONVERGE) in forge.remove_label_calls
    # Confirm no APPROVE review was attempted.
    assert not any(event == "APPROVE" for _ref, event, _body in forge.create_review_calls), (
        "Engine posted a forbidden APPROVE review on a self-authored PR"
    )
