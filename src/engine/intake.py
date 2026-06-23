"""Engine.intake — intake decision + atomic label swap + triager dispatch + audit."""

from __future__ import annotations

from dataclasses import dataclass

from src.db.audit import AuditLog
from src.decisions.intake import decide_intake
from src.decisions.triager_reconcile import (
    TRIAGER_REC_QUEUE,
    is_triager_comment,
    parse_triager_recommendation,
)
from src.domain.types import (
    DEFAULT_SWARM_MODEL,
    LABEL_AGENT_WORK,
    LABEL_AWAITING_PROMOTION,
    LABEL_TRIAGE,
    DispatchContext,
    IssueRef,
    RunHandle,
)
from src.ports.base import ForgePort, HarnessPort, SessionPort


@dataclass(frozen=True)
class IntakeResult:
    """Result of ``IntakeEngine.intake``.

    Carries both the triager ``RunHandle`` and the ``decision`` so callers can
    spawn the divergence-reconciliation background task with the correct decision
    string without re-deriving it.
    """

    handle: RunHandle | None
    """Triager dispatch handle, or ``None`` when intake was skipped (idempotency guard)."""

    decision: str | None
    """``'admit'`` or ``'queue'``, or ``None`` when intake was skipped."""


# Triager contract path (orchestration-agent contract file)
_TRIAGER_CONTRACT = "agents/triager.md"

# Triager max turns — a single structured comment; low cap
_TRIAGER_MAX_TURNS = 10

# Reconciliation comment template — posted on the issue when intake auto-admits
# despite the triager recommending caution (SPEC §10.4 reconciliation step).
_RECONCILIATION_COMMENT = (
    "<!-- orchestrator:intake-reconciliation -->\n"
    "**Intake reconciliation note:** this issue was auto-admitted ({reason}) "
    "despite the triager recommending *{triager_rec}*. "
    "The intake decision (trust axis) takes precedence; the triager recommendation "
    "is advisory (scope/risk axis). No action required — recorded for audit transparency."
)


class IntakeEngine:
    """Handles the intake/triage gate (SPEC §10.4)."""

    def __init__(
        self,
        forge: ForgePort,
        harness: HarnessPort,
        session: SessionPort,
        audit: AuditLog,
        allowlist: list[str],
        owner: str = "",
    ) -> None:
        self.forge = forge
        self.harness = harness
        self.session = session
        self.audit = audit
        self.allowlist = allowlist
        self.owner = owner

    async def intake(self, issue_ref: IssueRef) -> IntakeResult:
        """Run the intake gate for one issue.

        Steps (SPEC §10.4):
          1. Fetch the issue.
          1a. Idempotency guard (SPEC §10 intent): if the issue already carries
              LABEL_TRIAGE, intake has already run — skip to avoid re-dispatching
              a second triager.  This protects against re-delivery of opened events
              and the labeled-feedback loop fixed in issue #108.
          2. decide_intake(issue, allowlist) → 'admit' | 'queue'  [pure, sync — I4]
          3. Dispatch triager (forge_token_scope='repo-comment' — I5).
          4. set_labels([LABEL_TRIAGE, LABEL_AGENT_WORK | LABEL_AWAITING_PROMOTION])  (atomic — I7)
          5. Write audit record to DB (I6) — after observable state is committed.
          6. Return IntakeResult(handle, decision) so callers can spawn the divergence-
             reconciliation background task (SPEC §10.4 step 6).

        Returns ``IntakeResult(handle=None, decision=None)`` when intake was skipped
        (idempotency guard fired) or ``IntakeResult(handle, decision)`` otherwise.

        Note: triager divergence reconciliation (SPEC §10.4 step 6) is performed
        asynchronously via ``reconcile_triager_divergence`` after the triager agent
        has posted its comment.  The caller (OrchestratorService) spawns that step
        as a background task so intake itself remains non-blocking.
        """
        issue = await self.forge.get_issue(issue_ref)

        # Step 1a: idempotency guard — LABEL_TRIAGE is set atomically in step 4 of
        # the first intake run.  Its presence means intake already completed for this
        # issue; re-running would dispatch a redundant triager and override labels.
        # This is the defence-in-depth guard required by SPEC §10's idempotency intent
        # and the fix for the labeled-feedback loop in issue #108.
        if LABEL_TRIAGE in issue.labels:
            return IntakeResult(handle=None, decision=None)

        # Step 2: pure synchronous decision (I4 — never await this)
        decision = decide_intake(issue, self.allowlist, self.owner)

        # Step 3: dispatch triager (read-only; I5 — must use "repo-comment" scope)
        triager_context = DispatchContext(
            issue_ref=issue_ref,
            contract=_TRIAGER_CONTRACT,
            model=DEFAULT_SWARM_MODEL,
            max_turns=_TRIAGER_MAX_TURNS,
            forge_token_scope="repo-comment",  # I5 — MUST be repo-comment, never repo-branch
            allowed_agent_refs=None,
        )
        triager_handle = await self.harness.dispatch(triager_context)

        # Step 4: atomic label swap (I7 — set_labels has PUT semantics; no TOCTOU window)
        if decision == "admit":
            await self.forge.set_labels(issue_ref, [LABEL_TRIAGE, LABEL_AGENT_WORK])
        else:
            await self.forge.set_labels(issue_ref, [LABEL_TRIAGE, LABEL_AWAITING_PROMOTION])

        # Step 5: audit every intake decision (I6) — written AFTER observable state is set
        await self.audit.record(
            repo=issue_ref.repo,
            entity_ref=issue_ref,
            action=f"intake:{decision}",
        )

        return IntakeResult(handle=triager_handle, decision=decision)

    async def reconcile_triager_divergence(
        self,
        issue_ref: IssueRef,
        intake_decision: str,
    ) -> bool:
        """Detect and surface intake/triager recommendation divergence (SPEC §10.4 step 6).

        Called AFTER the triager agent has completed and posted its structured comment.
        Reads the issue's comments, finds the triager comment (identified by the
        ``## Triage Summary`` header), parses its ``**Recommended action**`` field, and
        compares it against ``intake_decision``.

        Divergence condition: ``intake_decision == "admit"`` AND triager recommends
        ``"queue for human review"``.  The inverse (decision=queue, triager=admit)
        is not a concern — the system is conservative when it queues.

        When divergence is detected:
          - Posts a reconciliation comment on the issue (human-visible; the control plane
            posts it directly, not the triager agent, so I5 is preserved).
          - Writes an ``"intake:triager-divergence"`` audit record (I6) with the
            triager recommendation in the ``escalation_cause`` field.

        When they agree, this method is a no-op (returns False).

        Returns True if a divergence was detected and surfaced, False otherwise.

        Ordering: this method must be called after the triager agent has posted its
        comment (i.e. after the triager run completes).  In ``OrchestratorService``,
        it is spawned as a background task (``_spawn_triager_reconcile``) that waits
        ``triager_reconcile_delay_s`` seconds before reading comments, giving the
        triager time to complete.  See SPEC §10.4 step 6.
        """
        if intake_decision != "admit":
            # Only admit decisions can diverge from a "queue" recommendation.
            # If intake decided queue, we're already conservative — no reconciliation needed.
            return False

        comments = await self.forge.list_comments(issue_ref)
        triager_rec: str | None = None
        for comment in comments:
            if is_triager_comment(comment.body):
                triager_rec = parse_triager_recommendation(comment.body)
                break  # first triager comment wins (should be exactly one per SPEC §10.4)

        if triager_rec is None:
            # Triager comment not yet posted or not parseable — no divergence to surface.
            return False

        if triager_rec != TRIAGER_REC_QUEUE:
            # Triager also recommends admit (or close) — no divergence.
            return False

        # Divergence detected: intake admitted, triager recommends caution.
        # Determine the admission reason for the reconciliation note.
        reason = _admission_reason(self.allowlist)

        body = _RECONCILIATION_COMMENT.format(
            reason=reason,
            triager_rec=triager_rec,
        )
        await self.forge.post_comment(issue_ref, body)

        # Audit the divergence (I6) — escalation_cause field carries the triager rec.
        await self.audit.record(
            repo=issue_ref.repo,
            entity_ref=issue_ref,
            action="intake:triager-divergence",
            escalation_cause=f"triager_rec={triager_rec}",
        )

        return True


def _admission_reason(allowlist: list[str]) -> str:
    """Return a short human-readable description of why the issue was auto-admitted.

    Used in the reconciliation comment body.  Pure, synchronous.
    """
    if not allowlist:
        return "owner-only default — empty allowlist admits the repo owner"
    return "repo owner or explicitly allowlisted author"
