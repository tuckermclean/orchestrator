# ROADMAP.md

**Authority:** This document is operator-authored build sequencing. It occupies the lowest
position in the document hierarchy (`AGENTS.md §2`): it may reference specs but never
override them. All behaviour must be grounded in `SPEC.md`, `SECURITY.md`,
`ARCHITECTURE.md`, `TESTING.md`, `WEBUI.md`, and `AGENTS.md`. When this document and a spec
conflict, the spec wins and this document must be updated.

---

## The Six Rules

Every step in this roadmap is contractually bound to all six:

1. **Full-stack product.** Every step ships a running backend + Web UI driveable end to end.
   No "backend only" or "UI stub" steps. Fake-backed through Step 7; real adapters from Step 8.

2. **Additive and non-breaking.** No step breaks any prior step. Enforced by:
   (a) additive-only changes — existing APIs, types, and decision functions are never removed
   or changed in breaking ways; (b) the **entire prior test suite must stay green** before a
   step is marked done (full regression gate, not just the new tests); (c) decision functions
   are pure and frozen once shipped — their truth tables are the single source of truth.

3. **Single-shot prompt.** Each step is scoped to what a single agent invocation can build and
   converge to zero blockers. The two heaviest methods (converge happy path; full fix loop) are
   split across Steps 5 and 6 so neither exceeds one shot.

4. **Step 1 is the maximal MVP.** The first step produces the most complete vertical slice a
   single prompt can land: foundation + observable dispatch + live agent-run stream in the
   browser.

5. **Converge to zero blockers per `AGENTS.md` rules.** Every step must converge — full test
   suite + `mypy --strict` + `ruff` all green; every `SPEC.md §8` truth-table row that has
   been reached covered via `coverage_map.yaml` + `@covers`; a converge review with zero
   blockers remaining — before the step is declared done.

6. **Agents visible in the Web UI from Step 1.** The Runs screen streams live agent-run
   events (SSE) from day one.

---

## Vertical-Slice Principle

`AGENTS.md §8` sequences the build **horizontally**: decision functions → port fakes → engine
→ service → real adapters → PWA → container. Correct for a horizontal layering, but it puts
the UI in Phase 6 and produces no runnable product until everything is wired.

This roadmap re-slices the same work **vertically**: each step is a thin end-to-end column
through all layers (domain → engine → service → API → UI) for a bounded slice of
functionality. Every step is runnable the day it ships.

The constraint is that each step still **obeys `AGENTS.md §8` intra-layer dependency order**:
decision functions and their tests before engine code that calls them; port fakes and their
contract suites before any engine code that uses those ports. The vertical slice picks up the
right subset of each horizontal layer in the right order.

---

## Universal Definition of Done

Stated once; every step references it.

- `pytest` — zero failures, all test layers (`tests/unit/`, `tests/contracts/`,
  `tests/integration/`, `tests/security/`)
- `mypy --strict` — zero errors across `src/`
- `ruff check` — zero warnings across `src/` and `tests/`
- `coverage_map.yaml` — every truth-table row for every `SPEC.md §8` function reached by the
  step is enumerated and tagged `@covers`; CI fails if a covered row has no test
- Converge review — zero blockers remaining (suggestions/nits do not block)
- Prior test suite green — all tests added by prior steps still pass (regression gate)
- Demoable product — the "what you see" outcome is verifiable by starting the service and
  driving the UI

---

## Non-Regression Model

Rule 2 is enforced structurally:

- **Additive API surface.** New endpoints are additions; existing endpoints keep their
  contracts. The `OrchestratorService` interface grows monotonically.
- **Frozen decision functions.** Once a pure decision function and its truth-table tests ship,
  neither is modified — only consumed by later steps.
- **Full regression gate.** Every step runs the complete inherited test suite, not just its
  new tests.
- **Contract-suite parity (fake → real).** Steps 1–7 use Fake ports. Step 8 swaps in real
  adapters (`GitHubForgePort`, real `HarnessPort`). The **same shared contract suites**
  (`TESTING.md §3`) run against both. Identical behaviour is proven, not assumed. A real
  adapter that fails any contract-suite test is a blocker on Step 8.
- **UI continuity.** Each UI addition is an additive new screen or section. Existing screens
  are never removed; their API endpoints remain stable.

---

## Model Tiering

Three sources of model assignment; each is independent and precedence is clear:

| Source | What it controls | Default |
|---|---|---|
| `route_entry` (§8.1) | Top-level entry dispatch (event-level) | `claude-opus-4-8` for `issues`; `claude-sonnet-4-6` for comments |
| `DEFAULT_SWARM_MODEL` | Routine swarm: implementer, converge reviewer R1/R2, fixer, specialists | `claude-sonnet-4-6` |
| `ADJUDICATION_MODEL` | Terminal verdict: converge reviewer R3 — the final approval gate and escalation-to-`needs-human` | `claude-opus-4-8` |

**`route_entry` is unchanged from the existing spec.** Opus orchestrates the top-level entry
(planning and dispatch). Sonnet runs the routine Tuesday swarm (review iterations, fix
rounds, specialist work). Opus adjudicates at the terminal boundary: it is the one that
decides "this is ready for a human to merge" or "this needs a human to intervene."

Per-slot overrides: `RepoConfig.model_config` (`ModelConfig { swarm, adjudication }` — see
`SPEC.md §11.2`). The Web UI exposes a model selector at each configurable slot; "Default"
resolves to the value in `ModelConfig`. Model strings are always config-sourced; never
constructed from contributor-supplied text (I9).

---

## Steps

### Step 0 — SPEC Amendment: Model Tiering (Pre-work, not a product step)

**This step is complete.** A surgical amendment to `SPEC.md` was applied before any build
work began:

- §7 constants: `DEFAULT_SWARM_MODEL = "claude-sonnet-4-6"`, `ADJUDICATION_MODEL = "claude-opus-4-8"`
- §7 new subsection: `### Model tier` — tier table and policy
- §10.2: `model` field set on reviewer `DispatchContext` (Adjudication at R3; Swarm at R1/R2) and fixer `DispatchContext` (Swarm)
- §11.2: `ModelConfig` type; `RepoConfig.model_config` field

No edit to `route_entry` §8.1, `TESTING.md`, or any agent contract.

---

### Step 1 — Walking Skeleton: Agents Live in the Browser

**What you see:** Start the service. Hit `POST /api/dev/dispatch` (or click Dispatch in the
UI). Watch a simulated agent run stream live events in the Runs screen. The Dashboard shows
the `pipeline_health` badge. This is the product from minute one.

#### Backend

- **Scaffold:** `pyproject.toml` (Python 3.12, `asyncio`), `ruff`, `mypy --strict`, `pytest`,
  `coverage_map.yaml` stub
- **Domain types** (`src/domain/types.py`) from `SPEC.md §7`:
  `IssueRef`, `PRRef`, `RunHandle`, `RunStatus`/`RunState`/`RunConclusion`,
  `RunSummary`, `RunDetail`, `HealthReport`, all label constants
- **Decision functions** (pure, sync — `AGENTS.md §5`):
  - `route_entry` (§8.1) — event → `{model, max_turns, contract}`
  - `pipeline_health` (§8.9) — `in_flight`, `blocked` → `HealthReport`
  - `derive_pr_state` / `derive_issue_state` (§8.10) — label sets → state enum
- **Fake ports** (`src/ports/fakes.py`):
  `FakeForgePort`, `FakeHarnessPort`, `FakeSessionPort` — implement the full port interfaces;
  `FakeHarnessPort.dispatch` emits a sequence of simulated `RunEvent`s (QUEUED → IN_PROGRESS
  → tool calls → COMPLETED) over an async queue that SSE consumes
- **Minimal `Engine.dispatch`** (`src/engine/dispatch.py`):
  `route_entry` → build `DispatchContext` → `harness.dispatch()` → store `RunSummary` in
  `SessionPort`
- **Minimal `OrchestratorService`**: `handle_event`, `list_runs`, `get_run`, `status`
- **Control-plane API** (`src/api/`):
  - `GET /api/status` → `pipeline_health` result
  - `GET /api/runs` → list of `RunSummary`
  - `GET /api/runs/{id}` → `RunDetail`
  - `GET /api/runs/{id}/stream` — SSE stream of `RunEvent`s from `FakeHarnessPort`
  - `POST /api/dev/dispatch` — dev trigger (fires `Engine.dispatch` with a fake issue event;
    auth stubbed, hardened in Step 9)

#### UI

Lightweight React/TypeScript SPA (Vite, served from FastAPI `StaticFiles`):

- **Runs screen** (`/runs`): live list; click a run → detail pane + SSE event tail
- **Dashboard** (`/`): `pipeline_health` badge; links to Runs

#### Tests

- `tests/unit/test_decision_functions.py`: every truth-table row for `route_entry` (§8.1),
  `pipeline_health` (§8.9), `derive_pr_state` / `derive_issue_state` (§8.10) — 100% branch
  coverage per `TESTING.md §1.3`
- `tests/contracts/test_forge_port.py`, `test_harness_port.py`, `test_session_port.py`:
  shared contract suites (`TESTING.md §3.1–§3.3`) passing against Fake implementations
- `tests/integration/test_dispatch_skeleton.py`: `Engine.dispatch` → `FakeHarnessPort` →
  `RunSummary` stored; SSE events delivered
- `coverage_map.yaml`: rows for §8.1, §8.9, §8.10

#### Spec coverage

`SPEC.md §7` (types, labels, constants), §8.1, §8.9, §8.10; `TESTING.md §2` (truth tables),
§3.1–§3.3 (fake port contracts); `AGENTS.md §8` Phase 1 (decision functions), Phase 2 (fakes
and contracts), Phase 3 partial (Engine.dispatch skeleton), Phase 4 partial (API subset)

#### Single-shot prompt seed

> Build the Walking Skeleton for the orchestrator. Scaffold a Python 3.12 + FastAPI project
> with ruff, mypy --strict, pytest, and a coverage_map.yaml. Implement domain types from
> SPEC.md §7, decision functions route_entry/pipeline_health/derive_pr_state/derive_issue_state
> with full truth-table tests. Implement FakeForgePort, FakeHarnessPort (emitting simulated
> RunEvents), FakeSessionPort with their TESTING.md §3 contract suites. Build a minimal
> Engine.dispatch and OrchestratorService. Expose GET /api/status, GET /api/runs,
> GET /api/runs/{id}, GET /api/runs/{id}/stream (SSE), POST /api/dev/dispatch. Ship a
> React/TypeScript SPA (Vite) with a Runs screen (live list, detail, SSE event tail) and a
> Dashboard (pipeline_health badge). Gate: mypy --strict, ruff, pytest all green; SSE events
> stream in the browser from a dev dispatch.

#### Definition of Done

Universal DoD + SSE events visibly streaming in the browser; `pipeline_health` badge renders
on Dashboard; `route_entry` truth table 100% covered.

#### Non-regression guarantee

First step; establishes the baseline. All subsequent steps inherit and must not break this
baseline.

---

### Step 2 — Intake & Triage: The Human Gate

**What you see:** A new (non-allowlisted) issue lands in the Triage Queue screen. You read
the triager's summary, click Promote, and watch the dispatch fire in the Runs screen.

#### Backend

- **Decision function:** `decide_intake` (§8.11) + full truth-table tests
- **`Engine.intake`** (`src/engine/intake.py`):
  `decide_intake` → atomic `set_labels` swap (AWAITING_PROMOTION vs direct AGENT_WORK) →
  dispatch triager via `FakeHarnessPort` (read-only scope, `forge_token_scope: "repo-comment"`)
  → write audit-log record to SQLite
- **Audit log** (`src/db/audit.py`): SQLite table `audit_events(id, ts, repo, entity_ref, action, operator)`
- **`OrchestratorService`** additions: `list_triage`, `promote`, `decline`
- **API additions:**
  - `GET /api/triage` → list of `TriageItem`
  - `POST /api/triage/{id}/promote`
  - `POST /api/triage/{id}/decline`

#### UI

- **Triage Queue screen** (`/triage`): list of queued issues with triager summary; Promote /
  Decline / Defer actions; audit log entries inline

#### Tests

- `tests/unit/test_decide_intake.py`: all §8.11 truth-table rows
- `tests/integration/test_intake.py`: full `Engine.intake` flow; admit path (direct dispatch)
  and queue path (AWAITING_PROMOTION label); triager dispatched with read-only scope
- `tests/security/test_intake_security.py`: I1 (allowlist gate), I4 (AWAITING_PROMOTION
  atomicity), I5 (triager token scope is repo-comment), I6 (triager cannot add labels),
  I7 (promote requires operator identity in audit record)

#### Spec coverage

`SPEC.md §8.11`, §10.0 (`Engine.intake`), §11.3 (`promote`/`decline`/audit); `TESTING.md §5`
(I1, I4, I5, I6, I7); `AGENTS.md §8` Phase 3 partial, Phase 4 partial

#### Single-shot prompt seed

> Add intake and triage to the orchestrator. Implement decide_intake (SPEC.md §8.11) with
> truth-table tests. Build Engine.intake: decide_intake → atomic set_labels swap → triager
> dispatch (forge_token_scope: repo-comment) → SQLite audit log. Add OrchestratorService
> list_triage/promote/decline and the API endpoints GET /api/triage, POST /api/triage/{id}/promote,
> POST /api/triage/{id}/decline. Add a Triage Queue screen to the UI. Add security tests
> I1/I4/I5/I6/I7. Prior test suite must stay green.

#### Definition of Done

Universal DoD + triage queue populates in UI; promote triggers visible dispatch in Runs
screen; I1/I5/I6 security tests pass; all prior tests green.

#### Non-regression guarantee

`Engine.dispatch`, `OrchestratorService`, and all Step 1 API endpoints are untouched.
`decide_intake` is a new pure function; no existing function is modified.

---

### Step 3 — Dispatch & Build: Draft PR → Implementer → BUILDING

**What you see:** Promote an issue → a draft PR appears in the fake forge → the implementer
run streams live in the Runs screen → PR detail shows BUILDING badge with changed files and
linked issue.

#### Backend

- **Full `Engine.dispatch`** (`src/engine/dispatch.py`):
  - `route_entry` → `DispatchContext` with `model = route_entry result` (Opus for issues event)
  - Dedup guard: skip if open implementing PR already exists for this issue
  - Open draft PR (`create_pr`), add `LABEL_IMPLEMENTING`
  - Dispatch orchestrator contract via `FakeHarnessPort` with `forge_token_scope: "repo-branch"`
  - Wire `agents/orchestrator.md` as the injected contract
  - For `@claude` comment events: dispatch implementer with Sonnet (`route_entry` result)
- **`FakeHarnessPort`** extension: simulate the orchestrator→implementer delegation sequence
  (open draft PR event, commit events, BUILDING label transition)
- **`OrchestratorService`** additions: `get_run` full detail including PR link, changed files,
  linked issue, build status

#### UI

- **PR/Build detail** view (accessible from Runs screen): BUILDING badge, draft PR title and
  branch, linked issue, changed-files count, live event tail

#### Tests

- `tests/integration/test_dispatch.py` (`TESTING.md §4.2`): dispatch lifecycle — dedup guard
  prevents second draft PR; `LABEL_IMPLEMENTING` added; orchestrator dispatched with correct
  model/scope; `@claude` comment routes with Sonnet model
- `tests/unit/test_route_entry.py`: verify model/max_turns per §8.1 truth table (existing
  tests from Step 1 continue to pass)

#### Spec coverage

`SPEC.md §8.1`, §10.1 (full `Engine.dispatch`); `TESTING.md §4.2`; `AGENTS.md §8` Phase 3
(Engine.dispatch complete), agents/orchestrator.md, agents/implementer.md

#### Single-shot prompt seed

> Complete Engine.dispatch (SPEC.md §10.1): route_entry → DispatchContext → dedup guard →
> create_pr (draft) + LABEL_IMPLEMENTING → dispatch orchestrator contract (model from
> route_entry). Wire agents/orchestrator.md as the injected contract. Extend FakeHarnessPort
> to simulate the orchestrator→implementer sequence emitting BUILDING state events. Add
> @claude comment routing (Sonnet model). Add PR/Build detail view to the UI (BUILDING badge,
> draft PR, linked issue, changed files). Add TESTING.md §4.2 dispatch-lifecycle tests.
> All prior tests must stay green.

#### Definition of Done

Universal DoD + promote → draft PR visible in fake forge state; Runs screen shows BUILDING;
dedup guard test passes; `@claude` comment dispatch test passes.

#### Non-regression guarantee

`Engine.intake` and triage API endpoints are untouched. `route_entry` truth table is
unchanged. New `Engine.dispatch` is a completed implementation of what Step 1 stubbed;
the stub's existing integration test is replaced by the fuller §4.2 suite.

---

### Step 4 — Converge (Happy Path): CONVERGING → APPROVED

**What you see:** Mark a draft PR ready → the converge reviewer (Sonnet, R1) runs with
specialists → zero blockers → Opus (R3, or directly via happy-path short-circuit) stamps the
final approval gate → PR becomes APPROVED. The Converge detail screen shows per-round
verdicts, specialist list, CI status, and the model used.

#### Backend

- **Decision functions:**
  - `resolve_blockers` (§8.2) — verdict JSON or comment-footer fallback
  - `decide_round` (§8.3) — `approve` path (0 blockers, CI green)
  - `decide_specialists` (§8.12) — base set + routing table + cap
- **Fake stores:** `FakeCounterStore`, `FakeConvergeStateStore` + their contract suites
  (`TESTING.md §3.5`, §3.6)
- **`Engine.converge`** (`src/engine/converge.py`) — round-1 `approve` path:
  - Idempotency gate, protected-path check, empty-diff check
  - Seed init sentinel; compute `specialist_refs`; build reviewer `DispatchContext` with
    `model = ADJUDICATION_MODEL if r == CONVERGE_ROUNDS else DEFAULT_SWARM_MODEL`
  - Await reviewer; poll CI; `resolve_blockers`; `decide_round` → `approve`
  - `add_label(LABEL_READY)`, remove `LABEL_CONVERGE`, post approving review;
    clear `ConvergeStateStore`
- Wire `agents/converge-reviewer.md`; `FakeHarnessPort` emits a zero-blocker verdict
- Model: reviewer dispatched with Sonnet at R1/R2, Opus at R3; in the happy path the
  reviewer reaches `approve` at R1 (Sonnet signs off a clean PR — correct)

#### UI

- **Converge detail screen** (per-PR): per-round verdict accordion (blockers · suggestions ·
  nits · `blocker_signatures`, specialist list with status, CI check grid, `decide_round`
  token, model used for each round)

#### Tests

- `tests/unit/test_decide_round.py`: `approve` rows of §8.3 truth table (full branch
  coverage including CI-green guard)
- `tests/unit/test_resolve_blockers.py`: rows 1–4 of §8.2 truth table
- `tests/unit/test_decide_specialists.py`: §8.12 truth table (base set, each routing
  pattern, cap enforcement)
- `tests/contracts/test_counter_store.py`, `test_converge_state_store.py`:
  §3.5, §3.6 contract suites against Fake stores
- `tests/integration/test_converge_happy.py`: full R1 approve path — sentinel seeded,
  reviewer dispatched (Sonnet), verdict written, CI green, `decide_round → approve`,
  `LABEL_READY` added, `LABEL_CONVERGE` removed
- `tests/security/test_converge_protected_path.py`: I2 — PR touching PROTECTED_PATHS
  escalates immediately to E1 before any specialist spawns

#### Spec coverage

`SPEC.md §5` (converge sub-machine, approve path), §8.2, §8.3, §8.12, §10.2 (happy path);
`TESTING.md §3.5`, §3.6, §5 (I2); `AGENTS.md §8` Phase 3 (Engine.converge partial),
agents/converge-reviewer.md

#### Single-shot prompt seed

> Add the converge happy path. Implement resolve_blockers (§8.2), decide_round (§8.3 approve
> rows), decide_specialists (§8.12) with full truth-table tests. Add FakeCounterStore and
> FakeConvergeStateStore with their TESTING.md §3.5/§3.6 contract suites. Implement
> Engine.converge (SPEC.md §10.2) for the round-1 approve path: idempotency gate →
> protected-path check → empty-diff check → seed sentinel → reviewer dispatch (model
> DEFAULT_SWARM_MODEL at R1, ADJUDICATION_MODEL at R3) → CI poll → resolve_blockers →
> decide_round → approve actions. Wire agents/converge-reviewer.md. Add a Converge detail
> screen (per-round verdicts, specialists, CI grid, model used). I2 security test. All prior
> tests must stay green.

#### Definition of Done

Universal DoD + converge detail renders zero-blocker verdict; `LABEL_READY` set; I2 test
passes; all prior tests green.

#### Non-regression guarantee

`Engine.dispatch` and `Engine.intake` are untouched. New pure functions are additions.
`FakeCounterStore` and `FakeConvergeStateStore` are new; no existing port is modified.

---

### Step 5 — Converge (Full Machine): Multi-Round Fix Loop + Escalation

**What you see:** A PR with blockers enters the converge loop → R1 reviewer (Sonnet) finds
blockers → R1 fixer (Sonnet) addresses them → R2 reviewer (Sonnet) re-checks → if still
stuck, R3 reviewer (Opus) issues the terminal verdict → either `approve` or `needs-human`.
The Converge detail screen shows the full round history, fixer runs, escalation cause, and
the model used per round.

#### Backend

- **Decision function:** `decide_cap_action` (§8.4) + tests
- **`Engine.converge`** — full 3-round loop:
  - `fix` (R1/R2): build fixer `DispatchContext` (`model = DEFAULT_SWARM_MODEL`); dispatch
    fixer; await; advance to next round
  - `escalate:no-progress` (E2), `escalate:no-verdict` (E3, with NO_VERDICT_RETRY_CAP),
    `escalate:ci-red` (OQ-1 recovery), `escalate:cap-reached` (E5)
  - Fixer timeout → `terminal_escalate(E11)`
  - `terminal_escalate` normative shorthand: `add_label(LABEL_NEEDS_HUMAN)`,
    `counter.reset`, `converge_state.clear` per §10.2
  - Accumulated nits → follow-up issue at finalize (omit if empty)
  - Wire `agents/converge-fixer.md`
- **Complete `coverage_map.yaml`**: all §8 truth-table rows (§8.1–§8.12) enumerated

#### UI

- **Converge detail** additions: full round history (each round with reviewer result then
  fixer run), escalation cause label (E-code), model used per dispatch

#### Tests

- `tests/unit/test_decide_cap_action.py`: §8.4 truth table (full branch coverage)
- `tests/unit/test_decide_round_full.py`: all §8.3 rows including no-progress, no-verdict,
  ci-red, cap-reached
- `tests/integration/test_converge_full.py`:
  - R1 blocker → R1 fix → R2 zero-blocker → `approve`
  - R1 same-signatures as R2 → `escalate:no-progress` (E2)
  - R3 `"unknown"` blockers → retry < cap → re-arm; cap reached → E3
  - R3 zero-blockers but CI red → CI re-trigger → green → `approve`; re-trigger fails → E4
  - R3 remaining blockers → `escalate:cap-reached` (E5)
  - Fixer timeout → E11
- `tests/security/test_converge_security.py`: I3 (forge token scoped to branch during
  converge), I9 (AgentRef allow-set enforcement; fixer cannot spawn out-of-set specialist),
  nit follow-up issue created when nits accumulate

#### Spec coverage

`SPEC.md §5` (full converge loop), §6 (E2–E5, E11), §8.3, §8.4, §10.2 (complete);
`TESTING.md §5` (I3, I9, OQ-1); `AGENTS.md §8` Phase 3 (Engine.converge complete),
agents/converge-fixer.md

#### Single-shot prompt seed

> Complete Engine.converge (SPEC.md §10.2) with the full 3-round fix loop. Implement
> decide_cap_action (§8.4) with truth-table tests. Add fix (R1/R2 fixer dispatch with
> DEFAULT_SWARM_MODEL), no-progress (E2), no-verdict (E3 with retry cap), ci-red (OQ-1
> CI re-trigger), cap-reached (E5), and fixer-timeout (E11) paths. Wire
> agents/converge-fixer.md. Complete coverage_map.yaml for all §8 truth-table rows.
> Add round history, fixer run entries, escalation cause, and per-dispatch model to the
> Converge detail screen. Add I3/I9 security tests and all missing §8.3/§8.4 rows.
> All prior tests must stay green.

#### Definition of Done

Universal DoD + full round history visible for a 3-round blocked-then-escalated run;
`coverage_map.yaml` enumerates all §8 rows; I3/I9 security tests pass; all prior tests green.

#### Non-regression guarantee

The happy-path approve from Step 4 continues to work. `decide_round` happy-path rows are
unchanged. All Fake stores remain compatible.

---

### Step 6 — Resilience: Reconciler, Crash Recovery, Escalate/Resume

**What you see:** Kill a run mid-flight → on the next reconciler tick (15 min, or trigger
manually via `POST /api/dev/reconcile`) the PR is recovered. Escalate a PR → Escalation
screen shows E-code and Resume/Re-queue/Acknowledge actions. AT_RISK badge appears when
`in_flight >= AT_RISK_THRESHOLD`.

#### Backend

- **Decision functions:**
  - `decide_stale_action` (§8.5)
  - `decide_rearm_action` (§8.6)
  - `decide_conflict_action` (§8.7)
  - `decide_redispatch_action` (§8.8)
  all with full truth-table tests
- **`Engine.reconcile`** (`src/engine/reconcile.py`):
  RC-1 (stale implementing recovery), RC-2 (merge-conflict), RC-3 (converge re-arm),
  RC-4 (orphan-issue), RC-5 (AWAITING_PROMOTION nudge)
- **Real SQLite `CounterStore`** (`src/db/counter.py`): atomic increment (`UPDATE … + 1`),
  `get_count`, `reset`; replaces `FakeCounterStore` in production path (Fake remains for
  tests)
- **`OrchestratorService`** additions: `deescalate_pr` (P16/P17), `reconcile_now`;
  cron/cadence loop (`RECONCILER_CRON`); `delivery_id` LRU dedup (DB-backed)
- **API additions:**
  - `GET /api/repos/{repo}/escalations` → list of escalated PRs with E-codes
  - `POST /api/repos/{repo}/prs/{id}/deescalate`
  - `POST /api/dev/reconcile` — dev trigger (runs `Engine.reconcile` immediately)

#### UI

- **Escalation section** (within PR detail): E-code, human-readable cause, Resume /
  Re-queue / Acknowledge actions
- **Dashboard** additions: BLOCKED badge count, AT_RISK badge when `pipeline_health`
  AT_RISK verdict

#### Tests

- `tests/unit/test_decide_stale_action.py`, `test_decide_rearm_action.py`,
  `test_decide_conflict_action.py`, `test_decide_redispatch_action.py`:
  full §8.5–§8.8 truth tables
- `tests/integration/test_reconcile.py` (`TESTING.md §6`):
  - RC-1: stale draft → redispatch; stale-draft escalate at cap; 0-diff draft → redispatch;
    0-diff non-draft → needs-human; has-converge → mark-ready
  - RC-2: conflicting PR → `needs-human`; MERGEABLE → skip
  - RC-3: re-arm after CI completion; skip-recent guard
  - RC-4: orphan issue → redispatch; cap → escalate
  - RC-5: AWAITING_PROMOTION > nudge threshold → push notification
- `tests/integration/test_idempotency.py` (`TESTING.md §6`): delivery-ID dedup; same event
  delivered twice produces one action; `reconcile_now` is idempotent
- `tests/integration/test_counter_store_atomic.py`: concurrent increment is atomic (no
  lost updates)

#### Spec coverage

`SPEC.md §4` (RC-1–RC-5), §8.5–§8.8, §10.3 (`Engine.reconcile`), §11.3 (`deescalate_pr`);
`TESTING.md §6` (idempotency, crash-only); `AGENTS.md §8` Phase 3 (Engine.reconcile)

#### Single-shot prompt seed

> Add the reconciler and crash recovery. Implement decide_stale_action (§8.5),
> decide_rearm_action (§8.6), decide_conflict_action (§8.7), decide_redispatch_action (§8.8)
> with full truth-table tests. Build Engine.reconcile (SPEC.md §10.3) with RC-1..RC-5.
> Replace FakeCounterStore with a real atomic SQLite CounterStore in the production path.
> Add OrchestratorService.deescalate_pr, reconcile_now, and the cron cadence loop.
> Add delivery_id LRU dedup. API: GET /api/repos/{repo}/escalations,
> POST /api/repos/{repo}/prs/{id}/deescalate, POST /api/dev/reconcile. UI: Escalation
> section on PR detail (E-code, Resume/Re-queue/Acknowledge); BLOCKED/AT_RISK on Dashboard.
> TESTING.md §6 idempotency and crash-only tests. All prior tests must stay green.

#### Definition of Done

Universal DoD + kill a run → reconciler recovers it on next tick; deescalate PR → visible in
UI; idempotency tests pass; AT_RISK badge renders; all prior tests green.

#### Non-regression guarantee

`FakeCounterStore` continues to exist and is still used in all existing tests. Real
`CounterStore` is injected via `PortProvider` in the production path only. Decision
functions §8.1–§8.4, §8.9–§8.12 are untouched.

---

### Step 7 — Full Converge Coverage + `coverage_map.yaml` Audit

**What you see:** CI gate enforces that every `SPEC.md §8` truth-table row is covered by at
least one `@covers` test annotation. A missing row is a CI failure, not a warning.

> **Why a separate step?** The `coverage_map.yaml` was seeded in Step 1 and extended
> incrementally, but the full enforcement gate (CI fails on uncovered rows) requires all
> decision functions to be present — which only becomes true after Step 6. This step
> completes the enforcement loop.

#### Backend

- **`coverage_map.yaml`** audit: enumerate all rows for §8.1–§8.12 (including both functions
  in §8.10); cross-check against `@covers` annotations in the test suite; add any missing
  test cases; lock the CI gate to fail on uncovered rows
- **`pytest` plugin / CI script** (`tests/conftest.py`): load `coverage_map.yaml`, assert
  each row has at least one `@covers` hit; fail with the list of uncovered rows

#### Tests

Gap-fill only: any §8 row not yet covered by a prior step gets a test. No new production
code.

#### Spec coverage

`SPEC.md §8.1–§8.12` (complete); `TESTING.md §1.3` (100% branch coverage requirement),
§7.3 (`coverage_map.yaml` enforcement)

#### Single-shot prompt seed

> Audit coverage_map.yaml against the full SPEC.md §8 truth tables. Enumerate every row for
> §8.1–§8.12 (including both derive_pr_state and derive_issue_state from §8.10). For each
> row without a @covers annotation in the test suite, add the missing test. Implement a
> pytest fixture (or conftest.py plugin) that loads coverage_map.yaml and fails CI when any
> row is uncovered. All prior tests must stay green.

#### Definition of Done

Universal DoD + CI fails when a row is removed from the test suite; all rows covered with
zero gaps.

---

### Step 8 — Go-Live: Real GitHub + Real Harness

**What you see:** The exact same UI — Runs, Triage, Converge detail, Escalation — now drives
**real agents on a real GitHub repo** via `claude-code-action`.

#### Backend

- **`GitHubForgePort`** (`src/ports/github.py`): implements the full `ForgePort` interface
  using the GitHub REST API; passes the **shared contract suite with zero skips** against the
  real GitHub API (in a sandboxed test repo)
- **Real `HarnessPort`** (`src/ports/harness.py`): calls `anthropics/claude-code-action` via
  the harness API; passes the **shared contract suite with zero skips**
- **`PortProvider`** (`src/ports/provider.py`): holds `FORGE_TOKEN`, `HARNESS_API_KEY`
  credentials; sourced from environment, never from `DispatchContext` or contributor input
- **Webhook ingress** (`src/api/webhook.py`): `POST /api/webhook` — HMAC-SHA256 validation
  (`X-Hub-Signature-256`), then `OrchestratorService.handle_event`
- **`OrchestratorService`** wired to real `PortProvider` in production entrypoint
  (`src/main.py`); Fake ports remain the default for `pytest`

#### Tests

- `tests/contracts/test_github_forge_port.py`: same contract suite as `test_forge_port.py`
  run against real `GitHubForgePort` (sandboxed repo; tagged `@integration_real`)
- `tests/contracts/test_real_harness_port.py`: same contract suite as `test_harness_port.py`
  against real `HarnessPort`
- `tests/security/test_webhook.py`: invalid HMAC → 403; replayed `X-GitHub-Delivery` → 200
  (deduped, no action)

#### Spec coverage

`SPEC.md §9.1` (`ForgePort` complete), §9.2 (`HarnessPort` real), §11` (`PortProvider`);
`TESTING.md §3.1`, §3.2` (contract parity); `ARCHITECTURE.md §5` (provenance);
`AGENTS.md §8` Phase 5

#### Single-shot prompt seed

> Implement GitHubForgePort (SPEC.md §9.1) against the GitHub REST API. Implement real
> HarnessPort calling anthropics/claude-code-action. Implement PortProvider holding
> FORGE_TOKEN and HARNESS_API_KEY from environment. Run the existing shared contract suites
> against both real implementations with zero skips — any contract failure is a blocker.
> Add POST /api/webhook with HMAC-SHA256 validation and delivery_id dedup. Wire the
> production entrypoint to real PortProvider; keep Fake ports as the pytest default.
> Add webhook security tests (invalid HMAC → 403, delivery-id replay → deduped). All prior
> tests must stay green.

#### Definition of Done

Universal DoD + both real port implementations pass their contract suites with zero skips;
webhook HMAC test passes; service starts against a real repo and processes a real issue
through to dispatch.

#### Non-regression guarantee

Fake ports are unchanged and remain the default for all existing tests. Real adapters are
injected only in the production `main.py` path. The same contract suite is the proof of
parity.

---

### Step 9 — Full PWA

**What you see:** The same product, now with offline-capable progressive web app features:
installable, push notifications for escalation/promotion/approval, JWT auth, WCAG 2.1 AA,
responsive on mobile.

#### Backend

- **JWT auth** (`src/api/auth.py`): `POST /api/auth/login` → JWT; `POST /api/auth/refresh`;
  middleware validates Bearer token on all API routes except `/api/webhook`
- **VAPID web push** (`src/api/push.py`): `POST /api/push/subscribe`, `DELETE /api/push/subscribe`;
  push events: escalation, promotion notification, approval
- **Operator management:** `POST /api/operators`, `GET /api/operators`

#### UI (complete `WEBUI.md` spec)

- **Service worker** (`public/sw.js`): cache-first assets; network-first API/SSE
- **Web app manifest** (`public/manifest.json`): installable PWA
- **VAPID push integration**: subscribe on install; receive escalation/promotion/approval
  notifications
- **All 7 screens** per `WEBUI.md`:
  Dashboard, Runs (list + detail + SSE), Triage Queue, PR/Build detail, Converge detail,
  Escalation, Settings (model selectors, config)
- **JWT login screen** (`/login`)
- **WCAG 2.1 AA**: ARIA labels, keyboard navigation, focus management, colour contrast
- **Responsive**: breakpoints for mobile/tablet/desktop
- **3s TTI budget**: Lighthouse score ≥ 90 on mobile

#### Tests

- `tests/security/test_auth.py`: unauthenticated API request → 401; expired JWT → 401;
  webhook bypasses auth
- `tests/unit/test_push.py`: VAPID subscription stored; push on escalation event
- Accessibility: manual WCAG audit against all 7 screens (blocker: any WCAG 2.1 AA failure)

#### Spec coverage

`WEBUI.md` (complete); `AGENTS.md §8` Phase 6

#### Single-shot prompt seed

> Harden the web UI to the full WEBUI.md spec. Add JWT auth (POST /api/auth/login, refresh,
> Bearer middleware). Add VAPID web push (subscribe/unsubscribe; push on escalation,
> promotion, approval). Add a service worker (cache-first assets, network-first API) and web
> app manifest (installable PWA). Implement all 7 WEBUI.md screens. Add model selector in
> Settings (per-slot, "Default" resolves to ModelConfig). WCAG 2.1 AA audit all screens.
> 3s TTI budget (Lighthouse ≥ 90 mobile). JWT security tests. All prior tests must stay green.

#### Definition of Done

Universal DoD + PWA installs from browser; VAPID push received on escalation; WCAG 2.1 AA
passes on all 7 screens; Lighthouse mobile score ≥ 90; JWT auth enforced; Settings model
selectors functional.

---

### Step 10 — Ship: Container + Kubernetes + Provenance

**What you see:** A signed, multi-arch OCI image deployable to Kubernetes via Helm. The SBOM
enumerates the specialist pack SHA.

#### Backend / Build

- **Multi-arch image** (`Dockerfile`): `python:3.12-slim`; blobless pack clone at build time
  (`git clone --filter=blob:none`), SHA assertion (`git rev-parse HEAD == AGENT_PACK_PINNED_REF`),
  flatten `.agents/` directory; Vite UI assets baked in
- **`cosign` signature** (CI step): image signed with keyless cosign at push time
- **SBOM** (`cyclonedx-bom` or `syft`): CycloneDX + SPDX; pack source URL and SHA
  enumerated per `AGENTS.md §7.5`
- **OCI annotations**: `org.opencontainers.image.revision`, `source`, `version`
- **Helm chart** (`charts/orchestrator/`): `Deployment`, `Service`, `Ingress`,
  `ConfigMap` (non-secret config), `Secret` (tokens), health probes (`/api/status`)
- **`helm lint` + `helm kubeconform`** in CI (already in `BLOCKING_CI_CHECKS`)

#### Tests

- `tests/build/test_image.py`: image starts; `GET /api/status` returns 200; SBOM contains
  pack SHA; cosign signature verifiable
- `helm lint` + `helm kubeconform` pass in CI

#### Spec coverage

`AGENTS.md §7.5` (pack supply-chain), §8` Phase 7; `ARCHITECTURE.md §5` (provenance, SBOM)

#### Single-shot prompt seed

> Package the orchestrator for deployment. Multi-arch Dockerfile: blobless pack clone at
> build, SHA assertion, flatten .agents/, bake Vite UI assets. Helm chart: Deployment,
> Service, Ingress, ConfigMap, Secret, health probes on /api/status. cosign keyless
> signature at CI push. CycloneDX + SPDX SBOM (pack URL + SHA enumerated). OCI annotations.
> Build test: image starts, GET /api/status 200, SBOM contains pack SHA. helm lint +
> kubeconform in CI. All prior tests must stay green.

#### Definition of Done

Universal DoD + `helm install` succeeds on a local cluster; `GET /api/status` 200; SBOM
contains the pinned pack SHA; cosign signature verifiable; `helm lint` and `helm kubeconform`
both pass.

---

## Traceability Matrix

| Step | SPEC.md §8 functions | TESTING.md sections | AGENTS.md §8 phases | Security invariants | Model tier |
|---|---|---|---|---|---|
| 0 | — | — | — | I9 (model from config) | DEFAULT_SWARM_MODEL, ADJUDICATION_MODEL defined |
| 1 | §8.1, §8.9, §8.10 | §2, §3.1–§3.3 | P1, P2, P3 partial, P4 partial | — | route_entry (unchanged) |
| 2 | §8.11 | §5 (I1,I4,I5,I6,I7) | P3 partial, P4 partial | I1, I4, I5, I6, I7 | — |
| 3 | §8.1 (consumed) | §4.2 | P3 partial | — | Opus for issues entry |
| 4 | §8.2, §8.3 (approve), §8.12 | §3.5, §3.6, §5 (I2) | P3 partial | I2 | Sonnet R1/R2, Opus R3 |
| 5 | §8.3 (full), §8.4 | §5 (I3,I9,OQ-1) | P3 complete | I3, I9 | Sonnet fixer |
| 6 | §8.5, §8.6, §8.7, §8.8 | §6 | P3 (reconcile) | — | — |
| 7 | §8.1–§8.12 (coverage gate) | §1.3, §7.3 | — | — | — |
| 8 | — | §3.1, §3.2 (real) | P5 | HMAC/I3 | — |
| 9 | — | — | P6 | JWT/auth | UI model selectors |
| 10 | — | — | P7 | supply-chain | — |

---

## What "Fake-Backed" Means

Steps 1–7 use the Fake port implementations (`FakeForgePort`, `FakeHarnessPort`,
`FakeSessionPort`) injected by `PortProvider`. This means:

- The service runs locally with no GitHub credentials and no harness API key
- `FakeHarnessPort.dispatch` emits a scripted sequence of simulated `RunEvent`s
- The forge state (issues, PRs, labels, comments) lives in an in-memory dict
- All non-trivial product behaviour (converge loop, reconciler, escalation) is exercised
  against this local simulation

The **shared contract suites** (`TESTING.md §3`) are the guarantees that the Fake and real
implementations are behaviourally equivalent. When Step 8 swaps in real adapters, the same
contract suites run against both with zero skips. A failing contract test on the real adapter
is a blocker on Step 8, not a warning.

This means every step before Step 8 is a **genuinely runnable product** — not a mock — that
happens to use simulated external services. The behaviour is correct by construction and
proven by the contract suites.
