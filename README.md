# orchestrator

Clean-room specification for a **forge-agnostic, harness-agnostic agent-orchestration
state machine** — the autonomous SWE-agent pipeline extracted from the `mirror`
reference implementation and defined precisely enough to re-implement (eventually as a
Python state machine) against any agent harness and any git forge.

> **Status:** spec only. No implementation yet. This first cut defines *what the
> machine is and does*, accurately and exhaustively. The abstraction layer
> (forge/harness/session adapters) and the implementation are deliberately out of
> scope for now.

## What this is

An autonomous pipeline moves two long-lived entities — a **Work Item** (issue) and a
**Change Set** (pull request) — from "filed" to "merged or escalated", driven by three
coordinating loops:

- **Dispatch** turns a queued Work Item into an implementing Change Set.
- **Converge** is a bounded 3-round Review→Fix loop that drives a Change Set to
  *approved* or *escalated*.
- **Reconciler** is an orthogonal supervisor (cron every 15 min) that detects and
  recovers stranded entities.

Entity state is encoded entirely in **forge labels** — there is no separate state store.
The decision logic is a set of small **pure functions** (originally bash scripts, each
backed by Vitest), which makes the whole machine portable and testable.

## The documents

| File | What it is |
|------|------------|
| [`STATE_MACHINE.md`](STATE_MACHINE.md) | The centerpiece. Entities, states, the full transition table, the converge sub-machine, the reconciler supervisor, the constants table, the reconciled escalation taxonomy, and Mermaid lifecycle diagrams. |
| [`DECISION_LOGIC.md`](DECISION_LOGIC.md) | The companion. Each of the 9 pure decision functions as an exhaustive input→output truth table, line-cited to source and matched row-for-row against the existing tests. This is the binding implementation contract for the port. |

Read `STATE_MACHINE.md` for the shape of the system; reach for `DECISION_LOGIC.md`
when you need the exact branching rule for a specific decision.

## Ground truth

Everything here is derived **only** from the live decision scripts, their Vitest tests,
the workflows, and the agent contracts in the `mirror` repo:

- `mirror/scripts/{dispatch,converge,reconciler,status,git}/*.sh`
- `mirror/tests/infra/*.test.ts` (~107 cases that enumerate the truth tables)
- `mirror/.github/workflows/{dispatch,pr-converge,agent-reconciler}.yml`
- `mirror/.agents/custom/*.md`

`mirror/ORCHESTRATION.md` was **not** used — it is stale (it documents a dead routing
path and undercounts the escalation taxonomy). Every claim in these docs is cited to a
script/workflow/contract `file:line`.

## Known issues surfaced during extraction

Two latent concerns in the reference implementation are documented faithfully (not
silently corrected) and await an owner decision — see `STATE_MACHINE.md` §10:

1. The `ci-red` recovery path re-checks only 3 of the 6 blocking CI checks, so a PR with
   red Docker/Helm checks can be auto-approved on that path.
2. `MAX_REDISPATCHES=2` is duplicated across three sites with nothing keeping them in sync.

## Roadmap (not yet started)

- Abstraction seams: forge port (GitHub / GitLab / Gitea), harness port
  (Claude Code / Codex / OpenCode), and a session-observability port (inspect and
  intervene in individual agent runs).
- Python implementation of the state machine + the decision functions.
