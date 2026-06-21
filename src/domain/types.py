"""Domain types and constants for the orchestrator."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# GitHub auto-closing keyword regex — https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue
# Nine keyword forms: close/closes/closed, fix/fixes/fixed, resolve/resolves/resolved
# ---------------------------------------------------------------------------

_CLOSING_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Entity references
# ---------------------------------------------------------------------------


class RepoRef(BaseModel):
    owner: str
    name: str


class IssueRef(BaseModel):
    repo: RepoRef
    number: int


class PRRef(BaseModel):
    repo: RepoRef
    number: int


# ---------------------------------------------------------------------------
# Run handle — serializable, round-trip lossless
# ---------------------------------------------------------------------------


class RunHandle(BaseModel):
    run_id: str  # stable string identifier

    @classmethod
    def from_run_id(cls, run_id: str) -> RunHandle:
        return cls(run_id=run_id)


# ---------------------------------------------------------------------------
# Run state / conclusion (SPEC.md §7)
# ---------------------------------------------------------------------------

RunState = Literal["queued", "in_progress", "completed"]
RunConclusion = Literal["success", "failure", "cancelled"]


class RunStatus(BaseModel):
    state: RunState
    conclusion: RunConclusion | None = None


# ---------------------------------------------------------------------------
# Issue state
# ---------------------------------------------------------------------------

IssueState = Literal["PENDING", "QUEUED", "ESCALATED", "CLOSED"]

# ---------------------------------------------------------------------------
# PR state (includes EMPTY — derived-only, no label)
# ---------------------------------------------------------------------------

PRState = Literal["MERGED", "ESCALATED", "APPROVED", "EMPTY", "CONVERGING", "BUILDING"]

# ---------------------------------------------------------------------------
# HealthReport
# ---------------------------------------------------------------------------


class HealthReport(BaseModel):
    implementing: int
    converge: int
    ready: int
    needs_human: int
    stale_drafts: int
    in_flight: int
    report_md: str
    verdict: Literal["BLOCKED", "AT_RISK", "ON_TRACK"]


# ---------------------------------------------------------------------------
# Forge types
# ---------------------------------------------------------------------------


class Issue(BaseModel):
    ref: IssueRef
    title: str
    body: str
    labels: list[str]
    closed: bool
    author: str


class PR(BaseModel):
    ref: PRRef
    title: str
    body: str
    head_branch: str
    draft: bool
    merged: bool
    labels: list[str]
    changed_files: int
    state: Literal["open", "closed"]


class Comment(BaseModel):
    id: str
    body: str
    created_at: datetime
    author: str


class CheckRun(BaseModel):
    name: str
    state: RunState
    conclusion: RunConclusion | None = None


# ---------------------------------------------------------------------------
# Dispatch context
# ---------------------------------------------------------------------------


class DispatchContext(BaseModel):
    # SEALED per SPEC §9.2: extra fields rejected to enforce I3 (no credential injection)
    model_config = ConfigDict(extra="forbid")

    issue_ref: IssueRef | None = None
    pr_ref: PRRef | None = None
    contract: str
    model: str
    max_turns: int
    forge_token_scope: Literal["repo-comment", "repo-branch"]
    allowed_agent_refs: list[str] | None = None


# ---------------------------------------------------------------------------
# RunSummary / RunDetail for SessionPort
# ---------------------------------------------------------------------------


class RunSummary(BaseModel):
    run_id: str
    repo: RepoRef
    type: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None


class RunEvent(BaseModel):
    event_type: str
    data: dict[str, object]
    timestamp: datetime


class RunDetail(BaseModel):
    run_id: str
    repo: RepoRef
    type: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    events: list[RunEvent] = []


# ---------------------------------------------------------------------------
# Constants (single-source — SPEC.md §7)
# ---------------------------------------------------------------------------

CONVERGE_ROUNDS = 3
MAX_REDISPATCHES = 2
RECONCILER_STALE_REDISPATCH_CAP = 3
ISSUE_REDISPATCH_CAP = 3
STALE_DRAFT_THRESHOLD_S = 1200
REARM_RECENT_GUARD_S = 300
ISSUE_COOLDOWN_S = 900
CI_WAIT_S = 480
NO_VERDICT_RETRY_CAP = 2
RECONCILER_CRON = "*/15 * * * *"
PARALLEL_SPECIALIST_CAP = 4
AT_RISK_THRESHOLD = 5
AWAITING_PROMOTION_NUDGE_S = 86400
DEFAULT_SWARM_MODEL = "claude-sonnet-4-6"
ADJUDICATION_MODEL = "claude-opus-4-8"

# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

LABEL_AGENT_WORK = "agent-work"
LABEL_NEEDS_HUMAN = "needs-human"
LABEL_IMPLEMENTING = "agent:implementing"
LABEL_CONVERGE = "converge"
LABEL_READY = "agent:ready"
LABEL_TRIAGE = "triage"
LABEL_AWAITING_PROMOTION = "awaiting-promotion"

# ---------------------------------------------------------------------------
# Protected paths
# ---------------------------------------------------------------------------

PROTECTED_PATHS = [
    ".github/workflows/**",
    "ARCHITECTURE.md",
    "SECURITY.md",
    "COMPLIANCE.md",
    ".agents/**",
    "agents/**",
]

# ---------------------------------------------------------------------------
# Specialist routing
# ---------------------------------------------------------------------------

CONVERGE_REVIEW_BASE = [
    "engineering-security-engineer.md",
    "engineering-code-reviewer.md",
]
