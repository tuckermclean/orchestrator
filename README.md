# orchestrator

**Next-gen agent-swarm harness orchestrator** — a clean-room specification for a
forge-agnostic, harness-agnostic autonomous SWE-agent pipeline. Containerized, k8s-hosted,
driven from a mobile-first PWA, and designed to act on publicly contributed issues through
a **contributor allowlist trust model** (default-deny for non-allowlisted authors).

> **Status:** spec only. No implementation yet. This document set defines *what the
> system is, does, and how to build and verify it* — exhaustively enough to implement
> and deploy without additional context.

## What this is

An autonomous pipeline moves two long-lived entities — a **Work Item** (issue) and a
**Change Set** (pull request) — from "filed" to "merged or escalated", driven by three
coordinating loops:

- **Intake** (new) screens every public issue via a read-only triager agent; allowlisted
  authors auto-advance, everyone else waits for a one-tap human promotion in the PWA.
- **Dispatch** turns a queued Work Item into an implementing Change Set.
- **Converge** is a bounded 3-round Review→Fix loop that drives a Change Set to
  *approved* or *escalated*.
- **Reconciler** is an orthogonal supervisor (cron every 15 min) that detects and
  recovers stranded entities.

Entity state is encoded entirely in **forge labels** — there is no separate state store
(crash-only durability). The decision logic is a set of small **pure synchronous functions**,
which makes the whole machine portable and thoroughly testable (~237+ required test cases
specified in `TESTING.md`).

## The documents

### Engine layer (foundation — do not modify without human review)

| File | What it is |
|------|------------|
| [`STATE_MACHINE.md`](STATE_MACHINE.md) | The centerpiece. Entities, states, the full transition table (I1–I6/P1–P17), the converge sub-machine, the reconciler supervisor (RC-1..RC-4), the escalation taxonomy (E1–E10), constants, and Mermaid lifecycle diagrams. |
| [`DECISION_LOGIC.md`](DECISION_LOGIC.md) | Frozen clean-room extraction. Each of the 9 pure decision functions as an exhaustive input→output truth table, line-cited to source and matched row-for-row against the existing tests. The binding implementation contract for the decision layer. |
| [`API.md`](API.md) | The API spec. Domain types, 10 decision functions (9 from `DECISION_LOGIC.md` + `decide_intake` §3.11 for public-issue intake), the three abstraction ports (ForgePort / HarnessPort / SessionPort), Engine entrypoints (dispatch / converge / reconcile / intake), and a §8 Service Contract (ForgeEvent, event routing with intake rows, OrchestratorService, RepoConfig with allowlist + intake_enabled, SwarmLimits). Language-neutral with Python and Rust mapping notes. |

### Specialist agent pack

The specialist agents used by the swarm (security engineer, code reviewer, database
optimizer, accessibility auditor, API tester, and ~175 others) are **not authored in this
repo**. They come from an external, SHA-pinned agent-pack repository
(`github.com/msitarzewski/agency-agents`) that is fetched and baked into the container
image at build time. The full provenance model is in [`AGENT_PACK.md`](AGENT_PACK.md).

### Product layer (next-gen specs)

| File | What it is |
|------|------------|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | System architecture. Component map (webhook ingress → OrchestratorService → Engine → ports → harness), the public-issue intake front-stage (§3 / `#intake`), persisted state model, k8s deployment topology, async execution summary, security architecture summary. Includes Mermaid diagrams for the intake flow and system context. |
| [`THREAT_MODEL.md`](THREAT_MODEL.md) | Security threat model. (**Protected path** — any PR touching this file auto-escalates to E1.) Trust boundary, 8 threat categories (T1–T8: prompt injection, untrusted code execution, resource exhaustion, secret exfiltration, supply chain, malicious PR content, allowlist bypass, webhook replay), 8 security invariants (I1–I8), threat matrix. |
| [`TESTING.md`](TESTING.md) | Test strategy — first-class deliverable, not an appendix. The hard gate (all tests + typecheck + lint = green before `agent:ready`). Exhaustive truth-table tests for all 10 decision functions (~116+ decision-layer cases), port contract suites, engine + intake integration tests, security/trust tests, idempotency tests. ~237+ named test cases total. |
| [`DEPLOYMENT.md`](DEPLOYMENT.md) | Container and k8s deployment spec. Multi-arch image build, image provenance (Sigstore/cosign, SBOM), Helm chart shape (Deployment, Ingress, Secret, ConfigMap, optional CronJob), scaling (SwarmLimits → semaphores, shared state for HA), observability (health probes, Prometheus metrics, structured logs), first-run setup, rolling upgrade/rollback. |
| [`WEBUI.md`](WEBUI.md) | PWA spec. Mobile-first responsive + installable; web push for escalations, promotions, and approvals; 7 screens: Dashboard (multi-repo pipeline health), Triage queue (promote/decline non-allowlisted issues — the human gate), Repos (allowlist CRUD, enable/disable), PR/Converge detail (rounds, verdict, CI), Runs (stream/cancel/intervene), Settings (all Config fields), Login. 26 control-plane API endpoints. |
| [`AGENTS.md`](AGENTS.md) | Build-agent guidance. How AI agents who *build* this orchestrator should work: doc hierarchy as source of truth, spec-first discipline, async boundary rules, Python/Rust toolchain, testing hard gate, PROTECTED_PATHS rule, code style, 7-phase build order, security-invariant checklist, quick-reference table. |
| [`AGENT_PACK.md`](AGENT_PACK.md) | **NEW.** Specialist agent provenance. The two-tier agent model (orchestration agents vs. the external specialist pack), `AgentPackConfig` (source + pinned SHA), build-time pack acquisition (bake-at-build, no runtime fetch), `AgentRef` naming, `decide_specialists` selection algorithm, spawn model, and supply-chain controls (SHA pinning, PROTECTED_PATHS, SBOM). |

### Runtime swarm contracts (`agents/`)

Harness-fed prompt/contract documents — the instructions injected into each agent at runtime.

| File | What it is |
|------|------------|
| [`agents/triager.md`](agents/triager.md) | **NEW.** Public-issue intake triager. Read-only; posts one structured triage summary comment per issue (author, type, scope, risk flags, summary, affected files). Never writes code. Treats all contributor text as untrusted data with explicit prompt-injection resistance rules. |
| [`agents/orchestrator.md`](agents/orchestrator.md) | Dispatch orchestrator. Opens draft PR immediately (`Closes #N`), stamps `agent:implementing`, commits early/often, delegates to implementer specialist, verifies gate is green, marks PR ready (`gh pr ready` + `converge` label) for the converge workflow. |
| [`agents/implementer.md`](agents/implementer.md) | Implementation specialist. Scope discipline, mandatory tests for every change, hard gate (typecheck + lint + full test suite green before done), async boundary rules, no secrets in code, protected-path awareness. |
| [`agents/converge-reviewer.md`](agents/converge-reviewer.md) | Converge review aggregator. Spawns up to 4 parallel specialist sub-agents (security + code quality + optional others). Aggregates into a `Verdict` with stable blocker signatures. Writes `.converge-verdict.json` **last** (crash-safe). Missing/failing tests = blocker. |
| [`agents/converge-fixer.md`](agents/converge-fixer.md) | Converge fix specialist. R1: fix blockers + suggestions; R2: fix blockers only; R3: never called. Routes blockers to owning specialists. Gate must be green before finishing. Commits each fix with blocker-signature reference. |

## Ground truth (engine layer)

The engine layer is derived **only** from the live decision scripts, their Vitest tests,
the workflows, and the agent contracts in the `mirror` repo:

- `mirror/scripts/{dispatch,converge,reconciler,status,git}/*.sh`
- `mirror/tests/infra/*.test.ts` (~107 cases that enumerate the truth tables)
- `mirror/.github/workflows/{dispatch,pr-converge,agent-reconciler}.yml`
- `mirror/.agents/custom/*.md`

`mirror/ORCHESTRATION.md` was **not** used — it is stale. Every claim in the engine-layer
docs is cited to a script/workflow/contract `file:line`.

## Known issues (surfaced during engine extraction)

Two latent concerns in the reference implementation documented in `STATE_MACHINE.md §10`
and `AGENTS.md §10`:

1. The `ci-red` recovery path re-checks only 3 of the 6 blocking CI checks — a PR with
   red Docker/Helm checks can be auto-approved on that path.
2. `MAX_REDISPATCHES=2` was duplicated in three places; now single-sourced in `API.md §2`.

## Reading order

**To understand the engine:** `STATE_MACHINE.md` → `DECISION_LOGIC.md` → `API.md`

**To understand the product:** `ARCHITECTURE.md` → `THREAT_MODEL.md` → `DEPLOYMENT.md` → `WEBUI.md`

**To build it:** `AGENTS.md` first, then follow the 7-phase build order it describes.

**To run agents in the swarm:** `agents/triager.md` (intake) → `agents/orchestrator.md` + `agents/implementer.md` (dispatch) → `agents/converge-reviewer.md` + `agents/converge-fixer.md` (converge)

**To understand where specialist agents come from:** `AGENT_PACK.md` — the two-tier model, external pinned pack, `AgentRef` naming, and supply-chain controls.
