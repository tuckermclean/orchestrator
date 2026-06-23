# SECURITY.md — Security Threat Model

> **PROTECTED PATH** — Any PR whose diff touches this file triggers E1 / `needs-human`
> in `Engine.converge` before any review round runs. Changes require explicit human review.
> See `SPEC.md §7 PROTECTED_PATHS`.

---

## §1 Trust Model

**Fundamental boundary.** All publicly contributed text — issue bodies, titles, comment
bodies — is **untrusted data**, never instructions. The harness receives issue content
inside explicit DATA delimiters in the prompt context; it is never embedded in the system
prompt.

**Actor classes.**

| Actor | Trust level | Notes |
|---|---|---|
| **Human operator** | Fully trusted | Configures the system, promotes triage queue, merges APPROVED PRs. Final authority on all irreversible actions. |
| **Allowlisted author** | Conditionally trusted | Admitted directly by `Engine.intake`; still subject to converge and protected-path controls. |
| **Non-allowlisted contributor** | Untrusted for dispatch | Held in `LABEL_AWAITING_PROMOTION`; no code-writing agent spawned without explicit human promotion (I1). |
| **Triage agent** | Read-only | Posts one structured comment. Sandbox receives `forge_token_scope: "repo-comment"` (`SPEC.md §9.2`) — cannot add labels, create PRs, or trigger workflows even if deceived (I5). |
| **Implementer / specialist agents** | Narrowly scoped | Sandbox has `forge_token_scope: "repo-branch"`. No multi-repo credentials. Cannot modify PROTECTED_PATHS without triggering E1 (I3). |

**Default-deny (fail-closed, issue #48).** An empty `allowlist` admits ONLY the repository owner and queues everyone else — it is NOT a gate-disable.  A non-empty `allowlist` admits listed authors and the owner; unlisted non-owners are queued. See `SPEC.md §8.11`.

---

## §2 Threat Summary

| ID | Threat | Severity | Key mitigation | Owner |
|---|---|---|---|---|
| **T1** | Prompt injection via issue/comment bodies | High | Triage agent is read-only. Issue text arrives as DATA, never instructions. Agent contracts include explicit injection-resistance rules. Any injection-caused state reaches E1 before auto-merge. | Triager contract, `HarnessPort` sandboxing |
| **T2** | Untrusted code execution | High | Harness sandbox has no production secrets; per-run resource limits bound blast radius. Allowlist gate blocks dispatch of untrusted issues until human promotion. | `HarnessPort`, `RepoConfig` allowlist |
| **T3** | Resource and cost exhaustion | Medium | `SwarmLimits` caps concurrent runs globally and per-repo. `MAX_REDISPATCHES=2`, `RECONCILER_STALE_REDISPATCH_CAP=3`, `ISSUE_REDISPATCH_CAP=3`, `NO_VERDICT_RETRY_CAP=2` prevent infinite cycling. `ISSUE_COOLDOWN_S=900` rate-limits reconciler. Non-allowlisted issues incur no harness cost until promoted. | `OrchestratorService` (`SwarmLimits`), `Config` constants |
| **T4** | Secret exfiltration via agent | High | The sandbox receives only an ephemeral, repo-scoped forge token (minimum needed for branch/PR writes). The orchestrator's `FORGE_TOKEN`, `HARNESS_API_KEY`, and all operator credentials are held exclusively by `PortProvider` and never surfaced in `DispatchContext`. The security specialist (always in `CONVERGE_REVIEW_BASE`) scans PR diffs for credential patterns per its agent-pack contract (`.agents/engineering-security-engineer.md`, SHA-pinned) — this behavior is pack-contract-enforced, not separately mandated in this spec. `.github/workflows/**` is PROTECTED_PATHS. | `PortProvider`, `HarnessPort`, `DispatchContext` schema (§9.2), `CONVERGE_REVIEW_BASE`, SHA-pinned agent pack |
| **T5** | Supply-chain / dependency poisoning + agent-pack SHA-bump | High | Security specialist checks new dependencies. Human merge gate is final control. Pack is SHA-pinned (`AgentPackConfig.pinned_ref`), baked at build, recorded in SBOM. SHA bumps require explicit diff review of upstream repo. `.agents/**` is PROTECTED_PATHS against in-band tampering. | Converge reviewer, `AgentPackConfig`, PROTECTED_PATHS, human merge gate |
| **T6** | Protected-path modification | High | `Engine.converge` checks changed files against PROTECTED_PATHS before round 1. Any match → `LABEL_NEEDS_HUMAN` immediately; no review round runs, no auto-merge. Applies to all PRs including allowlisted authors. | `Engine.converge` (P6, E1) |
| **T7** | Allowlist bypass / privilege escalation | High | `decide_intake` is a trivial pure function: fail-closed by default — empty allowlist admits only the owner; non-empty allowlist admits owner + listed authors; exact string match only. No error paths default to `admit`. Allowlist is in operator-controlled config, not the forge. Every admit/queue decision is audit-logged. | `decide_intake`, Config store, audit log |
| **T8** | Webhook replay / `delivery_id` collision | Low | LRU dedup cache keyed on `delivery_id` in `OrchestratorService`. `Engine.converge` idempotency gate checks label state before acting. Reconciler channels are idempotent. Correctness does not depend on the dedup cache. | `OrchestratorService`, `Engine.converge`, `Engine.reconcile` |

**Out of scope:** forge platform security, network-level DoS against the webhook endpoint,
cloud/physical infrastructure security, operator account compromise, LLM model behavioral
guarantees.

**Accepted risks (no current mitigation):**
- **Verdict file tampering.** `.converge-verdict.json` is on the contributor-controlled branch; the security specialist (`CONVERGE_REVIEW_BASE`) reviews any manipulation next round; human merge is the final gate.
- **Branch name injection.** `orchestrator.md` sanitizes branch names; no server-side validation before dispatch.
- **PR body `Closes #N` manipulation.** A contributor can modify the `Closes #N` reference after PR creation, affecting `has_issue` in RC-1/RC-4; no auto-merge occurs without human review.
- **Audit log mutability.** Audit records are standard DB rows; no write-once backend is mandated. Operator DB access can modify the trail.

---

## §3 Security Invariants

Every correct implementation must preserve all nine invariants. Test coverage is specified
in `TESTING.md §5`.

**I1 — Non-allowlisted authors never reach a code-writing agent without human promotion.**
`decide_intake` is fail-closed (default-deny, issue #48): an empty `allowlist` admits
ONLY the repository owner and queues everyone else.  When `decide_intake` returns `queue`
the issue receives `LABEL_AWAITING_PROMOTION` and no harness dispatch call is ever made
until a human explicitly adds `LABEL_AGENT_WORK`. No code path bypasses this gate.

**I2 — PROTECTED_PATHS modifications always escalate to E1 / `needs-human`.**
`Engine.converge` checks changed files against PROTECTED_PATHS before round 1. On match:
`LABEL_NEEDS_HUMAN` added, `ESCALATED` returned, no review round runs.

**I3 — The agent sandbox contains only an ephemeral, scoped forge token; no operator credentials.**
Harness injects `forge_token_scope: "repo-comment"` for the triager (read + comment only)
or `"repo-branch"` for all others (own branch/PR only). The orchestrator's `FORGE_TOKEN`,
`HARNESS_API_KEY`, and operator credentials are held by `PortProvider` and never appear in
`DispatchContext`. `DispatchContext` is a **sealed schema** (`SPEC.md §9.2`): the harness
adapter must reject any object with unrecognised fields (field-name checks alone are
insufficient — a credential under an unlisted name would pass a name-only check).

**Named test:** `test_security_no_credentials_in_dispatch_context` (`TESTING.md §5`).

**I4 — `decide_intake` is pure and synchronous with no forge calls and no side effects.**
Signature: `decide_intake(author: string, allowlist: list<string>) → IntakeDecision`.
No network calls, no external state reads, no side effects. Label writes are performed
by `Engine.intake`, not this function.

**I5 — The triage agent is read-only.**
May read issue content and post one structured comment; must not add labels, create PRs,
or trigger workflows. Two-layer enforcement:
1. **Credential scope:** `forge_token_scope: "repo-comment"` (`SPEC.md §9.2`) physically
   prevents label writes and PR creation (forge API rejects with 403).
2. **Contract discipline:** `agents/triager.md §Absolute Constraints`.

**I6 — Every admit/queue decision and every human promotion is audit-logged.**
`Engine.intake` writes an audit record for every `decide_intake` call (author, decision,
allowlist state at decision time) — see `SPEC.md §10.4` step 5 for the required DB write.
`OrchestratorService.promote` writes an audit record for every human promotion
(`LABEL_AWAITING_PROMOTION` → `LABEL_AGENT_WORK`) — see `SPEC.md §11.3` promote note.

**I7 — `LABEL_AWAITING_PROMOTION` and `LABEL_AGENT_WORK` are never simultaneously present.**
Transition uses `ForgePort.set_labels` (PUT semantics, atomic swap) under per-entity
advisory lock — never `remove_label` then `add_label` (TOCTOU window).

**Named test:** `test_security_awaiting_and_agent_work_never_coexist` (`TESTING.md §5`).

**I8 — `SECURITY.md` itself is in PROTECTED_PATHS; changes require human review.**
Any PR touching this file escalates to E1 / `needs-human`. Weakening the threat model
requires human judgment, not autonomous action.

**I9 — `AgentRef` values come only from `decide_specialists` output; contributor text is never interpolated into an `AgentRef`.**
The specialist spawning code derives the set of specialists exclusively from
`decide_specialists(changed_paths, round) → list<AgentRef>` (`SPEC.md §8.12`), which
reads the hardcoded `SPECIALIST_ROUTING` constant. Issue bodies, PR bodies, and comment
bodies must never be used to construct, select, or modify an `AgentRef` string.

**Mechanisation (D2 / `SPEC.md §9.2`).** Engine sets `allowed_agent_refs =
decide_specialists(changed_paths, round)` in `DispatchContext`. Harness adapter **must
reject** out-of-set spawns. The hook (baked into the agent-runner image at
`/opt/orchestrator/i9_spawn_hook.py`) validates spawns as follows:
1. `subagent_type` must equal `"general-purpose"` (per `AGENTS.md §7.4`); any other value → DENY.
2. The `AgentRef` is parsed from the prompt's `.agents/<AgentRef>` marker (requires the
   `.agents/` prefix; robust to surrounding text); absent marker → DENY.
3. The parsed `AgentRef` must be in the allow-set derived from `decide_specialists`; absent → DENY.
All deny paths exit 2 (the only Claude Code PreToolUse blocking exit code; exit 1 is non-blocking).
When `allowed_agent_refs` is `None` (implementer/orchestrator dispatches), harness-level
enforcement is disabled; I9 is enforced via agent-contract discipline instead (those agents
select from hardcoded routing tables, not contributor text).

**Named tests:** `test_security_agent_ref_not_from_contributor_text`,
`test_security_spawn_ref_outside_allowset_rejected`,
`test_security_spawn_rejected_when_allowed_refs_none` (`TESTING.md §5`).

---

## §4 PROTECTED_PATHS

Canonical list — single-sourced in `SPEC.md §7`. Inline copies in `agents/*.md` are
tagged `# from SPEC.md §7 — keep in sync`.

```
".github/workflows/**"    # CI workflow definitions
"ARCHITECTURE.md"         # system architecture governance
"SECURITY.md"             # this document — self-protecting
"COMPLIANCE.md"           # compliance requirements (doc not yet authored)
".agents/**"              # specialist pack dir — protects against in-band pack tampering
"agents/**"               # orchestration-agent contracts — self-protection for swarm contracts
```

**Note:** `COMPLIANCE.md` is referenced in PROTECTED_PATHS but has not yet been authored.
Preserve it in PROTECTED_PATHS to protect the reserved namespace; do not invent its content.
