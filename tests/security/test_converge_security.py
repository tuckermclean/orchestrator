"""Security tests for Engine.converge — I3, I9, and nit follow-up issue (SPEC §6, §10.2).

TESTING.md §5.
"""

from __future__ import annotations

import pytest

from src.decisions.decide_specialists import decide_specialists
from src.domain.types import (
    ADJUDICATION_MODEL,
    ADJUDICATOR_CONTRACT,
    CONVERGE_REVIEW_BASE,
    LABEL_CONVERGE,
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
    SpawnDenied,
)

_REPO = RepoRef(owner="acme", name="service")
_PR = PRRef(repo=_REPO, number=99)


def _engine(
    forge: FakeForgePort,
    harness: FakeHarnessPort,
    *,
    counter: FakeCounterStore | None = None,
) -> Engine:
    return Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=counter or FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
    )


def _green_pr(forge: FakeForgePort, *, changed_files: list[str]) -> None:
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=len(changed_files))
    forge._changed_files[forge._pr_key(_PR)] = changed_files
    # Seed one generic green check (no named allow-list required — all present checks pass).
    forge.seed_check_run(_PR, "CI", "completed", "success")


# ---------------------------------------------------------------------------
# I3 — forge token branch-scoped during converge (SPEC SECURITY.md §3)
# ---------------------------------------------------------------------------


async def test_security_converge_reviewer_uses_repo_branch_scope() -> None:
    """I3: reviewer DispatchContext uses forge_token_scope='repo-branch', not 'repo-comment'."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    reviewer_ctx = harness.dispatch_calls[0]
    assert reviewer_ctx.forge_token_scope == "repo-branch"


async def test_security_converge_fixer_uses_repo_branch_scope() -> None:
    """I3: fixer DispatchContext uses forge_token_scope='repo-branch'."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=["sig-a"]),
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[]),
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    fixer_ctx = harness.dispatch_calls[1]
    assert fixer_ctx.contract == "agents/converge-fixer.md"
    assert fixer_ctx.forge_token_scope == "repo-branch"


async def test_security_converge_no_extra_fields_in_dispatch_context() -> None:
    """I3: DispatchContext extra='forbid' prevents credential injection via extra fields."""
    from pydantic import ValidationError

    from src.domain.types import DispatchContext

    with pytest.raises(ValidationError):
        DispatchContext(
            pr_ref=_PR,
            contract="agents/converge-reviewer.md",
            model="claude-sonnet-4-6",
            max_turns=60,
            forge_token_scope="repo-branch",
            forge_token="ghp_secret",  # type: ignore[call-arg]  # must be rejected
        )


# ---------------------------------------------------------------------------
# I9 — fixer cannot spawn out-of-set specialists (SPEC §9.2, SECURITY.md §3)
# ---------------------------------------------------------------------------


async def test_security_fixer_cannot_spawn_out_of_set_specialist() -> None:
    """I9: harness must reject a fixer spawning an agent not in its allowed_agent_refs.

    The fixer's allowed_agent_refs is set by decide_specialists. The harness enforces
    this via simulate_spawn_attempt (stand-in for the real harness allow-set check).
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=["sig"]),
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[]),
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    # Fixer is dispatch_calls[1]: reviewer-R1(0), fixer-R1(1), reviewer-R2(2).
    fixer_ctx = harness.dispatch_calls[1]
    allowed = fixer_ctx.allowed_agent_refs or []

    # An agent NOT in the allowed set must be rejected by the harness.
    malicious_agent = "malicious-injected-agent.md"
    assert malicious_agent not in allowed

    # Set last_context to the fixer context and simulate a disallowed spawn attempt.
    harness._last_context = fixer_ctx
    with pytest.raises(SpawnDenied):
        harness.simulate_spawn_attempt(malicious_agent)


def test_security_fixer_allowed_refs_from_decide_specialists_only() -> None:
    """I9: fixer allowed_agent_refs must come only from decide_specialists output.

    Contributor-supplied changed paths that embed agent names must not affect the
    allowed set beyond what CONVERGE_REVIEW_BASE ∪ SPECIALIST_ROUTING define.
    """
    from src.domain.types import SPECIALIST_ROUTING

    # A path that looks like an agent injection attempt.
    injection_path = ".agents/malicious-hacker.md"
    result = decide_specialists([injection_path, "src/foo.py"], 1)

    allowed = set(CONVERGE_REVIEW_BASE) | {
        ref for entry in SPECIALIST_ROUTING for ref in entry.agent_refs
    }
    assert set(result) <= allowed
    assert "malicious-hacker.md" not in result
    assert all(".agents/" not in ref for ref in result)


# ---------------------------------------------------------------------------
# Adjudication phase — nits dispatched to nitpicker (SPEC §10.2 / §251)
# ---------------------------------------------------------------------------


async def test_converge_nits_dispatched_to_nitpicker_not_issue() -> None:
    """Nits from review rounds are handled by the nitpicker (Haiku) in-loop — NOT a follow-up issue.

    3-tier model (SPEC §251): nits/suggestions → nitpicker (Haiku) → adjudicator (Opus).
    No ``create_issue`` call is made; nits are resolved before the PR is approved.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=1, suggestions=0, nits=["nit-1"], blocker_signatures=["sig"]),
        Verdict(blockers=0, suggestions=0, nits=["nit-2", "nit-1"], blocker_signatures=[]),
    )
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "APPROVED"
    # No follow-up issue — nits resolved in-loop by nitpicker.
    assert forge.create_issue_calls == []
    # 4 dispatches: reviewer-R1, fixer-R1, reviewer-R2, nitpicker, adjudicator.
    contracts = [d.contract for d in harness.dispatch_calls]
    assert NITPICKER_CONTRACT in contracts
    assert ADJUDICATOR_CONTRACT in contracts
    # Nitpicker uses Haiku.
    nitpicker_ctx = next(d for d in harness.dispatch_calls if d.contract == NITPICKER_CONTRACT)
    assert nitpicker_ctx.model == NITPICKER_MODEL
    # Adjudicator uses Opus.
    adjudicator_ctx = next(d for d in harness.dispatch_calls if d.contract == ADJUDICATOR_CONTRACT)
    assert adjudicator_ctx.model == ADJUDICATION_MODEL


async def test_converge_nit_issue_not_created_when_no_nits() -> None:
    """No nitpicker dispatch and no follow-up issue when nits/suggestions are empty on approve."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    assert forge.create_issue_calls == []
    # Spotless: only reviewer + adjudicator dispatched (no nitpicker).
    contracts = [d.contract for d in harness.dispatch_calls]
    assert NITPICKER_CONTRACT not in contracts
    assert ADJUDICATOR_CONTRACT in contracts
