"""Engine.reconcile — four RC channels (SPEC §4, §10.3).

Runs RC-1 (stale implementing recovery), RC-2 (merge-conflict), RC-3 (converge
re-arm), RC-4 (orphan-issue redispatch), and RC-5 (awaiting-promotion nudge).
Returns a ``ReconcileReport`` with counters for each channel.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.decisions.decide_conflict_action import decide_conflict_action
from src.decisions.decide_rearm_action import decide_rearm_action
from src.decisions.decide_redispatch_action import decide_redispatch_action
from src.decisions.decide_stale_action import decide_stale_action
from src.decisions.route_entry import route_entry
from src.domain.types import (
    AWAITING_PROMOTION_NUDGE_S,
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    LABEL_CONVERGE,
    LABEL_IMPLEMENTING,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    STALE_DRAFT_THRESHOLD_S,
    DispatchContext,
    RepoRef,
    RunStatus,
)

if TYPE_CHECKING:
    from src.engine.dispatch import Engine

# Workflow name triggered when RC-3 re-arms a converge PR (P13).
_CONVERGE_WORKFLOW_NAME = "orchestrator-converge.yml"

# Audit-marker comments embedded in action posts (SPEC §8.2a).
_MARKER_STALE_PR = "<!-- orchestrator:redispatch ch=stale-pr -->"
_MARKER_ORPHAN = "<!-- orchestrator:redispatch ch=orphan -->"


@dataclass
class ReconcileReport:
    """Per-tick summary of reconciler actions (SPEC §10.3)."""

    stale_acted: int = field(default=0)
    conflicts_flagged: int = field(default=0)
    rearmed: int = field(default=0)
    redispatched: int = field(default=0)
    escalated: int = field(default=0)


# ---------------------------------------------------------------------------
# RC-1 — Stale implementing recovery
# ---------------------------------------------------------------------------


async def _rc1_stale(engine: Engine, repo: RepoRef) -> tuple[int, int]:
    """Process RC-1 channel; returns (stale_acted, escalated) counts."""
    # Scope: PRs with agent:implementing AND NOT (converge OR needs-human OR agent:ready)
    implementing_prs = await engine.forge.list_prs(
        repo, state="open", labels=[LABEL_IMPLEMENTING]
    )

    stale_acted = 0
    rc1_escalated = 0
    now = time.time()

    for pr in implementing_prs:
        # Scope exclusions: skip terminal labels
        if LABEL_NEEDS_HUMAN in pr.labels:
            continue
        if LABEL_READY in pr.labels:
            continue
        # Non-draft PRs with converge label belong to RC-3 scope; skip them.
        # Draft PRs with converge label (crash-window: converge added before set_pr_ready)
        # remain in RC-1 scope so the mark-ready row can recover them.
        if LABEL_CONVERGE in pr.labels and not pr.draft:
            continue

        pr_ref = pr.ref

        # Staleness check: last dispatch run > STALE_DRAFT_THRESHOLD_S
        last_run_at = await engine.forge.last_dispatch_run_at(pr_ref)
        if last_run_at is not None:
            elapsed = now - last_run_at.timestamp()
            if elapsed <= STALE_DRAFT_THRESHOLD_S:
                continue  # Not stale yet

        # Gather inputs for decide_stale_action
        check_runs = await engine.forge.get_check_runs(pr_ref)
        ci_runs = len(check_runs)
        failing_count = sum(
            1 for cr in check_runs
            if cr.state == "completed" and cr.conclusion not in ("success", "skipped", "neutral")
        )
        has_converge = LABEL_CONVERGE in pr.labels
        has_diff = pr.changed_files > 0
        is_draft = pr.draft

        closing_issue = await engine.forge.get_closing_issue(pr_ref)
        has_issue = closing_issue is not None

        redispatch_count = 0
        if engine.counter is not None:
            redispatch_count = await engine.counter.get_count(pr_ref, "stale-pr")

        action = decide_stale_action(
            redispatch_count, ci_runs, has_converge, failing_count, has_issue, has_diff, is_draft
        )

        # Execute action
        if action == "trigger-ci":
            await engine.harness.trigger_ci(pr_ref)
            stale_acted += 1

        elif action == "redispatch":
            # Post audit marker before incrementing so count is readable after
            if engine.counter is not None:
                new_count = await engine.counter.increment(pr_ref, "stale-pr")
            else:
                new_count = redispatch_count + 1
            await engine.forge.post_comment(
                pr_ref,
                f"{_MARKER_STALE_PR} count={new_count}",
            )
            # Re-dispatch the implementing agent for the closing issue
            if closing_issue is not None:
                result = route_entry("issues")
                context = DispatchContext(
                    issue_ref=closing_issue,
                    pr_ref=pr_ref,
                    contract=result.contract,
                    model=result.model,
                    max_turns=result.max_turns,
                    forge_token_scope="repo-branch",
                    allowed_agent_refs=None,
                )
                await engine.harness.dispatch(context)
            stale_acted += 1

        elif action == "mark-ready":
            await engine.forge.set_pr_ready(pr_ref)
            stale_acted += 1

        elif action == "mark-ready-and-converge":
            await engine.forge.set_pr_ready(pr_ref)
            await engine.forge.add_label(pr_ref, LABEL_CONVERGE)
            stale_acted += 1

        elif action == "needs-human":
            await engine.forge.add_label(pr_ref, LABEL_NEEDS_HUMAN)
            stale_acted += 1

        elif action == "escalate":
            await engine.forge.add_label(pr_ref, LABEL_NEEDS_HUMAN)
            stale_acted += 1
            rc1_escalated += 1

    return stale_acted, rc1_escalated


# ---------------------------------------------------------------------------
# RC-2 — Merge-conflict
# ---------------------------------------------------------------------------


async def _rc2_conflict(engine: Engine, repo: RepoRef) -> tuple[int, int]:
    """Process RC-2 channel; returns (conflicts_flagged, escalated) counts."""
    open_prs = await engine.forge.list_prs(repo, state="open")

    conflicts_flagged = 0
    escalated = 0

    for pr in open_prs:
        mergeable = await engine.forge.get_mergeable(pr.ref)
        already_needs_human = LABEL_NEEDS_HUMAN in pr.labels
        action = decide_conflict_action(mergeable, already_needs_human)

        if action == "escalate":
            await engine.forge.add_label(pr.ref, LABEL_NEEDS_HUMAN)
            conflicts_flagged += 1
            escalated += 1

    return conflicts_flagged, escalated


# ---------------------------------------------------------------------------
# RC-3 — Converge re-arm
# ---------------------------------------------------------------------------


async def _rc3_rearm(engine: Engine, repo: RepoRef) -> int:
    """Process RC-3 channel; returns rearmed count."""
    # Scope: non-draft PRs labeled converge AND NOT needs-human
    converge_prs = await engine.forge.list_prs(
        repo, state="open", labels=[LABEL_CONVERGE]
    )

    rearmed = 0
    now_dt = datetime.now(tz=UTC)

    for pr in converge_prs:
        # Scope: non-draft only
        if pr.draft:
            continue
        # Scope: NOT needs-human (scope filter)
        if LABEL_NEEDS_HUMAN in pr.labels:
            continue

        pr_ref = pr.ref
        check_runs = await engine.forge.get_check_runs(pr_ref)
        ci_runs = len(check_runs)
        has_terminal_label = LABEL_READY in pr.labels or LABEL_NEEDS_HUMAN in pr.labels

        # Determine the last converge run and seconds since it started (SPEC §8.6)
        run: RunStatus | None = None
        seconds_since_last_run: int | None = None

        if ci_runs == 0:
            # No runs at all — pass None (row 1 will fire)
            seconds_since_last_run = None
        else:
            # Try to get the most recent converge harness run
            workflow_run_at = await engine.forge.last_workflow_run_at(
                pr_ref, _CONVERGE_WORKFLOW_NAME
            )
            if workflow_run_at is not None:
                seconds_since_last_run = int((now_dt - workflow_run_at).total_seconds())
            # run is None when we don't have a RunHandle to poll — pass None

        action = decide_rearm_action(
            ci_runs, run, has_terminal_label, seconds_since_last_run, has_needs_human=False
        )

        if action == "trigger-ci":
            await engine.harness.trigger_ci(pr_ref)
            rearmed += 1

        elif action == "rearm":
            await engine.harness.trigger_workflow(
                _CONVERGE_WORKFLOW_NAME,
                pr.head_branch,
                {"pr_number": str(pr_ref.number)},
            )
            rearmed += 1

    return rearmed


# ---------------------------------------------------------------------------
# RC-4 — Orphan-issue redispatch
# ---------------------------------------------------------------------------


async def _rc4_orphan(engine: Engine, repo: RepoRef) -> tuple[int, int]:
    """Process RC-4 channel; returns (redispatched, escalated) counts."""
    # Scope: open issues with agent-work label
    agent_work_issues = await engine.forge.list_issues(repo, [LABEL_AGENT_WORK])

    redispatched = 0
    escalated = 0
    now_dt = datetime.now(tz=UTC)

    for issue in agent_work_issues:
        issue_ref = issue.ref

        # Check if an open PR references this issue
        open_prs = await engine.forge.list_prs(repo, state="open")
        has_open_pr = any(
            issue_ref.number in {int(m) for m in _extract_closing_numbers(pr.body)}
            for pr in open_prs
        )

        # Compute seconds since last activity
        comments = await engine.forge.list_comments(issue_ref)
        seconds_since_last_activity: int | None = None
        if comments:
            last_comment_at = max(c.created_at for c in comments)
            if last_comment_at.tzinfo is None:
                last_comment_at = last_comment_at.replace(tzinfo=UTC)
            seconds_since_last_activity = int(
                (now_dt - last_comment_at).total_seconds()
            )

        redispatch_count = 0
        if engine.counter is not None:
            redispatch_count = await engine.counter.get_count(issue_ref, "orphan")

        action = decide_redispatch_action(
            has_open_pr, seconds_since_last_activity, redispatch_count
        )

        if action == "redispatch":
            # Increment counter first, then post audit marker
            if engine.counter is not None:
                new_count = await engine.counter.increment(issue_ref, "orphan")
            else:
                new_count = redispatch_count + 1
            await engine.forge.post_comment(
                issue_ref,
                f"@claude {_MARKER_ORPHAN} count={new_count}",
            )
            # Re-dispatch the implementer for this issue
            result = route_entry("issues")
            context = DispatchContext(
                issue_ref=issue_ref,
                contract=result.contract,
                model=result.model,
                max_turns=result.max_turns,
                forge_token_scope="repo-branch",
                allowed_agent_refs=None,
            )
            await engine.harness.dispatch(context)
            redispatched += 1

        elif action == "escalate":
            await engine.forge.add_label(issue_ref, LABEL_NEEDS_HUMAN)
            escalated += 1

    return redispatched, escalated


# ---------------------------------------------------------------------------
# RC-5 — Awaiting-promotion nudge
# ---------------------------------------------------------------------------


async def _rc5_nudge(engine: Engine, repo: RepoRef) -> None:
    """Process RC-5 channel — nudge stale awaiting-promotion issues."""
    pending_issues = await engine.forge.list_issues(repo, [LABEL_AWAITING_PROMOTION])
    now_dt = datetime.now(tz=UTC)

    for issue in pending_issues:
        # Determine when the issue was last active (use comments or issue creation)
        comments = await engine.forge.list_comments(issue.ref)
        if comments:
            last_at = max(c.created_at for c in comments)
        else:
            # Fallback: use a far-past sentinel so nudge always fires when no comments
            last_at = datetime(2000, 1, 1, tzinfo=UTC)

        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=UTC)

        seconds_idle = int((now_dt - last_at).total_seconds())
        if seconds_idle >= AWAITING_PROMOTION_NUDGE_S:
            await engine.forge.post_comment(
                issue.ref,
                (
                    "<!-- orchestrator:awaiting-promotion-nudge --> "
                    "This issue has been waiting for human promotion for over 24 hours. "
                    "Please review and promote or decline."
                ),
            )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _extract_closing_numbers(body: str) -> list[str]:
    """Extract issue numbers from GitHub auto-closing keywords in PR body."""
    import re

    return re.findall(
        r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
        body,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# Main reconcile entry point
# ---------------------------------------------------------------------------


async def reconcile(engine: Engine, repo: RepoRef) -> ReconcileReport:
    """Run all four RC channels concurrently; return ReconcileReport (SPEC §10.3).

    Channels are independent and operate on disjoint entity sets so they can run
    concurrently. Within each channel, entities are processed serially to avoid
    conflicting label writes.
    """
    (
        stale_result,
        conflict_result,
        rearm_result,
        orphan_result,
        _,  # RC-5 returns None
    ) = await asyncio.gather(
        _rc1_stale(engine, repo),
        _rc2_conflict(engine, repo),
        _rc3_rearm(engine, repo),
        _rc4_orphan(engine, repo),
        _rc5_nudge(engine, repo),
    )

    stale_acted, rc1_escalated = stale_result
    conflicts_flagged, rc2_escalated = conflict_result
    rearmed: int = rearm_result
    redispatched, rc4_escalated = orphan_result

    # escalated = RC-1 escalate actions + RC-2 escalate actions + RC-4 escalate actions
    # (SPEC §10.3: each escalation from any channel increments this field once)
    escalated = rc1_escalated + rc2_escalated + rc4_escalated

    return ReconcileReport(
        stale_acted=stale_acted,
        conflicts_flagged=conflicts_flagged,
        rearmed=rearmed,
        redispatched=redispatched,
        escalated=escalated,
    )
