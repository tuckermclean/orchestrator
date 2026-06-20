# DECISION_LOGIC.md

> Implementation contract for the forge-agnostic / harness-agnostic
> agent-orchestration state machine. Each pure decision function is specified
> below as an exhaustive input→output truth table, derived **verbatim** from the
> existing bash scripts and their Vitest tests in the `mirror` repo. Every output
> token and every threshold is line-cited and is binding on the future Python
> port.
>
> Ground truth: `mirror/scripts/**` + `mirror/tests/infra/**`. This document does
> **not** derive from `mirror/ORCHESTRATION.md` (stale). Where a script and its
> test diverge, it is flagged inline and in the closing notes.
>
> Citation form `file:line` always refers to the **script**, except rows that
> cite a `*.test.ts` file, which pin a behavior the script realizes implicitly.

---

## Conventions

- **Domains** use set notation: `ℤ≥0` = non-negative integers; `∈ {…}` = enumerated
  allowed values; `string` = arbitrary text matched only by equality where noted.
- **Priority order**: rows are evaluated top-to-bottom; the **first** matching row
  fires and the function exits. Lower rows assume all higher guards were false.
- **Usage error** = exit code `2`, message on stderr, no decision token on stdout.
- **Test-only env vars** (dependency injection): flagged `[DI]`. They exist solely
  to bypass network (`gh`) calls in tests; production reads live data instead.

---

## 1. `route_entry` — `scripts/dispatch/decide-entry.sh`

**Purpose** — Map a forge event name to orchestrator entry parameters (model,
turn budget, contract path).

**Inputs**

| Name | Kind | Domain |
|---|---|---|
| `$1` (`EVENT`) | positional | `string` (may be empty/absent → `""`) |

**Output** — three `KEY=VALUE` lines on stdout (always all three keys):
`model` ∈ `{claude-opus-4-8, claude-sonnet-4-6}`, `max_turns` ∈ `{40, 30}`,
`contract` = `.agents/custom/orchestrator-contract.md` (constant).

**Decision table** (priority order)

| # | Condition | `model` | `max_turns` | `contract` | Cite |
|---|---|---|---|---|---|
| 1 | `EVENT == "issues"` | `claude-opus-4-8` | `40` | orchestrator-contract | `decide-entry.sh:27-31` |
| 2 | `EVENT ∈ {issue_comment, pull_request_review_comment}` | `claude-sonnet-4-6` | `30` | orchestrator-contract | `decide-entry.sh:32-36` |
| 3 | else (unknown / empty) | `claude-sonnet-4-6` | `30` | orchestrator-contract | `decide-entry.sh:37-42` |

- Exit `0` for **all** inputs, including unknown/empty (graceful default; `dispatch-decide-entry.test.ts:133-135`).
- `contract` is invariant across all branches (`decide-entry.sh:24`).

**Purity / side effects** — Pure. No network, no file I/O.

---

## 2. `resolve_blockers` — `scripts/converge/resolve-blockers.sh`

**Purpose** — Resolve the effective blocker count for one convergence round,
falling back from the machine verdict JSON to the reviewer's latest in-round
PR-comment footer when the init sentinel survived.

**Inputs**

| Name | Kind | Domain |
|---|---|---|
| `$1` (`verdict_file`) | positional | path; may be nonexistent |
| `$2` (`pr_number`) | positional | PR identifier (passed to `gh`) |
| `CONVERGE_ROUND_STARTED` | env | ISO-8601 timestamp or `""` (empty = unscoped fallback) |
| `CONVERGE_COMMENT_BODY` | env `[DI]` | single comment body; bypasses `gh` |
| `CONVERGE_COMMENTS_JSON` | env `[DI]` | JSON array of `{createdAt, body}`; bypasses `gh` |

**Output** — one token on stdout: a non-negative integer (the blocker count) **or**
`unknown`.

**Sentinel** — the verdict file is sentinel iff
`(.blocker_signatures // []) | index("verdict-file-not-written")` is truthy
(`resolve-blockers.sh:56-61`).

**Decision table** (priority order)

| # | Condition | Output | Cite |
|---|---|---|---|
| 0 | `verdict_file` empty **or** `pr_number` empty | **usage error, exit 2** | `resolve-blockers.sh:34-37` |
| 1 | not sentinel → trust JSON: `.blockers // "unknown"`, then `emit_int_or_unknown` | the int, or `unknown` if missing/non-numeric | `resolve-blockers.sh:64-67` |
| 2 | sentinel **and** `CONVERGE_COMMENT_BODY` set → parse footer from that body | parsed int, or `unknown` | `resolve-blockers.sh:78-79, 93-95` |
| 3 | sentinel, no `CONVERGE_COMMENT_BODY` → pick `last` matching-footer comment from `CONVERGE_COMMENTS_JSON` (or live `gh`), filtered to `createdAt >= CONVERGE_ROUND_STARTED` when that is non-empty | parsed int from that body, or `unknown` | `resolve-blockers.sh:81-95` |
| 4 | sentinel, no body resolved (empty / `null`) | `unknown` | `resolve-blockers.sh:98` |

**Helper semantics**

- `parse_comment_blockers` extracts the first `🔴 <N> blockers?` footer number via
  `grep -oE '🔴[[:space:]]*[0-9]+[[:space:]]*blockers?'` (`resolve-blockers.sh:40-46`).
- `emit_int_or_unknown`: empty or any non-`[0-9]` char → `unknown`; else echo
  as-is (`resolve-blockers.sh:49-54`).

**Edge cases from tests** (`converge-resolve-blockers.test.ts`)

| Input | Output | Rationale | Test line |
|---|---|---|---|
| JSON `{blockers:0}` (not sentinel) | `0` | trust JSON | `:71-73` |
| JSON `{blockers:2}` | `2` | trust JSON | `:75-77` |
| sentinel + body `🔴 0 blockers \| …` | `0` | footer fallback (#31 fix) | `:79-85` |
| sentinel + body `🔴 3 blockers \| …` | `3` | footer fallback | `:87-91` |
| sentinel + body `review still in progress…` | `unknown` | no footer match | `:93-97` |
| sentinel + only **stale** footer (before `ROUND_START`) | `unknown` | round-scoped filter drops it (#182 fix) | `:107-111` |
| sentinel + stale + **current** `🔴 0 blockers` | `0` | uses current-round footer | `:113-121` |
| sentinel + stale + current `🔴 2 blockers` | `2` | prefers current over stale | `:123-131` |
| sentinel + stale footer, `CONVERGE_ROUND_STARTED=""` | `1` | unscoped → reads any footer | `:133-135` |
| JSON `{suggestions:0}` (no blockers field) | `unknown` | `.blockers // "unknown"` | `:137-139` |
| nonexistent verdict file (+ no-footer body) | `unknown` | not sentinel (no file) → JSON path yields `unknown` | `:141-154` |
| no args | **exit 2** | usage guard | `:156-164` |

> Note on round scoping: when both a stale and a current footer match, the `jq`
> filter returns `last`, and the in-round filter (`createdAt >= $since`) is applied
> **before** `last`, so the most-recent in-round footer wins (`resolve-blockers.sh:86-90`).

**Purity / side effects** — **Impure unless injected.** Shells out to `gh pr view`
when neither `CONVERGE_COMMENT_BODY` nor `CONVERGE_COMMENTS_JSON` is set
(`resolve-blockers.sh:84`). Reads `verdict_file` from disk (`jq`). Pure when both
DI vars / a present file cover the path.

---

## 3. `decide_round` — `scripts/converge/decide-round.sh`

**Purpose** — Decide the convergence action for one round of the converge loop.

**Inputs** (all env vars, all required)

| Name | Domain |
|---|---|
| `ROUND` | ∈ `{1,2,3}` |
| `BLOCKERS` | ∈ `ℤ≥0 ∪ {"unknown"}` |
| `CI_GREEN` | ∈ `{true, false}` |
| `PREV_SIGS` | JSON array (sorted blocker_signatures of prior round; `[]` for round 1) |
| `CURR_SIGS` | JSON array (current round) |

**Output tokens** —
`approve | fix | escalate:no-progress | escalate:no-verdict | escalate:ci-red | escalate:cap-reached`.

**Validation** (each → usage error, exit 2)

| Check | Cite |
|---|---|
| any of the 5 vars empty | `decide-round.sh:34-41` |
| `ROUND ∉ {1,2,3}` | `decide-round.sh:43-46` |
| `CI_GREEN ∉ {true,false}` | `decide-round.sh:48-51` |
| `BLOCKERS != "unknown"` and not `^[0-9]+$` (e.g. `foo`, `1foo`) | `decide-round.sh:53-56` |

**Sentinel normalization** (applied before any decision):
`CURR_SIGS == '["verdict-file-not-written"]'` → `"[]"`; same for `PREV_SIGS`
(`decide-round.sh:64-66`). Treated identically to a reviewer that emitted no
signatures.

**Decision table** (priority order, post-normalization)

| # | Condition | Output | Cite |
|---|---|---|---|
| 1 | `BLOCKERS == "0"` **and** `CI_GREEN == "true"` | `approve` | `decide-round.sh:69-72` |
| 2 | `ROUND == 1` | `fix` | `decide-round.sh:75-78` |
| 3 | `CURR_SIGS == PREV_SIGS` **and** `CURR_SIGS != "[]"` **and** `BLOCKERS ∉ {"0","unknown"}` | `escalate:no-progress` | `decide-round.sh:84-88` |
| 4 | `ROUND == 2` | `fix` | `decide-round.sh:91-94` |
| 5 | `ROUND == 3` and `BLOCKERS == "unknown"` | `escalate:no-verdict` | `decide-round.sh:97-101` |
| 6 | `ROUND == 3` and `BLOCKERS == "0"` (CI not green, else row 1) | `escalate:ci-red` | `decide-round.sh:102-105` |
| 7 | `ROUND == 3` else (blockers remain, ≥1) | `escalate:cap-reached` | `decide-round.sh:106-108` |

**Key edge cases from tests** (`converge-decide-round.test.ts`)

| ROUND | BLOCKERS | CI_GREEN | PREV_SIGS | CURR_SIGS | Output | Why / test |
|---|---|---|---|---|---|---|
| 1 | 0 | true | `[]` | `[]` | `approve` | row 1 `:54-55` |
| 1 | 0 | false | `[]` | `[]` | `fix` | CI red = implicit blocker `:74-76` |
| 1 | unknown | false | `[]` | `[]` | `fix` | unknown never approves `:78-80` |
| 2 | unknown | false | `["some-sig"]` | `["some-sig"]` | `fix` | no-progress guard excludes `unknown` `:82-92` |
| 2 | 1 | false | `[a,b]` | `[b]` | `fix` | sigs differ = progress `:94-104` |
| 2 | 1 | false | `["missing-auth-check"]` | `["missing-auth-check"]` | `escalate:no-progress` | row 3 `:108-118` |
| 2 | 2 | false | `[]` | `[]` | `fix` | empty==empty is NOT "stuck" `:215-225` |
| 2 | 1 | false | `["verdict-file-not-written"]` | `["verdict-file-not-written"]` | `fix` | both sentinel → both `[]` `:246-256` |
| 2 | 1 | false | `["verdict-file-not-written"]` | `["missing-auth-check"]` | `fix` | sentinel→`[]` ≠ real sig `:258-268` |
| 2 | 1 | false | `["missing-auth-check"]` | `["verdict-file-not-written"]` | `fix` | real sig ≠ `[]` `:270-280` |
| 3 | 2 | false | `[a,b]` | `[a,b]` (equal) | `escalate:no-progress` | row 3 fires before R3 terminal `:120-130` |
| 3 | unknown | false | `[]` | `[]` | `escalate:no-verdict` | row 5 `:148-152` |
| 3 | 0 | false | `[]` | `[]` | `escalate:ci-red` | row 6 `:154-164` |
| 3 | 3 | false | `["blocker-a"]` | `["blocker-b"]` | `escalate:cap-reached` | row 7 `:166-176` |
| 3 | unknown | false | sentinel | sentinel | `escalate:no-verdict` | sentinel→`[]`, then row 5 `:282-292` |
| 3 | 1 | false | sentinel | sentinel | `escalate:cap-reached` | sentinel→`[]`, blockers remain, row 7 `:294-304` |

**Purity / side effects** — Pure. No network, no file I/O.

---

## 4. `decide_cap_action` — `scripts/converge/decide-cap-action.sh`

**Purpose** — When the converge loop cannot self-finish a PR (3-round cap reached
with blockers open, or empty no-diff PR), decide whether to re-dispatch the
implementing agent (bounded) or escalate to a human.

**Inputs** (positional; exactly 2 required)

| Name | Domain |
|---|---|
| `$1` (`redispatch_count`) | `ℤ≥0` (count of `<!-- converge-redispatch -->` markers) |
| `$2` (`has_issue_num`) | ∈ `{0, 1}` (closing issue found in PR body) |

**Constant** — `MAX_REDISPATCHES = 2` (`decide-cap-action.sh:34`).

**Output tokens** — `redispatch | escalate`.

**Decision table** (priority order)

| # | Condition | Output | Cite |
|---|---|---|---|
| 0 | arg count `!= 2` | **usage error, exit 2** | `decide-cap-action.sh:36-39` |
| 1 | `has_issue_num == 0` | `escalate` (nothing to dispatch to) | `decide-cap-action.sh:45-48` |
| 2 | `redispatch_count >= 2` | `escalate` (budget exhausted) | `decide-cap-action.sh:51-54` |
| 3 | else | `redispatch` | `decide-cap-action.sh:56` |

**Boundary cases from tests** (`converge-decide-cap.test.ts`)

| count | has_issue | Output | Test |
|---|---|---|---|
| 0 | 1 | `redispatch` | `:28-30` |
| 1 | 1 | `redispatch` (still under cap) | `:32-34` |
| 2 | 1 | `escalate` (cap reached, `>=`) | `:37-39` |
| 5 | 1 | `escalate` | `:41-43` |
| 0 | 0 | `escalate` (no issue wins even at count 0) | `:46-48` |
| 2 | 0 | `escalate` | `:50-52` |

**Purity / side effects** — Pure. No network, no file I/O.

---

## 5. `decide_stale_action` — `scripts/reconciler/decide-stale-action.sh`

**Purpose** — Decide the recovery action for a stale draft PR carrying
`agent:implementing`.

**Inputs** (positional; exactly 6 required)

| Pos | Name | Domain |
|---|---|---|
| `$1` | `redispatch_count` | `ℤ≥0` |
| `$2` | `ci_runs` | `ℤ≥0` (0 = CI never ran on HEAD) |
| `$3` | `has_converge` | ∈ `{0, 1}` |
| `$4` | `failing_count` | `ℤ≥0` (failing blocking CI checks) |
| `$5` | `has_issue_num` | ∈ `{0, 1}` |
| `$6` | `has_diff` | ∈ `{0, 1}` (0 = empty branch, no work) |

**Output tokens** —
`escalate | trigger-ci | redispatch | needs-human | mark-ready | mark-ready-and-converge`.

**Decision table** (priority order)

| # | Condition | Output | Cite |
|---|---|---|---|
| 0 | arg count `!= 6` | **usage error, exit 2** | `decide-stale-action.sh:27-30` |
| 1 | `redispatch_count >= 3` | `escalate` | `decide-stale-action.sh:40-43` |
| 2 | `ci_runs == 0` | `trigger-ci` | `decide-stale-action.sh:46-49` |
| 2.5a | `has_diff == 0` **and** `has_issue_num != 0` | `redispatch` | `decide-stale-action.sh:56-62` |
| 2.5b | `has_diff == 0` **and** `has_issue_num == 0` | `needs-human` | `decide-stale-action.sh:56-62` |
| 3 | `has_converge != 0` | `mark-ready` | `decide-stale-action.sh:66-69` |
| 4 | `failing_count == 0` | `mark-ready-and-converge` | `decide-stale-action.sh:72-75` |
| 5 | `has_issue_num != 0` | `redispatch` | `decide-stale-action.sh:78-81` |
| 6 | else (failing, no issue) | `needs-human` | `decide-stale-action.sh:84` |

> **Empty-PR guard (Priority 2.5)** is the key subtlety: the `converge` label is
> added at draft-PR creation, so its presence is *not* evidence of finished work.
> An empty (no-diff) PR must resume the work, never be marked ready — even when it
> carries `converge` (`decide-stale-action.sh:50-63`). Higher priorities
> (`escalate`, `trigger-ci`) still win over this guard.

**Boundary / guard cases from tests** (`reconciler-decide-stale.test.ts`,
arg order `count, ci_runs, has_converge, failing, has_issue, has_diff`)

| Args | Output | Test |
|---|---|---|
| `3,5,0,2,1,1` | `escalate` (cap exactly 3, `>=`) | `:53-55` |
| `5,5,0,2,1,1` | `escalate` | `:57-59` |
| `2,5,0,2,1,1` | `redispatch` (count 2 < 3) | `:61-64` |
| `0,0,0,0,1,1` | `trigger-ci` | `:68-70` |
| `1,0,1,3,0,1` | `trigger-ci` (wins over converge label) | `:72-74` |
| `0,5,1,2,1,1` | `mark-ready` | `:78-80` |
| `2,3,1,10,0,1` | `mark-ready` (ignores failing count) | `:82-84` |
| `0,5,0,0,1,1` | `mark-ready-and-converge` | `:88-90` |
| `0,5,0,0,0,1` | `mark-ready-and-converge` (no issue OK when CI green) | `:92-94` |
| `0,5,0,3,1,1` | `redispatch` | `:98-100` |
| `0,5,0,3,0,1` | `needs-human` | `:108-110` |
| `0,5,1,0,1,0` | `redispatch` (empty PR + converge label) | `:121-124` |
| `0,5,0,0,1,0` | `redispatch` (empty PR, CI green) | `:126-129` |
| `0,5,1,0,0,0` | `needs-human` (empty PR, no issue) | `:131-133` |
| `0,5,1,0,1,1` | `mark-ready` (non-empty regression guard) | `:135-137` |
| `3,5,1,0,1,0` | `escalate` (cap beats empty-PR guard) | `:139-141` |
| `0,0,1,0,1,0` | `trigger-ci` (beats empty-PR guard) | `:143-145` |

**Purity / side effects** — Pure. No network, no file I/O.

---

## 6. `decide_rearm_action` — `scripts/reconciler/decide-rearm-action.sh`

**Purpose** — For a **non-draft** converge PR, decide whether to trigger CI,
re-arm converge, or skip.

**Inputs** (positional; exactly 4 required)

| Pos | Name | Domain |
|---|---|---|
| `$1` | `ci_runs` | `ℤ≥0` |
| `$2` | `converge_state` | `string`, `"<status>:<conclusion>"` e.g. `in_progress:`, `queued:`, `completed:success`, `completed:failure`, `none:none` |
| `$3` | `has_terminal_label` | ∈ `{0, 1}` (1 = `agent:ready` or `needs-human`) |
| `$4` | `seconds_since_last_run` | `ℤ≥0 ∪ {""}` (`""` = never ran) |

**Output tokens** —
`trigger-ci | skip-in-progress | skip-done | skip-recent | rearm`.

**Decision table** (priority order)

| # | Condition | Output | Cite |
|---|---|---|---|
| 0 | arg count `!= 4` | **usage error, exit 2** | `decide-rearm-action.sh:23-26` |
| 1 | `ci_runs == 0` | `trigger-ci` | `decide-rearm-action.sh:34-37` |
| 2 | `converge_state ∈ {"in_progress:", "queued:"}` | `skip-in-progress` | `decide-rearm-action.sh:42-45` |
| 3 | `converge_state == "completed:success"` **and** `has_terminal_label != 0` | `skip-done` | `decide-rearm-action.sh:48-51` |
| 4 | `seconds_since_last_run` non-empty **and** `< 300` | `skip-recent` | `decide-rearm-action.sh:55-58` |
| 5 | else | `rearm` | `decide-rearm-action.sh:63` |

> `queued:` is folded into `in_progress:` deliberately — GitHub emits `queued`
> before `in_progress`, and treating them alike prevents duplicate dispatch
> (`decide-rearm-action.sh:39-41`).

**Boundary cases from tests** (`reconciler-decide-rearm.test.ts`)

| Args | Output | Test |
|---|---|---|
| `0,none:none,0,""` | `trigger-ci` | `:47-49` |
| `0,completed:success,1,600` | `trigger-ci` (ci_runs==0 wins) | `:51-53` |
| `5,in_progress:,0,""` | `skip-in-progress` | `:57-59` |
| `5,queued:,0,""` | `skip-in-progress` | `:61-63` |
| `5,completed:success,1,600` | `skip-done` | `:67-69` |
| `5,completed:success,1,50` | `skip-done` (terminal beats recency) | `:71-73` |
| `5,completed:success,0,100` | `skip-recent` | `:77-79` |
| `5,completed:success,0,0` | `skip-recent` (0 < 300) | `:81-83` |
| `5,completed:success,0,299` | `skip-recent` (boundary − 1) | `:85-87` |
| `5,completed:success,0,300` | `rearm` (exactly 300 = NOT recent) | `:91-93` |
| `5,completed:success,0,9000` | `rearm` | `:95-97` |
| `5,none:none,0,""` | `rearm` | `:99-101` |
| `5,completed:success,0,""` | `rearm` (empty seconds skips recency) | `:103-105` |
| `5,completed:failure,0,""` | `rearm` | `:107-109` |

**Purity / side effects** — Pure. No network, no file I/O.

---

## 7. `decide_conflict_action` — `scripts/reconciler/decide-conflict-action.sh`

**Purpose** — Decide whether to escalate a merge-conflicting PR.

**Inputs** (positional; exactly 2 required)

| Pos | Name | Domain |
|---|---|---|
| `$1` | `mergeable` | `string` (GitHub state; only `CONFLICTING` is special) |
| `$2` | `already_needs_human` | `ℤ≥0` (count of needs-human labels; 0 = unlabeled) |

**Output tokens** — `escalate | skip`.

**Decision table** (priority order)

| # | Condition | Output | Cite |
|---|---|---|---|
| 0 | arg count `!= 2` | **usage error, exit 2** | `decide-conflict-action.sh:16-19` |
| 1 | `mergeable == "CONFLICTING"` **and** `already_needs_human == 0` | `escalate` | `decide-conflict-action.sh:24-25` |
| 2 | else | `skip` | `decide-conflict-action.sh:26-27` |

**Cases from tests** (`reconciler-decide-conflict.test.ts`)

| Args | Output | Test |
|---|---|---|
| `CONFLICTING, 0` | `escalate` | `:35-37` |
| `MERGEABLE, 0` | `skip` | `:41-43` |
| `UNKNOWN, 0` | `skip` | `:45-47` |
| `"", 0` (empty) | `skip` | `:49-51` |
| `CONFLICTING, 1` | `skip` (already labeled) | `:55-57` |
| `CONFLICTING, 5` | `skip` | `:59-61` |

**Purity / side effects** — Pure. No network, no file I/O.

---

## 8. `decide_redispatch_action` — `scripts/reconciler/decide-redispatch-action.sh`

**Purpose** — Decide whether to re-dispatch an `agent-work` issue that has no open
PR.

**Inputs** (positional; exactly 3 required)

| Pos | Name | Domain |
|---|---|---|
| `$1` | `has_open_pr` | ∈ `{0, 1}` |
| `$2` | `seconds_since_last_activity` | `ℤ≥0 ∪ {""}` (`""` = never touched) |
| `$3` | `redispatch_count` | `ℤ≥0` |

**Output tokens** — `skip-has-pr | skip-recent | escalate | redispatch`.

**Decision table** (priority order)

| # | Condition | Output | Cite |
|---|---|---|---|
| 0 | arg count `!= 3` | **usage error, exit 2** | `decide-redispatch-action.sh:21-24` |
| 1 | `has_open_pr != 0` | `skip-has-pr` | `decide-redispatch-action.sh:31-34` |
| 2 | `seconds_since_last_activity` non-empty **and** `< 900` | `skip-recent` | `decide-redispatch-action.sh:38-41` |
| 3 | `redispatch_count >= 3` | `escalate` | `decide-redispatch-action.sh:44-47` |
| 4 | else | `redispatch` | `decide-redispatch-action.sh:50` |

**Boundary cases from tests** (`reconciler-decide-redispatch.test.ts`)

| Args | Output | Test |
|---|---|---|
| `1,600,0` | `skip-has-pr` | `:45-47` |
| `1,9999,5` | `skip-has-pr` (PR wins over cap) | `:49-51` |
| `0,100,0` | `skip-recent` | `:55-57` |
| `0,0,0` | `skip-recent` (0 < 900) | `:59-61` |
| `0,899,0` | `skip-recent` (boundary − 1) | `:63-65` |
| `0,900,0` | NOT `skip-recent` → `redispatch` (exactly 900 = not recent) | `:67-70` |
| `0,1200,0` | NOT `skip-recent` → `redispatch` | `:72-74` |
| `0,9999,3` | `escalate` (cap exactly 3, `>=`) | `:78-80` |
| `0,9999,7` | `escalate` | `:82-84` |
| `0,9999,0` | `redispatch` | `:88-90` |
| `0,9999,2` | `redispatch` (count 2 < 3) | `:92-94` |
| `0,"",0` | `redispatch` (never touched skips recency) | `:96-98` |
| `0,"",2` | `redispatch` | `:100-102` |

**Purity / side effects** — Pure. No network, no file I/O.

---

## 9. `pipeline_health` — `scripts/status/pipeline-status.sh`

**Purpose** — Emit a markdown Pipeline Status Report with per-label counts and a
single health verdict.

**Inputs**

| Name | Kind | Domain |
|---|---|---|
| `$1` (`repo`) | positional | `string` (required; passed to `gh pr list`) |
| `PIPELINE_PR_JSON` | env `[DI]` | JSON array of `{number, isDraft, labels:[{name}]}`; bypasses `gh` |

**Counts** (via `jq`, `pipeline-status.sh:31-44`)

- `implementing` = PRs with label `agent:implementing`
- `converge` = PRs with label `converge`
- `ready` = PRs with label `agent:ready`
- `needs_human` = PRs with label `needs-human`
- `stale_drafts` = PRs where `isDraft == true` **and** has `agent:implementing`
- `in_flight` = `implementing + converge` (`pipeline-status.sh:47`)

**Output** — a markdown report on stdout containing the four count rows, the
stale-drafts line, and `**Pipeline health: <verdict>**`.

**Verdict table** (priority order)

| # | Condition | `verdict` | Cite |
|---|---|---|---|
| 0 | `repo` empty | **usage error, exit 2** | `pipeline-status.sh:18-21` |
| 1 | `needs_human > 0` | `BLOCKED` | `pipeline-status.sh:49-50` |
| 2 | `in_flight >= 5` | `AT_RISK` | `pipeline-status.sh:51-52` |
| 3 | else (incl. all-zero) | `ON_TRACK` | `pipeline-status.sh:53-54` |

**Cases from tests** (`pipeline-status.test.ts`)

| PR fixture | verdict | Test |
|---|---|---|
| empty list | `ON_TRACK` (all counts 0) | `:29-36` |
| 1 impl + 1 conv + 2 ready | `ON_TRACK` (in_flight 2) | `:38-51` |
| 1 needs-human + 1 ready | `BLOCKED` | `:53-61` |
| 3 impl + 2 conv (total 5) | `AT_RISK` | `:63-75` |
| 1 of each (incl. needs-human) | `BLOCKED` (needs-human priority) | `:77-86` |
| 4 impl + 1 conv (total 5) | `AT_RISK` | `:88-100` |
| missing repo arg | **exit 2** | `:102-110` |

> `AT_RISK` threshold is `in_flight >= 5`, i.e. exactly 5 trips it (boundary
> confirmed by both the `3+2` and `4+1` tests). `needs-human` always wins over the
> in-flight count.

**Purity / side effects** — **Impure unless injected.** Shells out to `gh pr list`
when `PIPELINE_PR_JSON` is unset (`pipeline-status.sh:24-28`). Also calls
`date -u` for the report header (non-deterministic timestamp; not part of the
verdict). Verdict logic is otherwise pure over the input JSON.

---

## Constants Reference

| Constant | Value | Meaning | Source `file:line` |
|---|---|---|---|
| `MAX_TURNS` (issues) | `40` | Opus turn budget | `decide-entry.sh:29` |
| `MAX_TURNS` (comment/default) | `30` | Sonnet turn budget | `decide-entry.sh:34, 40` |
| Sentinel signature | `"verdict-file-not-written"` | init marker = "no verdict this round" | `resolve-blockers.sh:58`, `decide-round.sh:64` |
| `MAX_REDISPATCHES` (converge) | `2` | converge re-dispatch cap (`>=` escalates) | `decide-cap-action.sh:34, 51` |
| Reconciler redispatch cap (stale) | `3` | stale-PR escalate threshold (`>=`) | `decide-stale-action.sh:40` |
| Reconciler redispatch cap (issue) | `3` | no-PR issue escalate threshold (`>=`) | `decide-redispatch-action.sh:44` |
| Recent-run guard (rearm) | `300` s | converge "finished recently" (`<` skips) | `decide-rearm-action.sh:55` |
| Recent-activity guard (redispatch) | `900` s | issue "touched recently" (`<` skips) | `decide-redispatch-action.sh:38` |
| `AT_RISK` threshold | `5` | in_flight `>=` → AT_RISK | `pipeline-status.sh:51` |

### ⚠️ Duplicated-constant sync hazards

- **`MAX_REDISPATCHES = 2`** is the source of truth in
  `scripts/converge/decide-cap-action.sh:34`, but the same literal `2` is embedded
  **independently** in `.github/workflows/pr-converge.yml` in **two** places:
  the empty-PR gate (`pr-converge.yml:~81-83`, which has no checkout to call the
  script) and the stage-step inline fallback
  (`pr-converge.yml:~222, 227`: `[ "${1:-0}" -ge 2 ]`). The workflow comments
  reference the constant by name, but nothing enforces the sync — **changing the
  script's `2` silently leaves the workflow on the old cap.** The Python port
  should expose this as a single shared constant and have the workflow read it.

- The reconciler redispatch cap `3` appears in **two** scripts independently
  (`decide-stale-action.sh:40`, `decide-redispatch-action.sh:44`). They are
  semantically distinct counters (stale-PR vs. no-PR-issue) but share a magic
  number; the port should name them explicitly to avoid accidental "fix one,
  forget the other" drift.

- The two "recent" guards (`300` s rearm, `900` s redispatch) are **different**
  values for **different** purposes — do not unify them.

---

## Appendix: `scripts/git/strip-attribution.sh` (policy hook, not a transition)

A git `prepare-commit-msg` hook that strips Claude/Anthropic attribution from
commit messages. It is a **commit-message policy filter**, not a state-machine
decision function — included here only for completeness.

- **Modes**
  - `--install` (`strip-attribution.sh:18-25`): copies itself to
    `$(git rev-parse --git-dir)/hooks/prepare-commit-msg`, `chmod +x`. CI agent
    workflows install it at runtime so it fires on every agent commit.
  - hook mode (`$1` = commit-msg file path): edits the file in place.
  - missing `$1` (no mode): usage error, exit 2 (`strip-attribution.sh:28-32`).
- **Filter** (`strip-attribution.sh:36-40`) — `perl -i -ne 'print unless …'` drops
  any line matching:
  - `^Co-authored-by:.*[Aa]nthropic`
  - `^Co-authored-by:.*[Cc]laude`
  - `Generated with \[Claude Code\]`
- `perl -i` is used over `sed -i` for GNU/BSD portability
  (`strip-attribution.sh:34-35`).
- **Purity** — Impure by design: mutates the commit-message file (and the git
  hooks dir on `--install`). No decision token; no test in the infra suite.

---

## Discrepancies found

No behavioral discrepancies between any script and its test were found — every
truth-table row above is backed by a passing test case, and every script branch
is exercised. Two documentation-vs-code notes worth carrying into the port:

1. **`decide-cap-action.sh` doc header is incomplete.** The usage comment
   (`:17-22`) documents only the two positional args and the two output tokens,
   but the *header-level* purpose comment implies the cap is the only escalation
   trigger; in code, `has_issue_num == 0` escalates **before** the cap check
   (`:45-48`), independent of count. The tests confirm the code's behavior
   (`converge-decide-cap.test.ts:46-52`); the prose just under-describes it.

2. **`MAX_REDISPATCHES` is not single-sourced** across the script and
   `pr-converge.yml` (flagged above). This is a latent correctness hazard, not a
   current test failure — the workflow's inline `2` and the script's `2` happen to
   agree today.
