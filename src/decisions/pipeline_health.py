"""Pipeline health decision function — async, calls ForgePort."""

from __future__ import annotations

from src.domain.types import (
    AT_RISK_THRESHOLD,
    LABEL_CONVERGE,
    LABEL_IMPLEMENTING,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    PR,
    HealthReport,
    RepoRef,
)
from src.ports.base import ForgePort


def _has_label(pr: PR, label: str) -> bool:
    return label in pr.labels


async def pipeline_health(repo: RepoRef, forge: ForgePort) -> HealthReport:
    """Compute pipeline health from open PRs.

    Returns a HealthReport with counts and a verdict.
    """
    open_prs = await forge.list_prs(repo, state="open", labels=None)

    implementing_set: set[str] = set()
    converge_set: set[str] = set()
    ready_count = 0
    needs_human_count = 0
    stale_drafts = 0

    for pr in open_prs:
        pr_key = f"{pr.ref.repo.owner}/{pr.ref.repo.name}#{pr.ref.number}"
        is_implementing = _has_label(pr, LABEL_IMPLEMENTING)
        is_converge = _has_label(pr, LABEL_CONVERGE)
        is_ready = _has_label(pr, LABEL_READY)
        is_needs_human = _has_label(pr, LABEL_NEEDS_HUMAN)

        if is_implementing:
            implementing_set.add(pr_key)
        if is_converge:
            converge_set.add(pr_key)
        if is_ready:
            ready_count += 1
        if is_needs_human:
            needs_human_count += 1
        if pr.draft and is_implementing:
            stale_drafts += 1

    implementing_count = len(implementing_set)
    converge_count = len(converge_set)
    in_flight = len(implementing_set | converge_set)

    # Verdict: BLOCKED beats AT_RISK
    if needs_human_count > 0:
        verdict = "BLOCKED"
    elif in_flight >= AT_RISK_THRESHOLD:
        verdict = "AT_RISK"
    else:
        verdict = "ON_TRACK"

    report_md = (
        "## Pipeline Health\n"
        f"- implementing: {implementing_count}\n"
        f"- converge: {converge_count}\n"
        f"- ready: {ready_count}\n"
        f"- needs_human: {needs_human_count}\n"
        f"- stale_drafts: {stale_drafts}\n"
        f"- in_flight: {in_flight}\n"
        f"- verdict: **{verdict}**\n"
    )

    return HealthReport(
        implementing=implementing_count,
        converge=converge_count,
        ready=ready_count,
        needs_human=needs_human_count,
        stale_drafts=stale_drafts,
        in_flight=in_flight,
        report_md=report_md,
        verdict=verdict,  # type: ignore[arg-type]
    )
