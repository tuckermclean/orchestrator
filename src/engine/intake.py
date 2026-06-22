"""Engine.intake — intake decision + atomic label swap + triager dispatch + audit."""

from __future__ import annotations

from src.db.audit import AuditLog
from src.decisions.intake import decide_intake
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

# Triager contract path (orchestration-agent contract file)
_TRIAGER_CONTRACT = "agents/triager.md"

# Triager max turns — a single structured comment; low cap
_TRIAGER_MAX_TURNS = 10


class IntakeEngine:
    """Handles the intake/triage gate (SPEC §10.4)."""

    def __init__(
        self,
        forge: ForgePort,
        harness: HarnessPort,
        session: SessionPort,
        audit: AuditLog,
        allowlist: list[str],
    ) -> None:
        self.forge = forge
        self.harness = harness
        self.session = session
        self.audit = audit
        self.allowlist = allowlist

    async def intake(self, issue_ref: IssueRef) -> RunHandle | None:
        """Run the intake gate for one issue.

        Steps (SPEC §10.4):
          1. Fetch the issue.
          2. decide_intake(issue, allowlist) → 'admit' | 'queue'  [pure, sync — I4]
          3. Dispatch triager (forge_token_scope='repo-comment' — I5).
          4. set_labels([LABEL_TRIAGE, LABEL_AGENT_WORK | LABEL_AWAITING_PROMOTION])  (atomic — I7)
          5. Write audit record to DB (I6) — after observable state is committed.

        Returns the triager RunHandle, or None if dispatch fails.
        """
        issue = await self.forge.get_issue(issue_ref)

        # Step 2: pure synchronous decision (I4 — never await this)
        decision = decide_intake(issue, self.allowlist)

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

        return triager_handle
