"""Integration test — converge R1 approve happy path (SPEC §10.2 / TESTING.md §4.3)."""

from __future__ import annotations

import pytest

from src.decisions.decide_specialists import decide_specialists
from src.domain.types import (
    BLOCKING_CI_CHECKS,
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
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=len(changed_files))
    forge._changed_files[forge._pr_key(_PR)] = changed_files
    for name in BLOCKING_CI_CHECKS:
        forge.seed_check_run(_PR, name, "completed", "success")


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
    # Approving review posted.
    assert any(event == "APPROVE" for _ref, event, _body in forge.create_review_calls)


async def test_converge_sentinel_seeded_before_reviewer_dispatch() -> None:
    """Sentinel is written to .converge-verdict.json before the reviewer is dispatched."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    # put_file_on_branch(sentinel) must precede the reviewer dispatch.
    assert len(forge.put_file_on_branch_calls) >= 1
    sentinel_call = forge.put_file_on_branch_calls[0]
    assert sentinel_call[1] == ".converge-verdict.json"
    assert b"verdict-file-not-written" in sentinel_call[2]


async def test_converge_verdict_copied_per_round() -> None:
    """After the round, .converge-verdict.json is copied to .converge-verdict-r1.json (B3)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    assert (_PR, ".converge-verdict.json", ".converge-verdict-r1.json") in (
        forge.copy_file_on_branch_calls
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
    """
    # Force an immediate timeout in _await_run (deadline = now + 0).
    monkeypatch.setattr(dispatch_mod, "CI_WAIT_S", 0)

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    harness.never_completes = True  # reviewer run stays in_progress → _await_run times out
    _green_pr(forge, changed_files=["src/foo.py"])
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    # The reviewer was dispatched, timed out, and was cancelled with its own handle.
    assert len(harness.dispatch_calls) == 1
    assert len(harness.cancel_calls) == 1
    reviewer_run_id = "fake-run-1"
    assert harness.cancel_calls[0].run_id == reviewer_run_id
    # Sentinel verdict survives (reviewer never wrote one) → non-approve → escalates.
    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls
