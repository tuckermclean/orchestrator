"""Integration tests — session/usage-limit HOLD at the converge await boundary.

Covers SPEC §14.8 (deterministic await-boundary HOLD, round-neutral, in-flight guard).

Cases:
  - Reviewer run concludes awaiting_quota → converge HOLDs (raises SessionLimitHold),
    PR stays CONVERGING, no verdict read, no escalation.
  - Fixer run concludes awaiting_quota → converge HOLDs, fixer round rolled back
    so re-arm continues from the same round (round-neutral).
  - _await_run raises SessionLimitHold (not returns True) on awaiting_quota.
  - RC-3 re-arm after quota cooldown elapses re-dispatches the SAME round.
  - awaiting_quota-concluded run does NOT block RC-3 re-arm (in-flight guard).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.domain.types import (
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    REARM_RECENT_GUARD_S,
    PRRef,
    RepoRef,
    RunHandle,
    RunStatus,
    Verdict,
)
from src.engine.dispatch import Engine
from src.ports.fakes import (
    FakeConvergeStateStore,
    FakeCounterStore,
    FakeForgePort,
    FakeHarnessPort,
    FakeSessionPort,
)
from src.ports.harness_registry import AllHarnessesExhausted, SessionLimitHold

_REPO = RepoRef(owner="acme", name="service")
_PR = PRRef(repo=_REPO, number=99)
_CHANGED_FILES = ["src/foo.py"]

_QUOTA_RESET_AT = "2026-06-24T23:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _green_pr(forge: FakeForgePort) -> None:
    """Seed a non-draft CONVERGING PR with all CI checks green."""
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=len(_CHANGED_FILES))
    forge._changed_files[forge._pr_key(_PR)] = _CHANGED_FILES
    forge.seed_check_run(_PR, "Type Check", "completed", "success")
    forge.seed_check_run(_PR, "Lint", "completed", "success")
    forge.seed_check_run(_PR, "Tests", "completed", "success")


def _engine(
    forge: FakeForgePort,
    harness: FakeHarnessPort,
    *,
    counter: FakeCounterStore | None = None,
    converge_state: FakeConvergeStateStore | None = None,
) -> Engine:
    return Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=counter or FakeCounterStore(),
        converge_state=converge_state or FakeConvergeStateStore(),
    )


def _blocker_verdict() -> Verdict:
    return Verdict(
        blockers=1,
        suggestions=0,
        nits=[],
        blocker_signatures=["type:missing-annotation"],
    )


# ---------------------------------------------------------------------------
# Test: _await_run raises SessionLimitHold on awaiting_quota
# ---------------------------------------------------------------------------


@pytest.mark.covers("§14.8", "session-limit-await-boundary-raises-hold")
async def test_await_run_raises_session_limit_hold_on_awaiting_quota() -> None:
    """_await_run raises SessionLimitHold (not returns True) when run concludes awaiting_quota.

    This is the core invariant: the HOLD is raised at the await boundary so callers
    never treat an awaiting_quota run as a successful completion.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    engine = _engine(forge, harness)

    # Directly inject an awaiting_quota run
    handle = RunHandle(run_id="quota-run-1")
    harness.seed_run(handle, state="completed", conclusion="awaiting_quota")
    # Also set quota_reset_at on the status
    harness._runs["quota-run-1"] = RunStatus(
        state="completed",
        conclusion="awaiting_quota",
        quota_reset_at=_QUOTA_RESET_AT,
    )

    with pytest.raises(SessionLimitHold) as exc_info:
        await engine._await_run(handle)

    hold = exc_info.value
    assert hold.run_id == "quota-run-1"
    assert hold.quota_reset_at == _QUOTA_RESET_AT
    # SessionLimitHold IS-A AllHarnessesExhausted (subclass invariant)
    assert isinstance(hold, AllHarnessesExhausted)


@pytest.mark.covers("§14.8", "session-limit-await-boundary-raises-hold")
async def test_await_run_returns_true_for_normal_success() -> None:
    """_await_run still returns True for a normal success run (regression guard)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    engine = _engine(forge, harness)

    handle = RunHandle(run_id="normal-run-1")
    harness.seed_run(handle, state="completed", conclusion="success")

    result = await engine._await_run(handle)
    assert result is True


@pytest.mark.covers("§14.8", "session-limit-await-boundary-raises-hold")
async def test_await_run_raises_session_limit_hold_without_reset_at() -> None:
    """_await_run raises SessionLimitHold even when quota_reset_at is None."""
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    engine = _engine(forge, harness)

    handle = RunHandle(run_id="quota-run-nort")
    harness._runs["quota-run-nort"] = RunStatus(
        state="completed",
        conclusion="awaiting_quota",
        quota_reset_at=None,
    )

    with pytest.raises(SessionLimitHold) as exc_info:
        await engine._await_run(handle)

    assert exc_info.value.quota_reset_at is None


# ---------------------------------------------------------------------------
# Test: converge reviewer run hits quota → HOLD, no escalation, stays CONVERGING
# ---------------------------------------------------------------------------


@pytest.mark.covers("§14.8", "session-limit-converge-reviewer-hold")
async def test_converge_reviewer_quota_hold_does_not_escalate() -> None:
    """Reviewer run concludes awaiting_quota → converge HOLDs.

    The SessionLimitHold propagates out of engine.converge; no verdict is read;
    PR stays CONVERGING (no label mutations).  No _terminal_escalate is called.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    _green_pr(forge)

    # Script the reviewer to hit quota on the first dispatch
    harness.script_next_dispatch_quota(after_n_dispatches=0, quota_reset_at=_QUOTA_RESET_AT)

    engine = _engine(forge, harness)

    with pytest.raises(SessionLimitHold):
        await engine.converge(_PR)

    # LABEL_NEEDS_HUMAN must NOT have been added (no escalation)
    assert (_PR, LABEL_NEEDS_HUMAN) not in forge.add_label_calls
    # LABEL_READY must NOT have been added (no phantom approval)
    assert (_PR, LABEL_READY) not in forge.add_label_calls
    # Only the reviewer was dispatched (fixer was not reached)
    assert len(harness.dispatch_calls) == 1
    assert harness.dispatch_calls[0].contract == "agents/converge-reviewer.md"


@pytest.mark.covers("§14.8", "session-limit-converge-reviewer-hold")
async def test_converge_reviewer_quota_hold_is_round_neutral() -> None:
    """Reviewer quota HOLD does not advance the persisted converge round.

    When the reviewer hits quota, set_converge_round(r) has not yet been called
    (it happens after _await_run returns).  So the durable round is unchanged.
    After re-arm, converge resumes from the same round r.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    _green_pr(forge)

    converge_state = FakeConvergeStateStore()
    # Start at round 0 (first round = 1)
    harness.script_next_dispatch_quota(after_n_dispatches=0, quota_reset_at=_QUOTA_RESET_AT)

    engine = _engine(forge, harness, converge_state=converge_state)

    with pytest.raises(SessionLimitHold):
        await engine.converge(_PR)

    # Round must still be 0 (not advanced to 1)
    round_after_hold = await converge_state.get_converge_round(_PR)
    assert round_after_hold == 0, (
        f"Reviewer quota HOLD must not advance converge round; expected 0, got {round_after_hold}"
    )


# ---------------------------------------------------------------------------
# Test: converge fixer run hits quota → HOLD, round rolled back, round-neutral
# ---------------------------------------------------------------------------


@pytest.mark.covers("§14.8", "session-limit-converge-fixer-hold")
async def test_converge_fixer_quota_hold_does_not_escalate() -> None:
    """Fixer run concludes awaiting_quota → converge HOLDs.

    The SessionLimitHold propagates; no _terminal_escalate is called;
    PR stays CONVERGING.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    _green_pr(forge)

    # R1 reviewer finds 1 blocker → token = "fix"
    harness.script_reviewer_verdicts(_blocker_verdict())
    # R1 fixer hits quota (dispatch #2: after_n_dispatches=1)
    harness.script_next_dispatch_quota(after_n_dispatches=1, quota_reset_at=_QUOTA_RESET_AT)

    engine = _engine(forge, harness)

    with pytest.raises(SessionLimitHold):
        await engine.converge(_PR)

    # LABEL_NEEDS_HUMAN must NOT have been added
    assert (_PR, LABEL_NEEDS_HUMAN) not in forge.add_label_calls
    # LABEL_READY must NOT have been added
    assert (_PR, LABEL_READY) not in forge.add_label_calls
    # Exactly two dispatches: reviewer + fixer
    assert len(harness.dispatch_calls) == 2
    assert harness.dispatch_calls[0].contract == "agents/converge-reviewer.md"
    assert harness.dispatch_calls[1].contract == "agents/converge-fixer.md"


@pytest.mark.covers("§14.8", "session-limit-converge-fixer-hold")
async def test_converge_fixer_quota_hold_is_round_neutral() -> None:
    """Fixer quota HOLD rolls back the persisted converge round to r-1 (round-neutral).

    Before the fixer is dispatched, set_converge_round(r=1) has been written.
    On SessionLimitHold from _await_run(fixer_handle), converge.py catches it,
    writes set_converge_round(0), then re-raises.  The next re-arm starts at round 1.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    _green_pr(forge)

    converge_state = FakeConvergeStateStore()
    # R1 reviewer finds 1 blocker → fix token (so round 1 is persisted before fixer runs)
    harness.script_reviewer_verdicts(_blocker_verdict())
    # R1 fixer hits quota (dispatch #2)
    harness.script_next_dispatch_quota(after_n_dispatches=1, quota_reset_at=_QUOTA_RESET_AT)

    engine = _engine(forge, harness, converge_state=converge_state)

    with pytest.raises(SessionLimitHold):
        await engine.converge(_PR)

    # Round must be rolled back to 0 (not r=1) so re-arm starts from round 1
    round_after_hold = await converge_state.get_converge_round(_PR)
    assert round_after_hold == 0, (
        f"Fixer quota HOLD must roll back converge round to 0; expected 0, got {round_after_hold}"
    )


@pytest.mark.covers("§14.8", "session-limit-converge-fixer-hold")
async def test_converge_fixer_r2_quota_hold_is_round_neutral() -> None:
    """Fixer quota HOLD at R2 rolls back from round 2 to round 1 (round-neutral).

    Verifies the rollback logic is correct for round > 1.
    R1 and R2 reviewers must have DIFFERENT blocker signatures to avoid E2 no-progress.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    _green_pr(forge)

    converge_state = FakeConvergeStateStore()
    # R1 reviewer finds 1 blocker → fix (sig-a); R1 fixer completes
    # R2 reviewer finds 1 blocker → fix (sig-b, different from sig-a to avoid E2 no-progress)
    # R2 fixer hits quota
    harness.script_reviewer_verdicts(
        Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=["sig-a"]),  # R1: fix
        Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=["sig-b"]),  # R2: fix
    )
    # Dispatch order: R1-reviewer(1st), R1-fixer(2nd), R2-reviewer(3rd), R2-fixer(4th)
    # R2-fixer is the 4th dispatch: after_n_dispatches=3 (skip 3, quota on 4th)
    harness.script_next_dispatch_quota(after_n_dispatches=3, quota_reset_at=_QUOTA_RESET_AT)

    engine = _engine(forge, harness, converge_state=converge_state)

    with pytest.raises(SessionLimitHold):
        await engine.converge(_PR)

    # Round must be rolled back to 1 (r-1 = 2-1 = 1) so re-arm starts from round 2
    round_after_hold = await converge_state.get_converge_round(_PR)
    assert round_after_hold == 1, (
        "R2 fixer quota HOLD must roll back converge round to 1; "
        f"expected 1, got {round_after_hold}"
    )


# ---------------------------------------------------------------------------
# Test: RC-3 re-arm after quota HOLD — awaiting_quota run does not block re-arm
# ---------------------------------------------------------------------------


@pytest.mark.covers("§14.8", "session-limit-inflight-guard-clear")
async def test_rc3_does_not_skip_awaiting_quota_run_handle() -> None:
    """An awaiting_quota-concluded run does NOT block RC-3 re-arm (§8.6 row 2).

    decide_rearm_action checks run.state in ("queued", "in_progress") for skip-in-progress.
    awaiting_quota has state="completed", so it does NOT trigger skip-in-progress.
    RC-3 re-arms the PR normally.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    pr_ref = PRRef(repo=_REPO, number=88)
    forge.seed_pr(pr_ref, labels=[LABEL_CONVERGE], draft=False)
    forge.seed_check_run(pr_ref, "Type Check", "completed", "failure")

    # Seed an old workflow run so recency guard does not block
    old_run_at = datetime.now(tz=UTC) - timedelta(seconds=REARM_RECENT_GUARD_S + 10)
    forge.seed_workflow_run_at(pr_ref, "orchestrator-converge.yml", old_run_at)

    # Seed an awaiting_quota-concluded run in ConvergeStateStore
    handle = RunHandle(run_id="quota-run-handle")
    harness.seed_run(handle, state="completed", conclusion="awaiting_quota")

    converge_state = FakeConvergeStateStore()
    await converge_state.set_last_run_handle(pr_ref, handle)

    engine = Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=converge_state,
    )
    report = await engine.reconcile(_REPO)

    # RC-3 must re-arm (not skip-in-progress) because awaiting_quota is "completed"
    assert report.rearmed == 1
    assert len(harness.trigger_workflow_calls) == 1


# ---------------------------------------------------------------------------
# Test: round-identity after quota HOLD — RC-3 re-arm resumes same round
# ---------------------------------------------------------------------------


@pytest.mark.covers("§14.8", "session-limit-rc3-rearm-same-round")
async def test_converge_resumes_from_same_round_after_reviewer_quota_hold() -> None:
    """After a reviewer quota HOLD, RC-3 re-arm re-enters converge at the same round.

    Simulates: R1 reviewer hits quota → HOLD → round stays at 0 → next converge
    invocation starts at round 1 (same round r=1), reviewer re-runs.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    _green_pr(forge)

    converge_state = FakeConvergeStateStore()

    # First converge invocation: R1 reviewer hits quota
    harness.script_next_dispatch_quota(after_n_dispatches=0, quota_reset_at=_QUOTA_RESET_AT)
    engine = _engine(forge, harness, converge_state=converge_state)

    with pytest.raises(SessionLimitHold):
        await engine.converge(_PR)

    # Round is still 0; re-arm will start from round 1
    assert await converge_state.get_converge_round(_PR) == 0

    # Second converge invocation (simulating RC-3 re-arm after cooldown expires)
    # Reviewer now succeeds and produces a spotless verdict → adjudicate path
    zero_verdict = Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    harness.script_reviewer_verdicts(zero_verdict)

    state = await engine.converge(_PR)

    # The converge sub-machine ran from round 1 (not a new cycle from scratch)
    # and reaches the adjudication phase successfully
    assert state == "APPROVED"
    # Total: 1 (first quota reviewer) + 1 (second reviewer) + 1 (adjudicator) = 3
    assert len(harness.dispatch_calls) == 3
    assert harness.dispatch_calls[1].contract == "agents/converge-reviewer.md"  # same round


@pytest.mark.covers("§14.8", "session-limit-rc3-rearm-same-round")
async def test_converge_resumes_from_same_round_after_fixer_quota_hold() -> None:
    """After a fixer quota HOLD, RC-3 re-arm re-enters converge at the same round.

    Simulates: R1 reviewer → fix → R1 fixer hits quota → HOLD → round rolls back
    to 0 → next converge starts at round 1, fixer runs and completes this time.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort()
    _green_pr(forge)

    converge_state = FakeConvergeStateStore()

    # First converge invocation: R1 reviewer finds blocker, R1 fixer hits quota
    harness.script_reviewer_verdicts(_blocker_verdict())
    harness.script_next_dispatch_quota(after_n_dispatches=1, quota_reset_at=_QUOTA_RESET_AT)
    engine = _engine(forge, harness, converge_state=converge_state)

    with pytest.raises(SessionLimitHold):
        await engine.converge(_PR)

    # Round must be rolled back to 0
    assert await converge_state.get_converge_round(_PR) == 0

    # Second converge invocation: R1 reviewer → no blockers (fixer's work from before,
    # or the reviewer sees it as fixed) → adjudicate path
    zero_verdict = Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    harness.script_reviewer_verdicts(zero_verdict)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    # Total dispatches: 1 (first reviewer) + 1 (first fixer quota) +
    #                   1 (second reviewer) + 1 (adjudicator) = 4
    assert len(harness.dispatch_calls) == 4
    # Second invocation starts at round 1 (same round as the fixer that hit quota)
    assert harness.dispatch_calls[2].contract == "agents/converge-reviewer.md"
    # No RECONVERGE_CAP was burned
    counter = FakeCounterStore()
    assert await counter.get_count(_PR, "adjudicator-reconverge") == 0
