# AGENTS.md — Build-Agent Guidance

**Audience**: AI agents (Claude Code, Codex, or similar) assigned to implement this
codebase. Read this file first — before any source file, before any spec.

**Not for runtime agents.** If you are a runtime swarm agent governed by `agents/*.md`,
those files are your contract. This file does not apply to you.

---

## §1 What You Are Building

A forge-agnostic, harness-agnostic autonomous SWE-agent orchestration pipeline. The
specs are complete and treated as source of truth. Your job is to implement them
faithfully.

Three facts that govern everything:

- **Entity lifecycle state** (QUEUED, BUILDING, CONVERGING, …) lives in **forge labels**.
  Entity counters (`redispatch_count`, `retry_count`) and service-level data (repo
  registry, operator accounts, dedup cache) live in the service DB with atomic increment.
  A process crash leaves every entity in its last-written forge label state; the reconciler
  recovers it on the next tick.
- **Harness dispatch is fire-and-forget.** `HarnessPort.dispatch` returns immediately; the
  control plane never blocks awaiting an agent. The converge job may legitimately await its
  own spawned sub-agents within a single execution.
- **Decision functions are pure and synchronous.** The async boundary is at I/O only.

---

## §2 Document Hierarchy

When documents contradict each other, the one higher on this list wins. Do not silently
implement a compromise — escalate to human by opening an issue and stopping.

| Priority | Document | Authority |
|---|---|---|
| 1 | `SPEC.md §1–§6` | Engine state machine — source of truth |
| 2 | `SECURITY.md` | Security invariants — protected path |
| 3 | `SPEC.md §7–§13` | Constants, decision functions, ports, service contract |
| 4 | `ARCHITECTURE.md` | System architecture — implementation must match |
| 5 | `TESTING.md` | Test contract — binding, ~369+ cases |
| 6 | `WEBUI.md` | PWA spec |
| 7 | `agents/*.md` | Runtime swarm contracts — implement as harness-fed prompt templates |
| 8 | `README.md` | Index only — lowest authority |

---

## §3 Spec-First Discipline

- Do not implement features not in the spec. Open an issue first.
- Do not rewrite or contradict specs. Open an issue against the spec file; do not silently
  fix it in code.
- Always pull the latest spec versions before starting a build task.
- `SECURITY.md` is a PROTECTED_PATHS member. Any PR touching it triggers E1 /
  `needs-human` before any review round. Changes require human review.
- **Do not author specialist agents.** The only agents you author are the five
  orchestration contracts in `agents/` (triager, orchestrator, implementer,
  converge-reviewer, converge-fixer). Specialist agents come from an external SHA-pinned
  pack repo (§7). Creating files under `.agents/` or adding new `agents/*.md` files
  beyond the five contracts is incorrect — open an issue and stop.
- `agents/**` and `.agents/**` are both PROTECTED_PATHS. Any PR modifying these escalates
  to E1 / `needs-human`.

---

## §4 Implementation Language

Not yet fixed. Check open issues and any `architecture-decision-records/` directory first.

Default if no decision made: **Python 3.12+ with asyncio**.

| Tool | Python | Rust |
|---|---|---|
| Runtime | `asyncio` | `tokio` |
| Types/validation | `pydantic` | `serde` / `serde_json` |
| HTTP | `httpx` (async) | `reqwest` |
| Tests | `pytest` + `pytest-asyncio` | `#[tokio::test]` |
| Typecheck | `mypy --strict` | `cargo check` |
| Lint | `ruff` | `cargo clippy -- -D warnings` |

Write idiomatic code for the chosen language — not a line-for-line translation of the
reference bash scripts.

---

## §5 Async Boundary

**Async ONLY at genuine I/O boundaries:** `ForgePort` methods, `HarnessPort` methods,
`SessionPort` methods, `resolve_blockers` (uses `ForgePort.list_comments`),
`pipeline_health` (uses `ForgePort.list_prs`), all `Engine` methods (because they compose
async port calls).

**Pure decision functions are always synchronous:** `route_entry`, `decide_round`,
`decide_cap_action`, `decide_stale_action`, `decide_rearm_action`, `decide_conflict_action`,
`decide_redispatch_action`, `decide_intake`, `decide_specialists`, `derive_issue_state`,
`derive_pr_state`. Making any of these async is a blocker.

Decision functions are math. Ports are I/O. The Engine wires them together.

---

## §6 Testing

The full test contract is `TESTING.md`. Non-negotiables:

- Full test suite + typecheck + lint must all pass before `gh pr ready`.
- Every truth-table row in `SPEC.md §8` maps to at least one named test case.
- A missing or failing test is a **blocker**, not a nit.
- The minimum test count is **~369** (not ~343 — TESTING.md appendix is authoritative).

Build tests in this order: (1) decision functions, (2) port fakes + contract suites,
(3) engine integration, (4) security tests, (5) idempotency tests, then real adapters,
OrchestratorService, PWA, containers.

Security tests (`TESTING.md §5`) assert the invariants from `SECURITY.md §3`. A failing
security test cannot be deferred.

---

## §7 Two-Tier Agent Architecture and Specialist Pack

The orchestrator uses two agent tiers:

| Tier | Source | Authored by | Location | Referenced by |
|---|---|---|---|---|
| **Orchestration agents** | `agents/*.md` (this repo) | The operator | `/app/agents/*.md` | Contract file path |
| **Specialist pack** | External SHA-pinned repo | Pack upstream | `/app/.agents/*.md` (flattened) | `AgentRef` (flat filename) |

### §7.1 Pack provenance

Default pack: `https://github.com/msitarzewski/agency-agents`

```python
# SPEC.md §7 AgentPackConfig defaults — keep in sync
AGENT_PACK_REPO_URL    = "https://github.com/msitarzewski/agency-agents"
AGENT_PACK_PINNED_REF  = "d6553e261e595c651064f899a6c33dd5aa71c9e3"
AGENT_PACK_DEST_DIR    = ".agents"
```

The pack is fetched and flattened at image build time (§8, Phase 7). No runtime fetch.
All specialist `*.md` files are copied flat into `AGENT_PACK_DEST_DIR` — no subdirectory
structure. The flattened basename is the `AgentRef`.

### §7.2 AgentRef

An `AgentRef` is a flat filename string, e.g. `"engineering-security-engineer.md"`.
`AgentRef` values are derived only from `decide_specialists` output, which reads the
hardcoded `SPECIALIST_ROUTING` constant (`SPEC.md §7`). Contributor-supplied text is
**never** interpolated into an `AgentRef` (invariant I9, `SECURITY.md §3`).

### §7.3 `decide_specialists` algorithm (`SPEC.md §8.12`)

```
given changed_paths: list[str], round: int → list[AgentRef]:
  base   = list(CONVERGE_REVIEW_BASE)          # always included; ordered
  extras = []                                   # routing additions in SPECIALIST_ROUTING order
  for entry in SPECIALIST_ROUTING:             # iterate in definition order — NOT a set
    if any path matches entry.pattern for path in changed_paths:
      for ref in entry.agent_refs:
        if ref not in base and ref not in extras:
          extras.append(ref)
  # Cap: base always retained; extras truncated to fill remaining slots
  assert len(base) <= PARALLEL_SPECIALIST_CAP, "CONVERGE_REVIEW_BASE exceeds PARALLEL_SPECIALIST_CAP"
  return base + extras[:PARALLEL_SPECIALIST_CAP - len(base)]
```

The algorithm is **deterministic**: iterating `SPECIALIST_ROUTING` in definition order
guarantees the same result for the same inputs. The previous set-based formulation was
non-deterministic and could silently drop base-set members under adversarial cap values.

Constants (single-sourced in `SPEC.md §7`):
- `CONVERGE_REVIEW_BASE = ["engineering-security-engineer.md", "engineering-code-reviewer.md"]`
- `PARALLEL_SPECIALIST_CAP = 4`
- `SPECIALIST_ROUTING` = 3-entry routing table (db/schema → `engineering-database-optimizer.md`, ui → `testing-accessibility-auditor.md`, api → `testing-api-tester.md`). Security is always-on via `CONVERGE_REVIEW_BASE` and is the default for auth/session/crypto patterns (already included in the base set — no separate routing row).

### §7.4 Specialist spawn model

Orchestration agents spawn specialists with:
```
subagent_type: "general-purpose"
prompt: "Act as the agent defined in .agents/<AgentRef>. Read that file first."
```

Depth-1 only (from the orchestration agent). An orchestration agent (reviewer, fixer)
may spawn fix-specialists; a fix-specialist must not spawn further sub-agents. "Depth-1"
is measured from the orchestration agent, not from the Engine — this allows the fixer to
spawn fix-specialists (depth-1 from the fixer, depth-2 from the Engine). See `SPEC.md §9.2`.

**Allow-set enforcement (D2 / I9).** Before dispatching a reviewer or fixer, the Engine
computes `allowed_agent_refs = decide_specialists(changed_paths, round)` and passes it in
`DispatchContext.allowed_agent_refs`. The harness adapter **must reject** any sub-agent
spawn whose `AgentRef` is not in `allowed_agent_refs`. Reviewer/fixer contracts must read
`context.allowed_agent_refs` rather than recomputing the specialist set. This ensures the
spawn set is always Engine-controlled, not LLM-controlled — the diff cannot steer the
spawn even if the reviewer is deceived by injected content.

### §7.5 Pack supply-chain controls

- **SHA pinning** — `AGENT_PACK_PINNED_REF` must be a full 40-char SHA. Never bump to a
  floating branch or tag.
- **Diff review before bumping** — Before updating `AGENT_PACK_PINNED_REF`, review
  `https://github.com/msitarzewski/agency-agents/compare/<old>...<new>`. Do not bump
  without reading the diff.
- **Bake at build, never at runtime** — The pack is in the image; no network fetch at
  startup. Startup-time fetches are not acceptable.
- **SBOM** — The image SBOM must enumerate the pack source URL and SHA (see
  `ARCHITECTURE.md §5`).
- **Fork option** — For full supply-chain control, fork the pack into your own org and
  update `AGENT_PACK_REPO_URL`. The fork is then the only upstream.

---

## §8 Build Phases

Work through these in order. Don't start a phase until the previous phase's tests pass.

**Phase 1 — Decision Functions and Types.** Domain types from `SPEC.md §7` + all 13
decision functions (`SPEC.md §8.1–§8.12`; note §8.10 defines two functions:
`derive_issue_state` and `derive_pr_state`). Write every truth-table test from
`TESTING.md §2` alongside each function. **Also in Phase 1:** author
`coverage_map.yaml` at the repo root (see `TESTING.md §7.3`). This file must enumerate
every truth-table row from every `SPEC.md §8` function before the Phase 1 CI gate
can pass. Derive it by reading `SPEC.md §8` and listing each `(section, row-id)` key
with the test names that cover it. The CI gate will reject missing rows.

**Phase 2 — Port Fakes and Contract Suites.** `FakeForgePort`, `FakeHarnessPort`,
`FakeSessionPort`, `FakeCounterStore`, `FakeConvergeStateStore` — all five fakes — with
call log, configurable returns, fault injection, reset. Pass shared contract suites
(`TESTING.md §3`) before writing any real adapter.

**Phase 3 — Engine.** `Engine.dispatch`, `Engine.converge`, `Engine.reconcile`,
`Engine.intake` against the fakes. Write integration (`TESTING.md §4`), security
(`TESTING.md §5`), and idempotency (`TESTING.md §6`) tests alongside each method.
The engine holds no durable in-process state (`SPEC.md §10`).

**Phase 4 — OrchestratorService and Control-Plane API.** `ForgeEvent` + routing table,
`Config`/`RepoConfig`/`SwarmLimits`/`PortProvider`, `OrchestratorService` lifecycle,
event ingress, `delivery_id` LRU dedup, per-event/per-repo error isolation. Backing store:
SQLite for single-instance, Postgres for horizontal scaling.

**Phase 5 — Real Port Adapters.** `GitHubForgePort` + `HarnessPort` adapter for
`anthropics/claude-code-action`. Each must pass the shared contract suite before
production use. No adapter-specific skips.

**Phase 6 — PWA.** Mobile-first PWA per `WEBUI.md`. Speaks the `OrchestratorService`
HTTP API. No PWA code calls forge or harness APIs directly.

**Phase 7 — Container and Kubernetes.** Container image + Helm chart per
`ARCHITECTURE.md §5–§8`. The Dockerfile pack acquisition step must:
```dockerfile
ARG AGENT_PACK_REPO_URL="https://github.com/msitarzewski/agency-agents"
ARG AGENT_PACK_PINNED_REF="d6553e261e595c651064f899a6c33dd5aa71c9e3"
RUN git clone --no-tags --filter=blob:none ${AGENT_PACK_REPO_URL} /tmp/agency-agents \
 && git -C /tmp/agency-agents checkout ${AGENT_PACK_PINNED_REF} \
 && [ "$(git -C /tmp/agency-agents rev-parse HEAD)" = "${AGENT_PACK_PINNED_REF}" ] \
 && mkdir -p /app/.agents \
 && find /tmp/agency-agents -mindepth 2 -name "*.md" | while IFS= read -r f; do \
      target="/app/.agents/$(basename "$f")"; \
      [ -e "$target" ] && { echo "ERROR: basename collision: $f" >&2; exit 1; }; \
      cp "$f" "$target"; \
    done \
 && rm -rf /tmp/agency-agents
```
`--filter=blob:none` (blobless clone) reliably fetches any pinned SHA without shallow-clone
server-capability issues. The `rev-parse HEAD` assertion fails the build if the checkout
silently landed on the wrong commit. The basename-collision guard fails loudly if the pack
ever introduces two `*.md` files with the same name at different paths.

Verify the pack SHA appears in the image SBOM. Do not defer pack acquisition to runtime.

---

## §9 PROTECTED_PATHS — Build Agent Rule

These files may not be modified by any build agent PR. If a task requires touching one,
stop and escalate.

```
# from SPEC.md §7 — keep in sync
PROTECTED_PATHS = [
  ".github/workflows/**",
  "ARCHITECTURE.md",
  "SECURITY.md",
  "COMPLIANCE.md",
  ".agents/**",
  "agents/**",
]
```

Never hardcode path patterns in implementation code. Import from the `SPEC.md §7`
equivalent constant.

---

## §10 Code Style

- Match surrounding code: comment density, naming, import ordering.
- Python: `snake_case`, type hints everywhere, `pydantic` models, dataclasses for
  immutable records. Rust: `snake_case`, `impl` blocks, `derive` macros.
- No placeholder comments (`# TODO`, `// fixme`) in merged code.
- Function length: one screen. Engine methods orchestrate calls; logic belongs in
  decision functions or helpers.
- Error handling: every error either propagates to `OrchestratorService.handle_event` or
  maps to an escalation cause E1–E10. Silent failures don't pass review.
- Constants: never hardcode numeric thresholds. Import from the `SPEC.md §7` equivalent.
  Hardcoding `2` for `MAX_REDISPATCHES` is a blocker.

---

## §11 Security Invariants — Checklist for Every PR

See `SECURITY.md §3` for the authoritative invariant definitions and test names.

Before marking any PR ready, verify that none of your changes weakens:

- **I1** — Non-allowlisted authors never reach a code-writing agent without human
  promotion. (`test_security_unlisted_never_dispatches`, `test_security_promotion_required`)
- **I2** — PROTECTED_PATHS PRs escalate to E1 before any review round.
  (`test_security_protected_path_escalates`, `test_security_protected_path_all_patterns`)
- **I3** — No forge credentials or harness API keys in the agent sandbox.
- **I4** — `decide_intake` is pure and synchronous; no network, no side effects.
- **I5** — Triage agent is read-only. (`test_security_triage_agent_read_only`)
- **I6** — Every intake decision and human promotion is audit-logged.
- **I7** — `LABEL_AWAITING_PROMOTION` and `LABEL_AGENT_WORK` never coexist.
  (`test_security_awaiting_and_agent_work_never_coexist`)
- **I8** — `SECURITY.md` is in PROTECTED_PATHS.
  (`test_security_protected_path_all_patterns` includes `SECURITY.md`)
- **I9** — `AgentRef` values come only from `decide_specialists` output.
  (`test_security_agent_ref_not_from_contributor_text`)

---

## §13 Agent Selection for Tasks

**Override the default Explore reflex for any review, audit, or design task.** A
plan-mode workflow may default to the Explore subagent for "Phase 1 exploration." That
default is wrong whenever the task requires deep reading, cross-file comparison, or
design judgment. Use the purpose-built specialist agents:

| Task type | Right agent |
|---|---|
| Spec / design / buildability audit | Software Architect |
| Code correctness / logic review | Code Reviewer |
| Security analysis | Security Engineer |
| Workflow, branch, and recovery coverage | Workflow Architect |
| Test coverage and contract audit | Test Results Analyzer |
| "Where is X defined?" / file lookup | Explore |

**Why Explore is the wrong choice for reviews:** Explore reads file excerpts and is
optimized for "find the symbol / find the file." It cannot hold an 800-line spec in
context, compare sections, detect contradictions, or exercise design judgment. Using it
for audits or reviews produces shallow, single-angle output.

**How to override:** Select the matching specialist above. Make the agent prompt explicit:
"Read SPEC.md fully." Do not let the plan-mode Phase 1 default constrain specialist
selection — the phase boundary is workflow guidance, not a restriction on agent type.

---

## §12 Known Issues

Do not silently fix these. Flagged for human decision.

**OQ-1 (resolved): `ci-red` recovery now re-polls all 6 blocking CI checks.** The
former 3-of-6 behaviour (from the reference implementation) was a soundness hole: a PR
whose Docker Build / Helm Lint / Helm Kubeconform checks were still red could be
auto-approved. `Engine.converge §10.2` step 4g now polls all `BLOCKING_CI_CHECKS`
(SPEC.md §7) before approving on the `ci-red` recovery path. A new negative test
(`test_converge_ci_red_docker_still_red_escalates`) locks in the corrected behaviour.

**OQ-2: `MAX_REDISPATCHES` was duplicated in the reference implementation.** Now
single-sourced in `SPEC.md §7`. Never hardcode `2`. The CI gate will catch it.

**OQ-3: Two redispatch caps with different values govern overlapping situations.**
`MAX_REDISPATCHES = 2` (converge loop), `RECONCILER_STALE_REDISPATCH_CAP = 3` (stale PR
recovery), `ISSUE_REDISPATCH_CAP = 3` (orphan issue recovery). Distinct caps for distinct
situations. Do not unify without a human decision.

**OQ-4: `COMPLIANCE.md` is in PROTECTED_PATHS but not yet authored.** Do not invent its
content. The namespace is reserved.
