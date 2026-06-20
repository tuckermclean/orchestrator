# AGENTS.md — Build-Agent Guidance for the Forge-Agnostic Orchestrator

**Audience**: AI agents (Claude Code, Codex, or similar) assigned to implement this
codebase. If you are implementing something, read this file first — before any source
file, before any spec.

**Not for runtime agents.** If you are a runtime swarm agent governed by
`agents/*.md`, those files are your contract. This file does not apply to you.

---

## §1 Purpose and Orientation

This repository contains the specification for a forge-agnostic, harness-agnostic
autonomous SWE-agent orchestration pipeline. The specs are complete, internally
consistent, and treated as the source of truth. Your job as a build agent is to
implement them faithfully — not to interpret, extend, or simplify them without
first opening an issue.

The system you are building moves GitHub issues and pull requests through a
label-encoded state machine. Two entity types — Work Items (issues) and Change Sets
(pull requests) — are driven by three workflows: Dispatch, Converge, and Reconciler.
Their full lifecycle is specified in `STATE_MACHINE.md`. The decision functions that
govern every branching point are specified in `DECISION_LOGIC.md`. The API contract
— types, interfaces, constants — is in `API.md`. Architecture is in `ARCHITECTURE.md`.
Security invariants are in `THREAT_MODEL.md`. The test contract (binding, ~237+ cases)
is in `TESTING.md`.

The single most important facts about this system:

- All durable state lives in forge labels. There is no separate database for entity
  state. A process can crash at any point and the reconciler will recover on the next
  cron tick.
- The harness is single-shot. `HarnessPort.dispatch` returns immediately. Agents must
  commit early and often; there is no resume path.
- Decision functions are pure and synchronous. The async boundary is at I/O only.

Read those three facts again before touching any code.

---

## §2 Document Hierarchy — Source of Truth and Conflict Resolution

When documents appear to contradict each other, the one higher on this list wins.
Do not silently pick a side. Do not implement a compromise. Escalate the conflict
to a human by opening an issue and stopping.

| Priority | Document | Authority |
|---|---|---|
| 1 | `STATE_MACHINE.md` + `DECISION_LOGIC.md` | Engine ground truth — frozen |
| 2 | `THREAT_MODEL.md` | Security invariants — protected path |
| 3 | `API.md` | Implementation contract — all types, interfaces, constants |
| 4 | `ARCHITECTURE.md` | System architecture — implementation must match |
| 5 | `TESTING.md` | Test contract — all tests must satisfy this |
| 6 | `DEPLOYMENT.md` | Container, Helm, k8s deployment spec |
| 7 | `WEBUI.md` | PWA spec |
| 8 | `AGENT_PACK.md` | Specialist pack provenance — where agents come from, how to update |
| 9 | `agents/*.md` | Runtime swarm contracts — implement as harness-fed prompt templates |
| 10 | `README.md` | Index only — lowest authority |

If a document at level N contradicts a document at level N-1, the level N-1 document
wins. Stop and open an issue rather than implementing the level-N interpretation.

The practical implication: if `API.md` and `ARCHITECTURE.md` say different things
about an interface, `API.md` wins. If `DECISION_LOGIC.md` and `API.md` say different
things about a decision function's behavior, `DECISION_LOGIC.md` wins. If you find a
discrepancy, open an issue flagging both sides. Do not proceed with implementation
until a human resolves it.

---

## §3 Spec-First Discipline

**Do not implement what is not in the spec.** If a feature is needed but not
specified, open an issue first. Do not add it speculatively.

**Do not rewrite or contradict the specs.** If a spec appears wrong, open an issue
against the spec file. Do not silently fix it in code and do not implement the
corrected version until the spec is updated.

**Always pull the latest spec versions before starting a build task.** Specs may be
updated. A build task started against a stale spec produces divergent code.

**`STATE_MACHINE.md` and `DECISION_LOGIC.md` are frozen.** They are clean-room
extractions from the mirror reference implementation. If you find a bug in them,
open an issue and stop. Do not edit them. Do not implement a workaround. There are
two known latent issues documented in `STATE_MACHINE.md §10` and `DECISION_LOGIC.md`
— they are flagged, not fixed; the same discipline applies to any new findings.

**`THREAT_MODEL.md` is a `PROTECTED_PATHS` member.** Any PR whose diff touches this
file triggers E1 escalation in `Engine.converge` and is routed to `needs-human`
before any review round. This protection is intentional and non-negotiable.

**Do not author specialist agents.** The orchestrator uses a two-tier agent model
(`ARCHITECTURE.md §2.3`, `AGENT_PACK.md §1`). The only agents you author are the five
orchestration-agent contracts in `agents/` (triager, orchestrator, implementer,
converge-reviewer, converge-fixer). The specialist agents — security engineer, code
reviewer, database optimizer, accessibility auditor, API tester, and all others — come
from an external SHA-pinned pack repo baked into the image at build time
(`AGENT_PACK.md §3`). A build phase that creates new files under `agents/` or `.agents/`
beyond the five orchestration contracts is incorrect; open an issue and stop.

**`agents/**` and `.agents/**` are both `PROTECTED_PATHS` members.** Any PR that
modifies an orchestration-agent contract or a specialist definition in the pack directory
escalates to E1 / `needs-human`. This is intentional — agent contracts govern the
behavior of autonomous systems and must not be modified without human review.

---

## §4 Implementation Language

The implementation language is not yet fixed. Before writing any code, check:

1. Open issues and closed PRs for a chosen language decision.
2. `architecture-decision-records/` if that directory exists.

If no language has been chosen, default to **Python 3.12+ with asyncio**. The
rationale: the system's crash-only / reconciler durability model provides the same
reliability guarantees that Rust's ownership model would buy; the workload is I/O
and JSON glue; Python is more accessible for future maintainers of an orchestration
system in this domain.

**If Python is chosen:**
- Runtime: `asyncio`
- Types and validation: `pydantic`
- HTTP: `httpx` (async)
- Tests: `pytest` + `pytest-asyncio`
- Typecheck: `mypy --strict`
- Lint: `ruff`

**If Rust is chosen:**
- Runtime: `tokio`
- Serialization: `serde` / `serde_json`
- HTTP: `reqwest`
- Tests: `#[tokio::test]`
- Typecheck / lint: `cargo check` + `cargo clippy -- -D warnings`

Whichever language is chosen, write idiomatic code for that language. Do not write
Python that looks like a line-for-line translation of the reference bash scripts, and
do not write Rust that looks like Python with type annotations. The specs describe
behavior; the implementation expresses that behavior in the language's natural idiom.

---

## §5 Async Boundary — Non-Negotiable

This is not a style preference. It is a correctness and readability requirement.

**Async ONLY at genuine I/O boundaries:**

- `ForgePort` methods — HTTP calls to the forge API (GitHub/GitLab/Gitea).
- `HarnessPort` methods — spawning and querying harness (agent) runs.
- `SessionPort` methods — streaming run output, cancellation, intervention.
- `resolve_blockers` — calls `ForgePort.list_comments` for the comment-footer
  fallback when the sentinel survived.
- `pipeline_health` — calls `ForgePort.list_prs`.
- All `Engine` workflow methods (`dispatch`, `converge`, `reconcile`, `intake`) —
  because they compose async port calls.

**Pure decision functions are synchronous — always:**

`route_entry`, `decide_round`, `decide_cap_action`, `decide_stale_action`,
`decide_rearm_action`, `decide_conflict_action`, `decide_redispatch_action`,
`decide_intake`, `derive_issue_state`, `derive_pr_state` — every one of these
is a pure function with no network, no file I/O, and no side effects. They must
be synchronous. Making them async obscures their purity, adds scheduling overhead,
and makes tests harder to read.

The right mental model: decision functions are math. Ports are I/O. The Engine
wires them together. The Engine is async because it calls ports; decision functions
are sync because they do no I/O.

A PR that makes any of the above decision functions async will be rejected by the
converge reviewer as a blocker.

---

## §6 Testing — The Hard Gate

No code lands without tests. No PR may call `gh pr ready` with a red gate. This is
not advisory.

The full test contract is `TESTING.md`. What follows is the minimum you need to
understand before writing a single line of implementation code.

### The gate

Three items must all pass:

1. Full test suite (all five layers — see below).
2. Typecheck — `mypy --strict` (Python) or `cargo check` (Rust).
3. Lint — `ruff` (Python) or `cargo clippy -- -D warnings` (Rust).

These correspond to `BLOCKING_CI_CHECKS` in `API.md §2`. The converge reviewer
treats a missing or failing test as a blocker, not a nit.

### The five test layers

```
Layer 5 — Idempotency / crash-only tests         (TESTING.md §6)
Layer 4 — Security / trust tests                  (TESTING.md §5)
Layer 3 — Engine integration tests (over fakes)   (TESTING.md §4)
Layer 2 — Port contract tests                     (TESTING.md §3)
Layer 1 — Unit tests — decision functions         (TESTING.md §2)
```

Start at Layer 1 and build upward. Do not write Layer 3 tests before Layer 2 fakes
exist.

### Decision-function coverage is exhaustive

Every row in every truth table in `DECISION_LOGIC.md §§1–9` and in `API.md §3.11`
(`decide_intake`) must map to at least one named test case. The minimum test counts
are:

| Function | Min. tests |
|---|---|
| `decide_intake` | 9 |
| `route_entry` | 6 |
| `resolve_blockers` | 12 |
| `decide_round` | 22 |
| `decide_cap_action` | 7 |
| `decide_stale_action` | 18 |
| `decide_rearm_action` | 15 |
| `decide_conflict_action` | 7 |
| `decide_redispatch_action` | 14 |
| `pipeline_health` | 8 |
| State derivation helpers | 15 |
| **Decision-layer subtotal** | **~133** |

These are floors, not ceilings. The CI coverage check enforces the floor: if a truth
table row exists and no test exercises it, the gate fails.

### Port fakes come before real adapters

`FakeForgePort`, `FakeHarnessPort`, and `FakeSessionPort` must exist and pass their
shared contract suites (`TESTING.md §3`) before any real adapter is written. The
fakes are the test infrastructure for every layer above Layer 1. A PR that adds a
real GitHub adapter without a passing fake is incomplete.

Each fake must expose:
- A call log for assertion in integration and security tests.
- Configurable return values and fault injection.
- A reset method.

### Security tests are blockers

The eight security/trust tests in `TESTING.md §5` cover the invariants in
`THREAT_MODEL.md §4`. A failing security test is a blocker, not a nit. It cannot be
deferred or accepted with a comment. Fix it before the PR is marked ready.

### Implementation order for testing

Write tests in this order:

1. Decision functions and their truth-table tests (pure; no fakes needed).
2. Port fakes and contract suites.
3. Engine integration tests over fakes.
4. Security tests over fakes.
5. Idempotency tests.
6. Then and only then: real port adapters; real OrchestratorService; PWA; containers.

---

## §7 PROTECTED_PATHS Rule

These files may not be modified by any build agent PR without triggering E1 /
`needs-human`. If a build task requires touching one of them, stop and escalate.
Do not open a PR that modifies these files.

```
.github/workflows/**
ARCHITECTURE.md
THREAT_MODEL.md
COMPLIANCE.md
```

`STATE_MACHINE.md` and `DECISION_LOGIC.md` are effectively frozen as well (§3).
Flag any needed changes as an issue; do not edit them and do not submit a PR that
does so.

The `PROTECTED_PATHS` constant is single-sourced in `API.md §2`. Never hardcode the
path patterns in implementation code. Always import the constant from the single
source.

---

## §8 Code Style

- Match the surrounding code in the module you are editing: comment density, naming
  conventions, import ordering.
- Write idiomatic code for the chosen language. Python: `snake_case`, type hints
  everywhere, `pydantic` models for structured data, dataclasses for immutable
  records. Rust: `snake_case`, `impl` blocks, `derive` macros.
- No generated boilerplate. No placeholder comments (`# TODO`, `// fixme`) in merged
  code. A comment in a merged PR describes why, not what the next line obviously does.
- Function length: one screen. If it does not fit, split it. Each function does one
  thing. The engine methods (`dispatch`, `converge`, `reconcile`, `intake`) are
  orchestrators — they coordinate calls but contain no logic themselves; logic belongs
  in decision functions or helpers.
- Error handling: be explicit. Never swallow exceptions. Every error either propagates
  up (and is caught at the per-event boundary in `OrchestratorService.handle_event`)
  or maps to an escalation cause from E1–E10. An error that produces neither a logged
  event nor an escalation label is a silent failure and will not pass review.
- Constants: never hardcode numeric thresholds or string constants. All constants are
  single-sourced in `API.md §2`. Import them. A PR that hardcodes `2` for
  `MAX_REDISPATCHES` or `300` for `REARM_RECENT_GUARD_S` is flagged as a blocker —
  this is the exact class of bug that the reference implementation suffered
  (`DECISION_LOGIC.md Constants Reference`, `STATE_MACHINE.md §10`).

---

## §9 Build Phases — Suggested Implementation Order

Work through these phases in order. Do not start Phase 2 before Phase 1's tests pass.
Do not start Phase 3 before Phase 2's contract suites pass.

### Phase 1 — Decision Functions and Types

Implement the domain types from `API.md §2` (enums, constants, structs) and all
10 synchronous decision functions:

- `route_entry` (`DECISION_LOGIC.md §1`, `API.md §3.1`)
- `resolve_blockers` (`DECISION_LOGIC.md §2`, `API.md §3.2`) — async; uses ForgePort
- `decide_round` (`DECISION_LOGIC.md §3`, `API.md §3.3`)
- `decide_cap_action` (`DECISION_LOGIC.md §4`, `API.md §3.4`)
- `decide_stale_action` (`DECISION_LOGIC.md §5`, `API.md §3.5`)
- `decide_rearm_action` (`DECISION_LOGIC.md §6`, `API.md §3.6`)
- `decide_conflict_action` (`DECISION_LOGIC.md §7`, `API.md §3.7`)
- `decide_redispatch_action` (`DECISION_LOGIC.md §8`, `API.md §3.8`)
- `pipeline_health` (`DECISION_LOGIC.md §9`, `API.md §3.9`) — async; uses ForgePort
- `decide_intake` (`API.md §3.11`)
- `decide_specialists` (`API.md §3.12`) — pure sync; depends on `SPECIALIST_ROUTING` constant
- `derive_issue_state` and `derive_pr_state` (`API.md §3.10`)

Write every truth-table test from `TESTING.md §2` alongside the function. Do not
move to Phase 2 until 100% decision-function coverage passes.

These are pure functions with no I/O. They should be the fastest tests in the suite.
The entire Phase 1 test run should complete in seconds.

### Phase 2 — Port Fakes and Contract Suites

Implement `FakeForgePort`, `FakeHarnessPort`, and `FakeSessionPort` with their shared
contract suites (`TESTING.md §3`). Live in `tests/fakes/`.

The fakes are the test infrastructure for everything above. Investing in them now
pays dividends in every subsequent phase. A fake that does not fully implement the
port contract will cause false-passing tests in Phase 3.

Do not implement any real adapter (GitHub, harness) until a fake exists and its
contract suite passes.

### Phase 3 — Engine

Implement `Engine` (`API.md §5`) against the fakes from Phase 2:

- `Engine.dispatch` (`API.md §5.1`) — covers I2, P1
- `Engine.converge` (`API.md §5.2`) — covers P2, P6–P12, P15, P16, E1–E6
- `Engine.reconcile` (`API.md §5.3`) — covers RC-1..RC-4, P3–P5, P13–P14, I3–I4,
  E7–E10
- `Engine.intake` (`API.md §3.11`, `ARCHITECTURE.md §3`) — covers the triage
  front-stage

Write the engine integration tests from `TESTING.md §4` alongside each method.
Write the security tests from `TESTING.md §5` as you implement the paths they cover
(protected-path check, intake gate). Write the idempotency tests from `TESTING.md §6`
before declaring Phase 3 done.

The Engine holds no durable in-process state (`API.md §5`). If you find yourself
adding instance variables that accumulate across calls, stop — that is a design error.

### Phase 4 — OrchestratorService and Control-Plane API

Implement `OrchestratorService` (`API.md §8.4`):

- `ForgeEvent` and event routing table (`API.md §8.1`, `§8.3`)
- `Config`, `RepoConfig`, `SwarmLimits`, `PortProvider` (`API.md §8.2`)
- `OrchestratorService` lifecycle (`start`, `stop`), event ingress (`handle_event`),
  pipeline operations (`reconcile_now`, `status`), registry management, run observation
- `delivery_id` LRU dedup (`API.md §8.4`, `§8.5`)
- Per-event and per-repo error isolation (`API.md §8.5`)
- CLI verb mapping (illustrative; `API.md §8.6`)

The backing store for the repo registry, run index, and operator accounts is an
implementation detail: SQLite is appropriate for single-instance deployments;
Postgres for horizontal scaling (`ARCHITECTURE.md §4.2`). The dedup LRU is in-memory
for single-instance; Redis or a shared table for horizontal scaling.

### Phase 5 — Real Port Adapters

Implement `GitHubForgePort` and the `HarnessPort` adapter for
`anthropics/claude-code-action`. Each must pass the shared contract suite from
Phase 2 before it is usable in production. Do not relax the contract test or add
adapter-specific skips.

The forge adapter is the only component that knows forge-native concepts (GitHub API
idioms, label encoding, PR mergeable state, CI check runs). Everything else speaks
the port interface.

### Phase 6 — PWA

Implement the mobile-first Progressive Web Application per `WEBUI.md`. The primary
operator surfaces are: pipeline status (backed by `pipeline_health`), triage queue
(issues carrying `LABEL_AWAITING_PROMOTION`), active run detail with streaming events,
and repo and config management.

The PWA speaks the `OrchestratorService` HTTP API. No PWA code should bypass the
service layer and call forge or harness APIs directly.

### Phase 7 — Container and Kubernetes

Build the container image and Helm chart per `DEPLOYMENT.md`. The orchestrator
binary (or process group) hosts: webhook ingress (`/webhook/*`), control-plane API
(`/api/*`), and PWA static assets (`/`). The reconcile cadence is either the
internal loop or an external Kubernetes CronJob — both produce identical behavior
(`ARCHITECTURE.md §5.3`).

**Specialist pack acquisition is part of Phase 7.** The Dockerfile must include the
pack acquisition step (`DEPLOYMENT.md §2.1`, `AGENT_PACK.md §3.1`): clone → checkout
pinned SHA → flatten into `dest_dir`. Do not defer this to runtime or implement a
startup-time fetch. Verify the pack SHA appears in the image SBOM (`DEPLOYMENT.md §2.2`).
Do not add or author any specialist files; the pack content is fetched from the external
repo as specified in `AgentPackConfig`.

---

## §10 Open Questions and Known Issues

Do not silently fix these. They are flagged and tracked for human decision.

**OQ-1: `ci-red` recovery checks 3 of 6 blocking CI checks.**
The `escalate:ci-red` recovery path in `Engine.converge` re-polls only the first
three blocking checks (Type Check, Lint, Integration Tests) after re-triggering CI,
not all six (`STATE_MACHINE.md §10`, `API.md §5.2`). A PR that recovers its code
checks but whose Docker/Helm checks are still red can be auto-approved on this path.
This mirrors the behavior of the reference implementation exactly. Do not change it
without a human decision. If you believe it is a bug, open an issue and implement
the reference behavior in the meantime.

**OQ-2: `MAX_REDISPATCHES` was duplicated in the reference implementation.**
The reference bash scripts had `MAX_REDISPATCHES = 2` in three separate places
(`DECISION_LOGIC.md Constants Reference`). It is now single-sourced in `API.md §2
Constants`. Never hardcode `2` in implementation code. Always read from the constant.
The CI gate will catch this if you do — but it is better to get it right the first
time.

**OQ-3: Two redispatch caps with different values govern overlapping situations.**
`MAX_REDISPATCHES = 2` governs converge-loop redispatch (`decide_cap_action`).
`RECONCILER_STALE_REDISPATCH_CAP = 3` governs the reconciler's stale-PR recovery
(`decide_stale_action`). `ISSUE_REDISPATCH_CAP = 3` governs orphan-issue recovery
(`decide_redispatch_action`). These are distinct caps for distinct situations. Do not
unify them unless a human decision changes the spec.

---

## §11 What Not To Do

These are not style suggestions. Each is a contract violation that will produce a
blocker from the converge reviewer.

- Do not implement features not in the spec.
- Do not modify `DECISION_LOGIC.md`, `STATE_MACHINE.md`, or any `PROTECTED_PATHS`
  member.
- Do not make any of the 10 decision functions async.
- Do not hardcode numeric constants. Import from the `API.md §2` equivalent in code.
- Do not commit code with red tests, red typecheck, or red lint.
- Do not call `gh pr ready` with a failing gate. The converge reviewer will see the
  failing CI checks and file a blocker; you will have wasted a converge round.
- Do not add engine instance variables that accumulate state across calls. The engine
  is stateless per-call; all durable state lives in forge labels.
- Do not bypass the port interfaces. The engine talks to the forge through
  `ForgePort`, to the harness through `HarnessPort`, and to the run index through
  `SessionPort`. No engine method may import a forge SDK directly.
- Do not put credentials in the agent sandbox environment. `PortProvider` holds
  credentials in the orchestrator process. The harness environment must be clean
  (`THREAT_MODEL.md §4 I3`).
- Do not open issues or PRs against spec files without describing the exact
  discrepancy, which spec takes precedence, and what the correct behavior should be.
  "This seems wrong" is not enough context.

---

## §12 Security Invariants — Checklist for Every PR

Before marking any PR ready, verify that none of your changes weakens the following.
If any invariant is weakened, the PR must not be marked ready — open an issue instead.

**I1** — When `RepoConfig.allowlist` is non-empty and `decide_intake` returns
`queue`, no harness dispatch call is made for that issue until a human explicitly
adds `LABEL_AGENT_WORK`. Verify: `test_security_unlisted_never_dispatches`,
`test_security_promotion_required`.

**I2** — `Engine.converge` checks `forge.get_changed_files(pr)` against
`PROTECTED_PATHS` before round 1. On any match, it adds `LABEL_NEEDS_HUMAN` and
returns `ESCALATED` without dispatching any reviewer or fixer. Verify:
`test_security_protected_path_escalates`,
`test_security_protected_path_all_patterns`.

**I3** — No forge credentials or harness API keys are present in the agent sandbox
environment. Credentials are held by `PortProvider` only.

**I4** — `decide_intake` is pure and synchronous. No network calls. No external
state. No side effects. Side effects are performed by `Engine.intake`.

**I5** — The triage agent run by `Engine.intake` may read issue content and post
one comment. It may not add or remove labels, create PRs, or trigger workflows.
Verify: `test_security_triage_agent_read_only`.

**I6** — Every `decide_intake` call and every human promotion is written to the
audit log.

**I7** — `LABEL_AWAITING_PROMOTION` and `LABEL_AGENT_WORK` are never simultaneously
present on the same issue. Verify:
`test_security_awaiting_and_agent_work_never_coexist`.

**I8** — `THREAT_MODEL.md` is in `PROTECTED_PATHS`. Any PR touching it triggers E1.
This invariant is self-referential: the mechanism that enforces it must itself be
tested (`test_security_protected_path_all_patterns` includes `THREAT_MODEL.md`).

---

## §13 Quick Reference — Key Spec Locations

| What you need | Where to find it |
|---|---|
| Entity states, label encoding | `STATE_MACHINE.md §2` |
| Full transition table I1–I6, P1–P17 | `STATE_MACHINE.md §3` |
| Reconciler channels RC-1..RC-4 | `STATE_MACHINE.md §4` |
| Converge sub-machine (3-round loop) | `STATE_MACHINE.md §5` |
| Escalation taxonomy E1–E10 | `STATE_MACHINE.md §6` |
| All numeric constants | `STATE_MACHINE.md §7` and `API.md §2` |
| Decision function truth tables | `DECISION_LOGIC.md §1–§9` |
| Domain types, label vocabulary, enums | `API.md §2` |
| Decision function signatures | `API.md §3` |
| `decide_intake` truth table | `API.md §3.11` |
| ForgePort, HarnessPort, SessionPort | `API.md §4` |
| Engine methods | `API.md §5` |
| Async execution model | `API.md §6` |
| OrchestratorService, ForgeEvent, Config | `API.md §8` |
| Intake front-stage design | `ARCHITECTURE.md §3` |
| Persisted state model | `ARCHITECTURE.md §4` |
| Deployment topology | `ARCHITECTURE.md §5`, `DEPLOYMENT.md` |
| Security architecture | `ARCHITECTURE.md §8`, `THREAT_MODEL.md` |
| Security invariants I1–I8 | `THREAT_MODEL.md §4` |
| Test cases (binding) | `TESTING.md §2–§6` |
| CI gate | `TESTING.md §7`, `API.md §2 BLOCKING_CI_CHECKS` |
| Fake implementation pattern | `TESTING.md §3.1` |
| Runtime swarm agent contracts | `agents/*.md` |
