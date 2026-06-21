"""Derive state functions — pure, synchronous."""

from __future__ import annotations

from src.domain.types import (
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    IssueState,
    PRState,
)


def derive_issue_state(labels: list[str], closed: bool) -> IssueState:
    """Derive the canonical IssueState from labels and closed flag.

    Priority: closed > needs-human > default QUEUED.
    """
    if closed:
        return "CLOSED"
    if LABEL_NEEDS_HUMAN in labels:
        return "ESCALATED"
    return "QUEUED"


def derive_pr_state(
    labels: list[str],
    draft: bool,
    merged: bool,
    changed_files: int,
) -> PRState:
    """Derive the canonical PRState from PR attributes.

    Priority order:
      MERGED > ESCALATED > APPROVED > EMPTY (non-draft, 0 files) > CONVERGING > BUILDING
    """
    if merged:
        return "MERGED"
    if LABEL_NEEDS_HUMAN in labels:
        return "ESCALATED"
    if LABEL_READY in labels:
        return "APPROVED"
    # EMPTY check BEFORE CONVERGING
    if changed_files == 0 and not draft:
        return "EMPTY"
    if LABEL_CONVERGE in labels and not draft:
        return "CONVERGING"
    return "BUILDING"
