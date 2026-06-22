"""Engine.converge — converge sub-machine, round-1 approve happy path (SPEC §10.2).

This module implements the converge entry point. The full 3-round loop is specified in
SPEC §10.2; this build covers the happy path: idempotency gate → protected-path check (E1)
→ empty-diff check (E6) → seed sentinel → reviewer dispatch → poll CI → resolve_blockers →
decide_round → on `approve` finalize. The `fix`/escalation branches are stubbed to escalate
or no-op pending their dedicated issues.
"""

from __future__ import annotations

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
    CONVERGE_ROUNDS,
    DEFAULT_SWARM_MODEL,
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
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
_REVIEWER_MAX_TURNS = 60


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
        reviewer_context = DispatchContext(
            pr_ref=pr_ref,
            contract=_CONVERGE_REVIEWER_CONTRACT,
            model=model,
            max_turns=_REVIEWER_MAX_TURNS,
            forge_token_scope="repo-branch",
            allowed_agent_refs=specialist_refs,
        )
        reviewer_handle = await engine.harness.dispatch(reviewer_context)
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

        await converge_state.set_converge_round(pr_ref, r)
        await forge.copy_file_on_branch(
            pr_ref, _VERDICT_PATH, f".converge-verdict-r{r}.json"
        )

        if token == "approve":
            return await _finalize_approve(engine, pr_ref, accumulated_nits)

        # fix / escalation branches are out of scope for the happy-path build; a
        # non-approve outcome escalates to a human (placeholder pending their issues).
        await forge.add_label(pr_ref, LABEL_NEEDS_HUMAN)
        await converge_state.clear_converge_state(pr_ref)
        return "ESCALATED"

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
