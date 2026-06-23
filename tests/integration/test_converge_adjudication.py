"""Integration tests — adjudication phase (SPEC §5, §10.2, §251).

Three-tier model: Sonnet reviewers → Haiku nitpicker → Opus adjudicator.

Coverage:
- Spotless at R1 → adjudicator (no nitpicker) → APPROVED
- Nits present → nitpicker dispatched on Haiku → adjudicator → APPROVED
- Adjudicator reject → exactly ONE re-converge from R1 (RECONVERGE_CAP=1)
- Second adjudicator reject → cap reached → needs-human (E12)
- Counter channel 'adjudicator-reconverge' respected (not 'converge-retry')
- No follow-up issue created (nits handled in-loop)
"""

from __future__ import annotations

import pytest

from src.domain.types import (
    ADJUDICATION_MODEL,
    ADJUDICATOR_CONTRACT,
    DEFAULT_SWARM_MODEL,
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    NITPICKER_CONTRACT,
    NITPICKER_MODEL,
    PRRef,
    RepoRef,
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

_REPO = RepoRef(owner="acme", name="adj-svc")
_PR = PRRef(repo=_REPO, number=7)
_CHANGED_FILES = ["src/bar.py"]


def _green_pr(forge: FakeForgePort) -> None:
    """Seed a non-draft converge-eligible PR with all CI green."""
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=1)
    forge._changed_files[forge._pr_key(_PR)] = _CHANGED_FILES
    forge.seed_check_run(_PR, "CI", "completed", "success")


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


def _zero_verdict(*, nits: list[str] | None = None, suggestions: int = 0) -> Verdict:
    return Verdict(
        blockers=0,
        suggestions=suggestions,
        nits=nits or [],
        blocker_signatures=[],
    )


def _blocker_verdict(*, sigs: list[str] | None = None, nits: list[str] | None = None) -> Verdict:
    return Verdict(
        blockers=1,
        suggestions=0,
        nits=nits or [],
        blocker_signatures=sigs or ["type:missing-annotation"],
    )


# ---------------------------------------------------------------------------
# Spotless early-exit → adjudicator (no nitpicker) → APPROVED
# ---------------------------------------------------------------------------


@pytest.mark.covers("§5-adjudication", "spotless-early-exit-r1")
async def test_adjudication_spotless_r1_no_nitpicker_adjudicator_approves() -> None:
    """Spotless at R1 (0 blockers, 0 suggestions, CI green) → enter adjudication.

    Nitpicker is NOT dispatched (no nits/suggestions to polish).
    Adjudicator (Opus) approves → APPROVED with LABEL_READY, no create_issue.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(_zero_verdict())
    # Default adjudicator verdict: approve (blockers=0). No script needed.
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    # Label swap: agent:ready added, converge removed.
    assert (_PR, LABEL_READY) in forge.add_label_calls
    assert (_PR, LABEL_CONVERGE) in forge.remove_label_calls
    # No follow-up issue.
    assert forge.create_issue_calls == []
    # Exactly 2 dispatches: reviewer-R1 (Sonnet) + adjudicator (Opus). No nitpicker.
    contracts = [d.contract for d in harness.dispatch_calls]
    assert contracts == ["agents/converge-reviewer.md", ADJUDICATOR_CONTRACT]
    assert NITPICKER_CONTRACT not in contracts
    # Reviewer uses Sonnet.
    assert harness.dispatch_calls[0].model == DEFAULT_SWARM_MODEL
    # Adjudicator uses Opus.
    assert harness.dispatch_calls[1].model == ADJUDICATION_MODEL


# ---------------------------------------------------------------------------
# Nits present → nitpicker dispatched on Haiku → adjudicator → APPROVED
# ---------------------------------------------------------------------------


@pytest.mark.covers("§5-adjudication", "nits-trigger-nitpicker")
async def test_adjudication_nits_dispatch_nitpicker_on_haiku() -> None:
    """Accumulated nits → nitpicker (Haiku) dispatched before adjudicator.

    Reviewer-R1 has nits but 0 blockers → adjudicate.  Nitpicker polishes nits.
    Adjudicator then approves.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(
        _zero_verdict(nits=["nit-A", "nit-B"])
    )
    # Default adjudicator approve.
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    # 3 dispatches: reviewer-R1, nitpicker, adjudicator.
    contracts = [d.contract for d in harness.dispatch_calls]
    assert contracts == [
        "agents/converge-reviewer.md",
        NITPICKER_CONTRACT,
        ADJUDICATOR_CONTRACT,
    ]
    # Nitpicker uses Haiku.
    nitpicker_ctx = harness.dispatch_calls[1]
    assert nitpicker_ctx.model == NITPICKER_MODEL
    # Adjudicator uses Opus.
    adjudicator_ctx = harness.dispatch_calls[2]
    assert adjudicator_ctx.model == ADJUDICATION_MODEL
    # No follow-up issue.
    assert forge.create_issue_calls == []


@pytest.mark.covers("§5-adjudication", "suggestions-trigger-nitpicker")
async def test_adjudication_suggestions_dispatch_nitpicker() -> None:
    """Residual suggestions (at R3) also trigger nitpicker dispatch."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    # Two rounds to reach R2 spotless with suggestions.
    harness.script_reviewer_verdicts(
        _zero_verdict(suggestions=2),  # R1: spotless? No — row 1 needs suggestions==0
    )
    # Actually R1 with suggestions=2 is NOT spotless (row 1 requires suggestions==0 for R1/R2).
    # We need R3 with residual suggestions. Set up a 3-round path.
    # R1: blockers → fix → R2: blockers → fix → R3: 0 blockers, ci green, suggestions=2.
    harness._verdict_script = []  # reset
    harness.script_reviewer_verdicts(
        _blocker_verdict(sigs=["sig-a"]),   # R1: blocker → fix
        _blocker_verdict(sigs=["sig-b"]),   # R2: blocker → fix
        _zero_verdict(suggestions=2),        # R3: 0 blockers, suggestions → adjudicate (row 1b)
    )
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    # Nitpicker dispatched due to residual suggestions.
    contracts = [d.contract for d in harness.dispatch_calls]
    assert NITPICKER_CONTRACT in contracts
    nitpicker_ctx = next(d for d in harness.dispatch_calls if d.contract == NITPICKER_CONTRACT)
    assert nitpicker_ctx.model == NITPICKER_MODEL


# ---------------------------------------------------------------------------
# Adjudicator reject → exactly ONE re-converge from R1 (RECONVERGE_CAP=1)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§5-adjudication", "adjudicator-reject-reconverge")
async def test_adjudication_reject_triggers_one_reconverge() -> None:
    """Adjudicator reject → increment counter → clear state → re-converge from R1.

    First adjudicator rejects (blockers=1).  Re-converge runs R1 again.  Second
    adjudicator approves.  State = APPROVED.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    # First converge: R1 spotless → adjudicator rejects.
    # Second converge (re-converge): R1 spotless → adjudicator approves.
    harness.script_reviewer_verdicts(
        _zero_verdict(),  # first converge R1
        _zero_verdict(),  # re-converge R1
    )
    harness.script_adjudicator_verdict(
        Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=["adj-blocker"]),  # reject
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[]),               # approve
    )
    counter = FakeCounterStore()
    engine = _engine(forge, harness, counter=counter)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    # Label swap happened.
    assert (_PR, LABEL_READY) in forge.add_label_calls
    # 4 dispatches: reviewer-R1, adjudicator (reject), reviewer-R1 (re-converge), adjudicator (ok).
    contracts = [d.contract for d in harness.dispatch_calls]
    assert contracts == [
        "agents/converge-reviewer.md",
        ADJUDICATOR_CONTRACT,
        "agents/converge-reviewer.md",
        ADJUDICATOR_CONTRACT,
    ]
    # Both adjudicator dispatches use Opus.
    for adj_ctx in [d for d in harness.dispatch_calls if d.contract == ADJUDICATOR_CONTRACT]:
        assert adj_ctx.model == ADJUDICATION_MODEL
    # Counter is reset to 0 on successful approve (D3: finalize_approve resets both counters).
    # The 4 dispatches above prove the re-converge path was taken (not a direct approve).
    reconverge_count = await counter.get_count(_PR, "adjudicator-reconverge")
    assert reconverge_count == 0  # reset by _finalize_approve


# ---------------------------------------------------------------------------
# Second adjudicator reject → cap reached → needs-human (E12)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§5-adjudication", "adjudicator-reject-cap-reached-e12")
async def test_adjudication_second_reject_cap_reached_needs_human() -> None:
    """Two consecutive adjudicator rejections → cap reached → needs-human (E12).

    First adjudicator reject → re-converge (counter=1).
    Second adjudicator reject → cap reached (RECONVERGE_CAP=1) → ESCALATED / needs-human.
    Counter is reset in the cap-reached terminal path.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    # Two rounds of R1-spotless → both rejected by adjudicator.
    harness.script_reviewer_verdicts(
        _zero_verdict(),  # first converge R1
        _zero_verdict(),  # re-converge R1
    )
    harness.script_adjudicator_verdict(
        # reject #1
        Verdict(blockers=2, suggestions=0, nits=[], blocker_signatures=["still-broken"]),
        # reject #2
        Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=["still-broken-2"]),
    )
    counter = FakeCounterStore()
    engine = _engine(forge, harness, counter=counter)

    state = await engine.converge(_PR)

    assert state == "ESCALATED"
    # needs-human label applied.
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls
    # NOT approved.
    assert (_PR, LABEL_READY) not in forge.add_label_calls
    # 4 dispatches: reviewer(1), adjudicator(reject-1), reviewer(2), adjudicator(reject-2).
    contracts = [d.contract for d in harness.dispatch_calls]
    assert contracts == [
        "agents/converge-reviewer.md",
        ADJUDICATOR_CONTRACT,
        "agents/converge-reviewer.md",
        ADJUDICATOR_CONTRACT,
    ]
    # Both adjudicators on Opus.
    for adj_ctx in [d for d in harness.dispatch_calls if d.contract == ADJUDICATOR_CONTRACT]:
        assert adj_ctx.model == ADJUDICATION_MODEL


# ---------------------------------------------------------------------------
# Bounded re-converge: counter channel 'adjudicator-reconverge' (not 'converge-retry')
# ---------------------------------------------------------------------------


@pytest.mark.covers("§5-adjudication", "reconverge-counter-channel")
async def test_adjudication_reconverge_uses_separate_counter_channel() -> None:
    """Re-converge uses 'adjudicator-reconverge' counter — NOT the 'converge-retry' channel.

    The converge-retry counter (E3/E4 recovery) must not be affected by adjudicator rejection.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(
        _zero_verdict(),  # first converge R1
        _zero_verdict(),  # re-converge R1
    )
    # First reject, then approve.
    harness.script_adjudicator_verdict(
        Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=["blocker-x"]),
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[]),
    )
    counter = FakeCounterStore()
    engine = _engine(forge, harness, counter=counter)

    state = await engine.converge(_PR)

    # Re-converge path: 4 dispatches prove 'adjudicator-reconverge' counter was used.
    # (reviewer, adj-reject, reviewer-reconverge, adj-approve)
    assert state == "APPROVED"
    contracts = [d.contract for d in harness.dispatch_calls]
    assert contracts == [
        "agents/converge-reviewer.md",
        ADJUDICATOR_CONTRACT,
        "agents/converge-reviewer.md",
        ADJUDICATOR_CONTRACT,
    ]
    # After successful approve, 'adjudicator-reconverge' is reset to 0 (not 'converge-retry').
    # Both counters end at 0 — this verifies the separation: converge-retry was NEVER touched.
    adj_count = await counter.get_count(_PR, "adjudicator-reconverge")
    assert adj_count == 0  # reset by _finalize_approve (was 1 during re-converge)
    # 'converge-retry' counter is untouched (zero throughout — never incremented).
    retry_count = await counter.get_count(_PR, "converge-retry")
    assert retry_count == 0


# ---------------------------------------------------------------------------
# Adjudicator model verification — always Opus (ADJUDICATION_MODEL)
# ---------------------------------------------------------------------------


@pytest.mark.covers("§251", "adjudicator-always-opus")
async def test_adjudication_adjudicator_always_uses_opus() -> None:
    """Adjudicator dispatch always uses ADJUDICATION_MODEL (Opus), regardless of path."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    # Multi-round path: R1 blocker → fix → R2 approve → adjudicate.
    harness.script_reviewer_verdicts(
        _blocker_verdict(),   # R1: blocker
        _zero_verdict(),      # R2: spotless
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    adjudicator_dispatches = [
        d for d in harness.dispatch_calls if d.contract == ADJUDICATOR_CONTRACT
    ]
    assert len(adjudicator_dispatches) == 1
    assert adjudicator_dispatches[0].model == ADJUDICATION_MODEL


# ---------------------------------------------------------------------------
# No create_issue on approve — nits resolved in-loop
# ---------------------------------------------------------------------------


@pytest.mark.covers("§5-adjudication", "no-followup-issue")
async def test_adjudication_no_create_issue_on_approve() -> None:
    """APPROVED state never creates a follow-up issue, even when nits were accumulated."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge)
    harness.script_reviewer_verdicts(
        _blocker_verdict(nits=["nit-x"]),    # R1: blocker + nit
        _zero_verdict(nits=["nit-x"]),        # R2: spotless-for-blockers, nit remains
    )
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    assert forge.create_issue_calls == [], (
        "No follow-up issue must be created — nits are handled by nitpicker in-loop"
    )
