"""Engine.intake — intake decision + atomic label swap + triager dispatch + triager gate.

Two-gate flow (SPEC §10.4):

  Gate 1 — Trust (decide_intake):  pure, synchronous, owner/allowlist check.
    admit  → set [LABEL_TRIAGE], dispatch triager.  Do NOT add LABEL_AGENT_WORK yet.
    queue  → set [LABEL_TRIAGE, LABEL_AWAITING_PROMOTION].  (No triager gate needed —
             already conservative; human must promote.)

  Gate 2 — Content (apply_triager_gate):  deferred, reads triager verdict comment.
    actionable     → add LABEL_AGENT_WORK → fires issues:labeled → orchestrator.
    not-actionable → add LABEL_AWAITING_PROMOTION → issue enters human triage queue.
    no verdict yet → safe fallback — leave [LABEL_TRIAGE] only (awaiting human).

The triager is read-only (I5): it posts one structured comment containing a
``<!-- triager-verdict: actionable|not-actionable -->`` marker.  The control plane
(apply_triager_gate) reads that marker and applies the work label — never the triager.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.db.audit import AuditLog
from src.decisions.intake import decide_intake
from src.decisions.triager_reconcile import (
    TRIAGER_VERDICT_ACTIONABLE,
    is_triager_comment,
    parse_triager_verdict,
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
    spawn the triager-gate background task with the correct decision string.
    """

    handle: RunHandle | None
    """Triager dispatch handle, or ``None`` when intake was skipped (idempotency guard)."""

    decision: str | None
    """``'admit'`` or ``'queue'``, or ``None`` when intake was skipped."""


# Triager contract path (orchestration-agent contract file)
_TRIAGER_CONTRACT = "agents/triager.md"

# Triager max turns — a single structured comment; low cap
_TRIAGER_MAX_TURNS = 10

# Comment posted when gate falls back to awaiting-human (no verdict in window).
_NO_VERDICT_COMMENT = (
    "<!-- orchestrator:intake-no-verdict -->\n"
    "**Intake gate:** the triager did not post a machine-readable verdict within the "
    "expected window. This issue has been placed in the human triage queue "
    "(`awaiting-promotion`) as a safe fallback. An operator can promote it once "
    "they have reviewed the issue."
)

# Comment posted when gate is not actionable (triager says not-actionable or no verdict).
_NOT_ACTIONABLE_COMMENT = (
    "<!-- orchestrator:intake-not-actionable -->\n"
    "**Intake gate:** the triager classified this issue as **not actionable for "
    "autonomous dispatch** ({reason}). It has been placed in the human triage queue "
    "(`awaiting-promotion`). An operator can promote it after review."
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
        """Run the intake gate for one issue — Gate 1 (trust) only.

        Steps (SPEC §10.4 two-gate flow):
          1. Fetch the issue.
          1a. Idempotency guard: if the issue already carries LABEL_TRIAGE, intake
              has already run — skip to avoid re-dispatching a redundant triager.
          2. decide_intake(issue, allowlist) → 'admit' | 'queue'  [pure, sync — I4]
          3. Dispatch triager (forge_token_scope='repo-comment' — I5).
          4. admit → set_labels([LABEL_TRIAGE])             — I7: no LABEL_AGENT_WORK yet;
                                                               orchestrator must NOT fire.
             queue → set_labels([LABEL_TRIAGE, LABEL_AWAITING_PROMOTION])  — unchanged.
          5. Write audit record to DB (I6) — after observable state is committed.
          6. Return IntakeResult(handle, decision) so the caller (OrchestratorService)
             can spawn the deferred Gate 2 (apply_triager_gate) as a background task.

        Gate 2 (apply_triager_gate) runs after triager_reconcile_delay_s, reads the
        triager verdict, and conditionally adds LABEL_AGENT_WORK.

        Returns IntakeResult(handle=None, decision=None) when intake was skipped
        (idempotency guard fired) or IntakeResult(handle, decision) otherwise.
        """
        issue = await self.forge.get_issue(issue_ref)

        # Step 1a: idempotency guard — LABEL_TRIAGE is set atomically in step 4.
        # Its presence means intake already completed for this issue.
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

        # Step 4: atomic label swap (I7 — set_labels has PUT semantics; no TOCTOU window).
        # IMPORTANT: for 'admit', we set ONLY [LABEL_TRIAGE] — NOT LABEL_AGENT_WORK.
        # The orchestrator must NOT fire yet; Gate 2 (apply_triager_gate) applies
        # LABEL_AGENT_WORK only after the triager classifies the issue as actionable.
        if decision == "admit":
            await self.forge.set_labels(issue_ref, [LABEL_TRIAGE])
        else:
            await self.forge.set_labels(issue_ref, [LABEL_TRIAGE, LABEL_AWAITING_PROMOTION])

        # Step 5: audit every intake decision (I6) — written AFTER observable state is set
        await self.audit.record(
            repo=issue_ref.repo,
            entity_ref=issue_ref,
            action=f"intake:{decision}",
        )

        return IntakeResult(handle=triager_handle, decision=decision)

    async def apply_triager_gate(
        self,
        issue_ref: IssueRef,
        intake_decision: str,
    ) -> str:
        """Gate 2 — read the triager verdict and apply the work label (or queue).

        Called AFTER the triager agent has completed and posted its structured comment
        (after a delay of triager_reconcile_delay_s).

        This replaces the former ``reconcile_triager_divergence`` advisory behavior
        with a true dispatch gate (SPEC §10.4 two-gate flow).

        Logic:
          - Read issue comments; find the triager comment.
          - Parse the ``<!-- triager-verdict: ... -->`` marker.
          - actionable  → forge.add_label(LABEL_AGENT_WORK) → issues:labeled → I2.
          - not-actionable → forge.add_label(LABEL_AWAITING_PROMOTION) + short comment.
          - no verdict in window → forge.add_label(LABEL_AWAITING_PROMOTION) + fallback
            comment; do NOT auto-admit (safe fallback — the whole point is the triager
            gates; SPEC §10.4 gate constraint).

        Only runs when intake_decision == 'admit' (Gate 1 admitted the author by trust).
        If intake_decision == 'queue', the issue is already in AWAITING_PROMOTION —
        Gate 2 is a no-op.

        Returns one of: 'applied-agent-work', 'applied-awaiting-promotion', 'no-op'.

        Preserves I5: the triager only comments; this method (the control plane)
        applies the work label.
        Preserves I1: non-actionable verdict → awaiting-promotion; human must promote.
        Preserves I7: LABEL_AGENT_WORK and LABEL_AWAITING_PROMOTION never coexist
        (set_labels was PUT-semantics at intake; here we add_label onto [LABEL_TRIAGE]
        only — no overlap risk).
        Audit record I6: written after every label mutation.
        """
        if intake_decision != "admit":
            # Gate 1 already queued the issue — Gate 2 is a no-op.
            return "no-op"

        # Verify the issue is still in the expected post-admit state:
        # [LABEL_TRIAGE] only (Gate 1 set this, Gate 2 not yet run).
        # If LABEL_AGENT_WORK or LABEL_AWAITING_PROMOTION is already present,
        # Gate 2 already ran (re-delivery) — idempotency: skip.
        issue = await self.forge.get_issue(issue_ref)
        if LABEL_AGENT_WORK in issue.labels or LABEL_AWAITING_PROMOTION in issue.labels:
            return "no-op"

        # Read the triager's structured comment and parse the machine-readable verdict.
        comments = await self.forge.list_comments(issue_ref)
        verdict: str | None = None
        for comment in comments:
            if is_triager_comment(comment.body):
                verdict = parse_triager_verdict(comment.body)
                break  # first triager comment wins (exactly one per SPEC §10.4)

        if verdict == TRIAGER_VERDICT_ACTIONABLE:
            # Triager classified the issue as actionable for autonomous dispatch.
            # Add LABEL_AGENT_WORK → fires issues:labeled → I2 → orchestrator.
            # I5 preserved: the control plane (not the triager) applies the label.
            await self.forge.add_label(issue_ref, LABEL_AGENT_WORK)
            await self.audit.record(
                repo=issue_ref.repo,
                entity_ref=issue_ref,
                action="intake:gate-actionable",
            )
            return "applied-agent-work"

        elif verdict is not None:
            # Triager explicitly said not-actionable (risk flag, scope unclear, etc.)
            reason = "triager classified this issue as not actionable"
            await self.forge.add_label(issue_ref, LABEL_AWAITING_PROMOTION)
            await self.forge.post_comment(
                issue_ref,
                _NOT_ACTIONABLE_COMMENT.format(reason=reason),
            )
            await self.audit.record(
                repo=issue_ref.repo,
                entity_ref=issue_ref,
                action="intake:gate-not-actionable",
                escalation_cause=f"triager_verdict={verdict}",
            )
            return "applied-awaiting-promotion"

        else:
            # No triager verdict found within the window — safe fallback.
            # Do NOT auto-admit; leave issue awaiting human (I1 preserved).
            await self.forge.add_label(issue_ref, LABEL_AWAITING_PROMOTION)
            await self.forge.post_comment(issue_ref, _NO_VERDICT_COMMENT)
            await self.audit.record(
                repo=issue_ref.repo,
                entity_ref=issue_ref,
                action="intake:gate-no-verdict",
            )
            return "applied-awaiting-promotion"


def _admission_reason(allowlist: list[str]) -> str:
    """Return a short human-readable description of why the issue was auto-admitted.

    Used in gate comment bodies.  Pure, synchronous.
    """
    if not allowlist:
        return "owner-only default — empty allowlist admits the repo owner"
    return "repo owner or explicitly allowlisted author"
