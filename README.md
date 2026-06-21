# orchestrator

**Next-gen agent-swarm harness orchestrator** — a forge-agnostic, harness-agnostic
autonomous SWE-agent pipeline. Containerized, k8s-hosted, driven from a mobile-first
PWA, and designed to act on publicly contributed issues through a **contributor
allowlist trust model** (default-deny for non-allowlisted authors).

> **Status:** spec only. No implementation yet. This document set defines *what the
> system is, does, and how to build and verify it* — exhaustively enough to implement
> and deploy without additional context.

## What this is

An autonomous pipeline moves two long-lived entities — a **Work Item** (issue) and a
**Change Set** (pull request) — from "filed" to "merged or escalated", driven by three
coordinating loops:

- **Intake** screens every public issue via a read-only triager agent; allowlisted
  authors auto-advance, everyone else waits for a one-tap human promotion in the PWA.
- **Dispatch** turns a queued Work Item into an implementing Change Set.
- **Converge** is a bounded 3-round Review→Fix loop that drives a Change Set to
  *approved* or *escalated*.
- **Reconciler** is an orthogonal supervisor (cron every 15 min) that detects and
  recovers stranded entities.

Entity lifecycle state is encoded in **forge labels**; counters live in the service DB
with atomic increment. The decision logic is a set of small **pure synchronous functions**
(~290 required test cases specified in `TESTING.md`).

---

## Documents

### Core spec

| File | What it is |
|---|---|
| [`SPEC.md`](SPEC.md) | Engine, service contract, and single source of truth. State machine entities and transitions (I1–I6, P1–P17), reconciler (RC-1..RC-4), converge sub-machine, escalation taxonomy (E1–E10), all 13 decision functions with truth tables, ports, Engine methods, service contract, constants (`PROTECTED_PATHS`, labels, caps), Mermaid diagrams. |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | System architecture. Component map, two-tier agent architecture, public-issue intake front-stage, persisted state model, container image (multi-arch, cosign, SBOM, specialist pack bake), Kubernetes deployment (Helm chart shape, security context, health probes, Secrets, ConfigMap, scaling, HPA), observability (Prometheus metrics, structured logs), first-run setup, upgrade/rollback. |
| [`SECURITY.md`](SECURITY.md) | Security threat model. (**Protected path** — any PR touching this file triggers E1.) Trust model, 8 threats (T1–T8), 9 invariants (I1–I9), PROTECTED_PATHS canonical 6-entry list. |
| [`TESTING.md`](TESTING.md) | Test strategy — first-class deliverable. Hard gate (tests + typecheck + lint before `agent:ready`). All decision-function truth-table test cases (~264+ named), port contract suites, engine integration tests, security/trust tests, idempotency tests. |
| [`WEBUI.md`](WEBUI.md) | PWA spec. Mobile-first responsive + installable; web push for escalations, promotions, and approvals; 7 screens (Dashboard, Triage, Repos, PR/Converge detail, Runs, Settings, Login); 28 control-plane API endpoints. |
| [`AGENTS.md`](AGENTS.md) | Build-agent guidance + specialist pack provenance. Doc hierarchy (source of truth + conflict resolution), language/toolchain, async boundary rule, two-tier agent model, specialist pack (`AgentPackConfig`, `AgentRef`, bake-at-build acquisition, `decide_specialists`, spawn model, supply-chain controls), 7-phase build order, PROTECTED_PATHS rule, security invariant checklist. |

### Runtime swarm contracts (`agents/`)

Harness-fed prompt/contract documents injected into each agent at runtime.

| File | What it is |
|---|---|
| [`agents/triager.md`](agents/triager.md) | Public-issue intake triager. Read-only; posts one structured triage summary per issue. Never writes code. Explicit prompt-injection resistance rules. |
| [`agents/orchestrator.md`](agents/orchestrator.md) | Dispatch orchestrator. Opens draft PR immediately (`Closes #N`), stamps `agent:implementing`, delegates to implementer specialist, verifies gate is green, marks PR ready. |
| [`agents/implementer.md`](agents/implementer.md) | Implementation specialist. Scope discipline, mandatory tests for every change, hard gate before handing back. |
| [`agents/converge-reviewer.md`](agents/converge-reviewer.md) | Converge review aggregator. Receives the Engine-computed specialist allow-set (`allowed_agent_refs`) in its `DispatchContext`; spawns 2–4 parallel specialist sub-agents from that allow-set. Aggregates into a `Verdict` with stable blocker signatures. Writes `.converge-verdict.json` last (crash-safe). |
| [`agents/converge-fixer.md`](agents/converge-fixer.md) | Converge fix specialist. R1: fix blockers + suggestions; R2: fix blockers only; R3: never called. Routes blockers to owning specialists. Gate green before committing. |

---

## Reading order

**To understand the engine:** `SPEC.md §1–§6` (state machine) → `SPEC.md §7–§8`
(constants + decision functions)

**To understand the product:** `ARCHITECTURE.md` → `SECURITY.md` → `WEBUI.md`

**To build it:** `AGENTS.md` first, then the 7-phase build order it describes

**To run agents in the swarm:** `agents/triager.md` → `agents/orchestrator.md` +
`agents/implementer.md` → `agents/converge-reviewer.md` + `agents/converge-fixer.md`

---

## Specialist agents — where they come from

Specialist agents (security engineer, code reviewer, database optimizer, accessibility
auditor, API tester, and ~175 others) are **not authored in this repo**. They come from
an external SHA-pinned pack (`github.com/msitarzewski/agency-agents`), fetched and
baked into the container image at build time. See `AGENTS.md §7` for the full provenance
model, supply-chain controls, and how to update the pinned SHA.

---

## Known issues

Documented in `SPEC.md §13`:

1. **OQ-2** — `MAX_REDISPATCHES = 2` was duplicated in the reference implementation; now
   single-sourced in `SPEC.md §7`. Never hardcode `2`.
2. **OQ-3** — Three redispatch caps with different values govern overlapping situations;
   they are intentionally distinct. See `SPEC.md §13` before unifying them.
3. **OQ-4** — `COMPLIANCE.md` is reserved in `PROTECTED_PATHS` but not yet authored.

*(OQ-1 resolved: `ci-red` recovery now re-polls all 6 `BLOCKING_CI_CHECKS`.)*

---

## Archive

Superseded original documents are in [`archive/`](archive/). See
[`archive/README.md`](archive/README.md) for the mapping from old to new locations.
