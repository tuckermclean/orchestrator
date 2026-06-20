# API.md — Forge-Agnostic Agent-Orchestration API Specification

**Version**: 1.0
**Date**: 2026-06-20
**Status**: Draft
**Depends on**: STATE_MACHINE.md 1.0, DECISION_LOGIC.md 1.0

---

## §1 Overview

This document specifies the minimal async API that implements the state machine defined in
`STATE_MACHINE.md` and the decision functions defined in `DECISION_LOGIC.md`. It is
**not** an implementation — it is a language-neutral contract for any port (Python, Rust,
TypeScript, etc.) that wishes to realize the orchestration pipeline.

### Three layers

| Layer | Contents |
|---|---|
| **Domain types** | Named data records, enums, constants — the vocabulary shared by all layers (§2). |
| **Decision functions** | The 9 pure or async functions that map inputs to action tokens (§3). Ground truth in `DECISION_LOGIC.md`. |
| **Ports + Engine** | Three abstract ports (`ForgePort`, `HarnessPort`, `SessionPort`) and the `Engine` that orchestrates them (§4–§5). |

### Async principle

Async is reserved for genuine I/O. The rule is stated once here and applies throughout:

- **Pure decision functions** (no network, no file I/O) are **synchronous**. Making them async
  would hide their purity and add scheduling overhead with no benefit.
- **Forge-reading functions** (`resolve_blockers`, `pipeline_health`) are **async** because
  their reference implementations call out to the forge.
- **All port methods** are **async**.
- **All engine workflow methods** are **async**.
- **Pure state-derivation helpers** (`derive_issue_state`, `derive_pr_state`) are **sync**.

### Notation convention

Signatures are written in language-neutral pseudocode:

```
function_name(param: Type, ...) -> ReturnType        [sync]
async function_name(param: Type, ...) -> ReturnType  [async]
```

After each signature group, a brief Python (`asyncio`) and Rust (`tokio`) mapping note
(one or two lines each) shows how the neutral form maps to the target runtime. These notes
defer the runtime commitment — they do not mandate a particular runtime.

### Cross-references

- `STATE_MACHINE.md` is the authoritative source for entity definitions, state encodings,
  transition semantics, the escalation taxonomy (§6), constants (§7), and reconciler
  channels (§4).
- `DECISION_LOGIC.md` is the authoritative source for every decision function's truth
  table, input domains, output tokens, and edge cases.

This document cross-references both by `§N` and by function name but does **not** reproduce
their truth tables. When this document and either source disagree, the source wins.

---

## §2 Domain Types

### Refs

Named identifiers for forge objects. The underlying representation is forge-specific (e.g.,
integer ID, string slug, URL) — the API treats them as opaque scalars.

```
IssueRef  — forge-native issue identifier
PRRef     — forge-native pull-request identifier
EntityRef — IssueRef | PRRef (either entity)
RepoRef   — forge-native repository identifier
```

### State enums

Derived from `STATE_MACHINE.md §2`. These are the observable states of the two entities.

```
IssueState = QUEUED | ESCALATED | CLOSED

PRState    = BUILDING | CONVERGING | APPROVED | ESCALATED | MERGED | EMPTY
```

`EMPTY` is a transient detection state (zero-diff PR), not a label. It is detected at the
converge gate and by the reconciler; it is resolved by re-dispatch, not by converging a
zero-line diff (`STATE_MACHINE.md §2`, P15/P16).

### Label vocabulary (constants)

Seven label strings that encode entity state in the forge. The first five are the core
engine labels (unchanged from the reference implementation); the last two are added by
the public-issue intake front-stage (§3.11, `ARCHITECTURE.md §intake`):

```
LABEL_AGENT_WORK          = "agent-work"         # issue: QUEUED; enters core machine at I1
LABEL_NEEDS_HUMAN         = "needs-human"         # issue/PR: ESCALATED
LABEL_IMPLEMENTING        = "agent:implementing"  # PR: BUILDING
LABEL_CONVERGE            = "converge"            # PR: CONVERGING
LABEL_READY               = "agent:ready"         # PR: APPROVED

# Intake labels (added by the triage front-stage; not part of the core machine)
LABEL_TRIAGE              = "triage"              # issue: triage agent running / completed
LABEL_AWAITING_PROMOTION  = "awaiting-promotion"  # issue: non-allowlisted; blocked on human
```

### Verdict

The machine-readable output of one converge review round, written to `.converge-verdict.json`
(`STATE_MACHINE.md §5`).

```
Verdict {
  blockers:           int             # count of blocking findings
  suggestions:        int             # count of suggestion findings
  nits:               list<string>    # one-line descriptions of nit findings
  blocker_signatures: list<string>    # sorted stable slugs, one per blocker
}
```

**Init sentinel** — seeded at the start of each round before the reviewer runs. A reviewer
that crashes or times out before overwriting the file leaves a phantom blocker, which fails
safe:

```
Verdict {
  blockers:           1,
  suggestions:        0,
  nits:               [],
  blocker_signatures: ["verdict-file-not-written"]
}
```

Sentinel normalization: when `blocker_signatures == ["verdict-file-not-written"]`, decision
functions treat `blocker_signatures` as `[]` — "no machine verdict this round" — never as
evidence of being stuck (`DECISION_LOGIC.md §3`).

### EntryParams

The parameters passed to the harness when dispatching an agent run. Derived from
`route_entry` output (`DECISION_LOGIC.md §1`).

```
EntryParams {
  model:     string   # "claude-opus-4-8" | "claude-sonnet-4-6"
  max_turns: int      # 40 (issues) | 30 (comments / default)
  contract:  string   # path to orchestrator-contract.md (constant across all events)
}
```

### CheckRun

One CI check run as returned by the forge.

```
CheckRun {
  name:       string    # e.g. "Type Check", "Lint"
  status:     string    # "success" | "failure" | "neutral" | "skipped" | "pending" | ...
  conclusion: string?   # null when not yet complete
}
```

A check counts as **green** when `status ∈ {success, skipped, neutral}` (`STATE_MACHINE.md §7`).

### RunHandle

An opaque handle returned by `HarnessPort.dispatch`. Used to query run status via
`HarnessPort.get_run_status` and `SessionPort.get_run`. The engine does not inspect
its internal structure.

```
RunHandle  — opaque (forge-run-id, timestamp, or similar)
```

### Decision-token enums

One enum per decision function. Tokens must match `DECISION_LOGIC.md` exactly.

```
RoundAction    = approve
               | fix
               | escalate:no-progress
               | escalate:no-verdict
               | escalate:ci-red
               | escalate:cap-reached

CapAction      = redispatch | escalate

StaleAction    = escalate
               | trigger-ci
               | redispatch
               | needs-human
               | mark-ready
               | mark-ready-and-converge

RearmAction    = trigger-ci
               | skip-in-progress
               | skip-done
               | skip-recent
               | rearm

ConflictAction = escalate | skip

RedispatchAction = skip-has-pr | skip-recent | escalate | redispatch

HealthVerdict  = BLOCKED | AT_RISK | ON_TRACK
```

### Input context structs

These group the inputs that feed a single decision function. They exist for ergonomic
bundling; implementations may pass fields individually if the language makes that cleaner.

```
RoundContext {
  round:      1 | 2 | 3
  blockers:   int | "unknown"
  ci_green:   bool
  prev_sigs:  list<string>   # sorted blocker_signatures from prior round; [] for round 1
  curr_sigs:  list<string>   # sorted blocker_signatures from current round
}

StaleContext {
  redispatch_count: int      # count of prior re-dispatches on this draft PR
  ci_runs:          int      # number of CI runs on HEAD (0 = CI never ran)
  has_converge:     bool     # PR carries the "converge" label
  failing_count:    int      # count of failing blocking CI checks
  has_issue:        bool     # PR body contains a closing issue reference
  has_diff:         bool     # PR has at least one changed file vs. base
}

RearmContext {
  ci_runs:                int           # CI run count on HEAD
  converge_state:         string        # "<status>:<conclusion>", e.g. "in_progress:", "completed:success", "none:none"
  has_terminal_label:     bool          # PR carries "agent:ready" or "needs-human"
  seconds_since_last_run: int | null    # null = workflow never ran
}

HealthReport {
  implementing:  int              # PRs labeled "agent:implementing"
  converge:      int              # PRs labeled "converge"
  ready:         int              # PRs labeled "agent:ready"
  needs_human:   int              # PRs labeled "needs-human"
  stale_drafts:  int              # draft PRs with "agent:implementing"
  in_flight:     int              # implementing + converge
  verdict:       HealthVerdict
  report_md:     string           # markdown pipeline status report
}
```

### EscalationCause enum

Every cause that lands an entity in `needs-human`, matching `STATE_MACHINE.md §6` exactly.

```
EscalationCause = protected-path         # E1
                | no-progress            # E2
                | no-verdict             # E3
                | ci-red                 # E4
                | cap-reached            # E5
                | empty-pr               # E6
                | merge-conflict         # E7
                | stale-build-cap        # E8
                | stale-no-issue         # E9
                | issue-redispatch-cap   # E10
```

### Constants

Single source of truth for every numeric threshold and string constant used by the
state machine. Every constant in `STATE_MACHINE.md §7` and `DECISION_LOGIC.md Constants
Reference` is named here.

```
CONVERGE_ROUNDS              = 3       # maximum review rounds per converge run
MAX_REDISPATCHES             = 2       # converge re-dispatch cap (>= escalates)
RECONCILER_STALE_REDISPATCH_CAP = 3   # stale-PR reconciler escalate threshold (>= escalates)
ISSUE_REDISPATCH_CAP         = 3       # no-PR issue reconciler escalate threshold (>= escalates)
STALE_DRAFT_THRESHOLD_S      = 1200   # seconds; last dispatch run older than this -> stale
REARM_RECENT_GUARD_S         = 300    # seconds; converge "finished recently" (< skips rearm)
ISSUE_COOLDOWN_S             = 900    # seconds; issue "touched recently" (< skips redispatch)
CI_WAIT_S                    = 480    # seconds; max per-round CI poll window (8 min)
NO_VERDICT_RETRY_CAP         = 2      # converge no-verdict retries before escalating
RECONCILER_CRON              = "*/15 * * * *"
PARALLEL_SPECIALIST_CAP      = 4      # max specialist agent runs in parallel
AT_RISK_THRESHOLD            = 5      # in_flight >= this -> AT_RISK verdict

SENTINEL_SIGNATURE           = "verdict-file-not-written"

BLOCKING_CI_CHECKS = [
  { name: "Type Check",          ci_fail_slug: "ci-fail:type-check"        },
  { name: "Lint",                ci_fail_slug: "ci-fail:lint"              },
  { name: "Integration Tests",   ci_fail_slug: "ci-fail:integration-tests" },
  { name: "Docker Build & Scan", ci_fail_slug: "ci-fail:docker-build"      },
  { name: "Helm Lint",           ci_fail_slug: "ci-fail:helm-lint"         },
  { name: "Helm Kubeconform",    ci_fail_slug: "ci-fail:helm-kubeconform"  },
]

PROTECTED_PATHS = [
  ".github/workflows/**",
  "ARCHITECTURE.md",
  "THREAT_MODEL.md",
  "COMPLIANCE.md",
  ".agents/**",       # pack dir + custom agents — E1 on any PR modifying these
  "agents/**",        # orchestration contracts — E1 on any PR modifying these
]
```

### Agent pack types

**`AgentRef`** — The canonical identifier for one specialist within the flattened pack
directory. A plain basename string; no path prefix.

```
AgentRef  — string (basename, e.g. "engineering-security-engineer.md")
```

`AgentRef` values are defined by the specialist pack upstream. An `AgentRef` of
`"engineering-security-engineer.md"` resolves to the file at
`<AgentPackConfig.dest_dir>/engineering-security-engineer.md` inside the image.

**`AgentPackConfig`** — Source and version of the external specialist pack. Stored in
`Config.agent_pack` (§8.2). Details in `AGENT_PACK.md`.

```
AgentPackConfig {
  repo_url:   string          # Git-clonable HTTPS URL of the pack repo
  pinned_ref: string          # Full 40-character commit SHA (preferred) or tag
  dest_dir:   string          # flat directory inside image, default ".agents"
}
```

Default:

```
AgentPackConfig {
  repo_url:   "https://github.com/msitarzewski/agency-agents"
  pinned_ref: "d6553e261e595c651064f899a6c33dd5aa71c9e3"
  dest_dir:   ".agents"
}
```

### Specialist routing constants

The always-on specialist set required by every converge review round:

```
CONVERGE_REVIEW_BASE = [
  "engineering-security-engineer.md",   # security reviewer — required every round
  "engineering-code-reviewer.md",       # code quality reviewer — required every round
]
```

The diff-path → specialist routing table. Evaluated by `decide_specialists` (§3.12):

```
SPECIALIST_ROUTING = [
  # Glob patterns are tested against each path in changed_paths (fnmatch-style).
  # Multiple patterns per entry are OR-combined.
  { patterns: ["auth/**", "session/**", "crypto/**", "**/permission*", "**/rbac*"],
    agent_ref: "engineering-security-engineer.md" },
  { patterns: ["**/migrations/**", "**/*.sql", "**/schema*"],
    agent_ref: "engineering-database-optimizer.md" },
  { patterns: ["**/*.tsx", "**/*.css", "**/components/**", "**/ui/**"],
    agent_ref: "testing-accessibility-auditor.md" },
  { patterns: ["**/api/**", "**/routes/**", "**/handlers/**"],
    agent_ref: "testing-api-tester.md" },
]
```

> `DECISION_LOGIC.md` flagged `MAX_REDISPATCHES` as duplicated across three sites in the
> reference implementation (the decision script, the empty-PR gate, and the inline
> stage-step fallback). It is single-sourced here. All port implementations must read
> `MAX_REDISPATCHES` from this constants block and must not hard-code `2` at the call site.

> Python mapping: constants as module-level literals or a frozen `dataclass`; enums as
> `enum.Enum` subclasses; structs as `dataclasses.dataclass(frozen=True)`.
> Rust mapping: constants as `const` items; enums as `enum` types; structs as `struct` with
> derived `Clone`/`Debug`.

---

## §3 Decision Functions

Functions appear in the same order as `DECISION_LOGIC.md`. For each function: name,
purpose, inputs, output type, sync/async marker, citation, neutral signature, and
Python/Rust mapping notes.

### §3.1 `route_entry` (sync) — `DECISION_LOGIC.md §1`

**Purpose** — Map a forge event name to orchestrator entry parameters (model, turn budget,
contract path).

**Inputs**

| Name | Domain |
|---|---|
| `event` | `string` (may be empty) |

**Output** — `EntryParams`

**Signature**

```
route_entry(event: string) -> EntryParams
```

> Python: `def route_entry(event: str) -> EntryParams`
> Rust: `fn route_entry(event: &str) -> EntryParams`

All inputs return a valid `EntryParams` — there is no error path (`DECISION_LOGIC.md §1`).
Unknown or empty events fall through to the Sonnet/30-turn default.

---

### §3.2 `resolve_blockers` (async) — `DECISION_LOGIC.md §2`

**Purpose** — Resolve the effective blocker count for one convergence round, falling back
from the machine verdict JSON to the reviewer's latest in-round PR-comment footer when the
init sentinel survived.

**Inputs**

| Name | Domain |
|---|---|
| `forge` | `ForgePort` |
| `verdict` | `Verdict \| null` (null = file not present) |
| `pr` | `PRRef` |
| `round_started` | `datetime \| null` (null = unscoped fallback) |

**Output** — `int | "unknown"`

**Signature**

```
async resolve_blockers(forge: ForgePort, verdict: Verdict|null, pr: PRRef, round_started: datetime|null) -> int | "unknown"
```

> Python: `async def resolve_blockers(forge: ForgePort, verdict: Verdict | None, pr: PRRef, round_started: datetime | None) -> int | Literal["unknown"]`
> Rust: `async fn resolve_blockers(forge: &dyn ForgePort, verdict: Option<Verdict>, pr: PRRef, round_started: Option<DateTime<Utc>>) -> Result<BlockerCount>`

The pure sentinel-normalization and comment-parse core is a sync helper; only the
`ForgePort.list_comments` call (comment-footer fallback for sentinel verdicts) makes this
function async. When `verdict` is not sentinel, no forge call is made and the function
resolves immediately.

Decision order follows `DECISION_LOGIC.md §2`: trust JSON when not sentinel; fall back to
PR comment footer scoped to `round_started`; emit `"unknown"` when no footer found.

---

### §3.3 `decide_round` (sync) — `DECISION_LOGIC.md §3`

**Purpose** — Decide the convergence action for one round of the converge loop.

**Inputs**

| Name | Domain |
|---|---|
| `ctx` | `RoundContext` |

**Output** — `RoundAction`

**Signature**

```
decide_round(ctx: RoundContext) -> RoundAction
```

> Python: `def decide_round(ctx: RoundContext) -> RoundAction`
> Rust: `fn decide_round(ctx: &RoundContext) -> RoundAction`

Sentinel normalization is applied before the decision table: if `prev_sigs` or `curr_sigs`
equals `["verdict-file-not-written"]`, it is replaced with `[]`
(`DECISION_LOGIC.md §3`, sentinel normalization). Inputs `round`, `blockers`, `ci_green`
must all be present and valid; invalid inputs are a usage error.

---

### §3.4 `decide_cap_action` (sync) — `DECISION_LOGIC.md §4`

**Purpose** — When the converge loop cannot self-finish a PR (3-round cap reached with
blockers open, or empty no-diff PR), decide whether to re-dispatch the implementing agent
(bounded) or escalate to a human.

**Inputs**

| Name | Domain |
|---|---|
| `redispatch_count` | `int >= 0` |
| `has_issue` | `bool` (closing issue found in PR body) |

**Output** — `CapAction`

**Signature**

```
decide_cap_action(redispatch_count: int, has_issue: bool) -> CapAction
```

> Python: `def decide_cap_action(redispatch_count: int, has_issue: bool) -> CapAction`
> Rust: `fn decide_cap_action(redispatch_count: u32, has_issue: bool) -> CapAction`

`has_issue == false` escalates unconditionally, before the cap check.
`redispatch_count >= MAX_REDISPATCHES` escalates. Otherwise `redispatch`.

---

### §3.5 `decide_stale_action` (sync) — `DECISION_LOGIC.md §5`

**Purpose** — Decide the recovery action for a stale draft PR carrying `agent:implementing`.

**Inputs**

| Name | Domain |
|---|---|
| `ctx` | `StaleContext` |

**Output** — `StaleAction`

**Signature**

```
decide_stale_action(ctx: StaleContext) -> StaleAction
```

> Python: `def decide_stale_action(ctx: StaleContext) -> StaleAction`
> Rust: `fn decide_stale_action(ctx: &StaleContext) -> StaleAction`

Priority order: `redispatch_count >= RECONCILER_STALE_REDISPATCH_CAP` -> `escalate`;
`ci_runs == 0` -> `trigger-ci`; `has_diff == false` -> `redispatch` (with issue) or
`needs-human` (no issue); `has_converge` -> `mark-ready`; `failing_count == 0` ->
`mark-ready-and-converge`; `has_issue` -> `redispatch`; else `needs-human`.

The empty-PR guard (Priority 2.5) is critical: `converge` label presence is not evidence
of finished work on an empty PR (`DECISION_LOGIC.md §5`).

---

### §3.6 `decide_rearm_action` (sync) — `DECISION_LOGIC.md §6`

**Purpose** — For a non-draft converge PR, decide whether to trigger CI, re-arm converge,
or skip.

**Inputs**

| Name | Domain |
|---|---|
| `ctx` | `RearmContext` |

**Output** — `RearmAction`

**Signature**

```
decide_rearm_action(ctx: RearmContext) -> RearmAction
```

> Python: `def decide_rearm_action(ctx: RearmContext) -> RearmAction`
> Rust: `fn decide_rearm_action(ctx: &RearmContext) -> RearmAction`

`queued:` is treated identically to `in_progress:` — GitHub emits `queued` before
`in_progress`, and treating them alike prevents duplicate dispatch
(`DECISION_LOGIC.md §6`). `seconds_since_last_run == null` skips the recency guard.

---

### §3.7 `decide_conflict_action` (sync) — `DECISION_LOGIC.md §7`

**Purpose** — Decide whether to escalate a merge-conflicting PR.

**Inputs**

| Name | Domain |
|---|---|
| `mergeable` | `string` (forge mergeable state; only `"CONFLICTING"` is special) |
| `already_needs_human` | `int >= 0` (count of `needs-human` labels; 0 = unlabeled) |

**Output** — `ConflictAction`

**Signature**

```
decide_conflict_action(mergeable: string, already_needs_human: int) -> ConflictAction
```

> Python: `def decide_conflict_action(mergeable: str, already_needs_human: int) -> ConflictAction`
> Rust: `fn decide_conflict_action(mergeable: &str, already_needs_human: u32) -> ConflictAction`

Only `mergeable == "CONFLICTING"` and `already_needs_human == 0` produces `escalate`.
All other combinations produce `skip`.

---

### §3.8 `decide_redispatch_action` (sync) — `DECISION_LOGIC.md §8`

**Purpose** — Decide whether to re-dispatch an `agent-work` issue that has no open PR.

**Inputs**

| Name | Domain |
|---|---|
| `has_open_pr` | `bool` |
| `seconds_since` | `int >= 0 \| null` (null = never touched) |
| `redispatch_count` | `int >= 0` |

**Output** — `RedispatchAction`

**Signature**

```
decide_redispatch_action(has_open_pr: bool, seconds_since: int|null, redispatch_count: int) -> RedispatchAction
```

> Python: `def decide_redispatch_action(has_open_pr: bool, seconds_since: int | None, redispatch_count: int) -> RedispatchAction`
> Rust: `fn decide_redispatch_action(has_open_pr: bool, seconds_since: Option<u32>, redispatch_count: u32) -> RedispatchAction`

`seconds_since == null` skips the recency guard. `redispatch_count >= ISSUE_REDISPATCH_CAP`
escalates. Priority: `has_open_pr` -> `skip-has-pr`; recent -> `skip-recent`; cap ->
`escalate`; else `redispatch`.

---

### §3.9 `pipeline_health` (async) — `DECISION_LOGIC.md §9`

**Purpose** — Emit a pipeline health report with per-label PR counts and a single health
verdict.

**Inputs**

| Name | Domain |
|---|---|
| `forge` | `ForgePort` |
| `repo` | `RepoRef` |

**Output** — `HealthReport`

**Signature**

```
async pipeline_health(forge: ForgePort, repo: RepoRef) -> HealthReport
```

> Python: `async def pipeline_health(forge: ForgePort, repo: RepoRef) -> HealthReport`
> Rust: `async fn pipeline_health(forge: &dyn ForgePort, repo: RepoRef) -> Result<HealthReport>`

Calls `forge.list_prs` to enumerate open PRs; counts labels; derives verdict.
Verdict priority: `needs_human > 0` -> `BLOCKED`; `in_flight >= AT_RISK_THRESHOLD` ->
`AT_RISK`; else `ON_TRACK` (`DECISION_LOGIC.md §9`).

---

### §3.10 State derivation helpers (sync)

These functions translate raw forge data (labels, draft flag, merged flag, changed-file
count) into the typed state enums defined in §2. They are not in `DECISION_LOGIC.md`
because they perform no decision — they are pure projections onto the label vocabulary.

```
derive_issue_state(labels: set<string>, closed: bool) -> IssueState

derive_pr_state(labels: set<string>, draft: bool, merged: bool, changed_files: int) -> PRState
```

> Python: `def derive_issue_state(labels: frozenset[str], closed: bool) -> IssueState`
> Rust: `fn derive_issue_state(labels: &HashSet<&str>, closed: bool) -> IssueState`

**`derive_issue_state` logic:**

- `closed == true` -> `CLOSED`
- `labels` contains `LABEL_NEEDS_HUMAN` -> `ESCALATED`
- `labels` contains `LABEL_AGENT_WORK` -> `QUEUED`
- else -> `QUEUED` (default; label may be in flight)

**`derive_pr_state` logic:**

- `merged == true` -> `MERGED`
- `labels` contains `LABEL_NEEDS_HUMAN` -> `ESCALATED`
- `labels` contains `LABEL_READY` -> `APPROVED`
- `changed_files == 0` -> `EMPTY` (transient; checked at converge gate)
- `labels` contains `LABEL_CONVERGE` and `draft == false` -> `CONVERGING`
- `labels` contains `LABEL_IMPLEMENTING` -> `BUILDING`
- else -> `BUILDING` (default)

---

### §3.11 `decide_intake` (sync) — `ARCHITECTURE.md §intake`

**Not in `DECISION_LOGIC.md`** — that document is a frozen clean-room extraction from the
`mirror` reference implementation. `decide_intake` is a new next-gen function with no mirror
antecedent. Its truth table is reproduced in `TESTING.md §2.1`.

The **default-deny** rule: when the allowlist is non-empty, unlisted authors are queued for
human promotion; an empty allowlist disables the gate entirely (all authors are admitted).

```
decide_intake(author: string, allowlist: list<string>) -> IntakeDecision
  where IntakeDecision = "admit" | "queue"
```

> Python: `def decide_intake(author: str, allowlist: list[str]) -> Literal["admit", "queue"]`
> Rust: `fn decide_intake(author: &str, allowlist: &[&str]) -> IntakeDecision`

**Truth table:**

| `allowlist` | `author in allowlist` | result |
|---|---|---|
| empty (`[]`) | n/a | `admit` (gate disabled — all authors admitted) |
| non-empty | `true` | `admit` |
| non-empty | `false` | `queue` |

**Called by:** `Engine.intake` (see `ARCHITECTURE.md §intake`). Never called by the core
engine (`Engine.dispatch`, `Engine.converge`, `Engine.reconcile`) — intake is a
pre-machine front-stage.

**Side effects (performed by `Engine.intake`, not by this function):**

- `admit` → `forge.add_label(issue_ref, LABEL_TRIAGE)` then `forge.add_label(issue_ref, LABEL_AGENT_WORK)` (→ I1/QUEUED; triggers `issues:labeled` → dispatch)
- `queue` → `forge.add_label(issue_ref, LABEL_TRIAGE)` then `forge.add_label(issue_ref, LABEL_AWAITING_PROMOTION)` (→ held; human promotion via PWA triage queue adds `LABEL_AGENT_WORK`)

---

### §3.12 `decide_specialists` (sync) — `AGENT_PACK.md §4`

**Not in `DECISION_LOGIC.md`** — that document is a frozen clean-room extraction from the
`mirror` reference implementation. `decide_specialists` is a new next-gen function with no
mirror antecedent. Its truth table is reproduced in `TESTING.md §2.12`.

**Purpose** — Given the set of file paths changed by a PR and the current round number,
return the ordered list of specialist `AgentRef`s to spawn in a converge review round.
The result is always bounded by `PARALLEL_SPECIALIST_CAP`.

```
decide_specialists(changed_paths: list<string>, round: int) -> list<AgentRef>
```

> Python: `def decide_specialists(changed_paths: list[str], round: int) -> list[AgentRef]`
> Rust: `fn decide_specialists(changed_paths: &[&str], round: u32) -> Vec<AgentRef>`

**Algorithm (pure, synchronous):**

1. Start with `result = list(CONVERGE_REVIEW_BASE)` (the always-on base set, §2).
2. For each entry in `SPECIALIST_ROUTING` (§2):
   - If any path in `changed_paths` matches any of the entry's glob patterns
     (fnmatch-style, case-sensitive):
     - Add `entry.agent_ref` to `result` if not already present.
3. Cap: if `len(result) > PARALLEL_SPECIALIST_CAP`, retain the base set entries and
   drop routing-added entries (in `SPECIALIST_ROUTING` definition order) until
   `len(result) == PARALLEL_SPECIALIST_CAP`.
4. Return `result`.

The `round` parameter is accepted but currently unused. It is reserved for future extensions
that may suppress certain specialist tiers in later rounds.

**Invariants:**

- `CONVERGE_REVIEW_BASE` entries are always present in the result (never dropped by cap).
- `len(result) >= len(CONVERGE_REVIEW_BASE)` (2) and `len(result) <= PARALLEL_SPECIALIST_CAP` (4).
- Deduplication: an `AgentRef` appears at most once in `result`.

**Called by:** `agents/converge-reviewer.md §1` (the converge review aggregator). Not called
by the core engine directly — the orchestration agent handles selection.

**Side effects (none):** this function is pure. The actual spawning is performed by the
calling orchestration agent via `HarnessPort`-style `subagent_type: "general-purpose"` calls
(see `AGENT_PACK.md §4.4`).

---

## §4 Ports

Ports are abstract interfaces — named sets of async methods that the engine calls. Each
port has exactly one today-implementation and is designed to swap without touching the
engine or decision functions.

All port methods are **async**.

> Python mapping: ports as `Protocol` or abstract base classes (`abc.ABC`); implementations
> inject the concrete GitHub/harness client.
> Rust mapping: ports as `trait` objects with `async fn` (via `async-trait` or native async
> traits in Rust 1.75+); implementations are structs that `impl` the trait.

---

### §4.1 `ForgePort`

The forge abstraction. Today: GitHub. Future: GitLab, Gitea, or any forge that supports
labels, draft PRs, CI check runs, and PR reviews.

`ForgePort` is the only component that knows about forge-native concepts (labels, draft
state, `Closes #N` auto-close). All state derivation is performed by `derive_issue_state`
/ `derive_pr_state` after reading labels from the forge; there is no separate state store
(`STATE_MACHINE.md §9`, A1).

```
async get_issue(ref: IssueRef) -> Issue
```
Fetch a single issue by ref. Returns the issue including its current label set and closed
state.

```
async list_issues(label: string) -> list<Issue>
```
List all open issues carrying the given label.

```
async add_label(ref: EntityRef, label: string) -> void
```
Add a label to an issue or PR. Idempotent — adding an already-present label is a no-op.

```
async remove_label(ref: EntityRef, label: string) -> void
```
Remove a label from an issue or PR. Idempotent — removing an absent label is a no-op.

```
async create_pr(repo: RepoRef, title: string, body: string, draft: bool, closes: IssueRef|null) -> PRRef
```
Open a new pull request. When `closes` is non-null, the implementation must include
`Closes #N` in `body` to activate the forge's auto-close behavior on merge (A3).
Returns the new `PRRef`.

```
async get_pr(ref: PRRef) -> PR
```
Fetch a single PR by ref. Returns labels, draft state, merged state, and body.

```
async list_prs(label: string|null, state: string) -> list<PR>
```
List PRs filtered by label and state (`"open"`, `"closed"`, `"all"`). `label == null`
returns all PRs matching `state`.

```
async set_pr_ready(ref: PRRef) -> void
```
Convert a draft PR to ready-for-review. Corresponds to `gh pr ready`. Used by the
reconciler `mark-ready` / `mark-ready-and-converge` paths (P3).

```
async get_changed_files(ref: PRRef) -> list<string>
```
Return the list of file paths changed by the PR relative to its base branch. Used by
the idempotency gate (EMPTY detection, P15/P16) and the protected-path check (E1).

```
async get_check_runs(ref: PRRef) -> list<CheckRun>
```
Return the current CI check runs for the PR's HEAD commit. Used by the converge CI
poll loop and by `decide_stale_action` input collection.

```
async get_mergeable(ref: PRRef) -> string
```
Return the forge's mergeable state for a PR. Expected values include `"MERGEABLE"`,
`"CONFLICTING"`, and `"UNKNOWN"`. Passed directly to `decide_conflict_action`.

```
async list_comments(ref: PRRef, since: datetime|null) -> list<Comment>
```
Return PR comments, optionally filtered to those created at or after `since`. Used by
`resolve_blockers` for the comment-footer fallback when the verdict sentinel survived.

```
async post_comment(ref: PRRef, body: string) -> Comment
```
Post a new comment on a PR. Used by the engine to post nit follow-up and escalation
notices.

```
async create_review(ref: PRRef, verdict: "APPROVE"|"REQUEST_CHANGES", body: string) -> void
```
Submit a PR review. Used by the engine's converge finalize step to post an approving
review (P8) or a blocking review when escalating.

```
async last_workflow_run_at(ref: PRRef, workflow_name: string) -> datetime|null
```
Return the timestamp of the most-recent completed run of the named workflow for the
PR's HEAD commit. Returns null if the workflow has never run. Used by RC-3 recency
guard (`REARM_RECENT_GUARD_S`).

```
async last_dispatch_run_at(ref: PRRef) -> datetime|null
```
Return the timestamp of the most-recent completed dispatch run for a PR. Returns null
if no dispatch run has completed. Used by RC-1 stale-draft detection
(`STALE_DRAFT_THRESHOLD_S`).

---

### §4.2 `HarnessPort`

The agent runtime abstraction. Today: `anthropics/claude-code-action`. Future: Codex,
OpenCode, or any harness that can execute an agent contract and return a run handle.

The single-shot contract is the defining constraint: `dispatch` returns a handle
immediately; the engine does not block awaiting the agent. If a run must be resumed, the
reconciler detects the stale draft and re-dispatches. This is why agents commit early and
often — their changes survive the death of the harness process
(`STATE_MACHINE.md §1`).

```
async dispatch(params: EntryParams, context: DispatchContext) -> RunHandle
```
Launch a single-shot agent run. Returns immediately with a `RunHandle`. The caller
must NOT await completion in-process. Covers the agent step of transitions I2 and P1.

`DispatchContext` is a plain record:

```
DispatchContext {
  repo:         RepoRef
  issue:        IssueRef | null
  pr:           PRRef | null
  comment_body: string | null    # triggering comment text, if any
}
```

```
async trigger_workflow(name: string, ref: PRRef|null, inputs: map<string,string>) -> void
```
Fire a named forge workflow. Used for no-verdict re-arm (P12) and CI re-trigger
recovery. `ref` scopes the workflow to a specific PR when non-null.

```
async trigger_ci(ref: PRRef) -> void
```
Re-trigger CI on the PR's HEAD commit. Shorthand used by RC-1 (`trigger-ci` token,
P4) and the `escalate:ci-red` recovery path (P9). Equivalent to a push-event
re-trigger or an explicit check-suite re-run depending on the harness.

**Specialist spawn model** — Specialist agents are not a registered `subagent_type`. They
are always spawned as `subagent_type: "general-purpose"` with a prompt of the form
*"Act as the agent defined in `<dest_dir>/<AgentRef>`. Read that file first."*, followed by
the task-specific instructions. Specialists are **depth-1 only** — they do not spawn further
sub-agents. The `dispatch` call for a specialist blocks until that specialist finishes.
The spawning orchestration agent dispatches the full set of specialists for a round in
parallel (up to `PARALLEL_SPECIALIST_CAP`), then awaits all of them before aggregating
their output. See `AGENT_PACK.md §4.4` for the full invocation pattern.

```
async get_run_status(handle: RunHandle) -> RunStatus
```
Poll the status of a dispatched run. Non-blocking — returns the current state without
waiting.

`RunStatus` is a plain record:

```
RunStatus {
  state:      "queued" | "in_progress" | "completed" | "failed"
  conclusion: string | null    # null when not yet complete
}
```

---

### §4.3 `SessionPort`

The observability seam. Today: not implemented in the reference implementation — part of
the roadmap (`README.md`). Future: an operator-facing layer for inspecting and intervening
in individual harness runs.

`SessionPort` does not affect state-machine transitions. Those are driven entirely by
`ForgePort` label state. `SessionPort` allows operators to inspect and intervene in
individual harness runs without altering the state machine's durable state.

```
async list_runs(repo: RepoRef, since: datetime|null) -> list<RunSummary>
```
List agent runs for a repository, optionally filtered to those started after `since`.

```
async get_run(handle: RunHandle) -> RunDetail
```
Fetch full detail for a specific run, including parameters, timing, and outcome.

```
async stream_events(handle: RunHandle) -> AsyncIterator<RunEvent>
```
Stream agent turn events from an in-flight run in real time. Implementations that do
not support streaming may return a completed iterator over buffered events.

```
async cancel(handle: RunHandle) -> void
```
Cancel an in-flight agent run. Does not alter forge label state — the reconciler
detects the resulting stale draft on the next cron tick and recovers it.

```
async intervene(handle: RunHandle, message: string) -> void
```
Inject a human message into an in-flight run, if the harness supports it. No-op or
error if the harness does not support mid-run injection. Does not alter forge label
state.

---

## §5 Engine

`Engine` is constructed with the three ports and exposes three async entrypoints. It
orchestrates calls to the ports and decision functions to realize the full state machine.

**Construction:**

```
Engine(forge: ForgePort, harness: HarnessPort, session: SessionPort)
```

The engine holds **no durable in-process state**. All durable state lives in forge labels
on issues and PRs (`STATE_MACHINE.md §1`, A1). An engine instance that crashes mid-run
leaves entities in their last-written label state; the reconciler recovers them on the next
cron tick.

> Python mapping: `Engine` is a class; `__init__` takes the three ports.
> Rust mapping: `Engine` is a struct with an `impl` block; the constructor takes boxed
> port trait objects.

---

### §5.1 `Engine.dispatch` (async)

**Signature:**

```
async Engine.dispatch(event: ForgeEvent) -> PRRef | null
```

`ForgeEvent` is defined in §8.1.

Handles the initial dispatch of a work item. Returns the `PRRef` of the newly opened draft
PR, or `null` if the event did not trigger dispatch.

**Steps:**

1. Call `route_entry(event.name)` -> `EntryParams`.
2. Build `DispatchContext { repo, issue: event.issue_ref, pr: null, comment_body: null }`.
3. Call `harness.dispatch(params, ctx)` -> `RunHandle`. Do not await completion.
4. Call `forge.create_pr(repo, title, body, draft=true, closes=event.issue_ref)`. The body
   must contain `Closes #N` when `issue_ref` is non-null (A3). Returns `PRRef`.
5. Call `forge.add_label(pr, LABEL_IMPLEMENTING)`.
6. Return the `PRRef`.

Covers transitions I2, P1.

---

### §5.2 `Engine.converge` (async)

**Signature:**

```
async Engine.converge(pr: PRRef) -> PRState
```

Drives a CONVERGING PR through the 3-round Review->Fix sub-machine to APPROVED or
ESCALATED. Returns the final `PRState`.

**Steps:**

**Idempotency gate** — before any work:
- Read current PR labels via `forge.get_pr`.
- If PR is closed or merged, return `MERGED`.
- If labels contain `LABEL_NEEDS_HUMAN`, return `ESCALATED`.
- If labels contain `LABEL_READY`, return `APPROVED`.
- Call `forge.get_changed_files(pr)`. If count is 0 (EMPTY state, P15/P16):
  - If PR has a closing issue and `redispatch_count < MAX_REDISPATCHES`:
    re-dispatch the closing issue via `harness.dispatch`; return `BUILDING` (P15).
  - Else: `forge.add_label(pr, LABEL_NEEDS_HUMAN)`, return `ESCALATED` (P16, E6).

**Protected-path check** (E1) — before round 1:
- Call `forge.get_changed_files(pr)`.
- If any file matches any pattern in `PROTECTED_PATHS`:
  - `forge.add_label(pr, LABEL_NEEDS_HUMAN)`, return `ESCALATED` (P6, E1).

**3-round loop** — for `round` in `1..CONVERGE_ROUNDS`:

1. **Seed** — write the init sentinel `Verdict` to `.converge-verdict.json`. Record
   `round_started = now()`.

2. **Review** — call `harness.dispatch` for up to `PARALLEL_SPECIALIST_CAP` specialist
   reviewer agents concurrently. The last reviewer to finish writes `.converge-verdict.json`.
   Read the resulting `Verdict`.

3. **Save verdict copy** — copy to `.converge-verdict-rN.json` where N = round number.

4. **CI poll** — call `forge.get_check_runs(pr)` repeatedly, up to `CI_WAIT_S` seconds
   with backoff, until all `BLOCKING_CI_CHECKS` leave `"pending"`. Compute
   `ci_green = all(check.status in {success, skipped, neutral} for check in blocking_checks)`.

5. **Resolve blockers** — call `await resolve_blockers(forge, verdict, pr, round_started)` ->
   `blockers: int | "unknown"`.

6. **Decide** — call `decide_round(RoundContext { round, blockers, ci_green, prev_sigs, curr_sigs })`
   -> `RoundAction`.

7. **Act on token:**

   - `approve` -> finalize:
     - `forge.add_label(pr, LABEL_READY)`
     - `forge.remove_label(pr, LABEL_CONVERGE)`
     - `forge.create_review(pr, "APPROVE", body)`
     - Collect nits from all rounds, deduplicate, open one follow-up issue.
     - Return `APPROVED` (P8).

   - `fix` (R1 or R2) ->
     - Dispatch fixer agent(s) via `harness.dispatch` for each blocker.
     - R1: address blockers + suggestions. R2: blockers only. R3: no fix step
       (`STATE_MACHINE.md §5`).
     - Advance to next round.

   - `escalate:no-progress` ->
     - `forge.add_label(pr, LABEL_NEEDS_HUMAN)`
     - Return `ESCALATED` (P10, E2).

   - `escalate:no-verdict` ->
     - If `retry_count < NO_VERDICT_RETRY_CAP`: increment `retry_count`, re-arm from round
       1 via `harness.trigger_workflow`. Return `CONVERGING` (P12).
     - Else: `forge.add_label(pr, LABEL_NEEDS_HUMAN)`. Return `ESCALATED` (P10, E3).

   - `escalate:ci-red` ->
     - Call `harness.trigger_ci(pr)`.
     - Poll `forge.get_check_runs` for up to `CI_WAIT_S` seconds checking the first 3
       blocking checks (Type Check, Lint, Integration Tests).
     - If green: finalize as `approve` above. Return `APPROVED` (P9).
     - Else: `forge.add_label(pr, LABEL_NEEDS_HUMAN)`. Return `ESCALATED` (P10, E4).

   - `escalate:cap-reached` ->
     - Call `decide_cap_action(redispatch_count, has_issue)` -> `CapAction`.
     - `redispatch`: dispatch the closing issue via `forge.post_comment` / `@claude`.
       Return `CONVERGING` (P11).
     - `escalate`: `forge.add_label(pr, LABEL_NEEDS_HUMAN)`. Return `ESCALATED`
       (P10, E5).

Covers P2, P6-P12, P15, P16, E1-E6.

---

### §5.3 `Engine.reconcile` (async)

**Signature:**

```
async Engine.reconcile(repo: RepoRef) -> ReconcileReport
```

The orthogonal supervisor. Runs the four recovery channels concurrently and returns a
summary of actions taken.

```
ReconcileReport {
  stale_acted:       int    # RC-1 actions taken
  conflicts_flagged: int    # RC-2 escalations
  rearmed:           int    # RC-3 re-arms and CI triggers
  redispatched:      int    # RC-4 issue re-dispatches
  escalated:         int    # total escalations across all channels
}
```

The four channels are **independent and may run concurrently** (e.g. `asyncio.gather` /
`tokio::join!`). Each channel iterates its entity set serially within the channel to avoid
conflicting label writes on the same entity.

**RC-1 Stale-draft recovery:**

- Query: draft PRs labeled `LABEL_IMPLEMENTING` where `forge.last_dispatch_run_at(pr)` is
  more than `STALE_DRAFT_THRESHOLD_S` seconds ago (or null).
- For each stale draft:
  - Collect `StaleContext` inputs from forge.
  - `decide_stale_action(ctx)` -> `StaleAction`.
  - `mark-ready` -> `forge.set_pr_ready(pr)` (P3).
  - `mark-ready-and-converge` -> `forge.set_pr_ready(pr)` + `forge.add_label(pr, LABEL_CONVERGE)` (P3).
  - `trigger-ci` -> `harness.trigger_ci(pr)` (P4).
  - `redispatch` -> `harness.dispatch(...)` fixer run on the PR (P4).
  - `escalate` -> `forge.add_label(pr, LABEL_NEEDS_HUMAN)`, `forge.remove_label(pr, LABEL_IMPLEMENTING)` (P5, E8).
  - `needs-human` -> `forge.add_label(pr, LABEL_NEEDS_HUMAN)`, `forge.remove_label(pr, LABEL_IMPLEMENTING)` (P5, E9).

**RC-2 Merge-conflict flagging:**

- Query: all open PRs.
- For each PR:
  - `decide_conflict_action(forge.get_mergeable(pr), already_needs_human)` -> `ConflictAction`.
  - `escalate` -> `forge.add_label(pr, LABEL_NEEDS_HUMAN)` (P13, E7).
  - `skip` -> no-op.

**RC-3 Converge re-arm:**

- Query: non-draft PRs labeled `LABEL_CONVERGE`.
- For each:
  - Collect `RearmContext` inputs: `ci_runs`, `converge_state`, `has_terminal_label`,
    `seconds_since_last_run` from `forge.last_workflow_run_at(pr, "pr-converge")`.
  - `decide_rearm_action(ctx)` -> `RearmAction`.
  - `trigger-ci` -> `harness.trigger_ci(pr)` (P14).
  - `rearm` -> `harness.trigger_workflow("pr-converge", pr, {})` (P14).
  - `skip-in-progress` / `skip-done` / `skip-recent` -> no-op.

**RC-4 Orphan-issue re-dispatch:**

- Query: open issues labeled `LABEL_AGENT_WORK`.
- For each:
  - Determine `has_open_pr` by checking `forge.list_prs` for a PR referencing this issue.
  - `decide_redispatch_action(has_open_pr, seconds_since, redispatch_count)` -> `RedispatchAction`.
  - `redispatch` -> `harness.dispatch(...)` on the issue (I3).
  - `escalate` -> `forge.remove_label(issue, LABEL_AGENT_WORK)` + `forge.add_label(issue, LABEL_NEEDS_HUMAN)` (I4, E10).
  - `skip-has-pr` / `skip-recent` -> no-op.

Covers P3, P4, P5, P13, P14, I3, I4, E7-E10.

---

## §6 Async Execution Model

### Crash-only durability

The engine holds no in-process state. All durable state is forge labels on issues and PRs
(`STATE_MACHINE.md §1`, A1). A process that crashes mid-converge leaves the entity in its
last-written label state. The reconciler detects the stale draft on the next cron tick
(every `RECONCILER_CRON = "*/15 * * * *"`) and recovers it via RC-1.

Agents therefore must commit early and often — their file changes survive the death of the
harness process because they are in the git history, not in process memory.

### Single-shot harness contract

`HarnessPort.dispatch` returns immediately with a `RunHandle`. The engine does not block
awaiting the agent. If an agent run crashes or the process is killed, the result is a stale
draft PR. The reconciler re-dispatches on the next tick.

This is an explicit design choice: the harness is single-shot with no resume. Durability
comes from the combination of (a) early commits and (b) the reconciler as supervisor, not
from in-process waiting or resumable agent sessions.

### Concurrency in the reconciler

The four RC channels run concurrently — they operate on disjoint entity sets (draft PRs,
open PRs, converge PRs, agent-work issues) and do not write to the same forge objects at
the same time. Within each channel, entities are processed serially to avoid conflicting
label writes on the same PR or issue.

Implementations should use `asyncio.gather` (Python) or `tokio::join!` (Rust) to run the
four channels in parallel, then await all four before returning `ReconcileReport`.

### Async CI polling

`Engine.converge` polls `forge.get_check_runs` for up to `CI_WAIT_S = 480` seconds with a
backoff interval rather than a tight loop. Implementations should yield control between
polls (e.g. `asyncio.sleep(backoff)` / `tokio::time::sleep(backoff)`). The backoff
interval is an implementation detail; an exponential backoff capped at 30 seconds is
reasonable.

### Parallel specialist cap

During the review and fix phases of `Engine.converge`, up to `PARALLEL_SPECIALIST_CAP = 4`
specialist agents may be dispatched concurrently via `harness.dispatch`. The engine spawns
these as concurrent tasks and awaits all of them before reading the final verdict file.

### Cancellation and intervention

`SessionPort.cancel` and `SessionPort.intervene` operate on in-flight harness runs only.
They do not alter forge label state. After cancellation, the entity remains in its
last-written label state, and the reconciler recovers it on the next cron tick. The state
machine remains correct.

---

## §7 Traceability

Every state-machine transition, escalation cause, and decision function is covered by at
least one engine method or port call.

| API element | State machine | Decision logic | Notes |
|---|---|---|---|
| `Engine.dispatch` (route_entry) | I2 | `DECISION_LOGIC.md §1` | Maps event to EntryParams |
| `Engine.dispatch` (harness.dispatch) | P1 | — | Opens draft PR, single-shot |
| `Engine.dispatch` (forge.create_pr + add_label) | P1 | — | Draft + `agent:implementing` stamp |
| `Engine.converge` (idempotency gate) | P7 gate | — | Guards all CONVERGING entries |
| `Engine.converge` (EMPTY -> re-dispatch) | P15 | `decide_cap_action` | Re-dispatch under cap |
| `Engine.converge` (EMPTY -> needs-human) | P16 | — | E6: empty-PR unrecoverable |
| `Engine.converge` (protected-path check) | P6 | — | E1: protected-path short-circuit |
| `Engine.converge` (resolve_blockers) | P7 inner | `DECISION_LOGIC.md §2` | Blocker count for round |
| `Engine.converge` (decide_round -> approve) | P8 | `DECISION_LOGIC.md §3` row 1 | Finalize: add agent:ready |
| `Engine.converge` (decide_round -> approve, ci-red recovery) | P9 | `DECISION_LOGIC.md §3` row 6 | CI re-trigger then approve |
| `Engine.converge` (decide_round -> fix R1/R2) | P7 loop | `DECISION_LOGIC.md §3` rows 2,4 | Fixer dispatch, next round |
| `Engine.converge` (decide_round -> escalate:no-progress) | P10 | `DECISION_LOGIC.md §3` row 3 | E2: no-progress |
| `Engine.converge` (decide_round -> escalate:no-verdict, retry) | P12 | `DECISION_LOGIC.md §3` row 5 | No-verdict retry < cap |
| `Engine.converge` (decide_round -> escalate:no-verdict, final) | P10 | `DECISION_LOGIC.md §3` row 5 | E3: no-verdict after retries |
| `Engine.converge` (decide_round -> escalate:ci-red, recovered) | P9 | `DECISION_LOGIC.md §3` row 6 | E4 avoided: CI recovers |
| `Engine.converge` (decide_round -> escalate:ci-red, unrecovered) | P10 | `DECISION_LOGIC.md §3` row 6 | E4: ci-red after re-trigger |
| `Engine.converge` (decide_cap_action -> redispatch) | P11 | `DECISION_LOGIC.md §4` | Re-dispatch issue, PR stays converge |
| `Engine.converge` (decide_cap_action -> escalate) | P10 | `DECISION_LOGIC.md §4` | E5: cap-reached |
| `Engine.converge` (nit follow-up issue) | P8 finalize | — | Collect nits, open one follow-up |
| `Engine.reconcile` RC-1 (decide_stale_action -> mark-ready) | P3 | `DECISION_LOGIC.md §5` | Reconciler mark-ready |
| `Engine.reconcile` RC-1 (decide_stale_action -> mark-ready-and-converge) | P3 | `DECISION_LOGIC.md §5` | Reconciler mark-ready + converge |
| `Engine.reconcile` RC-1 (decide_stale_action -> trigger-ci) | P4 | `DECISION_LOGIC.md §5` | Reconciler CI trigger |
| `Engine.reconcile` RC-1 (decide_stale_action -> redispatch) | P4 | `DECISION_LOGIC.md §5` | Reconciler re-dispatch |
| `Engine.reconcile` RC-1 (decide_stale_action -> escalate) | P5 | `DECISION_LOGIC.md §5` | E8: stale build cap |
| `Engine.reconcile` RC-1 (decide_stale_action -> needs-human) | P5 | `DECISION_LOGIC.md §5` | E9: stale no-issue |
| `Engine.reconcile` RC-2 (decide_conflict_action -> escalate) | P13 | `DECISION_LOGIC.md §7` | E7: merge-conflict |
| `Engine.reconcile` RC-2 (decide_conflict_action -> skip) | — | `DECISION_LOGIC.md §7` | No-op |
| `Engine.reconcile` RC-3 (decide_rearm_action -> trigger-ci) | P14 | `DECISION_LOGIC.md §6` | Converge re-arm: CI trigger |
| `Engine.reconcile` RC-3 (decide_rearm_action -> rearm) | P14 | `DECISION_LOGIC.md §6` | Converge re-arm: workflow dispatch |
| `Engine.reconcile` RC-3 (decide_rearm_action -> skip-*) | — | `DECISION_LOGIC.md §6` | No-op |
| `Engine.reconcile` RC-4 (decide_redispatch_action -> redispatch) | I3 | `DECISION_LOGIC.md §8` | Orphan issue re-dispatch |
| `Engine.reconcile` RC-4 (decide_redispatch_action -> escalate) | I4 | `DECISION_LOGIC.md §8` | E10: issue redispatch cap |
| `Engine.reconcile` RC-4 (decide_redispatch_action -> skip-*) | — | `DECISION_LOGIC.md §8` | No-op |
| `pipeline_health` | — | `DECISION_LOGIC.md §9` | Operator health report; not a transition |
| Human adds `agent-work` label | I1 | — | Forge-native trigger; not an engine call |
| Human merges APPROVED PR | P17, I6 | — | Forge-native auto-close; not an engine call |
| `derive_issue_state` | I1, I3, I4 reads | — | Label->state projection |
| `derive_pr_state` | P1-P17 reads | — | Label+draft->state projection |
| `ForgePort.add_label(LABEL_NEEDS_HUMAN)` | P5, P6, P10, P13, P16 | — | Realized by engine on escalation |
| `ForgePort.add_label(LABEL_READY)` | P8, P9 | — | Realized by engine on approve |
| `ForgePort.remove_label(LABEL_CONVERGE)` | P8, P9 | — | Converge finalize |
| `ForgePort.set_pr_ready` | P3 | — | Reconciler mark-ready |
| `HarnessPort.trigger_ci` | P4, P9, P14 | — | CI re-trigger across contexts |
| `HarnessPort.trigger_workflow` | P12, P14 | — | No-verdict retry; converge re-arm |
| E1 (protected-path) | P6 | — | `Engine.converge` protected-path check |
| E2 (no-progress) | P10 | `DECISION_LOGIC.md §3` row 3 | `Engine.converge` |
| E3 (no-verdict) | P10 | `DECISION_LOGIC.md §3` row 5 | `Engine.converge` after retries |
| E4 (ci-red) | P10 | `DECISION_LOGIC.md §3` row 6 | `Engine.converge` after re-trigger |
| E5 (cap-reached) | P10 | `DECISION_LOGIC.md §4` | `Engine.converge` via decide_cap_action |
| E6 (empty-pr) | P16 | — | `Engine.converge` idempotency gate |
| E7 (merge-conflict) | P13 | `DECISION_LOGIC.md §7` | `Engine.reconcile` RC-2 |
| E8 (stale-build-cap) | P5 | `DECISION_LOGIC.md §5` | `Engine.reconcile` RC-1 |
| E9 (stale-no-issue) | P5 | `DECISION_LOGIC.md §5` | `Engine.reconcile` RC-1 |
| E10 (issue-redispatch-cap) | I4 | `DECISION_LOGIC.md §8` | `Engine.reconcile` RC-4 |
| `OrchestratorService.handle_event` | I2, P1, P7, I5 | — | Routes `ForgeEvent` to `Engine.dispatch` or `Engine.converge` per §8.3 |
| `OrchestratorService.reconcile_now` | RC-1..RC-4, I3, I4, P3–P5, P13, P14 | — | Calls `Engine.reconcile` for each enabled repo |
| `OrchestratorService.status` | — | `DECISION_LOGIC.md §9` | Calls `pipeline_health` per enabled repo |
| `OrchestratorService.handle_event` (dedup) | — | — | `delivery_id` LRU dedup; correctness does not depend on it (idempotency gate + reconciler) |
| `PortProvider.ports` | — | — | Resolves `(ForgePort, HarnessPort, SessionPort)` per repo; credentials stay behind provider |
| `Engine.intake` (triage agent) | — | — | Runs read-only triager harness agent; posts structured summary + risk flags; see `ARCHITECTURE.md §intake` |
| `Engine.intake` (decide_intake → admit) | I1 | `§3.11` | `author in allowlist` (or empty list): add `LABEL_TRIAGE` + `LABEL_AGENT_WORK` → enters core machine at I1 |
| `Engine.intake` (decide_intake → queue) | — | `§3.11` | Author not in allowlist: add `LABEL_TRIAGE` + `LABEL_AWAITING_PROMOTION`; awaits human promotion |
| Human promotes `awaiting-promotion` → `agent-work` | I1 | — | PWA triage queue one-tap; equivalent to human adding `agent-work` directly (→ `issues:labeled` → I2) |
| `LABEL_TRIAGE` | — | — | Applied by `Engine.intake` on every public issue; marks triage complete |
| `LABEL_AWAITING_PROMOTION` | — | — | Applied by `Engine.intake` on queue outcome; removed on human promotion |
| `decide_specialists` | — | `§3.12` | Pure sync; selects specialist `AgentRef`s for a converge round; called by `agents/converge-reviewer.md` |
| `CONVERGE_REVIEW_BASE` | — | — | Always-on base set (security + code-quality); always present in `decide_specialists` output |
| `SPECIALIST_ROUTING` | — | — | Diff-path → `AgentRef` routing table; evaluated by `decide_specialists` |
| `AgentPackConfig` | — | — | Pack source/version; stored in `Config.agent_pack`; pack baked at image build time |
| `AgentRef` | — | — | Flat basename of a specialist file within `dest_dir`; used by `agents/converge-reviewer.md` and `agents/converge-fixer.md` |
| `PROTECTED_PATHS` (`.agents/**`) | P6 | — | Pack + orchestration-agent dir; E1 on any PR modifying these |
| `PROTECTED_PATHS` (`agents/**`) | P6 | — | Orchestration-agent contracts; E1 on any PR modifying these |
| Pack acquisition (build-time) | — | — | `DEPLOYMENT.md §2`; baked into image; no runtime network; SHA in SBOM |

---

## §8 Service Contract

`OrchestratorService` is the thin coordination shell that wraps the `Engine` and makes
the orchestrator drivable from a CLI or web GUI across many repos. It adds:

1. A normalized event type (`ForgeEvent`) for forge-webhook ingress.
2. A `PortProvider` that resolves the three ports per repo (credentials stay behind it).
3. A `Config` that registers repos, sets swarm concurrency limits, and optionally carries
   a cron schedule for the built-in reconcile cadence.
4. An `OrchestratorService` control-plane type whose methods a CLI calls directly or a web
   adapter exposes over HTTP.

There is no new durable state store. The service holds an in-memory repo registry and a
bounded delivery-id dedup cache; all durable state remains in forge labels (§6, A1).

---

### §8.1 `ForgeEvent`

The forge-neutral normalization of a raw webhook payload. Produced by a `ForgePort` adapter
from the forge's native format; only `name` / `action` / `label` plus the entity refs drive
routing.

```
ForgeEvent {
  delivery_id:  string          # forge-assigned unique delivery id (used for dedup)
  name:         string          # e.g. "issues" | "issue_comment" |
                                #   "pull_request_review_comment" | "pull_request" | ...
  action:       string | null   # e.g. "labeled" | "ready_for_review" | "synchronize" | ...
  label:        string | null   # populated for "labeled" actions
  repo:         RepoRef
  issue_ref:    IssueRef | null
  pr_ref:       PRRef | null
  comment_body: string | null   # for comment events
}
```

This is the type that `Engine.dispatch(event: ForgeEvent)` (§5.1) accepts.

> Python: `@dataclass(frozen=True) class ForgeEvent`
> Rust: `#[derive(Debug, Clone)] struct ForgeEvent`

---

### §8.2 Configuration types

#### `SwarmLimits`

Backpressure parameters for the harness-dispatch semaphores.

```
SwarmLimits {
  max_concurrent_runs_global:   int   # semaphore across all repos combined
  max_concurrent_runs_per_repo: int   # per-repo cap
  max_concurrent_reconciles:    int   # concurrent reconcile(repo) calls
}
```

Sane defaults: `global=10, per_repo=4, reconciles=4`.

#### `RepoConfig`

One entry per managed repository.

```
RepoConfig {
  repo:             RepoRef
  enabled:          bool          # false = registered but not dispatching (pause without unregistering)
  intake_enabled:   bool = true   # false = skip intake; issues with LABEL_AGENT_WORK still dispatch normally
  allowlist:        list<string>  # GitHub usernames that auto-admit; empty list = gate disabled (all admit)
}
```

`intake_enabled = false` disables the triage front-stage entirely for a repo (useful for
private or fully-trusted repos). When `true`, `issues:opened`/`issues:reopened` route to
`Engine.intake`, which runs `decide_intake` against `allowlist`.

`allowlist` may also be configured globally in `Config` as a fallback when a `RepoConfig`
carries an empty allowlist (implementation detail; the functional contract is §3.11).

#### `Config`

Top-level service configuration.

```
Config {
  repos:           list<RepoConfig>
  limits:          SwarmLimits
  agent_pack:      AgentPackConfig              # specialist pack source + pinned ref
  reconcile_cron:  string = RECONCILER_CRON   # default "*/15 * * * *"
  dedup_window:    int                         # delivery_ids to remember (e.g. 1000)
}
```

`agent_pack` defaults to the `AgentPackConfig` defaults (§2) when omitted. Operators
may override `repo_url` to point at a private fork. Changing `pinned_ref` requires an
image rebuild — the pack is baked at build time, not fetched at runtime
(`AGENT_PACK.md §3`, `DEPLOYMENT.md §2`).

#### `PortProvider`

Resolves the three ports for a given repo. The concrete implementation holds credentials
(tokens, API keys) and is never exposed to the engine or the control plane.

```
PortProvider.ports(repo: RepoRef) -> (ForgePort, HarnessPort, SessionPort)   [sync]
```

A single GitHub App credential that spans many repos and a per-org token map are both
expressible as `PortProvider` implementations — the call site is identical.

> Python: `Protocol` with `def ports(self, repo: RepoRef) -> tuple[ForgePort, HarnessPort, SessionPort]`
> Rust: `trait PortProvider { fn ports(&self, repo: &RepoRef) -> (Box<dyn ForgePort>, Box<dyn HarnessPort>, Box<dyn SessionPort>); }`

---

### §8.3 Event routing

`handle_event` routes a `ForgeEvent` to the appropriate `Engine` method using the following
table, evaluated top-to-bottom (first match wins). This mirrors the live workflow triggers
in `dispatch.yml` and `pr-converge.yml` (`STATE_MACHINE.md §1`).

| `name` | `action` | `label` / body condition | Routes to | Transitions |
|---|---|---|---|---|
| `issues` | `opened` | `repo.intake_enabled == true` | `Engine.intake(event)` | intake → I1 or `awaiting-promotion` |
| `issues` | `reopened` | `repo.intake_enabled == true` | `Engine.intake(event)` | intake → I1 or `awaiting-promotion` |
| `issues` | `labeled` | `label == LABEL_AGENT_WORK` | `Engine.dispatch(event)` | I2, P1 |
| `issue_comment` | any | `comment_body` contains `@claude` | `Engine.dispatch(event)` | I5 |
| `pull_request_review_comment` | any | `comment_body` contains `@claude` | `Engine.dispatch(event)` | I5 |
| `pull_request` | `ready_for_review` | — | `Engine.converge(event.pr_ref)` | P2, P7 |
| `pull_request` | `labeled` | `label == LABEL_CONVERGE` | `Engine.converge(event.pr_ref)` | P7 |
| `pull_request` | `synchronize` | — | `Engine.converge(event.pr_ref)` | P7 (re-enter via gate) |
| cron tick | — | — | `Engine.reconcile(repo)` for each enabled repo | RC-1..RC-4 |
| anything else | — | — | no-op | — |

The `synchronize` entry is safe because `Engine.converge` begins with the idempotency gate
(§5.2): if the PR is not in a valid CONVERGING state the gate returns immediately.

---

### §8.4 `OrchestratorService`

Constructed with `(provider: PortProvider, config: Config)`. For each event or reconcile
call, the service calls `provider.ports(repo)` and builds a stateless `Engine(forge, harness,
session)` for that call. The `Engine` is therefore effectively per-call; the `OrchestratorService`
owns the registry, the semaphores, and the dedup cache.

> Python: `class OrchestratorService`; `asyncio.Semaphore` for limits; `collections.OrderedDict`
> as LRU for dedup.
> Rust: `struct OrchestratorService`; `tokio::sync::Semaphore`; an `LruCache` from the `lru`
> crate for dedup.

#### Lifecycle

```
async start() -> void
```
Begin the internal reconcile cadence loop (fires `reconcile_now(null)` per
`config.reconcile_cron`). Idempotent — calling `start()` twice is harmless. Optional: an
external scheduler (systemd timer, cron, Kubernetes CronJob) calling `reconcile_now` via
the CLI is equally valid and preferred in environments that already have a scheduler.

```
async stop() -> void
```
Signal the cadence loop to stop and drain all in-flight async tasks before returning.

#### Event ingress

```
async handle_event(event: ForgeEvent) -> EventOutcome
```
The webhook / poll ingress point. Steps:
1. If `event.delivery_id` is in the dedup cache → return `EventOutcome { handled: false, routed_to: "dedup", ... }`.
2. Add `delivery_id` to the LRU cache (evicting the oldest entry if at `dedup_window`).
3. Look up `event.repo` in the registry; if not found or not enabled → return early.
4. Apply the routing table (§8.3). If no row matches → return `EventOutcome { handled: false, routed_to: null, ... }`.
5. Acquire the appropriate semaphore slot(s) from `config.limits`.
6. Call `provider.ports(event.repo)` → build `Engine` → call the routed method.
7. Release semaphore; return `EventOutcome { handled: true, routed_to: "<method>", ... }`.

Errors are caught per event; they log and return an error-tagged `EventOutcome` without
propagating to the caller.

```
EventOutcome {
  handled:    bool
  routed_to:  string | null    # "Engine.dispatch" | "Engine.converge" | "dedup" | null
  pr_ref:     PRRef | null
  error:      string | null
}
```

#### Pipeline operations

```
async reconcile_now(repo: RepoRef | null) -> list<ReconcileReport>
```
Run `Engine.reconcile` immediately. `null` = all enabled repos (concurrently, up to
`max_concurrent_reconciles`). Per-repo errors are caught and logged; the successful reports
are still returned. This is the primitive that `start()` calls on the cron cadence and that
the CLI / web GUI exposes for on-demand reconcile.

```
async status(repo: RepoRef | null) -> list<HealthReport>
```
Call `pipeline_health(forge, repo)` (§3.9) for each specified (or all enabled) repo.
Returns one `HealthReport` per repo.

#### Registry management

```
register_repo(cfg: RepoConfig) -> void       [sync]
unregister_repo(repo: RepoRef) -> void       [sync]
pause_repo(repo: RepoRef) -> void            [sync]   # sets enabled=false
resume_repo(repo: RepoRef) -> void           [sync]   # sets enabled=true
list_repos() -> list<RepoConfig>             [sync]
```

Registry mutations are synchronous (in-memory map update; no I/O). Pausing a repo stops
new dispatch and reconcile calls but leaves forge label state untouched — the reconciler
will resume correctly when the repo is resumed.

#### Run observation

```
async list_runs(repo: RepoRef) -> list<RunSummary>
async get_run(handle: RunHandle) -> RunDetail
async cancel_run(handle: RunHandle) -> void
async intervene_run(handle: RunHandle, message: string) -> void
```

Thin delegation to `SessionPort` methods (§4.3) via `provider.ports(repo).session`. These
do not alter forge label state.

---

### §8.5 Scheduling, concurrency, and robustness

#### Reconcile cadence

`reconcile_now(null)` is the single durable reconcile primitive. `start()` provides a
built-in cadence loop as a convenience; external schedulers (systemd timer, Kubernetes
CronJob, cron) calling `reconcile_now` via the CLI are an equally valid — and often more
operationally transparent — deployment pattern.

#### Swarm backpressure

`SwarmLimits` provides two semaphores:
- A **global** semaphore capping total concurrent `harness.dispatch` calls across all repos.
- A **per-repo** semaphore capping concurrent calls within one repo.

`Engine.converge` dispatches up to `PARALLEL_SPECIALIST_CAP` reviewers/fixers concurrently
(§6, §5.2); the swarm semaphores sit above that, limiting how many `converge` calls can be
in-flight simultaneously. `max_concurrent_reconciles` governs concurrent `Engine.reconcile`
calls during a `reconcile_now(null)` sweep.

#### Per-repo fault isolation

`handle_event` and `reconcile_now` catch all errors at the per-repo / per-event boundary.
A forge that is temporarily unreachable, a harness that returns an unexpected error, or a
bug in one repo's handling must not abort other repos or the reconcile sweep. Errors are
logged with the repo and event context; the call continues to the next repo.

#### Idempotent ingress

The `delivery_id` LRU in `handle_event` prevents the same webhook from triggering duplicate
dispatch when the forge retries delivery. Correctness does not depend on this cache: the
`Engine.converge` idempotency gate (§5.2) and the reconciler's per-channel guards already
make reprocessing safe. The dedup cache is a latency / rate-limit optimization.

#### Paused repos

`handle_event` and `reconcile_now` skip repos where `enabled == false`. Forge label state
is untouched during the pause; when `resume_repo` sets `enabled=true`, the next reconcile
sweep picks up any stale entities as normal.

---

### §8.6 Transport mapping (illustrative, non-binding)

The `OrchestratorService` exposes in-process async methods. A CLI driver calls them
directly; a web adapter wraps them in HTTP. Neither is mandated by this spec — the table
below is illustrative only.

| Service method | CLI verb (example) | Web surface (example) |
|---|---|---|
| `handle_event` | `orch event --payload <file>` | `POST /webhook` (forge webhook target) |
| `reconcile_now(null)` | `orch reconcile` | `POST /reconcile` |
| `reconcile_now(repo)` | `orch reconcile --repo owner/name` | `POST /reconcile?repo=owner/name` |
| `status(null)` | `orch status` | `GET /status` |
| `status(repo)` | `orch status --repo owner/name` | `GET /status?repo=owner/name` |
| `register_repo` | `orch repo add owner/name` | `PUT /repos/owner/name` |
| `unregister_repo` | `orch repo rm owner/name` | `DELETE /repos/owner/name` |
| `pause_repo` | `orch repo pause owner/name` | `POST /repos/owner/name/pause` |
| `resume_repo` | `orch repo resume owner/name` | `POST /repos/owner/name/resume` |
| `list_repos` | `orch repo list` | `GET /repos` |
| `list_runs` | `orch runs --repo owner/name` | `GET /repos/owner/name/runs` |
| `get_run` | `orch run <handle>` | `GET /runs/<handle>` |
| `cancel_run` | `orch run cancel <handle>` | `POST /runs/<handle>/cancel` |
| `intervene_run` | `orch run say <handle> "message"` | `POST /runs/<handle>/intervene` |

A minimal deployment: a single `POST /webhook` handler that calls `handle_event`, plus a
cron job calling `orch reconcile`. The full control plane methods become available when
a CLI or richer web dashboard is wired up.
