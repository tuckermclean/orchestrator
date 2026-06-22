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

**Deterministic**: `SPECIALIST_ROUTING` iterated in definition order; same inputs → same output.

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

**Allow-set enforcement (D2 / I9).** Engine sets `DispatchContext.allowed_agent_refs =
decide_specialists(changed_paths, round)`; harness adapter must reject out-of-set spawns.
Reviewer/fixer contracts must read `context.allowed_agent_refs`, not recompute it. See
`SPEC.md §9.2`, `SECURITY.md §3 I9`.

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

Explore is optimized for file/symbol lookup — it reads excerpts and cannot hold an
800-line spec in context. For audits and reviews, always use the specialist above.
Make the prompt explicit: "Read SPEC.md fully."

---

## §12 Known Issues

Do not silently fix these. Flagged for human decision.

**OQ-1 (resolved): `ci-red` recovery re-polls all 6 `BLOCKING_CI_CHECKS`** (formerly 3-of-6;
soundness hole). `test_converge_ci_red_docker_still_red_escalates` locks in the fix.

**OQ-2/OQ-3: `MAX_REDISPATCHES`** was duplicated; now single-sourced in `SPEC.md §7`.
Never hardcode `2`. Two remaining active caps: `RECONCILER_STALE_REDISPATCH_CAP=3` (RC-1),
`ISSUE_REDISPATCH_CAP=3` (RC-4). Do not unify without a human decision. See `SPEC.md §13`.

**OQ-4: `COMPLIANCE.md` is in PROTECTED_PATHS but not yet authored.** Do not invent its
content. The namespace is reserved.

---

## §14 Field Notes — Operating the Build Swarm

Hard-won lessons from driving issues through the **implement → review-swarm → fix →
adjudicate** pipeline in this repo. None are derivable from the specs or the code — a
freshly-spawned agent will not have them. Read these before orchestrating multi-PR work.

Throughout, *the adjudicator* is the orchestrating agent acting as the terminal converge
gate — the role the converge reviewer plays at R3 with the `ADJUDICATION_MODEL` (Opus)
verdict (`SPEC.md §5`). It runs the final review and stamps approve or escalate.

**Rebase every PR branch onto current `main` before the final review and before merge.**
This repo squash-merges PRs, which creates two traps. (1) *Stranded content*: a PR stacked
on another branch can be merged into that branch while the base is *separately*
squash-merged to `main` — so the stacked changes never reach `main`. A `SECURITY.md` /
`ARCHITECTURE.md` fix was lost exactly this way and had to be re-landed in a fresh PR.
(2) *Stale-base illusion*: a branch cut before other PRs merged shows a diff against current
`main` that appears to **revert** those merged changes — producing false-positive
protected-path review findings and a real risk of reverting merged work on squash. The fix
for both is the same: `git fetch` and rebase onto current `main` before reviewing or merging.
Never review a diff computed against a stale base.

**Re-run the gates yourself; never trust an agent's "all green."** Implementer and fixer
agents reliably *report* success, but independently re-running `pytest` / `mypy --strict` /
`ruff` plus behavioral spot-checks has repeatedly caught real defects the agent missed or
mis-tested — e.g. a `decide_round` that silently accepted out-of-range rounds (the `Literal`
type was never enforced at runtime), and a "timeout" test whose assertion was vacuously true
over an empty list. The adjudicator verifies behavior, it does not relay a report.

**The adjudicator resolves reviewer severity disagreements — don't just count votes.**
Reviewers will split on blocker-vs-suggestion (the uncancelled reviewer-timeout was a
SUGGESTION to one reviewer and a BLOCKER to another). Decide on the merits: a wired-in code
path that violates its own documented contract is a blocker even if "the happy path doesn't
hit it."

**Route reviewers by the actual risk surface, and over-staff logic/coverage-heavy PRs.**
`decide_specialists` routing (security + code-reviewer base, plus a11y/db/api by changed
path) is the floor for a review swarm. For decision-function or engine changes, also add a
Test-Results-Analyzer (truth-table → named-test mapping — it caught a missing SPEC-named
regression guard and a dangling `coverage_map` reference) and a Software-Architect
(spec fidelity — it independently caught the stale-base hazard and an OQ-1 named-guard gap).
No single reviewer catches every class of defect.

**Escalate protected-path contradictions to a human; never silently fix them; disclose every
touched file.** Correcting a spec can leave a contradiction in a PROTECTED_PATH doc
(`ARCHITECTURE.md`, `SECURITY.md`). Open an escalation issue and route a *separate*,
clearly-labeled human-authorized PR — do not edit the protected file inside a build PR.
Protected-path review is **path-based, not semantic**: even a one-character cross-reference
fix in `SECURITY.md` trips E1. Always list every touched file (especially protected ones) in
the PR body so the human gate is never surprised by an undisclosed edit.

**Keep a model-tier fallback and confirm spawns actually started.** The swarm tier
(`DEFAULT_SWARM_MODEL`, Sonnet) had transient capacity failures (HTTP 529); falling back to
the adjudication tier (Opus) for implementers kept work moving. An async agent that returns
with **0 tool-uses** died at startup and did *nothing* — verify it produced a branch/commit
before assuming progress, and relaunch rather than continue.

**To push a fix onto an agent's PR branch that is checked out in that agent's worktree:** you
cannot check the branch out a second time. Either work in a fresh worktree with
`git reset --hard origin/<branch>` then `git push origin HEAD:<branch>`, or rebase a temporary
local branch and push it to the remote ref with `git push --force-with-lease origin
<tmp>:<branch>`.

**Confirm that claimed CI gates actually run.** `coverage_map.yaml` was documented since
Step 1 as a gate that "rejects missing rows," but the `@covers` enforcement was never
implemented — truth-table coverage was enforced only by human/swarm review (which is how a
dangling row name survived into a PR). A hand-maintained map with no validator gives false
confidence: if a doc claims a gate, verify the gate exists and has teeth before relying on it.

---

The notes below were added after a full autonomous build session (≈10 issues, ≈14 PRs in
one run). Each is tagged **[Obligatory]** (a gate, invariant, or merge correctness depends
on it — skipping it has bitten us) or **[Advisory]** (saves time/tokens, no correctness risk).

**[Obligatory] Know the exact CI gates; never instruct an implementer to run `ruff format`.**
CI (`.github/workflows/ci.yml`) runs exactly four things: `pytest`, `mypy --strict src/`,
`ruff check src/ tests/`, and the `ui-build`. **`ruff format --check` is NOT a gate.** When
an implementer prompt told an agent to satisfy `ruff format --check`, it ran `ruff format`
across the tree and bundled **26 unrelated reformatted files** into its PR — which then
conflicted with every other in-flight branch and buried the real change. Scope every
implementer's gate list to those four commands and explicitly forbid `ruff format`. (`mypy`
on CI is `--strict src/` only; running it over `tools/` too is fine but not required.)

**[Obligatory] Treat GitHub CI as the source of truth for gates — agent worktree venvs lie.**
A background agent's isolated worktree may have an **incomplete venv** (one lacked `PyJWT`),
so the agent reported "8 pre-existing failures / 4 mypy errors" that **did not exist** on
`main` and were pure environment artifacts. Do not merge on an agent's pytest counts. Either
re-run in a known-good venv pointing at the branch's `src`, or — simplest and canonical —
push the branch and read `gh pr checks` (CI installs full deps in a clean env). This extends
"re-run the gates yourself": the *where* matters as much as the *whether*.

**[Obligatory] Hook-based controls deny with exit code 2, not 1.** The I9 spawn-allow-set
PreToolUse hook denied out-of-set spawns by exiting **1** — but Claude Code's hook contract
blocks a tool **only on exit 2** (or exit 0 + JSON `{"hookSpecificOutput":{"permission
Decision":"deny"}}`); exit 1 is a *non-blocking* error and the tool proceeds. The control was
inert, and its 30 unit tests all passed because they asserted the script's own return value,
not the runtime contract. For any hook enforcing a security invariant: deny == `exit 2`, and
verify against the published hooks reference — a self-referential unit test proves nothing
about whether the runtime actually blocks.

**[Obligatory] Adjudicate the diff and strip scope-creep before merge.** Beyond re-running
gates, read the security/correctness-critical lines yourself — that is how the exit-1 hook
bug and the over-privileged token scope were caught. When an agent's PR carries changes
outside its task (the reformat pollution above), revert them to `origin/main`
(`git checkout origin/main -- <files>`) and amend, so the PR is *only* its real change. A PR
that touches 32 files when 6 are real is both a conflict magnet and unreviewable.

**[Obligatory] Parallelize only on disjoint files; serialize on the contention hubs.**
Multiple implementers run concurrently safely **iff** their file sets don't overlap. The hubs
that force serialization in this repo: `src/api/main.py`, `src/ports/harness.py`, `SPEC.md`,
and the shared contract suite (`tests/contracts/`). `coverage_map.yaml` is touched by almost
every PR but concurrent additions of *different* sections auto-merge on rebase, so it rarely
truly conflicts. Before launching a wave, map each issue to the hub it touches and never
start two issues that write the same hub at once — adjudicate-and-merge one, then launch the
next onto fresh `main`.

**[Advisory] After `gh pr merge --delete-branch`, the local branch survives if a worktree
holds it.** Clean up with `git worktree remove <wt> --force` then `git branch -D <branch>`
(and `git worktree prune`). Also: keep the orchestrator's *own* checkout parked on `main` —
worktree-isolated agents have been observed leaving the main checkout switched onto a feature
branch with uncommitted state; verify `git branch --show-current` before operating there.

**[Advisory] Docs/spec/config-only changes don't need the full review swarm.** For a verified
documentation, `coverage_map`-neutral, or single-line config change, self-review against the
ground-truth source (the code or the implemented convention) and merge — spending a multi-
agent swarm on a typo-class edit burns tokens for no added assurance. Reserve the swarm for
behavioral code. (Still rebase, still confirm CI.)
