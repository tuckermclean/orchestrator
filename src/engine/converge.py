"""Engine.converge — full 3-round converge sub-machine (SPEC §5, §6, §8.3, §8.4, §10.2).

Implements the complete converge loop: happy-path approve, fix (R1/R2), and all escalation
paths: no-progress (E2), no-verdict (E3), ci-red (E4), cap-reached (E5), fixer-timeout (E11).
Protected-path (E1) and empty-diff (E6) gates are also implemented.
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
    BLOCKING_CI_CHECKS,
    CI_WAIT_S,
    CONVERGE_ROUNDS,
    DEFAULT_SWARM_MODEL,
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    NO_VERDICT_RETRY_CAP,
    POLL_INTERVAL_S,
    PROTECTED_PATHS,
    SENTINEL_SIGNATURE,
    SENTINEL_VERDICT,
    CheckRun,
    DispatchContext,
    PRRef,
    PRState,
    Verdict,
)

if TYPE_CHECKING:
    from src.engine.dispatch import Engine

_VERDICT_PATH = ".converge-verdict.json"
_CONVERGE_REVIEWER_CONTRACT = "agents/converge-reviewer.md"
_CONVERGE_FIXER_CONTRACT = "agents/converge-fixer.md"
_REVIEWER_MAX_TURNS = 60
_FIXER_MAX_TURNS = 60
_NO_VERDICT_RETRY_MARKER = "<!-- orchestrator:converge-retry -->"


def _touches_protected_path(changed_paths: list[str]) -> bool:
    """True if any changed path matches a PROTECTED_PATHS glob (gitignore semantics)."""
    spec = pathspec.PathSpec.from_lines("gitignore", PROTECTED_PATHS)
    return any(spec.match_file(path) for path in changed_paths)


def _ci_green(checks: list[CheckRun]) -> bool:
    """All BLOCKING_CI_CHECKS present and green (success/skipped/neutral) — SPEC §7."""
    by_name = {c.name: c for c in checks}
    for name in BLOCKING_CI_CHECKS:
        check = by_name.get(name)
        if check is None or check.state != "completed":
            return False
        if check.conclusion not in _CI_GREEN_CONCLUSIONS:
            return False
    return True


def _normalize_sigs(sigs: list[str]) -> list[str]:
    if sigs == [SENTINEL_SIGNATURE]:
        return []
    return sigs


async def _read_verdict(engine: Engine, pr_ref: PRRef) -> Verdict:
    """Read .converge-verdict.json from the PR branch; sentinel if absent/unparseable."""
    raw = await engine.forge.get_file_contents(pr_ref, _VERDICT_PATH)
    if raw is None:
        return SENTINEL_VERDICT
    try:
        return Verdict.model_validate_json(raw)
    except ValueError:
        return SENTINEL_VERDICT


async def _poll_ci_until_green(engine: Engine, pr_ref: PRRef) -> bool:
    """Poll all BLOCKING_CI_CHECKS up to CI_WAIT_S after a trigger_ci.

    Returns True if all checks become green within the deadline, False otherwise.
    Yields to the event loop between polls so async tasks are not starved.
    """
    deadline = time.monotonic() + CI_WAIT_S
    while True:
        checks = await engine.forge.get_check_runs(pr_ref)
        if _ci_green(checks):
            return True
        if time.monotonic() >= deadline:
            return False
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


async def converge(engine: Engine, pr_ref: PRRef) -> PRState:
    """Run the converge sub-machine for one PR (SPEC §10.2)."""
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

    for r in range(start, CONVERGE_ROUNDS + 1):
        round_literal = cast("Literal[1, 2, 3]", r)
        round_started = datetime.now(tz=UTC)
        await converge_state.set_round_started(pr_ref, round_started)

        # Seed init sentinel BEFORE dispatching the reviewer (crash fail-safe).
        await forge.put_file_on_branch(
            pr_ref,
            _VERDICT_PATH,
            SENTINEL_VERDICT.model_dump_json().encode(),
            "chore: init converge sentinel",
        )

        specialist_refs = decide_specialists(changed_paths, r)
        model = ADJUDICATION_MODEL if r == CONVERGE_ROUNDS else DEFAULT_SWARM_MODEL
        # P0.4: head_branch ensures the reviewer/fixer operate on the PR diff.
        pr_head_branch: str = pr.head_branch
        reviewer_context = DispatchContext(
            pr_ref=pr_ref,
            contract=_CONVERGE_REVIEWER_CONTRACT,
            model=model,
            max_turns=_REVIEWER_MAX_TURNS,
            forge_token_scope="repo-branch",
            allowed_agent_refs=specialist_refs,
            head_branch=pr_head_branch,
        )
        reviewer_handle = await engine.harness.dispatch(reviewer_context)
        # Persist the handle so RC-3 can poll run status on the next reconcile tick.
        await converge_state.set_last_run_handle(pr_ref, reviewer_handle)
        await engine._await_run(reviewer_handle)

        checks = await forge.get_check_runs(pr_ref)
        ci_green = _ci_green(checks)

        blockers = await resolve_blockers(forge, pr_ref, r, round_started)
        verdict = await _read_verdict(engine, pr_ref)
        curr_sigs = _normalize_sigs(verdict.blocker_signatures)
        prev_sigs: list[str] = []
        if r > 1:
            prev_raw = await forge.get_file_contents(
                pr_ref, f".converge-verdict-r{r - 1}.json"
            )
            if prev_raw is not None:
                try:
                    prev_sigs = _normalize_sigs(
                        Verdict.model_validate_json(prev_raw).blocker_signatures
                    )
                except ValueError:
                    prev_sigs = []
        accumulated_nits.extend(verdict.nits)

        token = decide_round(round_literal, blockers, ci_green, prev_sigs, curr_sigs)

        # Conditionally persist round (only for advancing decisions — NOT P11 re-arm).
        # P11: escalate:no-verdict when retry_count < NO_VERDICT_RETRY_CAP does NOT advance.
        is_no_verdict_retry = False
        if token == "escalate:no-verdict" and engine.counter is not None:
            retry_count = await engine.counter.get_count(pr_ref, "converge-retry")
            is_no_verdict_retry = retry_count < NO_VERDICT_RETRY_CAP

        if not is_no_verdict_retry:
            await converge_state.set_converge_round(pr_ref, r)

        await forge.copy_file_on_branch(
            pr_ref, _VERDICT_PATH, f".converge-verdict-r{r}.json"
        )

        # Act on token.
        if token == "approve":
            return await _finalize_approve(engine, pr_ref, accumulated_nits)

        if token == "fix":
            # R1/R2: dispatch the fixer, await it; on timeout → E11.
            fixer_context = DispatchContext(
                pr_ref=pr_ref,
                contract=_CONVERGE_FIXER_CONTRACT,
                model=DEFAULT_SWARM_MODEL,
                max_turns=_FIXER_MAX_TURNS,
                forge_token_scope="repo-branch",
                allowed_agent_refs=specialist_refs,
                head_branch=pr_head_branch,
            )
            fixer_handle = await engine.harness.dispatch(fixer_context)
            completed = await engine._await_run(fixer_handle)
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
            # OQ-1: trigger CI, then re-poll ALL BLOCKING_CI_CHECKS up to CI_WAIT_S.
            await engine.harness.trigger_ci(pr_ref)
            recovered = await _poll_ci_until_green(engine, pr_ref)
            if recovered:
                # P9: full approve actions.
                return await _finalize_approve(engine, pr_ref, accumulated_nits)
            return await _terminal_escalate(engine, pr_ref)

        if token == "escalate:cap-reached":
            # D3: work never discarded — always a human problem.
            return await _terminal_escalate(engine, pr_ref)

    # Loop exhausted without a decision (start > CONVERGE_ROUNDS): no further action.
    return "CONVERGING"


async def _finalize_approve(
    engine: Engine, pr_ref: PRRef, accumulated_nits: list[str]
) -> PRState:
    """Execute the `approve` token actions (SPEC §10.2 step 4c)."""
    forge = engine.forge
    assert engine.converge_state is not None
    await forge.add_label(pr_ref, LABEL_READY)
    await forge.remove_label(pr_ref, LABEL_CONVERGE)
    await forge.create_review(pr_ref, "APPROVE", "Converge approved: zero blockers, CI green.")

    deduped = _dedupe_nits(accumulated_nits)
    if deduped:
        await forge.create_issue(
            pr_ref.repo,
            "Converge follow-up nits",
            "\n".join(f"- {nit}" for nit in deduped),
        )

    if engine.counter is not None:
        await engine.counter.reset(pr_ref, "converge-retry")
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
