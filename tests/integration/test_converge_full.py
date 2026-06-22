"""Integration tests — Engine.converge full 3-round loop and all escalation paths.

Covers SPEC §10.2 complete: fix (R1/R2), E2 no-progress, E3 no-verdict with retry/cap,
E4 ci-red recovery (pass and fail), E5 cap-reached, E11 fixer-timeout.
TESTING.md §4.3 (Engine.converge integration).
"""

from __future__ import annotations

import pytest

from src.decisions.decide_specialists import decide_specialists
from src.domain.types import (
    ADJUDICATION_MODEL,
    BLOCKING_CI_CHECKS,
    DEFAULT_SWARM_MODEL,
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    NO_VERDICT_RETRY_CAP,
    CheckRun,
    PRRef,
    RepoRef,
    Verdict,
)
from src.engine import converge as converge_mod
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
_PR = PRRef(repo=_REPO, number=42)
_CHANGED_FILES = ["src/foo.py"]


def _green_pr(forge: FakeForgePort, *, changed_files: list[str] | None = None) -> None:
    files = changed_files if changed_files is not None else _CHANGED_FILES
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=len(files))
    forge._changed_files[forge._pr_key(_PR)] = files
    for name in BLOCKING_CI_CHECKS:
        forge.seed_check_run(_PR, name, "completed", "success")


def _red_pr(forge: FakeForgePort, *, changed_files: list[str] | None = None) -> None:
    """PR with CI failing (no passing CI checks seeded)."""
    files = changed_files if changed_files is not None else _CHANGED_FILES
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=len(files))
    forge._changed_files[forge._pr_key(_PR)] = files


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


def _blocker_verdict(
    sigs: list[str] | None = None,
    *,
    blockers: int = 1,
    nits: list[str] | None = None,
) -> Verdict:
    return Verdict(
        blockers=blockers,
        suggestions=0,
        nits=nits or [],
        blocker_signatures=sigs or ["type:missing-annotation"],
    )


def _zero_verdict(*, nits: list[str] | None = None) -> Verdict:
    return Verdict(blockers=0, suggestions=0, nits=nits or [], blocker_signatures=[])


# ---------------------------------------------------------------------------
# R1 blocker → R1 fix → R2 approve (SPEC §5 happy path with fix)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3-integration", "row-2-fix-r1-engine-wired")
@pytest.mark.covers("§8.3-integration", "row-4-fix-r2-engine-wired")
async def test_converge_r1_blocker_fix_r2_approve() -> None:
    """R1 reviewer finds blockers → fixer dispatched → R2 reviewer approves → APPROVED."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)

    # R1 reviewer: 1 blocker; R2 reviewer: 0 blockers
    harness.script_reviewer_verdicts(
        _blocker_verdict(["type:missing-annotation"]),
        _zero_verdict(),
    )
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    # Reviewer R1, fixer, reviewer R2 = 3 dispatches
    assert len(harness.dispatch_calls) == 3
    reviewer_r1 = harness.dispatch_calls[0]
    fixer = harness.dispatch_calls[1]
    reviewer_r2 = harness.dispatch_calls[2]
    assert reviewer_r1.contract == "agents/converge-reviewer.md"
    assert reviewer_r1.model == DEFAULT_SWARM_MODEL
    assert fixer.contract == "agents/converge-fixer.md"
    assert fixer.model == DEFAULT_SWARM_MODEL
    assert reviewer_r2.contract == "agents/converge-reviewer.md"
    assert reviewer_r2.model == DEFAULT_SWARM_MODEL
    # Approved labels applied.
    assert (_PR, LABEL_READY) in forge.add_label_calls
    assert (_PR, LABEL_CONVERGE) in forge.remove_label_calls


@pytest.mark.covers("§8.3-integration", "row-2-fix-r1-engine-wired")
async def test_converge_r1_fixer_allowed_refs_match_specialists() -> None:
    """Fixer dispatch carries the same allowed_agent_refs as the reviewer (I9/D2)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/api/routes.py"])  # triggers api routing
    harness.script_reviewer_verdicts(
        _blocker_verdict(),
        _zero_verdict(),
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    fixer_ctx = harness.dispatch_calls[1]
    assert fixer_ctx.contract == "agents/converge-fixer.md"
    expected_refs = decide_specialists(["src/api/routes.py"], 1)
    assert fixer_ctx.allowed_agent_refs == expected_refs


@pytest.mark.covers("§8.3-integration", "row-4-fix-r2-engine-wired")
async def test_converge_r3_adjudication_model() -> None:
    """R3 reviewer uses ADJUDICATION_MODEL (Opus); R1/R2 use DEFAULT_SWARM_MODEL."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    # R1: fix, R2: fix, R3: approve (need 2 fixers too)
    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),
        _blocker_verdict(["sig-b"]),  # different sigs so not no-progress
        _zero_verdict(),
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    # dispatches: reviewer-r1, fixer-r1, reviewer-r2, fixer-r2, reviewer-r3
    assert len(harness.dispatch_calls) == 5
    assert harness.dispatch_calls[0].model == DEFAULT_SWARM_MODEL  # reviewer R1
    assert harness.dispatch_calls[1].model == DEFAULT_SWARM_MODEL  # fixer R1
    assert harness.dispatch_calls[2].model == DEFAULT_SWARM_MODEL  # reviewer R2
    assert harness.dispatch_calls[3].model == DEFAULT_SWARM_MODEL  # fixer R2
    assert harness.dispatch_calls[4].model == ADJUDICATION_MODEL   # reviewer R3


# ---------------------------------------------------------------------------
# E2 — no-progress (SPEC §5, §6)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3-integration", "row-3-no-progress-engine-wired")
@pytest.mark.covers("§6-escalations", "E2-no-progress")
async def test_converge_no_progress_e2() -> None:
    """R1 and R2 produce identical non-empty signatures → ESCALATED (E2)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    same_sig = ["type:missing-annotation"]
    harness.script_reviewer_verdicts(
        _blocker_verdict(same_sig),
        _blocker_verdict(same_sig),  # identical sigs after fixer → no-progress
    )
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls


@pytest.mark.covers("§8.3-integration", "row-3-no-progress-engine-wired")
@pytest.mark.covers("§6-escalations", "E2-no-progress")
async def test_converge_no_progress_e2_r3_fires_before_cap_reached() -> None:
    """R3 no-progress fires before cap-reached (row 3 > row 7 priority)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    same_sig = ["type:missing-annotation"]
    # R1 fix (new sig), R2 fix (same sig as R2 → no-progress R3)
    # Actually: R2 no-progress if R2 sigs == R1 sigs.
    # To test R3 no-progress: R1 sig-A, R2 sig-A (no-progress at R2) but R2 is Literal[2]
    # → R3: need R2 sig == R3 sig. Let's make R1 unique, R2 same as R1 (→ no-progress E2 at R2).
    # For R3 no-progress: make R1 fix, R2 advances (different sig), R3 same as R2.
    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-unique-r1"]),  # R1 → fix
        _blocker_verdict(same_sig),           # R2 → fix (different from R1)
        _blocker_verdict(same_sig),           # R3 → no-progress (same as R2)
    )
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls
    # counter.reset called (terminal_escalate)
    assert isinstance(engine.counter, FakeCounterStore)
    assert (_PR, "converge-retry") in engine.counter.reset_calls


# ---------------------------------------------------------------------------
# E3 — no-verdict (SPEC §5, §6)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3-integration", "row-5-no-verdict-engine-wired")
@pytest.mark.covers("§6-escalations", "E3-no-verdict")
async def test_converge_no_verdict_retry_then_cap_e3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 unknown blockers: retry < cap → CONVERGING (P11); at cap → ESCALATED (E3)."""
    # Simplification: test the final escalation path only (counter already at cap).
    forge2 = FakeForgePort()
    harness2 = FakeHarnessPort(forge=forge2)
    counter2 = FakeCounterStore()
    _green_pr(forge2)
    # Seed converge-retry counter at cap so P11 triggers E3 directly.
    counter2.seed_count(_PR, "converge-retry", NO_VERDICT_RETRY_CAP)

    # R1 fix, R2 fix. For R3: make reviewer time out → unknown → E3 (cap already reached).
    harness2.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),  # R1
        _blocker_verdict(["sig-b"]),  # R2
        # R3 reviewer times out; no verdict written.
    )
    # Make R3 reviewer (3rd dispatch) time out. Dispatches: reviewer-R1, fixer-R1,
    # reviewer-R2, fixer-R2, reviewer-R3 → 5th dispatch times out.
    monkeypatch.setattr(dispatch_mod, "CI_WAIT_S", 0)
    harness2.script_fixer_timeout(after_n_dispatches=4)  # 5th dispatch times out
    engine2 = _engine(forge2, harness2, counter=counter2)

    state = await engine2.converge(_PR)

    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge2.add_label_calls


@pytest.mark.covers("§8.3-integration", "row-5-no-verdict-engine-wired")
@pytest.mark.covers("§6-escalations", "E3-no-verdict")
async def test_converge_no_verdict_retry_below_cap_returns_converging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 unknown blockers with retry_count < cap → CONVERGING (P11), not escalated."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    counter = FakeCounterStore()
    _green_pr(forge)
    # counter at 0 (below NO_VERDICT_RETRY_CAP=2) → retry path (P11)
    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),  # R1
        _blocker_verdict(["sig-b"]),  # R2
    )
    # Make R3 reviewer timeout so resolve_blockers → "unknown"
    monkeypatch.setattr(dispatch_mod, "CI_WAIT_S", 0)
    harness.script_fixer_timeout(after_n_dispatches=4)  # 5th dispatch = reviewer R3
    engine = _engine(forge, harness, counter=counter)

    state = await engine.converge(_PR)

    assert state == "CONVERGING"
    # Re-arm comment posted.
    pr_comments = forge._comments.get(forge._entity_key(_PR), [])
    assert any("orchestrator:converge-retry" in c.body for c in pr_comments)
    # counter incremented (not reset).
    assert (_PR, "converge-retry") in counter.increment_calls
    assert (_PR, "converge-retry") not in counter.reset_calls


# ---------------------------------------------------------------------------
# E4 — ci-red recovery (SPEC §5, §6, OQ-1)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3-integration", "row-6-ci-red-engine-wired")
@pytest.mark.covers("§6-escalations", "E4-ci-red")
async def test_converge_ci_red_recovery_approve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 zero blockers but CI red → trigger_ci → CI recovers → APPROVED (P9)."""
    # Set CI_WAIT_S=0 in converge module so the poll exits immediately if not green.
    # Since we script the checks to become green immediately after trigger_ci, the
    # first poll sees green and returns True before the deadline check fires.
    monkeypatch.setattr(converge_mod, "CI_WAIT_S", 0)

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    # Start with CI red (no check runs seeded).
    _red_pr(forge)

    # R1 fix (blockers), R2 fix (different blockers), R3 zero blockers (CI still red).
    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),  # R1
        _blocker_verdict(["sig-b"]),  # R2
        _zero_verdict(),              # R3 → ci-red (CI not green at time of decide_round)
    )
    # After trigger_ci, script green checks — synchronously updates forge's check_runs.
    green_checks = [
        CheckRun(name=name, state="completed", conclusion="success")
        for name in BLOCKING_CI_CHECKS
    ]
    harness.script_trigger_ci_checks(green_checks)
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    assert len(harness.trigger_ci_calls) == 1
    assert (_PR, LABEL_READY) in forge.add_label_calls


@pytest.mark.covers("§8.3-integration", "row-6-ci-red-engine-wired")
@pytest.mark.covers("§6-escalations", "E4-ci-red")
async def test_converge_ci_red_retrigger_fails_e4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3 zero blockers, CI red → trigger_ci → CI stays red → ESCALATED (E4)."""
    monkeypatch.setattr(converge_mod, "CI_WAIT_S", 0)

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _red_pr(forge)

    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),  # R1
        _blocker_verdict(["sig-b"]),  # R2
        _zero_verdict(),              # R3 → ci-red
    )
    # No CI recovery: trigger_ci_check_scripts left empty → CI stays red.
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "ESCALATED"
    assert len(harness.trigger_ci_calls) == 1
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls


@pytest.mark.covers("§8.3-integration", "row-6-ci-red-engine-wired")
@pytest.mark.covers("§6-escalations", "E4-ci-red")
async def test_converge_ci_red_polls_all_six_blocking_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OQ-1: ci-red recovery re-polls ALL 6 BLOCKING_CI_CHECKS (not a subset)."""
    monkeypatch.setattr(converge_mod, "CI_WAIT_S", 0)

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _red_pr(forge)

    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),
        _blocker_verdict(["sig-b"]),
        _zero_verdict(),
    )
    # Script checks: only 5 of 6 green (one still failing) → CI not green → E4.
    partial_checks = [
        CheckRun(name=name, state="completed", conclusion="success")
        for name in list(BLOCKING_CI_CHECKS)[:5]
    ] + [
        CheckRun(
            name=BLOCKING_CI_CHECKS[5], state="completed", conclusion="failure"
        )
    ]
    harness.script_trigger_ci_checks(partial_checks)
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    # 5/6 green is not enough → ESCALATED (E4).
    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls


@pytest.mark.covers("§8.3-integration", "row-6-ci-red-engine-wired")
@pytest.mark.covers("§6-escalations", "E4-ci-red")
async def test_converge_ci_red_docker_still_red_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OQ-1 regression guard: code checks recover but Docker/Helm tier stays RED → E4.

    At R3 with 0 blockers, CI re-trigger recovers indices 0-2 (Type Check, Lint,
    Integration Tests) to green, but indices 3-5 (Docker Build & Scan, Helm Lint,
    Helm Kubeconform) stay red.  _poll_ci_until_green must return False because all
    6 BLOCKING_CI_CHECKS are required → ESCALATED (E4), not APPROVED (P9).

    SPEC §13 (OQ-1), TESTING.md §4.3.
    """
    monkeypatch.setattr(converge_mod, "CI_WAIT_S", 0)

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _red_pr(forge)

    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),  # R1
        _blocker_verdict(["sig-b"]),  # R2
        _zero_verdict(),              # R3 → ci-red (CI still not green at decide_round)
    )
    # After trigger_ci: code-check tier (indices 0-2) recovers; Docker/Helm tier stays red.
    partial_recovery_checks = [
        CheckRun(name=BLOCKING_CI_CHECKS[i], state="completed", conclusion="success")
        for i in range(3)  # Type Check, Lint, Integration Tests → green
    ] + [
        CheckRun(name=BLOCKING_CI_CHECKS[i], state="completed", conclusion="failure")
        for i in range(3, 6)  # Docker Build & Scan, Helm Lint, Helm Kubeconform → red
    ]
    harness.script_trigger_ci_checks(partial_recovery_checks)
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    # Docker/Helm checks still red → _poll_ci_until_green returns False → ESCALATED (E4).
    assert state == "ESCALATED"
    assert len(harness.trigger_ci_calls) == 1
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls
    assert (_PR, LABEL_READY) not in forge.add_label_calls


# ---------------------------------------------------------------------------
# E5 — cap-reached (SPEC §5, §6)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§8.3-integration", "row-7-cap-reached-engine-wired")
@pytest.mark.covers("§6-escalations", "E5-cap-reached")
async def test_converge_cap_reached_e5() -> None:
    """R3 remaining blockers → ESCALATED (E5, D3: always human)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)

    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),  # R1
        _blocker_verdict(["sig-b"]),  # R2 (different → not no-progress)
        _blocker_verdict(["sig-c"]),  # R3 (different → not no-progress, cap-reached)
    )
    counter = FakeCounterStore()
    engine = _engine(forge, harness, counter=counter)

    state = await engine.converge(_PR)

    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls
    # terminal_escalate resets the converge-retry counter.
    assert (_PR, "converge-retry") in counter.reset_calls


# ---------------------------------------------------------------------------
# E11 — fixer timeout (SPEC §6, §10.2)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§6-escalations", "E11-fixer-timeout")
async def test_converge_fixer_timeout_e11(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixer times out at R1 → harness.cancel(fixer_handle) → ESCALATED (E11)."""
    monkeypatch.setattr(dispatch_mod, "CI_WAIT_S", 0)

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)

    # R1 reviewer completes (blocker → fix), fixer times out (2nd dispatch).
    harness.script_reviewer_verdicts(_blocker_verdict())
    harness.script_fixer_timeout(after_n_dispatches=1)  # 2nd dispatch times out
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls
    # Fixer handle was cancelled.
    assert len(harness.cancel_calls) == 1
    fixer_run_id = "fake-run-2"
    assert harness.cancel_calls[0].run_id == fixer_run_id


@pytest.mark.covers("§6-escalations", "E11-fixer-timeout")
async def test_converge_fixer_timeout_r2_e11(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixer times out at R2 → ESCALATED (E11)."""
    monkeypatch.setattr(dispatch_mod, "CI_WAIT_S", 0)

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)

    # R1 fix (reviewer+fixer), R2 fix (reviewer then fixer times out).
    # Dispatches: r1-reviewer(1), r1-fixer(2), r2-reviewer(3), r2-fixer(4) times out.
    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),  # R1
        _blocker_verdict(["sig-b"]),  # R2 (different sig → not no-progress)
    )
    harness.script_fixer_timeout(after_n_dispatches=3)  # 4th dispatch times out
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls
    # 4 dispatches: r1-reviewer, r1-fixer, r2-reviewer, r2-fixer(timeout)
    assert len(harness.dispatch_calls) == 4
    assert len(harness.cancel_calls) == 1
    assert harness.cancel_calls[0].run_id == "fake-run-4"


# ---------------------------------------------------------------------------
# Nit follow-up issue created on approve; skipped when empty
# ---------------------------------------------------------------------------


async def test_converge_nit_issue_created_on_approve_with_nits() -> None:
    """Accumulated nits from multiple rounds are deduped and posted as a follow-up issue."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)

    harness.script_reviewer_verdicts(
        _blocker_verdict(nits=["nit-from-r1", "nit-dup"]),
        _zero_verdict(nits=["nit-from-r2", "nit-dup"]),  # nit-dup deduplicated
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    assert len(forge.create_issue_calls) == 1
    _repo, title, body = forge.create_issue_calls[0]
    assert title == "Converge follow-up nits"
    assert "nit-from-r1" in body
    assert "nit-from-r2" in body
    assert body.count("nit-dup") == 1  # deduplicated


async def test_converge_nit_issue_omitted_when_empty() -> None:
    """No nit follow-up issue created when accumulated nits are empty."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(_zero_verdict())
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    assert forge.create_issue_calls == []


# ---------------------------------------------------------------------------
# Round-state persistence (converge state store)
# ---------------------------------------------------------------------------


async def test_converge_round_state_persisted_after_fix() -> None:
    """After R1 fix, round 1 is persisted to ConvergeStateStore."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(
        _blocker_verdict(),
        _zero_verdict(),
    )
    cs = FakeConvergeStateStore()
    engine = _engine(forge, harness, converge_state=cs)

    await engine.converge(_PR)

    # R1 and R2 both set (R2 approve also sets).
    round_calls = [r for _pr, r in cs.set_converge_round_calls]
    assert 1 in round_calls
    assert 2 in round_calls


async def test_converge_resumes_from_persisted_round() -> None:
    """Engine resumes at R2 when ConvergeStateStore reports last completed round = 1."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    # Only script R2 verdict (resume skips R1).
    harness.script_reviewer_verdicts(_zero_verdict())
    cs = FakeConvergeStateStore()
    cs.seed_round(_PR, 1)  # already completed R1
    # Seed the R1 verdict file so prev_sigs lookup works.
    forge.seed_file(
        _PR,
        ".converge-verdict-r1.json",
        _blocker_verdict().model_dump_json().encode(),
    )
    engine = _engine(forge, harness, converge_state=cs)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    # Only 1 reviewer dispatch (R2 only).
    assert len(harness.dispatch_calls) == 1
    assert harness.dispatch_calls[0].model == DEFAULT_SWARM_MODEL
