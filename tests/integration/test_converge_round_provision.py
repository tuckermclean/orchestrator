"""Integration tests — Engine.converge provides authoritative ROUND/CONVERGE_ROUND_STARTED.

Covers the PR #52 live bug: agents were guessing the round by counting
`## Converge Review — Round N` comments.  When converge was re-triggered the old-cycle
comments remained, causing miscounts.  Fix: engine injects converge_round and
converge_round_started into reviewer and fixer DispatchContexts; harness surfaces them
as ROUND=<n> and CONVERGE_ROUND_STARTED=<iso> in the agent prompt.

Tests:
  - DispatchContext accepts and round-trips the two new fields.
  - DispatchContext still rejects unknown fields (sealed / extra='forbid').
  - converge() sets converge_round=r on reviewer context.
  - converge() sets converge_round=r on fixer context.
  - converge() sets converge_round_started (ISO timestamp) on reviewer context.
  - converge() sets converge_round_started on fixer context.
  - converge_round advances correctly across rounds (R1 reviewer=1, R2 reviewer=2).
  - Nitpicker and adjudicator do NOT receive converge_round (not converge roles).
  - Harness _build_prompt includes ROUND= when converge_round is set.
  - Harness _build_prompt includes CONVERGE_ROUND_STARTED= when converge_round_started is set.
  - Harness _build_prompt omits ROUND/CONVERGE_ROUND_STARTED for non-converge dispatches.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.domain.types import (
    DEFAULT_SWARM_MODEL,
    LABEL_CONVERGE,
    DispatchContext,
    IssueRef,
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
from src.ports.harness import ClaudeCodeHarnessPort

_REPO = RepoRef(owner="acme", name="svc")
_PR = PRRef(repo=_REPO, number=42)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _green_pr(forge: FakeForgePort, *, changed_files: list[str]) -> None:
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


# ---------------------------------------------------------------------------
# DispatchContext field tests
# ---------------------------------------------------------------------------


def test_dispatch_context_accepts_converge_round_and_started() -> None:
    """DispatchContext round-trips the two new converge fields (typed, not free-form)."""
    ctx = DispatchContext(
        pr_ref=_PR,
        contract="agents/converge-reviewer.md",
        model="claude-sonnet-4-6",
        max_turns=60,
        forge_token_scope="repo-branch",
        converge_round=2,
        converge_round_started="2026-06-21T10:00:00+00:00",
    )
    assert ctx.converge_round == 2
    assert ctx.converge_round_started == "2026-06-21T10:00:00+00:00"


def test_dispatch_context_converge_fields_default_to_none() -> None:
    """converge_round and converge_round_started default to None for non-converge dispatches."""
    ctx = DispatchContext(
        issue_ref=IssueRef(repo=_REPO, number=1),
        contract="agents/implementer.md",
        model="claude-sonnet-4-6",
        max_turns=80,
        forge_token_scope="repo-branch",
    )
    assert ctx.converge_round is None
    assert ctx.converge_round_started is None


def test_dispatch_context_still_rejects_unknown_fields() -> None:
    """I3: sealed schema (extra='forbid') still rejects unknown fields after new converge fields."""
    with pytest.raises(ValidationError):
        DispatchContext(
            pr_ref=_PR,
            contract="agents/converge-reviewer.md",
            model="claude-sonnet-4-6",
            max_turns=60,
            forge_token_scope="repo-branch",
            forge_token="ghp_secret",  # type: ignore[call-arg]  # must be rejected
        )


def test_dispatch_context_rejects_unknown_field_with_converge_fields_present() -> None:
    """I3: sealed schema still rejects unknown fields even when new converge fields are provided."""
    with pytest.raises(ValidationError):
        DispatchContext(
            pr_ref=_PR,
            contract="agents/converge-reviewer.md",
            model="claude-sonnet-4-6",
            max_turns=60,
            forge_token_scope="repo-branch",
            converge_round=1,
            converge_round_started="2026-06-21T10:00:00+00:00",
            injected_cred="evil",  # type: ignore[call-arg]  # must be rejected
        )


# ---------------------------------------------------------------------------
# Engine.converge — reviewer gets converge_round
# ---------------------------------------------------------------------------


async def test_converge_reviewer_context_carries_round_r1() -> None:
    """Reviewer dispatch at R1 receives converge_round=1."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    reviewer_ctx = harness.dispatch_calls[0]
    assert reviewer_ctx.contract == "agents/converge-reviewer.md"
    assert reviewer_ctx.converge_round == 1, (
        f"Reviewer at R1 must have converge_round=1; got {reviewer_ctx.converge_round!r}"
    )


async def test_converge_reviewer_context_carries_round_started() -> None:
    """Reviewer dispatch at R1 receives converge_round_started as a non-empty ISO string."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    reviewer_ctx = harness.dispatch_calls[0]
    assert reviewer_ctx.converge_round_started is not None, (
        "Reviewer context must carry converge_round_started"
    )
    # Must be a non-empty string (ISO-8601 format from datetime.isoformat())
    assert len(reviewer_ctx.converge_round_started) > 10, (
        f"converge_round_started looks too short: {reviewer_ctx.converge_round_started!r}"
    )


async def test_converge_fixer_context_carries_round_r1() -> None:
    """Fixer dispatch at R1 receives converge_round=1."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=["logic:bug"]),
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[]),
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    # Dispatch order: reviewer-R1(0), fixer-R1(1), reviewer-R2(2), adjudicator(3)
    fixer_ctx = harness.dispatch_calls[1]
    assert fixer_ctx.contract == "agents/converge-fixer.md"
    assert fixer_ctx.converge_round == 1, (
        f"Fixer at R1 must have converge_round=1; got {fixer_ctx.converge_round!r}"
    )


async def test_converge_fixer_context_carries_round_started() -> None:
    """Fixer dispatch at R1 receives converge_round_started matching the reviewer's round."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=["logic:bug"]),
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[]),
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    reviewer_ctx = harness.dispatch_calls[0]
    fixer_ctx = harness.dispatch_calls[1]
    # Fixer and reviewer for the same round must share the same round_started timestamp.
    assert fixer_ctx.converge_round_started is not None
    assert fixer_ctx.converge_round_started == reviewer_ctx.converge_round_started, (
        "Fixer and reviewer for the same round must share converge_round_started"
    )


async def test_converge_reviewer_round_advances_to_r2() -> None:
    """Reviewer dispatch at R2 receives converge_round=2."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    # R1: 1 blocker → fixer → R2: 0 blockers → adjudicate
    harness.script_reviewer_verdicts(
        Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=["logic:bug"]),
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[]),
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    # Dispatch order: reviewer-R1(0), fixer-R1(1), reviewer-R2(2), adjudicator(3)
    reviewer_r2_ctx = harness.dispatch_calls[2]
    assert reviewer_r2_ctx.contract == "agents/converge-reviewer.md"
    assert reviewer_r2_ctx.converge_round == 2, (
        f"Reviewer at R2 must have converge_round=2; got {reviewer_r2_ctx.converge_round!r}"
    )


async def test_converge_nitpicker_does_not_receive_converge_round() -> None:
    """Nitpicker (adjudication phase) does NOT receive converge_round — not a converge role."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    # Reviewer returns nits so nitpicker is dispatched
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=["nit-a"], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    # Dispatch order: reviewer(0), nitpicker(1), adjudicator(2)
    nitpicker_ctx = harness.dispatch_calls[1]
    assert nitpicker_ctx.contract == "agents/nitpicker.md"
    assert nitpicker_ctx.converge_round is None, (
        f"Nitpicker must NOT receive converge_round; got {nitpicker_ctx.converge_round!r}"
    )
    assert nitpicker_ctx.converge_round_started is None, (
        "Nitpicker must NOT receive converge_round_started; "
        f"got {nitpicker_ctx.converge_round_started!r}"
    )


async def test_converge_adjudicator_does_not_receive_converge_round() -> None:
    """Adjudicator (adjudication phase) does NOT receive converge_round — not a converge role."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _green_pr(forge, changed_files=["src/foo.py"])
    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    await engine.converge(_PR)

    # Dispatch order: reviewer(0), adjudicator(1) — no nits so no nitpicker
    adjudicator_ctx = harness.dispatch_calls[1]
    assert adjudicator_ctx.contract == "agents/adjudicator.md"
    assert adjudicator_ctx.converge_round is None, (
        f"Adjudicator must NOT receive converge_round; got {adjudicator_ctx.converge_round!r}"
    )
    assert adjudicator_ctx.converge_round_started is None


# ---------------------------------------------------------------------------
# Harness _build_prompt — ROUND and CONVERGE_ROUND_STARTED injection
# ---------------------------------------------------------------------------


def _make_harness() -> ClaudeCodeHarnessPort:
    """Build a ClaudeCodeHarnessPort suitable for prompt testing (no real credentials)."""
    return ClaudeCodeHarnessPort(
        claude_oauth_token="tok",
        app_id="app",
        private_key_pem="key",
        installation_id="inst",
        repo_owner="acme",
        repo_name="svc",
    )


def test_build_prompt_includes_round_when_set() -> None:
    """_build_prompt includes 'ROUND=<n>' when converge_round is set."""
    harness = _make_harness()
    ctx = DispatchContext(
        pr_ref=_PR,
        contract="agents/converge-reviewer.md",
        model=DEFAULT_SWARM_MODEL,
        max_turns=60,
        forge_token_scope="repo-branch",
        converge_round=2,
        converge_round_started="2026-06-21T10:00:00+00:00",
    )
    prompt = harness._build_prompt(ctx)
    assert "ROUND=2" in prompt, (
        f"Prompt must contain 'ROUND=2' when converge_round=2; got:\n{prompt}"
    )


def test_build_prompt_includes_round_started_when_set() -> None:
    """_build_prompt includes 'CONVERGE_ROUND_STARTED=<iso>' when converge_round_started is set."""
    harness = _make_harness()
    ctx = DispatchContext(
        pr_ref=_PR,
        contract="agents/converge-reviewer.md",
        model=DEFAULT_SWARM_MODEL,
        max_turns=60,
        forge_token_scope="repo-branch",
        converge_round=1,
        converge_round_started="2026-06-21T08:30:00+00:00",
    )
    prompt = harness._build_prompt(ctx)
    assert "CONVERGE_ROUND_STARTED=2026-06-21T08:30:00+00:00" in prompt, (
        f"Prompt must contain CONVERGE_ROUND_STARTED=...; got:\n{prompt}"
    )


def test_build_prompt_omits_round_for_non_converge_dispatch() -> None:
    """_build_prompt omits ROUND and CONVERGE_ROUND_STARTED when fields are None (non-converge)."""
    harness = _make_harness()
    ctx = DispatchContext(
        issue_ref=IssueRef(repo=_REPO, number=5),
        contract="agents/implementer.md",
        model=DEFAULT_SWARM_MODEL,
        max_turns=80,
        forge_token_scope="repo-branch",
        # converge_round and converge_round_started default to None
    )
    prompt = harness._build_prompt(ctx)
    assert "ROUND=" not in prompt, (
        f"Non-converge prompt must NOT contain ROUND=; got:\n{prompt}"
    )
    assert "CONVERGE_ROUND_STARTED=" not in prompt, (
        f"Non-converge prompt must NOT contain CONVERGE_ROUND_STARTED=; got:\n{prompt}"
    )


def test_build_prompt_round_r1_fixer() -> None:
    """_build_prompt includes ROUND=1 for a converge fixer at R1."""
    harness = _make_harness()
    ctx = DispatchContext(
        pr_ref=_PR,
        contract="agents/converge-fixer.md",
        model=DEFAULT_SWARM_MODEL,
        max_turns=60,
        forge_token_scope="repo-branch",
        converge_round=1,
        converge_round_started="2026-06-21T09:00:00+00:00",
    )
    prompt = harness._build_prompt(ctx)
    assert "ROUND=1" in prompt
    assert "CONVERGE_ROUND_STARTED=" in prompt
