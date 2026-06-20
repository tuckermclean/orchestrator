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
| **Human operator** | Fully trusted | Configures the system, promotes triage queue issues, merges APPROVED PRs. Final authority on all irreversible actions. |
| **Allowlisted author** | Conditionally trusted for dispatch | Authors whose username is in `RepoConfig.allowlist` are admitted directly to the core machine by `Engine.intake`. Still subject to all converge and protected-path controls. |
| **Non-allowlisted contributor** | Untrusted for dispatch | Held in triage queue (`LABEL_AWAITING_PROMOTION`). No code-writing agent is spawned without explicit human promotion. |
| **Triage agent** | Narrowly trusted; read-only | May read issue content and post one structured comment. Cannot add labels, create PRs, or advance the state machine. |
| **Implementer / specialist agents** | Narrowly trusted; scoped | Operate in the harness sandbox with no forge credentials, no harness API keys, no ability to modify PROTECTED_PATHS without triggering E1. |

**Default-deny.** When `RepoConfig.allowlist` is non-empty, unlisted authors are queued,
never dispatched. An empty allowlist disables the gate entirely (appropriate for private
repos). See `SPEC.md §8.11` (`decide_intake`).

**Human as final authority** on: issue promotion, PR merge, config changes, and any
entity in `LABEL_NEEDS_HUMAN` state.

---

## §2 Threat Summary

| ID | Threat | Severity | Key mitigation | Owner |
|---|---|---|---|---|
| **T1** | Prompt injection via issue/comment bodies | High | Triage agent is read-only. Issue text arrives as DATA, never instructions. Agent contracts include explicit injection-resistance rules. Any injection-caused state reaches E1 before auto-merge. | Triager contract, `HarnessPort` sandboxing |
| **T2** | Untrusted code execution | High | Harness sandbox has no production secrets; per-run resource limits bound blast radius. Allowlist gate blocks dispatch of untrusted issues until human promotion. | `HarnessPort`, `RepoConfig` allowlist |
| **T3** | Resource and cost exhaustion | Medium | `SwarmLimits` caps concurrent runs globally and per-repo. `MAX_REDISPATCHES=2`, `RECONCILER_STALE_REDISPATCH_CAP=3`, `ISSUE_REDISPATCH_CAP=3`, `NO_VERDICT_RETRY_CAP=2` prevent infinite cycling. `ISSUE_COOLDOWN_S=900` rate-limits reconciler. Non-allowlisted issues incur no harness cost until promoted. | `OrchestratorService` (`SwarmLimits`), `Config` constants |
| **T4** | Secret exfiltration via agent | High | No credentials injected into sandbox. `PortProvider` holds forge tokens and harness API keys in the orchestrator process only. Security specialist in every converge round scans PR diffs for credential patterns. `.github/workflows/**` is PROTECTED_PATHS. | `PortProvider`, `HarnessPort`, converge security specialist |
| **T5** | Supply-chain / dependency poisoning + agent-pack SHA-bump | High | Security specialist checks new dependencies. Human merge gate is final control. Pack is SHA-pinned (`AgentPackConfig.pinned_ref`), baked at build, recorded in SBOM. SHA bumps require explicit diff review of upstream repo. `.agents/**` is PROTECTED_PATHS against in-band tampering. | Converge reviewer, `AgentPackConfig`, PROTECTED_PATHS, human merge gate |
| **T6** | Protected-path modification | High | `Engine.converge` checks changed files against PROTECTED_PATHS before round 1. Any match → `LABEL_NEEDS_HUMAN` immediately; no review round runs, no auto-merge. Applies to all PRs including allowlisted authors. | `Engine.converge` (P6, E1) |
| **T7** | Allowlist bypass / privilege escalation | High | `decide_intake` is a trivial pure function: `author in allowlist`, exact string match, no error paths that default to `admit`. Allowlist is in operator-controlled config, not the forge. Every admit/queue decision is audit-logged. | `decide_intake`, Config store, audit log |
| **T8** | Webhook replay / `delivery_id` collision | Low | LRU dedup cache keyed on `delivery_id` in `OrchestratorService`. `Engine.converge` idempotency gate checks label state before acting. Reconciler channels are idempotent. Correctness does not depend on the dedup cache. | `OrchestratorService`, `Engine.converge`, `Engine.reconcile` |

**Out of scope:** forge platform security, network-level DoS against the webhook endpoint,
cloud/physical infrastructure security, operator account compromise, LLM model behavioral
guarantees.

---

## §3 Security Invariants

Every correct implementation must preserve all nine invariants. Test coverage is specified
in `TESTING.md §5`.

**I1 — Non-allowlisted authors never reach a code-writing agent without human promotion.**
When `allowlist` is non-empty and `decide_intake` returns `queue`, the issue receives
`LABEL_AWAITING_PROMOTION` and no harness dispatch call is ever made until a human
explicitly adds `LABEL_AGENT_WORK`. No code path bypasses this gate.

**I2 — PROTECTED_PATHS modifications always escalate to E1 / `needs-human`.**
`Engine.converge` checks `forge.get_changed_files(pr)` against PROTECTED_PATHS before
round 1. On any match: add `LABEL_NEEDS_HUMAN`, return `ESCALATED`, no review round runs.
No protected-path PR is ever auto-merged.

**I3 — No forge credentials or harness API keys ever appear in the agent sandbox.**
`HarnessPort` must not inject forge tokens, API keys, or operator credentials into the
agent environment. All credential resolution is performed by `PortProvider` in the
orchestrator process, before and after harness dispatch — never inside it.

**I4 — `decide_intake` is pure and synchronous with no forge calls and no side effects.**
Signature: `decide_intake(author: string, allowlist: list<string>) → IntakeDecision`.
No network calls, no external state reads, no side effects. Label writes are performed
by `Engine.intake`, not this function.

**I5 — The triage agent is read-only.**
The triage specialist may read issue content and post one structured comment. It must
not add or remove labels, create PRs, trigger workflow dispatches, or invoke any forge
action that advances the state machine. Enforced by the triage contract and by absence
of credentials in its sandbox.

**I6 — Every admit/queue decision and every human promotion is audit-logged.**
`Engine.intake` writes an audit record for every `decide_intake` call (author, decision,
allowlist state at decision time) and for every human promotion
(`LABEL_AWAITING_PROMOTION` → `LABEL_AGENT_WORK`).

**I7 — `LABEL_AWAITING_PROMOTION` and `LABEL_AGENT_WORK` are never simultaneously present.**
An issue carries exactly one of: `LABEL_AWAITING_PROMOTION` (held), `LABEL_AGENT_WORK`
(admitted), or neither (not yet processed). The transition must atomically remove the
former before adding the latter.

**I8 — `SECURITY.md` itself is in PROTECTED_PATHS; changes require human review.**
Any PR touching this file escalates to E1 / `needs-human`. Weakening the threat model
requires human judgment, not autonomous action.

**I9 — `AgentRef` values come only from `decide_specialists` output; contributor text is never interpolated into an `AgentRef`.**
The specialist spawning code derives the set of specialists exclusively from
`decide_specialists(changed_paths, round) → list<AgentRef>` (`SPEC.md §8.12`), which
reads the hardcoded `SPECIALIST_ROUTING` constant. Issue bodies, PR bodies, and comment
bodies must never be used to construct, select, or modify an `AgentRef` string.

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
