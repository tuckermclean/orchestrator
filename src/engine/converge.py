"""Engine.converge — full 3-round converge sub-machine with adjudication endgame.

Implements SPEC §5, §6, §8.3, §8.4, §10.2 (amended for 3-tier model, SPEC §251):

  Tier 1 — Reviewers (all rounds R1/R2/R3): DEFAULT_SWARM_MODEL (Sonnet)
  Tier 2 — Fixers (R1/R2 only):             DEFAULT_SWARM_MODEL (Sonnet)
  Tier 3 — Nitpicker (adjudication phase):   NITPICKER_MODEL (Haiku)
  Tier 4 — Adjudicator (terminal gate):      ADJUDICATION_MODEL (Opus)

Round loop
----------
Each round: Seed → Review → Check-CI → Decide → (Fix | Adjudicate | Escalate).

Spotless early-exit (SPEC §5): ``blockers == 0 AND suggestions == 0 AND ci_green``
at ANY round (R1/R2/R3) skips remaining fix rounds and enters the ADJUDICATION phase
immediately.

R3 terminal (SPEC §5): ``blockers == 0 AND ci_green`` (suggestions may remain) →
ADJUDICATION phase. Blockers remain at R3 → ``escalate:cap-reached`` (E5, D3).

Adjudication phase (SPEC §5, §10.2)
-------------------------------------
1. Nitpicker pass (Haiku): if accumulated nits + residual suggestions > 0, dispatch
   NITPICKER_CONTRACT; await CI green (up to CI_WAIT_S). Skip if nothing to polish.
2. Adjudicator (Opus): dispatch ADJUDICATOR_CONTRACT; read Verdict; ``blockers == 0``
   → FINALIZE (APPROVED). ``blockers >= 1`` → bounded re-converge (cap RECONVERGE_CAP=1).
   If re-converge count >= cap → ``escalate:needs-human`` (E12).

FINALIZE (approve path, SPEC §10.2)
--------------------------------------
Add LABEL_READY, remove LABEL_CONVERGE, reset counters (converge-retry +
adjudicator-reconverge), clear converge state. No follow-up nits issue (nits are
resolved by the nitpicker in-loop). No ``forge.create_review("APPROVE")`` — self-authored
PRs return HTTP 422; the ``agent:ready`` label IS the approval signal.

Verdict channel (SPEC §5, §8.2)
--------------------------------
The reviewer/adjudicator emits its ``Verdict`` as a fenced JSON block in its final
message. The harness captures it from the run's event stream (``RunEventStore``); the
engine reads it via ``harness.get_run_verdict(handle)`` — no file is committed to the
PR branch.

If no parseable verdict is found (agent crashed / omitted output), ``get_run_verdict``
returns ``None``. ``resolve_blockers`` treats ``None`` as the absent-verdict case and
falls back to the comment-footer heuristic; if no footer exists either, it returns
``"unknown"``, which routes to the P11 no-verdict retry path or E3 escalation.

CI green definition (SPEC §7)
------------------------------
``ci_green`` is true iff every check run present on the PR is completed and green
(``conclusion ∈ {"success", "skipped", "neutral"}``). A PR with no check runs at
all is also green (vacuously — the repo has no CI or none apply).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, cast

import pathspec

from src.decisions.decide_round import decide_round
from src.decisions.decide_specialists import decide_specialists
from src.decisions.resolve_blockers import resolve_blockers
from src.domain.types import (
    _CI_GREEN_CONCLUSIONS,
    ADJUDICATION_MODEL,
    ADJUDICATOR_CONTRACT,
    CI_WAIT_S,
    CONVERGE_ROUNDS,
    DEFAULT_SWARM_MODEL,
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    NITPICKER_CONTRACT,
    NITPICKER_MODEL,
    NO_VERDICT_RETRY_CAP,
    POLL_INTERVAL_S,
    PROTECTED_PATHS,
    RECONVERGE_CAP,
    CheckRun,
    DispatchContext,
    PRRef,
    PRState,
    Verdict,
)

if TYPE_CHECKING:
    from src.engine.dispatch import Engine

# AllHarnessesExhausted and SessionLimitHold propagate naturally through the
# converge dispatcher (SPEC §14.5: all-exhausted → stay CONVERGING, let RC-3
# re-arm).  SessionLimitHold is a subclass of AllHarnessesExhausted so all
# existing except AllHarnessesExhausted handlers catch it automatically.
# SessionLimitHold is explicitly caught in the fixer branch to roll back the
# persisted converge round so RC-3 re-arms at the correct round (SPEC §14.8
# round-neutral quota HOLD).
from src.ports.harness_registry import SessionLimitHold

_CONVERGE_REVIEWER_CONTRACT = "agents/converge-reviewer.md"
_CONVERGE_FIXER_CONTRACT = "agents/converge-fixer.md"
_REVIEWER_MAX_TURNS = 60
_FIXER_MAX_TURNS = 60
_NITPICKER_MAX_TURNS = 40
_ADJUDICATOR_MAX_TURNS = 60
_NO_VERDICT_RETRY_MARKER = "<!-- orchestrator:converge-retry -->"
_RECONVERGE_CHANNEL = "adjudicator-reconverge"


def _touches_protected_path(changed_paths: list[str]) -> bool:
    """True if any changed path matches a PROTECTED_PATHS glob (gitignore semantics)."""
    spec = pathspec.PathSpec.from_lines("gitignore", PROTECTED_PATHS)
    return any(spec.match_file(path) for path in changed_paths)


def _all_checks_green(checks: list[CheckRun]) -> bool:
    """True iff every present check is completed and green (SPEC §7 CI green definition).

    - Empty list → True (no CI / no applicable checks — vacuously green).
    - Any check not yet ``"completed"`` → False (pending; poll and wait).
    - Any check ``conclusion`` outside the green set → False (failing).
    """
    for check in checks:
        if check.state != "completed":
            return False
        if check.conclusion not in _CI_GREEN_CONCLUSIONS:
            return False
    return True


def _any_checks_pending(checks: list[CheckRun]) -> bool:
    """True iff at least one check is not yet in a terminal state."""
    return any(check.state != "completed" for check in checks)


def _normalize_sigs(sigs: list[str]) -> list[str]:
    """Strip the sentinel signature so prev/curr comparison is against real slugs."""
    from src.domain.types import SENTINEL_SIGNATURE
    if sigs == [SENTINEL_SIGNATURE]:
        return []
    return sigs


async def _poll_checks_until_complete(
    engine: Engine,
    pr_ref: PRRef,
) -> list[CheckRun]:
    """Poll check runs up to CI_WAIT_S until all present checks reach a terminal state.

    Returns the final list of check runs (all completed, or deadline expired).
    Yields to the event loop between polls so async tasks are not starved.
    Called both before computing ``ci_green`` at each review round and during the
    ``ci-red`` recovery path after ``trigger_ci`` (SPEC §7, §10.2 step 4g).
    """
    deadline = time.monotonic() + CI_WAIT_S
    while True:
        checks = await engine.forge.get_check_runs(pr_ref)
        if not _any_checks_pending(checks):
            return checks
        if time.monotonic() >= deadline:
            return checks
        await asyncio.sleep(POLL_INTERVAL_S)


async def _terminal_escalate(engine: Engine, pr_ref: PRRef) -> PRState:
    """Add LABEL_NEEDS_HUMAN, reset counter, clear converge state → ESCALATED.

    Write order is normative (SPEC §10.2): label write MUST precede DB mutations.
    """
    await engine.forge.add_label(pr_ref, LABEL_NEEDS_HUMAN)
    if engine.counter is not None:
        await engine.counter.reset(pr_ref, "converge-retry")
    assert engine.converge_state is not None
    await engine.converge_state.clear_converge_state(pr_ref)
    return "ESCALATED"


async def _terminal_escalate_reconverge_cap(engine: Engine, pr_ref: PRRef) -> PRState:
    """Adjudicator rejected after max re-converge attempts → needs-human (E12).

    Resets both counters and clears converge state.
    Write order is normative: label write MUST precede DB mutations.
    """
    await engine.forge.add_label(pr_ref, LABEL_NEEDS_HUMAN)
    if engine.counter is not None:
        await engine.counter.reset(pr_ref, "converge-retry")
        await engine.counter.reset(pr_ref, _RECONVERGE_CHANNEL)
    assert engine.converge_state is not None
    await engine.converge_state.clear_converge_state(pr_ref)
    return "ESCALATED"


async def _run_adjudication_phase(
    engine: Engine,
    pr_ref: PRRef,
    pr_head_branch: str,
    accumulated_nits: list[str],
    residual_suggestions: int,
) -> PRState:
    """Execute the adjudication phase: nitpicker pass → adjudicator (SPEC §5, §10.2).

    Entered after the round loop produces ``adjudicate`` (spotless early-exit OR
    R3-no-blockers).

    Step 1 — Nitpicker (Haiku): if ``accumulated_nits`` or ``residual_suggestions > 0``,
    dispatch the nitpicker. Awaits CI green after any commits (up to CI_WAIT_S).
    If nothing to polish, skip entirely.

    Step 2 — Adjudicator (Opus): dispatch and await. Reads a ``Verdict``; approve when
    ``blockers == 0``. Reject → bounded re-converge (cap ``RECONVERGE_CAP``).
    """
    assert engine.converge_state is not None

    # --- Step 1: Nitpicker pass ---
    deduped_nits = _dedupe_nits(accumulated_nits)
    has_polish = bool(deduped_nits) or residual_suggestions > 0

    if has_polish:
        nitpicker_context = DispatchContext(
            pr_ref=pr_ref,
            contract=NITPICKER_CONTRACT,
            model=NITPICKER_MODEL,
            max_turns=_NITPICKER_MAX_TURNS,
            forge_token_scope="repo-branch",
            allowed_agent_refs=None,  # nitpicker is depth-1; no further spawns
            head_branch=pr_head_branch,
        )
        # AllHarnessesExhausted propagates up to OrchestratorService (SPEC §14.5).
        nitpicker_handle = await engine.harness.dispatch(nitpicker_context)
        nitpicker_completed = await engine._await_run(nitpicker_handle)
        if not nitpicker_completed:
            # Nitpicker timed out → escalate (safety: don't approve without checking).
            return await _terminal_escalate(engine, pr_ref)

        # After nitpicker commits, wait for CI to go green.
        final_checks = await _poll_checks_until_complete(engine, pr_ref)
        if not _all_checks_green(final_checks):
            # Nitpicker introduced a CI failure → escalate (human investigates).
            return await _terminal_escalate(engine, pr_ref)

    # --- Step 2: Adjudicator (Opus) ---
    adjudicator_context = DispatchContext(
        pr_ref=pr_ref,
        contract=ADJUDICATOR_CONTRACT,
        model=ADJUDICATION_MODEL,
        max_turns=_ADJUDICATOR_MAX_TURNS,
        forge_token_scope="repo-branch",
        allowed_agent_refs=None,  # adjudicator may spawn from its own allow-set
        head_branch=pr_head_branch,
    )
    # AllHarnessesExhausted propagates up to OrchestratorService (SPEC §14.5).
    adjudicator_handle = await engine.harness.dispatch(adjudicator_context)
    adj_completed = await engine._await_run(adjudicator_handle)
    if not adj_completed:
        # Adjudicator timed out → escalate.
        return await _terminal_escalate(engine, pr_ref)

    adj_verdict: Verdict | None = await engine.harness.get_run_verdict(adjudicator_handle)

    if adj_verdict is None or adj_verdict.blockers > 0:
        # Adjudicator rejected (blockers >= 1 or no verdict) → bounded re-converge.
        if engine.counter is not None:
            reconverge_count = await engine.counter.get_count(pr_ref, _RECONVERGE_CHANNEL)
        else:
            reconverge_count = 0

        if reconverge_count < RECONVERGE_CAP:
            # Increment counter, reset converge state → re-enter converge from R1.
            if engine.counter is not None:
                await engine.counter.increment(pr_ref, _RECONVERGE_CHANNEL)
            await engine.converge_state.clear_converge_state(pr_ref)
            # Re-run converge from R1 (tail-call, not a true loop — bounded by cap).
            return await converge(engine, pr_ref)
        else:
            # Cap reached → needs-human (E12).
            return await _terminal_escalate_reconverge_cap(engine, pr_ref)

    # Adjudicator approved (blockers == 0) → FINALIZE.
    return await _finalize_approve(engine, pr_ref)


async def converge(
    engine: Engine,
    pr_ref: PRRef,
) -> PRState:
    """Run the converge sub-machine for one PR (SPEC §10.2, amended for 3-tier model).

    Three model tiers:
    - Reviewers (R1/R2/R3): DEFAULT_SWARM_MODEL (Sonnet)
    - Nitpicker (adjudication phase): NITPICKER_MODEL (Haiku)
    - Adjudicator (terminal gate): ADJUDICATION_MODEL (Opus)

    Spotless early-exit: ``adjudicate`` token at any round skips remaining fix
    rounds and enters the adjudication phase immediately.

    ``ci_green`` is computed by trusting the repo's actual check runs — every
    present check must be completed and green (SPEC §7 CI green definition).
    Pending checks are awaited (up to ``CI_WAIT_S``) before the approve/escalate
    decision is made. A PR with no check runs at all is vacuously green.
    """
    assert engine.converge_state is not None, "converge requires a ConvergeStateStore"
    forge = engine.forge
    converge_state = engine.converge_state

    # Step 1 — idempotency gate.
    pr = await forge.get_pr(pr_ref)
    if pr.state == "closed" or pr.merged:
        return "MERGED"
    if LABEL_NEEDS_HUMAN in pr.labels:
        return "ESCALATED"
    if LABEL_READY in pr.labels:
        return "APPROVED"
    if pr.draft:
        return "BUILDING"

    # Step 2 — setup.
    changed_paths = await forge.get_changed_files(pr_ref)

    # Step 2a — protected-path check (E1, before any specialist spawn).
    if _touches_protected_path(changed_paths):
        await forge.add_label(pr_ref, LABEL_NEEDS_HUMAN)
        await converge_state.clear_converge_state(pr_ref)
        return "ESCALATED"

    # Step 3 — empty-diff check (E6).
    if len(changed_paths) == 0:
        await forge.add_label(pr_ref, LABEL_NEEDS_HUMAN)
        await converge_state.clear_converge_state(pr_ref)
        return "ESCALATED"

    # Step 4 — converge loop.
    start = await converge_state.get_converge_round(pr_ref) + 1
    accumulated_nits: list[str] = []
    residual_suggestions: int = 0
    # prev_sigs is tracked in-memory across rounds; no branch files needed.
    prev_sigs: list[str] = []

    # P0.4: head_branch ensures the reviewer/fixer/nitpicker/adjudicator operate on
    # the PR diff.
    pr_head_branch: str = pr.head_branch

    for r in range(start, CONVERGE_ROUNDS + 1):
        round_literal = cast("Literal[1, 2, 3]", r)
        round_started = datetime.now(tz=UTC)
        await converge_state.set_round_started(pr_ref, round_started)

        specialist_refs = decide_specialists(changed_paths, r)
        # All reviewer rounds use DEFAULT_SWARM_MODEL (Sonnet).
        # Opus is reserved for the adjudicator (terminal gate, not the reviewer).
        # converge_round + converge_round_started are injected so the reviewer (and
        # fixer below) receive the authoritative round from the engine — they must NOT
        # infer the round by counting "## Converge Review" comments on the PR, because a
        # re-triggered cycle starts fresh while old-cycle comments remain (SPEC §9.2).
        reviewer_context = DispatchContext(
            pr_ref=pr_ref,
            contract=_CONVERGE_REVIEWER_CONTRACT,
            model=DEFAULT_SWARM_MODEL,
            max_turns=_REVIEWER_MAX_TURNS,
            forge_token_scope="repo-branch",
            allowed_agent_refs=specialist_refs,
            head_branch=pr_head_branch,
            converge_round=r,
            converge_round_started=round_started.isoformat(),
        )
        # AllHarnessesExhausted propagates up to OrchestratorService; no label change
        # so the PR stays CONVERGING and RC-3 re-arms it on the next tick (SPEC §14.5).
        reviewer_handle = await engine.harness.dispatch(reviewer_context)
        # Persist the handle so RC-3 can poll run status on the next reconcile tick.
        await converge_state.set_last_run_handle(pr_ref, reviewer_handle)
        await engine._await_run(reviewer_handle)

        # Read the verdict from the run result (structured output channel, SPEC §5).
        # get_run_verdict returns None when the reviewer crashed or omitted the block.
        verdict: Verdict | None = await engine.harness.get_run_verdict(reviewer_handle)

        # Wait for any pending checks to complete before computing ci_green (SPEC §7).
        checks = await _poll_checks_until_complete(engine, pr_ref)
        ci_green = _all_checks_green(checks)

        # resolve_blockers reads blockers from the verdict (Row 1) or falls back to
        # the comment-footer heuristic when verdict is None (Rows 2–4, SPEC §8.2).
        blockers = await resolve_blockers(forge, pr_ref, r, round_started, verdict)

        # Extract signatures for no-progress detection.
        # prev_sigs is carried in-memory from the previous round; r==1 → [].
        if verdict is not None:
            curr_sigs = _normalize_sigs(verdict.blocker_signatures)
        else:
            curr_sigs = []

        if verdict is not None:
            accumulated_nits.extend(verdict.nits)
            # Track residual suggestions (may remain at R3 — nitpicker handles them).
            residual_suggestions = verdict.suggestions

        suggestions = verdict.suggestions if verdict is not None else 0
        token = decide_round(
            round_literal, blockers, ci_green, prev_sigs, curr_sigs, suggestions
        )

        # Conditionally persist round (only for advancing decisions — NOT P11 re-arm).
        # P11: escalate:no-verdict when retry_count < NO_VERDICT_RETRY_CAP does NOT advance.
        is_no_verdict_retry = False
        if token == "escalate:no-verdict" and engine.counter is not None:
            retry_count = await engine.counter.get_count(pr_ref, "converge-retry")
            is_no_verdict_retry = retry_count < NO_VERDICT_RETRY_CAP

        if not is_no_verdict_retry:
            await converge_state.set_converge_round(pr_ref, r)

        # Advance prev_sigs for the next round (in-memory, not committed to branch).
        prev_sigs = curr_sigs

        # Act on token.
        if token == "adjudicate":
            # Spotless early-exit or R3-no-blockers: enter adjudication phase.
            return await _run_adjudication_phase(
                engine, pr_ref, pr_head_branch, accumulated_nits, residual_suggestions
            )

        if token == "fix":
            # R1/R2: dispatch the fixer, await it; on timeout → E11.
            # R1 fixer: blockers + suggestions; R2/R3: blockers only.
            # (The reviewer contract controls what the fixer addresses via its comment.)
            # converge_round + converge_round_started are injected so the fixer knows
            # its round authoritatively and can scope comment lookups to the current
            # cycle (SPEC §9.2; same reasoning as the reviewer context above).
            fixer_context = DispatchContext(
                pr_ref=pr_ref,
                contract=_CONVERGE_FIXER_CONTRACT,
                model=DEFAULT_SWARM_MODEL,
                max_turns=_FIXER_MAX_TURNS,
                forge_token_scope="repo-branch",
                allowed_agent_refs=specialist_refs,
                head_branch=pr_head_branch,
                converge_round=r,
                converge_round_started=round_started.isoformat(),
            )
            # AllHarnessesExhausted propagates up to OrchestratorService (SPEC §14.5).
            fixer_handle = await engine.harness.dispatch(fixer_context)
            try:
                completed = await engine._await_run(fixer_handle)
            except SessionLimitHold:
                # Fixer hit session/usage limit.  set_converge_round(r) was already
                # called above (before the fix branch), so the persisted round is r.
                # Roll it back to r-1 so RC-3 re-arms at round r (round-neutral
                # quota HOLD, SPEC §14.8).  The fixer for round r will re-run on
                # the next converge invocation.
                await converge_state.set_converge_round(pr_ref, r - 1)
                raise
            if not completed:
                # Fixer timed out — cancel already happened inside _await_run.
                return await _terminal_escalate(engine, pr_ref)
            # Fixer completed; advance to next round (continue loop).
            continue

        if token == "escalate:no-progress":
            return await _terminal_escalate(engine, pr_ref)

        if token == "escalate:no-verdict":
            # is_no_verdict_retry was computed above.
            if is_no_verdict_retry:
                # Post re-arm comment and increment the retry counter (P11).
                await forge.post_comment(pr_ref, _NO_VERDICT_RETRY_MARKER)
                if engine.counter is not None:
                    await engine.counter.increment(pr_ref, "converge-retry")
                # Do NOT persist round; RC-3 or direct trigger resumes at this round.
                return "CONVERGING"
            return await _terminal_escalate(engine, pr_ref)

        if token == "escalate:ci-red":
            # Trigger CI, then poll until all present checks complete (SPEC §10.2 4g).
            await engine.harness.trigger_ci(pr_ref)
            final_checks = await _poll_checks_until_complete(engine, pr_ref)
            if _all_checks_green(final_checks):
                # P9: CI recovered → enter adjudication phase (not direct approve).
                # residual_suggestions is 0 here (blockers == 0 → suggestions may exist).
                return await _run_adjudication_phase(
                    engine, pr_ref, pr_head_branch, accumulated_nits, residual_suggestions
                )
            return await _terminal_escalate(engine, pr_ref)

        if token == "escalate:cap-reached":
            # D3: work never discarded — always a human problem.
            return await _terminal_escalate(engine, pr_ref)

    # Loop exhausted without a decision (start > CONVERGE_ROUNDS): no further action.
    return "CONVERGING"


async def _finalize_approve(
    engine: Engine, pr_ref: PRRef
) -> PRState:
    """Execute the approve actions (SPEC §10.2 step 4c, amended).

    The canonical approval signal is the ``agent:ready`` label swap — NOT a formal
    GitHub APPROVE review. Posting ``{"event": "APPROVE"}`` on a self-authored PR
    raises HTTP 422 (GitHub rejects self-approval), which would cause the converge task
    to raise, the reconciler (RC-3) to re-arm, and an unbounded re-arm storm.
    The reviewer/adjudicator already posts a human-readable summary comment; the label
    is machine-readable; a formal GitHub review is both redundant and forbidden here.

    Nits are handled by the nitpicker in-loop; no follow-up issue is created here.
    """
    forge = engine.forge
    assert engine.converge_state is not None
    await forge.add_label(pr_ref, LABEL_READY)
    await forge.remove_label(pr_ref, LABEL_CONVERGE)
    # Intentionally no create_review("APPROVE") call — see docstring above.
    # Intentionally no follow-up nits issue — nits are fixed in-loop by the nitpicker.

    if engine.counter is not None:
        await engine.counter.reset(pr_ref, "converge-retry")
        await engine.counter.reset(pr_ref, _RECONVERGE_CHANNEL)
    await engine.converge_state.clear_converge_state(pr_ref)
    return "APPROVED"


def _dedupe_nits(nits: list[str]) -> list[str]:
    """Deduplicate by exact string equality, preserving first-seen order (SPEC §10.2)."""
    seen: set[str] = set()
    result: list[str] = []
    for nit in nits:
        if nit not in seen:
            seen.add(nit)
            result.append(nit)
    return result
