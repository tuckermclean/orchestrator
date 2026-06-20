# TESTING.md — Test Specification

> **First-class deliverable.** The gate between written code and a merged PR.
> Nothing in this document is advisory.

---

## §1 Testing Philosophy and Gate

### §1.1 The Hard Gate

Every PR must satisfy all of the following before `gh pr ready` is called:

1. Full test suite passes (all five layers, §§2–6).
2. Typecheck passes (`mypy --strict` for Python / `cargo check` for Rust).
3. Lint passes (`ruff` for Python / `clippy` for Rust).

These correspond exactly to `BLOCKING_CI_CHECKS` (`SPEC.md §7`). Automated CI blocks
merge. The converge reviewer raises missing tests or a failing gate as a **blocker**, not
a nit. Implementer and fixer agents must ship tests alongside every functional change.

### §1.2 Test Pyramid

```
Layer 5 — Idempotency / crash-only tests         (§6)
Layer 4 — Security / trust tests                  (§5)
Layer 3 — Engine integration tests (over fakes)   (§4)
Layer 2 — Port contract tests (fake + real)        (§3)
Layer 1 — Unit tests — decision functions          (§2)
```

Build upward: Layer 1 before Layer 2 fakes exist, Layer 2 before Layer 3, etc.
No layer touches the network. Layer 1 tests run in milliseconds.

### §1.3 Coverage Requirement

Every row in every truth table in `SPEC.md §8` must map to at least one named test case.
The CI gate must fail if any truth-table row is unexercised. When a truth table row is
added, the coverage check fails until a matching test is added.

---

## §2 Unit Tests — Decision Functions

All Layer 1 tests are **synchronous** except `resolve_blockers` and `pipeline_health`,
which are async and use the fake `ForgePort` (§3.2). No test in this layer makes a
real network call.

### §2.1 `decide_intake` — `SPEC.md §8.11`

| Test name | `allowlist` | `author` | Expected |
|---|---|---|---|
| `test_intake_gate_disabled` | `[]` | `"anyone"` | `admit` |
| `test_intake_gate_disabled_empty_string_author` | `[]` | `""` | `admit` |
| `test_intake_allowlisted_first` | `["alice", "bob"]` | `"alice"` | `admit` |
| `test_intake_allowlisted_second` | `["alice", "bob"]` | `"bob"` | `admit` |
| `test_intake_unlisted` | `["alice", "bob"]` | `"eve"` | `queue` |
| `test_intake_case_sensitive_upper_in_list` | `["Alice"]` | `"alice"` | `queue` |
| `test_intake_case_sensitive_lower_in_list` | `["alice"]` | `"Alice"` | `queue` |
| `test_intake_single_match` | `["solo"]` | `"solo"` | `admit` |
| `test_intake_single_nomatch` | `["solo"]` | `"other"` | `queue` |

Key boundary: empty `allowlist` always returns `admit` (gate disabled). Exact-string
equality only; no case-folding. **Minimum: 9.**

### §2.2 `route_entry` — `SPEC.md §8.1`

| Test name | `event` | Expected `model` | Expected `max_turns` |
|---|---|---|---|
| `test_route_entry_issues` | `"issues"` | `claude-opus-4-8` | `40` |
| `test_route_entry_issue_comment` | `"issue_comment"` | `claude-sonnet-4-6` | `30` |
| `test_route_entry_pr_review_comment` | `"pull_request_review_comment"` | `claude-sonnet-4-6` | `30` |
| `test_route_entry_unknown` | `"pull_request"` | `claude-sonnet-4-6` | `30` |
| `test_route_entry_empty_string` | `""` | `claude-sonnet-4-6` | `30` |
| `test_route_entry_contract_invariant` | any | same `contract` path | constant across all rows |

Must never raise an error for unknown inputs. **Minimum: 6.**

### §2.3 `resolve_blockers` — `SPEC.md §8.2`

Async; uses fake `ForgePort` for comment-footer fallback.

| Test name | Setup | Expected |
|---|---|---|
| `test_resolve_blockers_usage_error_no_verdict_path` | no verdict file argument | usage error |
| `test_resolve_blockers_trust_json_zero` | non-sentinel verdict, `blockers: 0` | `0` |
| `test_resolve_blockers_trust_json_two` | non-sentinel verdict, `blockers: 2` | `2` |
| `test_resolve_blockers_sentinel_footer_zero` | sentinel + footer `"🔴 0 blockers \| ..."` | `0` |
| `test_resolve_blockers_sentinel_footer_three` | sentinel + footer `"🔴 3 blockers \| ..."` | `3` |
| `test_resolve_blockers_sentinel_no_footer_match` | sentinel + body with no footer | `unknown` |
| `test_resolve_blockers_sentinel_stale_footer_only` | sentinel + only stale footer (before `ROUND_START`) | `unknown` |
| `test_resolve_blockers_sentinel_stale_and_current_zero` | sentinel + stale + current `🔴 0 blockers` | `0` |
| `test_resolve_blockers_sentinel_stale_and_current_two` | sentinel + stale + current `🔴 2 blockers` | `2` |
| `test_resolve_blockers_sentinel_unscoped_fallback` | sentinel + stale footer + `round_started=None` | `1` |
| `test_resolve_blockers_json_missing_blockers_field` | non-sentinel, `suggestions` but no `blockers` | `unknown` |
| `test_resolve_blockers_nonexistent_file_no_footer` | verdict file absent + no footer | `unknown` |

Key boundary: `round_started` timestamp scopes the footer search; stale footers from
prior rounds must not bleed through. `round_started=None` disables scoping.
**Minimum: 12.**

### §2.4 `decide_round` — `SPEC.md §8.3`

| Test name | `round` | `blockers` | `ci_green` | `prev_sigs` | `curr_sigs` | Expected |
|---|---|---|---|---|---|---|
| `test_decide_round_approve_r1` | 1 | 0 | true | `[]` | `[]` | `approve` |
| `test_decide_round_fix_r1_ci_red` | 1 | 0 | false | `[]` | `[]` | `fix` |
| `test_decide_round_fix_r1_unknown_blockers` | 1 | unknown | false | `[]` | `[]` | `fix` |
| `test_decide_round_fix_r2_unknown_no_progress_guard` | 2 | unknown | false | `["some-sig"]` | `["some-sig"]` | `fix` |
| `test_decide_round_fix_r2_sigs_differ` | 2 | 1 | false | `["a","b"]` | `["b"]` | `fix` |
| `test_decide_round_no_progress_r2` | 2 | 1 | false | `["missing-auth-check"]` | `["missing-auth-check"]` | `escalate:no-progress` |
| `test_decide_round_fix_r2_empty_sigs_not_stuck` | 2 | 2 | false | `[]` | `[]` | `fix` |
| `test_decide_round_sentinel_both_r2_fix` | 2 | 1 | false | `["verdict-file-not-written"]` | `["verdict-file-not-written"]` | `fix` |
| `test_decide_round_sentinel_prev_only_r2_fix` | 2 | 1 | false | `["verdict-file-not-written"]` | `["missing-auth-check"]` | `fix` |
| `test_decide_round_sentinel_curr_only_r2_fix` | 2 | 1 | false | `["missing-auth-check"]` | `["verdict-file-not-written"]` | `fix` |
| `test_decide_round_no_progress_r3_before_terminal` | 3 | 2 | false | `["a","b"]` | `["a","b"]` | `escalate:no-progress` |
| `test_decide_round_approve_r3` | 3 | 0 | true | `[]` | `[]` | `approve` |
| `test_decide_round_no_verdict_r3` | 3 | unknown | false | `[]` | `[]` | `escalate:no-verdict` |
| `test_decide_round_ci_red_r3` | 3 | 0 | false | `[]` | `[]` | `escalate:ci-red` |
| `test_decide_round_cap_reached_r3` | 3 | 3 | false | `["blocker-a"]` | `["blocker-b"]` | `escalate:cap-reached` |
| `test_decide_round_sentinel_both_r3_no_verdict` | 3 | unknown | false | `["verdict-file-not-written"]` | `["verdict-file-not-written"]` | `escalate:no-verdict` |
| `test_decide_round_sentinel_both_r3_cap_reached` | 3 | 1 | false | `["verdict-file-not-written"]` | `["verdict-file-not-written"]` | `escalate:cap-reached` |

Validation error tests (`test_decide_round_invalid_round_zero/four`, `_invalid_ci_green`,
`_invalid_blockers`, `_invalid_blockers_mixed`): 5 tests asserting usage error.

Key boundary: `unknown` blockers never produce `approve`. Empty `prev_sigs == curr_sigs
== []` is NOT no-progress (row 3 requires `CURR_SIGS != "[]"`). Row 3 (no-progress)
fires before rows 5–7 even in round 3. **Minimum: 22.**

### §2.5 `decide_cap_action` — `SPEC.md §8.4`

| Test name | `redispatch_count` | `has_issue` | Expected |
|---|---|---|---|
| `test_cap_action_redispatch_zero` | 0 | true | `redispatch` |
| `test_cap_action_redispatch_one` | 1 | true | `redispatch` |
| `test_cap_action_escalate_at_cap` | 2 | true | `escalate` |
| `test_cap_action_escalate_above_cap` | 5 | true | `escalate` |
| `test_cap_action_no_issue_zero` | 0 | false | `escalate` |
| `test_cap_action_no_issue_at_cap` | 2 | false | `escalate` |
| `test_cap_action_usage_error` | (missing args) | — | usage error |

`has_issue == false` escalates unconditionally before the cap check.
`count == MAX_REDISPATCHES` (2) is a cap-boundary hit → escalate. **Minimum: 7.**

### §2.6 `decide_stale_action` — `SPEC.md §8.5`

Arg order: `redispatch_count, ci_runs, has_converge, failing_count, has_issue, has_diff`.

| Test name | Args | Expected |
|---|---|---|
| `test_stale_escalate_at_cap` | `3,5,0,2,1,1` | `escalate` |
| `test_stale_escalate_above_cap` | `5,5,0,2,1,1` | `escalate` |
| `test_stale_redispatch_below_cap` | `2,5,0,2,1,1` | `redispatch` |
| `test_stale_trigger_ci_no_runs` | `0,0,0,0,1,1` | `trigger-ci` |
| `test_stale_trigger_ci_beats_converge_label` | `1,0,1,3,0,1` | `trigger-ci` |
| `test_stale_mark_ready_with_converge` | `0,5,1,2,1,1` | `mark-ready` |
| `test_stale_mark_ready_ignores_failing` | `2,3,1,10,0,1` | `mark-ready` |
| `test_stale_mark_ready_and_converge` | `0,5,0,0,1,1` | `mark-ready-and-converge` |
| `test_stale_mark_ready_and_converge_no_issue` | `0,5,0,0,0,1` | `mark-ready-and-converge` |
| `test_stale_redispatch_failing_with_issue` | `0,5,0,3,1,1` | `redispatch` |
| `test_stale_needs_human_failing_no_issue` | `0,5,0,3,0,1` | `needs-human` |
| `test_stale_empty_pr_redispatch_with_converge` | `0,5,1,0,1,0` | `redispatch` |
| `test_stale_empty_pr_redispatch_ci_green` | `0,5,0,0,1,0` | `redispatch` |
| `test_stale_empty_pr_needs_human_no_issue` | `0,5,1,0,0,0` | `needs-human` |
| `test_stale_non_empty_regression_guard` | `0,5,1,0,1,1` | `mark-ready` |
| `test_stale_cap_beats_empty_pr` | `3,5,1,0,1,0` | `escalate` |
| `test_stale_trigger_ci_beats_empty_pr` | `0,0,1,0,1,0` | `trigger-ci` |
| `test_stale_usage_error` | (wrong arg count) | usage error |

Priority guard: empty-PR (`has_diff=0`) → redispatch/needs-human, but checked after
escalate (row 1) and trigger-ci (row 2). `redispatch_count == RECONCILER_STALE_REDISPATCH_CAP
- 1` (2) must not escalate; `3` must. **Minimum: 18.**

### §2.7 `decide_rearm_action` — `SPEC.md §8.6`

| Test name | Args (`ci_runs, converge_state, has_terminal, seconds`) | Expected |
|---|---|---|
| `test_rearm_trigger_ci_no_runs` | `0,none:none,0,""` | `trigger-ci` |
| `test_rearm_trigger_ci_wins_over_done` | `0,completed:success,1,600` | `trigger-ci` |
| `test_rearm_skip_in_progress` | `5,in_progress:,0,""` | `skip-in-progress` |
| `test_rearm_skip_queued` | `5,queued:,0,""` | `skip-in-progress` |
| `test_rearm_skip_done` | `5,completed:success,1,600` | `skip-done` |
| `test_rearm_skip_done_beats_recency` | `5,completed:success,1,50` | `skip-done` |
| `test_rearm_skip_recent_no_terminal` | `5,completed:success,0,100` | `skip-recent` |
| `test_rearm_skip_recent_zero_seconds` | `5,completed:success,0,0` | `skip-recent` |
| `test_rearm_skip_recent_boundary_minus_one` | `5,completed:success,0,299` | `skip-recent` |
| `test_rearm_rearm_at_boundary` | `5,completed:success,0,300` | `rearm` |
| `test_rearm_rearm_above_boundary` | `5,completed:success,0,9000` | `rearm` |
| `test_rearm_rearm_none_none` | `5,none:none,0,""` | `rearm` |
| `test_rearm_rearm_empty_seconds` | `5,completed:success,0,""` | `rearm` |
| `test_rearm_rearm_completed_failure` | `5,completed:failure,0,""` | `rearm` |
| `test_rearm_usage_error` | (wrong arg count) | usage error |

`seconds == 299` → `skip-recent`; `== 300` → `rearm` (strictly `< 300`). `queued:`
is treated as in-progress. `has_terminal == 1` beats recency. **Minimum: 15.**

### §2.8 `decide_conflict_action` — `SPEC.md §8.7`

| Test name | `mergeable` | `already_needs_human` | Expected |
|---|---|---|---|
| `test_conflict_escalate` | `"CONFLICTING"` | `0` | `escalate` |
| `test_conflict_skip_mergeable` | `"MERGEABLE"` | `0` | `skip` |
| `test_conflict_skip_unknown` | `"UNKNOWN"` | `0` | `skip` |
| `test_conflict_skip_empty_string` | `""` | `0` | `skip` |
| `test_conflict_skip_already_labeled_one` | `"CONFLICTING"` | `1` | `skip` |
| `test_conflict_skip_already_labeled_many` | `"CONFLICTING"` | `5` | `skip` |
| `test_conflict_usage_error` | (wrong arg count) | — | usage error |

Only exact `"CONFLICTING"` triggers escalation. `already_needs_human >= 1` always skips.
**Minimum: 7.**

### §2.9 `decide_redispatch_action` — `SPEC.md §8.8`

| Test name | `has_open_pr, seconds_since, redispatch_count` | Expected |
|---|---|---|
| `test_redispatch_skip_has_pr` | `1,600,0` | `skip-has-pr` |
| `test_redispatch_skip_has_pr_beats_cap` | `1,9999,5` | `skip-has-pr` |
| `test_redispatch_skip_recent` | `0,100,0` | `skip-recent` |
| `test_redispatch_skip_recent_zero` | `0,0,0` | `skip-recent` |
| `test_redispatch_skip_recent_boundary_minus_one` | `0,899,0` | `skip-recent` |
| `test_redispatch_redispatch_at_boundary` | `0,900,0` | `redispatch` |
| `test_redispatch_redispatch_above_boundary` | `0,1200,0` | `redispatch` |
| `test_redispatch_escalate_at_cap` | `0,9999,3` | `escalate` |
| `test_redispatch_escalate_above_cap` | `0,9999,7` | `escalate` |
| `test_redispatch_redispatch_zero_count` | `0,9999,0` | `redispatch` |
| `test_redispatch_redispatch_below_cap` | `0,9999,2` | `redispatch` |
| `test_redispatch_never_touched` | `0,"",0` | `redispatch` |
| `test_redispatch_never_touched_below_cap` | `0,"",2` | `redispatch` |
| `test_redispatch_usage_error` | (wrong arg count) | usage error |

`seconds_since == 899` → `skip-recent`; `== 900` → `redispatch` or `escalate` (strictly
`< 900`). `None` skips recency guard entirely. **Minimum: 14.**

### §2.10 `pipeline_health` — `SPEC.md §8.9`

Async; uses fake `ForgePort`.

| Test name | PR fixture | Expected verdict |
|---|---|---|
| `test_health_on_track_empty` | `[]` | `ON_TRACK` |
| `test_health_on_track_mixed` | 1 impl + 1 conv + 2 ready | `ON_TRACK` (`in_flight=2`) |
| `test_health_blocked` | 1 needs-human + 1 ready | `BLOCKED` |
| `test_health_at_risk_3_plus_2` | 3 impl + 2 conv | `AT_RISK` (`in_flight=5`) |
| `test_health_blocked_beats_at_risk` | 1 needs-human + 3 impl + 2 conv | `BLOCKED` |
| `test_health_at_risk_4_plus_1` | 4 impl + 1 conv | `AT_RISK` (`in_flight=5`) |
| `test_health_on_track_four` | 4 impl + 0 conv | `ON_TRACK` (`in_flight=4`) |
| `test_health_usage_error` | `repo` arg absent | usage error |

Each test must verify all `HealthReport` fields: `implementing`, `converge`, `ready`,
`needs_human`, `stale_drafts`, `in_flight`, `report_md`. `needs_human > 0` always →
`BLOCKED` even when `in_flight >= AT_RISK_THRESHOLD`. **Minimum: 8.**

### §2.11 State Derivation Helpers — `SPEC.md §8.10`

**`derive_issue_state`:**

| Test name | `labels` | `closed` | Expected |
|---|---|---|---|
| `test_derive_issue_closed` | any | `true` | `CLOSED` |
| `test_derive_issue_escalated` | `{needs-human}` | `false` | `ESCALATED` |
| `test_derive_issue_queued_agent_work` | `{agent-work}` | `false` | `QUEUED` |
| `test_derive_issue_queued_default` | `{}` | `false` | `QUEUED` |
| `test_derive_issue_closed_beats_needs_human` | `{needs-human}` | `true` | `CLOSED` |
| `test_derive_issue_closed_beats_agent_work` | `{agent-work}` | `true` | `CLOSED` |

**`derive_pr_state`:**

| Test name | `labels` | `draft` | `merged` | `changed_files` | Expected |
|---|---|---|---|---|---|
| `test_derive_pr_merged` | any | any | `true` | any | `MERGED` |
| `test_derive_pr_escalated` | `{needs-human}` | `false` | `false` | 1 | `ESCALATED` |
| `test_derive_pr_approved` | `{agent:ready}` | `false` | `false` | 1 | `APPROVED` |
| `test_derive_pr_empty` | `{}` | `false` | `false` | 0 | `EMPTY` |
| `test_derive_pr_converging` | `{converge}` | `false` | `false` | 1 | `CONVERGING` |
| `test_derive_pr_building_implementing` | `{agent:implementing}` | `true` | `false` | 1 | `BUILDING` |
| `test_derive_pr_building_default` | `{}` | `true` | `false` | 1 | `BUILDING` |
| `test_derive_pr_merged_beats_needs_human` | `{needs-human}` | `false` | `true` | 1 | `MERGED` |
| `test_derive_pr_converging_requires_non_draft` | `{converge}` | `true` | `false` | 1 | `BUILDING` |

**Minimum: 15.**

### §2.12 `decide_specialists` — `SPEC.md §8.12`

**Base set (always-on):**

| Test name | `changed_paths` | `round` | Expected (must include) |
|---|---|---|---|
| `test_decide_specialists_empty_paths` | `[]` | 1 | `["engineering-security-engineer.md", "engineering-code-reviewer.md"]` exactly |
| `test_decide_specialists_unrelated_paths` | `["src/main.py"]` | 1 | base set; no routing additions |
| `test_decide_specialists_base_set_round2` | `[]` | 2 | base set |
| `test_decide_specialists_base_set_round3` | `[]` | 3 | base set |

**Routing additions:**

| Test name | `changed_paths` | Expected additions |
|---|---|---|
| `test_decide_specialists_auth_path` | `["auth/login.py"]` | security (already in base; no duplicate) |
| `test_decide_specialists_session_path` | `["session/manager.py"]` | security (already in base) |
| `test_decide_specialists_migrations_path` | `["db/migrations/001_users.sql"]` | `engineering-database-optimizer.md` |
| `test_decide_specialists_sql_path` | `["queries/users.sql"]` | `engineering-database-optimizer.md` |
| `test_decide_specialists_schema_path` | `["models/schema_v2.py"]` | `engineering-database-optimizer.md` |
| `test_decide_specialists_tsx_path` | `["src/components/Button.tsx"]` | `testing-accessibility-auditor.md` |
| `test_decide_specialists_css_path` | `["styles/main.css"]` | `testing-accessibility-auditor.md` |
| `test_decide_specialists_ui_path` | `["src/ui/panel.py"]` | `testing-accessibility-auditor.md` |
| `test_decide_specialists_api_path` | `["api/users.py"]` | `testing-api-tester.md` |
| `test_decide_specialists_routes_path` | `["routes/auth.py"]` | `testing-api-tester.md` |
| `test_decide_specialists_handlers_path` | `["handlers/webhook.py"]` | `testing-api-tester.md` |

**Deduplication and cap:**

| Test name | `changed_paths` | What to verify |
|---|---|---|
| `test_decide_specialists_security_not_duplicated` | `["auth/crypto.py", "src/main.py"]` | `engineering-security-engineer.md` appears exactly once |
| `test_decide_specialists_multi_routing_deduped` | `["api/users.py", "routes/auth.py"]` | `testing-api-tester.md` appears exactly once |
| `test_decide_specialists_cap_at_4` | `["db/schema.sql", "components/ui/Button.tsx", "api/routes.py"]` | result has exactly 4 entries; base set always present |
| `test_decide_specialists_cap_preserves_base` | paths matching all 3 non-base routing entries | base set (2) always present; only 2 of 3 routing additions |
| `test_decide_specialists_result_from_routing_only` | any | all `AgentRef` values ∈ `SPECIALIST_ROUTING` agent_refs ∪ `CONVERGE_REVIEW_BASE` |

**Minimum: 20.**

### §2.12a Pack-acquisition contract test

Build-time verification, not a unit test. Runs against the image or locally acquired pack.

| Test name | What to verify |
|---|---|
| `test_pack_acquisition_base_refs_present` | `engineering-security-engineer.md` and `engineering-code-reviewer.md` exist as flat files in `dest_dir` |
| `test_pack_acquisition_routing_refs_present` | `engineering-database-optimizer.md`, `testing-accessibility-auditor.md`, `testing-api-tester.md` exist in `dest_dir` |
| `test_pack_acquisition_flat_structure` | All `*.md` files in `dest_dir` are flat (no subdirectories) |
| `test_pack_acquisition_sha_pinned` | git HEAD of cloned pack matches `AgentPackConfig.pinned_ref` exactly |

**Minimum: 4.**

---

## §3 Port Contract Tests

### §3.1 The Fake-Implementation Pattern

Each port gets: (1) an **in-memory fake** in `tests/fakes/` with a call log, configurable
return values, fault injection, and a reset method; (2) a **shared contract test suite**
in `tests/contracts/` parameterized over the implementation under test. Any new adapter
(GitLab, Gitea) must pass the shared suite.

### §3.2 `ForgePort` Contract Suite — `SPEC.md §9.1`

| Test name | Method | What it asserts |
|---|---|---|
| `test_forge_get_issue_returns_correct_fields` | `get_issue` | Correct `ref`, `labels`, `closed` for pre-seeded issue |
| `test_forge_list_issues_filters_by_label` | `list_issues` | Returns only issues with the requested label |
| `test_forge_add_label_applies` | `add_label` | Label appears in subsequent `get_issue` |
| `test_forge_add_label_idempotent` | `add_label` | Adding a present label is a no-op |
| `test_forge_remove_label_removes` | `remove_label` | Label absent in subsequent `get_issue` |
| `test_forge_remove_label_idempotent` | `remove_label` | Removing an absent label is a no-op |
| `test_forge_create_pr_returns_ref` | `create_pr` | Returns `PRRef`; subsequent `get_pr` has `draft=true` and correct body |
| `test_forge_create_pr_closes_issue` | `create_pr` | Body contains `Closes #N` when `closes` is non-null |
| `test_forge_get_pr_all_fields` | `get_pr` | Returns `labels`, `draft`, `merged`, `body` correctly |
| `test_forge_list_prs_by_label` | `list_prs` | Returns only PRs with the requested label |
| `test_forge_list_prs_no_label` | `list_prs` | `label=null` returns all PRs matching `state` filter |
| `test_forge_set_pr_ready_converts_draft` | `set_pr_ready` | `draft == false` in subsequent `get_pr` |
| `test_forge_get_changed_files_returns_paths` | `get_changed_files` | Correct file path list for a known diff |
| `test_forge_get_changed_files_empty_pr` | `get_changed_files` | Empty list for a PR with no changes |
| `test_forge_get_check_runs_returns_runs` | `get_check_runs` | Correct `name` and `status` fields |
| `test_forge_get_mergeable_conflicting` | `get_mergeable` | Returns `"CONFLICTING"` for a conflict PR |
| `test_forge_get_mergeable_mergeable` | `get_mergeable` | Returns `"MERGEABLE"` for a clean PR |
| `test_forge_list_comments_returns_comments` | `list_comments` | Returns comments in chronological order |
| `test_forge_list_comments_since_filters` | `list_comments` | `since` excludes comments before cutoff |
| `test_forge_post_comment_appears_in_list` | `post_comment` | Appears in subsequent `list_comments` |
| `test_forge_create_review_approve` | `create_review` | Approve review recorded against PR |
| `test_forge_last_workflow_run_at_known` | `last_workflow_run_at` | Timestamp of most-recent completed run |
| `test_forge_last_workflow_run_at_never_ran` | `last_workflow_run_at` | Returns `null` when never ran for this PR |
| `test_forge_last_dispatch_run_at_known` | `last_dispatch_run_at` | Timestamp of most-recent completed dispatch run |
| `test_forge_last_dispatch_run_at_never` | `last_dispatch_run_at` | Returns `null` when no dispatch run has completed |

**Minimum: 25.**

### §3.3 `HarnessPort` Contract Suite — `SPEC.md §9.2`

| Test name | Method | What it asserts |
|---|---|---|
| `test_harness_dispatch_returns_handle` | `dispatch` | Returns non-null `RunHandle` |
| `test_harness_dispatch_records_params` | `dispatch` | Fake records `EntryParams` and `DispatchContext` |
| `test_harness_dispatch_does_not_block` | `dispatch` | Returns immediately without awaiting agent completion |
| `test_harness_trigger_ci_records_call` | `trigger_ci` | Call recorded with correct `PRRef` |
| `test_harness_trigger_workflow_records_name` | `trigger_workflow` | Call recorded with correct `name` and `inputs` |
| `test_harness_get_run_status_queued` | `get_run_status` | Returns `state="queued"` for newly-dispatched run |
| `test_harness_get_run_status_completed` | `get_run_status` | Returns `state="completed"` after fake completes run |
| `test_harness_get_run_status_failed` | `get_run_status` | Returns `state="failed"` when fake injects failure |

**Minimum: 8.**

### §3.4 `SessionPort` Contract Suite — `SPEC.md §9.3`

| Test name | Method | What it asserts |
|---|---|---|
| `test_session_list_runs_returns_summaries` | `list_runs` | Returns `RunSummary` list for a repo with known runs |
| `test_session_list_runs_since_filter` | `list_runs` | `since` excludes runs started before cutoff |
| `test_session_get_run_returns_detail` | `get_run` | Returns `RunDetail` with correct handle and fields |
| `test_session_stream_events_yields_in_order` | `stream_events` | Emitted events in chronological order |
| `test_session_cancel_transitions_state` | `cancel` | Subsequent `get_run` shows cancelled/failed state |
| `test_session_intervene_acknowledged` | `intervene` | No error raised; fake records injected message |

**Minimum: 6.**

### §3.5 Counter Derivation Helpers — `SPEC.md §8.2a`

These helpers use `ForgePort.list_comments` to reconstruct `redispatch_count` and
`retry_count` from durable forge comment history. Tests use the fake `ForgePort`.

| Test name | Setup | Expected |
|---|---|---|
| `test_derive_redispatch_count_zero` | issue has 0 comments with `ch=converge` marker | `0` |
| `test_derive_redispatch_count_from_markers` | issue has 3 comments, 2 contain `<!-- orchestrator:redispatch ch=converge -->` | `2` |
| `test_derive_redispatch_count_channel_isolation` | issue has `ch=converge` (×2) and `ch=orphan` (×1) markers; channel=`"converge"` | `2` (orphan not counted) |
| `test_derive_redispatch_count_stale_pr_channel` | PR has 3 `ch=stale-pr` markers | `3` |
| `test_derive_redispatch_count_orphan_channel` | issue has 3 `ch=orphan` markers | `3` |
| `test_derive_retry_count_zero` | PR has 0 `<!-- orchestrator:converge-retry -->` markers | `0` |
| `test_derive_retry_count_from_markers` | PR has 2 comments containing the converge-retry marker | `2` |
| `test_derive_retry_count_unrelated_comments` | PR has comments with no marker | `0` |

Key invariant: markers are counted independently per channel; a comment may contain only
one marker. Partial comment matches (e.g. wrong channel slug) must not be counted.

**Minimum: 8.**

---

## §4 Engine Integration Tests (Over Fakes)

Construct `Engine(forge=FakeForgePort(), harness=FakeHarnessPort(), session=FakeSessionPort())`
with fakes pre-seeded for each scenario. Assert on fake state (label log, call log), not
engine internals.

### §4.1 Intake Paths — `Engine.intake`

| Test name | Setup | Expected outcome |
|---|---|---|
| `test_intake_admit_allowlisted` | `allowlist=["alice"]`, issue by `"alice"` | `LABEL_TRIAGE` + `LABEL_AGENT_WORK`; `LABEL_AWAITING_PROMOTION` absent |
| `test_intake_queue_unlisted` | `allowlist=["alice"]`, issue by `"bob"` | `LABEL_TRIAGE` + `LABEL_AWAITING_PROMOTION`; `LABEL_AGENT_WORK` absent |
| `test_intake_gate_disabled` | `allowlist=[]`, any author | `LABEL_AGENT_WORK` added (admit); `LABEL_AWAITING_PROMOTION` absent |
| `test_intake_disabled_repo` | `intake_enabled=false`, `issues:opened` | `Engine.intake` not called; no labels added |
| `test_intake_human_promotion` | issue has `LABEL_AWAITING_PROMOTION`; operator adds `LABEL_AGENT_WORK` | `Engine.dispatch` fires (I2); harness dispatch called |
| `test_intake_label_order` | `allowlist=["alice"]`, issue by `"alice"` | `LABEL_TRIAGE` added before `LABEL_AGENT_WORK` (call log order) |

### §4.2 Dispatch Lifecycle

| Test name | Scenario | What it asserts |
|---|---|---|
| `test_dispatch_opens_draft_pr` | `issues:labeled` with `agent-work` | Draft PR with `Closes #N`; `LABEL_IMPLEMENTING` added |
| `test_dispatch_calls_harness` | same | `harness.dispatch` called with `model=claude-opus-4-8`, `max_turns=40` |
| `test_dispatch_comment_uses_sonnet` | `issue_comment` with `@claude` | `route_entry` returns Sonnet/30 params; harness called with those |
| `test_dispatch_redispatch_via_comment` | PR in BUILDING; `@claude` on issue | Second `harness.dispatch` with comment body in context |
| `test_dispatch_full_lifecycle` | QUEUED → dispatch → PR CONVERGING → APPROVED → merged | Label progression and one `harness.dispatch` call |

### §4.3 Converge Sub-Machine — `SPEC.md §5`

| Test name | Setup | Expected outcome |
|---|---|---|
| `test_converge_idempotency_gate_merged` | PR merged | Returns `MERGED` immediately; no reviewer dispatched |
| `test_converge_idempotency_gate_needs_human` | PR has `needs-human` | Returns `ESCALATED` immediately |
| `test_converge_idempotency_gate_approved` | PR has `agent:ready` | Returns `APPROVED` immediately |
| `test_converge_approve_round1` | R1: `blockers=0`, CI green | Returns `APPROVED`; `LABEL_READY` added; approving review posted |
| `test_converge_fix_r1_to_approve_r2` | R1: 1 blocker; R2: 0 blockers, CI green | Returns `APPROVED` after 2 rounds; fixer dispatched once |
| `test_converge_escalate_no_progress` | R1 and R2: same non-empty blocker signatures | Returns `ESCALATED`; `LABEL_NEEDS_HUMAN` added (E2) |
| `test_converge_escalate_cap_reached` | R3: blockers remain, `has_issue=false` | Returns `ESCALATED` (E5) |
| `test_converge_cap_reached_redispatch` | R3: blockers remain, `has_issue=true`, `redispatch_count=0` | `harness.dispatch` on issue; returns `CONVERGING` (P11) |
| `test_converge_protected_path_e1` | PR touches `.github/workflows/deploy.yml` | Returns `ESCALATED` before round 1; no reviewer (P6, E1) |
| `test_converge_empty_pr_redispatch` | `changed_files=0`, `has_issue=true`, `redispatch_count=0` | Re-dispatches issue; returns `BUILDING` (P15) |
| `test_converge_empty_pr_escalate_no_issue` | `changed_files=0`, `has_issue=false` | Returns `ESCALATED` (P16, E6) |
| `test_converge_empty_pr_escalate_cap` | `changed_files=0`, `has_issue=true`, `redispatch_count=2` | Returns `ESCALATED` (P16, E6 via cap) |
| `test_converge_no_verdict_retry` | R3: `blockers="unknown"`, `retry_count=0` | `harness.trigger_workflow` called; returns `CONVERGING` (P12) |
| `test_converge_no_verdict_escalate` | R3: `blockers="unknown"`, `retry_count=2` | Returns `ESCALATED` after retries exhausted (E3) |
| `test_converge_ci_red_recovers` | R3: `blockers=0`, CI red → re-trigger → **all 6** `BLOCKING_CI_CHECKS` green | Returns `APPROVED` (P9) |
| `test_converge_ci_red_escalates` | R3: `blockers=0`, CI red → re-trigger → all 6 checks still red | Returns `ESCALATED` (E4) |
| `test_converge_ci_red_docker_still_red_escalates` | R3: `blockers=0`, code checks 1–3 recover, Docker/Helm checks 4–6 still red | Returns `ESCALATED` (E4); partial recovery is not approved (OQ-1 regression guard) |
| `test_converge_nit_followup_issue` | R1 approve with nits | `forge.create_issue` called once with deduplicated nits |

### §4.4 Reconciler Channels RC-1..RC-4 — `SPEC.md §4`

| Test name | Setup | Expected outcome |
|---|---|---|
| `test_reconciler_rc1_stale_trigger_ci` | Draft PR, `ci_runs=0`, `last_dispatch > STALE_DRAFT_THRESHOLD_S` | `harness.trigger_ci` called |
| `test_reconciler_rc1_stale_mark_ready` | Draft PR, `has_converge=true`, CI ran, not empty | `forge.set_pr_ready` called |
| `test_reconciler_rc1_stale_redispatch` | Draft PR, `failing_count > 0`, `has_issue=true`, not empty | `harness.dispatch` called |
| `test_reconciler_rc1_stale_escalate` | Draft PR, `redispatch_count=3` | `LABEL_NEEDS_HUMAN` added (E8) |
| `test_reconciler_rc1_not_stale_skipped` | Draft PR, `last_dispatch < STALE_DRAFT_THRESHOLD_S` | No action |
| `test_reconciler_rc2_conflict_escalates` | PR `CONFLICTING`, `already_needs_human=0` | `LABEL_NEEDS_HUMAN` added (E7) |
| `test_reconciler_rc2_conflict_already_labeled` | PR `CONFLICTING`, has `needs-human` | No-op |
| `test_reconciler_rc2_mergeable_skip` | PR `MERGEABLE` | No-op |
| `test_reconciler_rc3_rearm_triggers` | Non-draft `converge` PR, `seconds_since_last_run >= 300` | `harness.trigger_workflow` called (P14) |
| `test_reconciler_rc3_skip_in_progress` | Non-draft `converge` PR, `in_progress:` | No-op |
| `test_reconciler_rc3_skip_done` | Non-draft `converge` PR, `completed:success`, has terminal label | No-op |
| `test_reconciler_rc3_trigger_ci_no_runs` | Non-draft `converge` PR, `ci_runs=0` | `harness.trigger_ci` called (P14) |
| `test_reconciler_rc4_redispatch_orphan` | `agent-work` issue, no open PR, `seconds_since >= 900`, `count=0` | `harness.dispatch` called (I3) |
| `test_reconciler_rc4_escalate_cap` | `agent-work` issue, no open PR, `count=3` | `LABEL_NEEDS_HUMAN` added (I4, E10) |
| `test_reconciler_rc4_skip_has_pr` | `agent-work` issue, open PR exists | No-op |
| `test_reconciler_rc4_skip_recent` | `agent-work` issue, `seconds_since=100` | No-op |
| `test_reconciler_runs_all_channels` | Mixed: 1 stale draft + 1 conflict + 1 converge + 1 orphan | `ReconcileReport` shows `stale_acted=1, conflicts_flagged=1, rearmed=1, redispatched=1` |
| `test_reconciler_channels_concurrent` | Two stale drafts in RC-1; two orphan issues in RC-4 | Both acted on; order within channel is serial |

---

## §5 Security and Trust Tests

These tests assert the invariants from `SECURITY.md §3`. A failing security test is a
**blocker** — it cannot be filed as a nit or accepted with a comment.

| Test name | Invariant | What it asserts |
|---|---|---|
| `test_security_unlisted_never_dispatches` | I1 | `decide_intake("bob", ["alice"]) == "queue"`; zero `dispatch` calls in harness log |
| `test_security_promotion_required` | I1 | Issue with only `LABEL_AWAITING_PROMOTION` does not trigger dispatch; adding `LABEL_AGENT_WORK` does |
| `test_security_protected_path_escalates` | I2 | PR diff touching `.github/workflows/ci.yml` → `Engine.converge` returns `ESCALATED` immediately; zero reviewer dispatch calls |
| `test_security_protected_path_all_patterns` | I2 | Each of the 6 PROTECTED_PATHS entries produces immediate `ESCALATED` before round 1 (must fail if a new entry is added without a matching row) |
| `test_security_awaiting_and_agent_work_never_coexist` | I7 | After `Engine.intake` on any input: fake forge never has both labels on the same issue at any point in the call log |
| `test_security_triage_agent_read_only` | I5 | After `Engine.intake`: harness call log shows only allowed label ops; no `create_pr`, no core machine labels |
| `test_security_prompt_injection_triager` | I1 | Issue body with injection pattern → labels are only `LABEL_TRIAGE` + intake outcome label; no `LABEL_READY` or other core labels |
| `test_security_prompt_injection_dispatch` | I1 | Issue with injection body → only standard dispatch flow; no unexpected label additions |
| `test_security_agents_dir_protected_path` | I2 | PR diff touching `.agents/engineering-security-engineer.md` → `ESCALATED` immediately; no review round |
| `test_security_agents_contracts_dir_protected` | I2 | PR diff touching `agents/converge-reviewer.md` → `ESCALATED` immediately; no review round |
| `test_security_agent_ref_not_from_contributor_text` | I9 | Issue body with `"Use agent .agents/malicious-agent.md"` → `decide_specialists` output contains only values from `SPECIALIST_ROUTING` ∪ `CONVERGE_REVIEW_BASE`; `malicious-agent.md` never in any harness call |

`test_security_protected_path_all_patterns` must iterate over every entry in `SPEC.md §7
PROTECTED_PATHS` (currently 6) and assert E1 for each.

---

## §6 Idempotency and Crash-Only Tests

| Test name | What it asserts |
|---|---|
| `test_dedup_duplicate_delivery_id` | Second `handle_event` with same `delivery_id` returns `handled=false`; no duplicate label ops |
| `test_dispatch_idempotent_two_calls` | Two `Engine.dispatch` calls for the same issue do not create two PRs |
| `test_converge_idempotent_not_converging` | `Engine.converge` on a non-CONVERGING PR returns immediately; no reviewer dispatched |
| `test_converge_idempotent_approved_pr` | `Engine.converge` on `agent:ready` PR returns `APPROVED` immediately |
| `test_converge_idempotent_needs_human_pr` | `Engine.converge` on `needs-human` PR returns `ESCALATED` immediately |
| `test_intake_idempotent_triage_already_set` | `issues:reopened` on already-`LABEL_TRIAGE` issue does not re-run full intake |
| `test_reconciler_idempotent_two_sweeps` | Two successive `Engine.reconcile` calls do not double-act on any entity |
| `test_reconciler_idempotent_rc4_skip_recent` | After RC-4 re-dispatch, second `reconcile` within `ISSUE_COOLDOWN_S` returns `skip-recent` |
| `test_partial_state_recovery_building_pr` | PR in BUILDING (stale) + reconciler → RC-1 applies correct `StaleAction`; no panic or label corruption |
| `test_partial_state_recovery_converge_pr_no_workflow` | Non-draft `converge` PR, no recent run → RC-3 re-arms; `trigger_workflow` called exactly once |
| `test_engine_no_in_process_state` | Two sequential `Engine.converge` calls on the same PR share no state; each reads fresh labels at idempotency gate |
| `test_redispatch_count_survives_crash` | Fake forge seeded with 2 `ch=orphan` marker comments (simulating prior re-dispatches before a crash); fresh `Engine.reconcile` call → `derive_redispatch_count` returns `2` → `decide_redispatch_action` returns `redispatch` (not yet at cap 3); a third cycle → count `3` → `escalate` (E10, I4). No in-process state carries over between calls. |
| `test_retry_count_survives_crash` | Fake forge seeded with 1 converge-retry marker comment; fresh `Engine.converge` on the same PR → `derive_retry_count` returns `1` → `NO_VERDICT_RETRY_CAP=2` not yet hit → re-arm triggered; second crash + recover with 2 markers → `retry_count=2` → `ESCALATED` (E3). |

---

## §7 Test Tooling and CI Gate

### §7.1 Directory Structure

```
tests/
  unit/
    test_decide_intake.py  test_route_entry.py  test_resolve_blockers.py
    test_decide_round.py   test_decide_cap_action.py  test_decide_stale_action.py
    test_decide_rearm_action.py  test_decide_conflict_action.py
    test_decide_redispatch_action.py  test_pipeline_health.py
    test_state_derivation.py  test_decide_specialists.py
  fakes/
    fake_forge_port.py  fake_harness_port.py  fake_session_port.py
  contracts/
    test_forge_port_contract.py  test_harness_port_contract.py  test_session_port_contract.py
    test_counter_derivation.py
  integration/
    test_intake.py  test_dispatch.py  test_converge.py  test_reconciler.py
  security/
    test_security.py
  idempotency/
    test_idempotency.py
  test_pack/
    test_pack_acquisition.py
```

### §7.2 CI Gate — `BLOCKING_CI_CHECKS` (`SPEC.md §7`)

| Check | Python | Rust |
|---|---|---|
| Unit tests (decision functions) | `pytest tests/unit/` | `cargo test --lib` |
| Contract tests (all three port suites) | `pytest tests/contracts/` | `cargo test --test contracts` |
| Integration tests (engine lifecycle) | `pytest tests/integration/` | `cargo test --test integration` |
| Security / trust tests | `pytest tests/security/` | `cargo test --test security` |
| Idempotency tests | `pytest tests/idempotency/` | `cargo test --test idempotency` |
| Typecheck | `mypy --strict src/` | `cargo check` |
| Lint | `ruff check src/ tests/` | `cargo clippy -- -D warnings` |

All seven are non-negotiable gates.

### §7.3 Coverage Enforcement

A CI step must walk every truth-table row in `SPEC.md §8` and `SPEC.md §8.11–§8.12`
and assert at least one named test exists for that row. If any row is uncovered, the step
fails with a report naming the uncovered rows. Adding a truth table row without adding a
test must be impossible to land.

---

## Appendix: Minimum Test Count Summary

| Section | Function / area | Min. cases |
|---|---|---|
| §2.1 | `decide_intake` | 9 |
| §2.2 | `route_entry` | 6 |
| §2.3 | `resolve_blockers` | 12 |
| §2.4 | `decide_round` | 22 |
| §2.5 | `decide_cap_action` | 7 |
| §2.6 | `decide_stale_action` | 18 |
| §2.7 | `decide_rearm_action` | 15 |
| §2.8 | `decide_conflict_action` | 7 |
| §2.9 | `decide_redispatch_action` | 14 |
| §2.10 | `pipeline_health` | 8 |
| §2.11 | State derivation helpers | 15 |
| §2.12 | `decide_specialists` | 20 |
| §2.12a | Pack acquisition | 4 |
| §3.2 | `ForgePort` contract | 25 |
| §3.3 | `HarnessPort` contract | 8 |
| §3.4 | `SessionPort` contract | 6 |
| §3.5 | Counter derivation helpers | 8 |
| §4.1 | Intake paths | 6 |
| §4.2 | Dispatch lifecycle | 5 |
| §4.3 | Converge sub-machine | 18 |
| §4.4 | Reconciler channels | 18 |
| §5 | Security / trust | 11 |
| §6 | Idempotency / crash-only | 13 |
| **Total** | | **~275** |

Floors, not ceilings. The coverage check enforces the floor automatically.
