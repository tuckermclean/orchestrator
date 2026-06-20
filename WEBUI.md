# WEBUI.md — Progressive Web Application Specification

**Version**: 1.0
**Date**: 2026-06-20
**Status**: Draft
**Depends on**: `API.md` 1.0, `ARCHITECTURE.md` 1.0, `STATE_MACHINE.md` 1.0, `THREAT_MODEL.md` 1.0

---

## §1 Overview

The orchestrator PWA is the primary operator interface for the autonomous SWE-agent
pipeline. It is a mobile-first, responsive, installable Progressive Web Application
served by the orchestrator process itself — no separate CDN or frontend server is required
for a standard deployment. The same binary that handles forge webhooks and drives the
agent pipeline serves the PWA static assets at `/` and the control-plane API at `/api/*`
(`ARCHITECTURE.md §5.1`).

The PWA speaks the same control-plane API as the CLI. Every `OrchestratorService` method
is reachable from the UI. The REST mapping for the base control-plane methods is
illustrative in `API.md §8.6`; the PWA-specific extensions are specified in §6 of this
document. The architectural role of the control-plane API and its relationship to the CLI
are described in `ARCHITECTURE.md §7`. The PWA is not a read-only dashboard: every
`Config` field, every `RepoConfig` field, and every `OrchestratorService` method must be
accessible from the UI. Operators should be able to stand up, configure, monitor, and
intervene in the full pipeline without touching the CLI.

The primary mobile use cases drive the design: triage approvals and escalation
acknowledgment on a phone, configuration and log inspection on desktop. The triage queue
screen is the most time-sensitive surface in the system; it must be one-tap operable and
must load within budget on a mobile connection.

Implementation technology is not mandated. Any modern PWA stack capable of producing a
web app manifest, a service worker, server-sent events consumption, and a responsive
layout is suitable.

---

## §2 Authentication and Authorization

### §2.1 Operator Authentication

All control-plane operations require operator authentication. The PWA authenticates via
`POST /api/auth` with operator credentials (username and password) and receives a
short-lived JWT or session cookie in response. Credentials are managed in the operator
accounts store (`ARCHITECTURE.md §4.2`); the credential secret key is configured via
`OPERATOR_SECRET_KEY` in the deployment secrets (`DEPLOYMENT.md §3.4`).

The JWT carries a standard expiry claim. Token refresh is performed silently by the
service worker before expiry via `POST /api/auth/refresh`, without interrupting the
operator's session. A refresh failure — for example, if the secret key rotates — redirects
to the login screen, preserving the current URL so the operator returns to the right
screen after re-authentication.

Multi-operator support is provided: each operator has their own account with independent
credentials. Accounts are managed through the Settings screen (§5.6). The system does not
implement RBAC in this version; all authenticated operators have full access to all
control-plane operations.

Unauthenticated requests to any `/api/*` endpoint return `401` and the PWA redirects to
the login screen (§5.7).

### §2.2 Authorization Model

All authenticated operators have full access. There is no role hierarchy in this
specification. Authorization for contributor-level actions — specifically, the
classification of non-allowlisted authors as `queue` vs. `admit` — is governed entirely
by `decide_intake` and the per-repo allowlist (`API.md §3.11`), not by PWA authentication.
The allowlist is operator-controlled configuration; contributors have no PWA access and
no path to modify it.

Config-CRUD endpoints require operator authentication. The allowlist stored in operator
configuration is write-protected: contributors cannot open a forge pull request to modify
it, and they have no access to the `/api/*` endpoints that write it (`THREAT_MODEL.md §4
I1`, `THREAT_MODEL.md §3 T7`).

---

## §3 PWA Technical Requirements

### §3.1 Installability

The PWA must ship a Web App Manifest with the following fields:

- `name`: full product name
- `short_name`: short name for home screen display (12 characters or fewer)
- `icons`: at minimum 192x192 and 512x512 PNG icons suitable for home screen and splash
  screen use
- `display`: `standalone` — hides browser chrome when installed
- `start_url`: `/`
- `theme_color`: matches the UI header color
- `background_color`: matches the splash screen background
- `orientation`: `any` — supports both portrait (mobile) and landscape (desktop/tablet)

The service worker must implement:

- Cache-first strategy for all static assets. Asset filenames must be content-addressed
  (hashed) so that cache-busting is automatic on deployment. Cached assets are considered
  valid indefinitely.
- Network-first strategy for all `/api/*` requests. API responses are never cached; the
  UI always reflects live state.
- Background sync for JWT refresh (§2.1).
- Web push event handling (§3.2).

HTTPS is required for service worker registration and push subscription. TLS is enforced
at the Kubernetes Ingress layer (`DEPLOYMENT.md §3.3`).

### §3.2 Web Push Notifications

The PWA uses VAPID-based web push to deliver time-sensitive alerts to operators. Push
subscriptions are managed per device in the Settings screen (§5.6). VAPID keys are
configured in the deployment secrets (`DEPLOYMENT.md §3.4`). The push subscription
endpoint path is `/push/` (`DEPLOYMENT.md §3.3`).

Three notification categories, each configurable independently:

1. **Escalation alerts** — any issue or PR that receives `LABEL_NEEDS_HUMAN`, covering
   all ten escalation causes E1 through E10 (`STATE_MACHINE.md §6`, `API.md §2
   EscalationCause`). These are the highest-priority alerts; an escalated entity requires
   human decision before the pipeline can advance.

2. **Promotion requests** — a new non-allowlisted issue lands in the triage queue with
   `LABEL_AWAITING_PROMOTION`. The operator must review and either promote or decline
   before any code-writing agent runs (`THREAT_MODEL.md §4 I1`).

3. **Merge-ready approvals** — a PR transitions to `APPROVED` state (labeled
   `LABEL_READY` = `agent:ready`). The pipeline has completed autonomous review; a human
   merge is the only remaining action.

Push notification payload structure:

```
{
  type:               "escalation" | "promotion" | "approval",
  repo:               "owner/name",
  issue_or_pr_number: int,
  title:              string,
  url:                string    // link to PWA screen for the entity
}
```

Notifications must include action buttons where the platform supports them (Android,
desktop Chrome): "Open" navigates to the relevant PWA screen; "Dismiss" closes the
notification without navigation.

### §3.3 Responsive Breakpoints

Three layout tiers govern the entire application:

**Mobile** (viewport width 640px and below):
- Single-column layout throughout
- Bottom navigation bar with five destinations (§4)
- All interactive elements at minimum 44x44px touch target
- Compressed information density — secondary metadata collapsed behind disclosure
- Full-width cards and list items
- Pull-to-refresh on list screens

**Tablet** (viewport width 641px to 1024px):
- Two-column layouts where the content benefits from it (dashboard card grid, repo
  list + edit panel)
- Bottom navigation bar or compact top bar, depending on orientation

**Desktop** (viewport width 1025px and above):
- Persistent sidebar navigation replacing the bottom bar
- Expanded table views with more visible columns
- Full log panels displayed inline rather than as overlays
- Multi-panel layouts (list + detail) for runs and repos

---

## §4 Navigation Structure

The application has five top-level destinations. On mobile these appear as a bottom
navigation bar with icon and label. On desktop they appear as a sidebar navigation rail.

1. **Dashboard** — Multi-repo pipeline health overview
2. **Triage** — Human gate for non-allowlisted issues awaiting promotion
3. **Repos** — Repo registry management and per-repo configuration
4. **Runs** — Active and recent harness run observation and control
5. **Settings** — Full operator configuration panel

The active destination is always visually indicated. Deep links (for example, a
notification tap that opens a specific run or PR) must resolve to the correct destination
with the correct detail state pre-loaded.

---

## §5 Screen Specifications

### §5.1 Dashboard

**Purpose**: At-a-glance multi-repo pipeline health. The first screen an operator sees
and the screen that surfaces when no specific entity requires attention.

**Data source**: `OrchestratorService.status()` returning `list<HealthReport>` per
enabled repo (`API.md §3.9`, `API.md §8.4`). Called on load and on pull-to-refresh.

**Content**:

Global summary bar at the top of the screen:
- Total in-flight runs across all repos (implementing + converge PRs combined)
- Total escalations pending human action across all repos (`needs-human` count)
- Last reconcile tick timestamp (most recent across all repos)
- "Reconcile now" button calling `POST /api/reconcile`

Per-repo health card, one per enabled repo:
- Repo name (`owner/name`)
- Pipeline health status badge: `ON_TRACK` (green), `AT_RISK` (amber), or `BLOCKED`
  (red), sourced from `HealthReport.verdict` (`API.md §2 HealthVerdict`,
  `DECISION_LOGIC.md §9`)
- Count row: in-flight runs, issues QUEUED (`agent-work` label), PRs CONVERGING
  (`converge` label), active escalations (`needs-human` count across all entities)
- Triage queue count: issues with `LABEL_AWAITING_PROMOTION` awaiting human review
- Last reconcile timestamp for this repo

The `BLOCKED` verdict (one or more `needs-human` entities) renders the card with a
prominent visual indicator. `AT_RISK` (in-flight count at or above
`AT_RISK_THRESHOLD = 5`, `API.md §2` Constants) renders an amber indicator. `ON_TRACK`
renders the card normally.

**Interactions**:
- Tap or click a repo card navigates to the Repo detail view (PR list, §5.3)
- Tap the escalations count on a card navigates to the Runs view filtered to escalated
  entities for that repo
- Tap the triage queue count navigates to the Triage screen filtered to that repo
- "Reconcile now" calls `POST /api/reconcile` and shows a brief in-progress indicator;
  card data refreshes on completion

**Mobile layout**:

```
┌──────────────────────────────────────────┐
│  In-flight: 3   Escalated: 1             │
│  Last reconcile: 2 min ago  [Reconcile]  │
├──────────────────────────────────────────┤
│  [ON_TRACK]  owner/repo-alpha            │
│  In-flight: 2   Converging: 2            │
│  Queued: 1   Escalated: 0                │
│  Triage queue: 0                         │
│  Last reconcile: 2 min ago               │
├──────────────────────────────────────────┤
│  [BLOCKED]   owner/repo-beta             │
│  In-flight: 1   Converging: 1            │
│  Queued: 3   Escalated: 1                │
│  Triage queue: 2                         │
│  Last reconcile: 2 min ago               │
└──────────────────────────────────────────┘
[ Dashboard ] [ Triage ] [ Repos ] [ Runs ] [ Settings ]
```

**Pull-to-refresh**: Re-calls `GET /api/status` for all repos. A loading indicator is
shown on mobile while the request is in flight.

**Empty state**: If no repos are registered, the card area shows a prompt to add a repo
via the Repos screen.

### §5.2 Triage Queue

**Purpose**: The human gate for non-allowlisted public issues. This is the most
time-sensitive screen in the application and the primary mobile use case. An operator
reviewing issues in the triage queue is fulfilling the invariant that no code-writing
agent runs without explicit human approval (`THREAT_MODEL.md §4 I1`).

Every promotion or decline action is recorded in the audit log, satisfying the audit
requirement in `THREAT_MODEL.md §4 I6`.

**Data source**: `GET /api/triage` returning issues with `LABEL_AWAITING_PROMOTION`
across all enabled repos. Each issue includes the triager's structured comment
(`ARCHITECTURE.md §3`, `agents/triager.md`).

**Content**:

Filter and sort controls:
- Filter by repo (all repos by default)
- Sort by: oldest first (default), newest first, risk level
- Count badge showing total issues awaiting review

Per-issue card:
- Repo name and issue number
- Issue title
- Author username, visually distinguished as not on the allowlist
- Time waiting in queue (relative: "2 hours ago")
- Triager summary: the structured plain-language summary posted by `Engine.intake`'s
  triager agent (`ARCHITECTURE.md §3.2`)
- Risk flags extracted by the triager
- Expand toggle showing the full issue body (collapsed by default on mobile)

**Interaction — one-tap actions on mobile**:

Three actions per issue, presented as clearly labeled buttons. On mobile these are
full-width with generous tap targets:

- **Promote** — Remove `LABEL_AWAITING_PROMOTION`, add `LABEL_AGENT_WORK`. This fires
  the `issues:labeled` webhook (label = `agent-work`), which routes to `Engine.dispatch`
  (transition I2 then P1 in `STATE_MACHINE.md §3`). An audit record is written.
  The issue is removed from the triage queue.

- **Decline** — Close the issue on the forge; optionally show a text field for a brief
  comment. An audit record is written. The issue is removed from the triage queue.

- **Defer** — Mark the issue as "reviewed, not decided" in local operator state only.
  No forge action; no label change. The issue remains in the triage queue but is
  visually de-emphasized. Deferred state persists only for the current operator session;
  a refresh restores the default sort.

After Promote or Decline, the card animates out and the next issue receives focus.

**Mobile layout**:

```
┌──────────────────────────────────────────┐
│ TRIAGE QUEUE                  2 pending  │
│ Filter: [All repos v]  Sort: [Oldest v]  │
├──────────────────────────────────────────┤
│ owner/repo-beta  #42          3 hours ago│
│ "Add dark mode support"                  │
│ by: external-contributor  (not listed)   │
│                                          │
│ Triager summary:                         │
│ Requests a UI theme toggle. Low risk.    │
│ No file path conflicts. Cosmetic change. │
│                                          │
│ Risk flags: none                         │
│                          [v Show issue]  │
│                                          │
│  [ PROMOTE ]   [ DECLINE ]   [ DEFER ]   │
├──────────────────────────────────────────┤
│ owner/repo-beta  #41          5 hours ago│
│ "Modify CI workflow to skip tests"       │
│ by: anonymous-user  (not listed)         │
│                                          │
│ Triager summary:                         │
│ Requests modification to CI workflow.    │
│ Touches .github/workflows/ci.yml.        │
│                                          │
│ Risk flags: PROTECTED PATH               │
│                          [v Show issue]  │
│                                          │
│  [ PROMOTE ]   [ DECLINE ]   [ DEFER ]   │
└──────────────────────────────────────────┘
[ Dashboard ] [Triage 2] [ Repos ] [ Runs ] [ Settings ]
```

Note: the second issue shown in the layout would trigger the E1 protected-path
short-circuit (`STATE_MACHINE.md §6`, `API.md §5.2`) if the resulting PR touches
`.github/workflows/**`. The triager's risk flags serve as an early advisory for the
operator reviewing it; the E1 gate in `Engine.converge` is the structural enforcement
regardless of the promotion decision (`THREAT_MODEL.md §4 I2`).

**Empty state**: "No issues awaiting promotion." This is the expected steady state for
a well-configured pipeline. The Triage navigation destination shows no badge when the
queue is empty.

**Notification integration**: A push notification for a new promotion request (§3.2)
navigates directly to this screen. The relevant issue is scrolled into view if it is
not already visible.

### §5.3 Repos

**Purpose**: Manage the repo registry and per-repo configuration. Every `RepoConfig`
field and every registry management method in `OrchestratorService` is accessible here
(`API.md §8.2 RepoConfig`, `API.md §8.4`).

**Subviews**:

**Repo list** — All registered repos in a list. Each row shows:
- Repo name (`owner/name`)
- Enabled toggle calling `pause_repo` (sets `enabled=false`) or `resume_repo` (sets
  `enabled=true`). A disabled repo is visually de-emphasized. Pausing does not
  unregister the repo or alter forge label state; the reconciler resumes correctly on
  re-enable (`API.md §8.5`).
- `intake_enabled` indicator showing whether the triage front-stage is active
- Allowlist size (count of usernames)
- "Edit" button opening the edit panel or navigating to the edit subview
- "Unregister" button with a confirmation prompt calling
  `DELETE /api/repos/:owner/:repo`

**Add repo** — A form collecting:
- Repository name in `owner/repo` format, validated as non-empty and correctly formatted
- `intake_enabled` toggle (default: on). When off, `Engine.intake` is bypassed entirely;
  issues with `LABEL_AGENT_WORK` dispatch normally without triage (`API.md §8.2`)
- Allowlist textarea, one GitHub username per line. An empty textarea disables the gate
  entirely so all authors auto-admit. A non-empty list enables default-deny for unlisted
  authors (`API.md §3.11`, `THREAT_MODEL.md §1.3`)
- `SwarmLimits` override section (collapsed by default): per-repo
  `max_concurrent_runs_per_repo` override; left blank to use the global default

Submit calls `POST /api/repos` → `register_repo(RepoConfig)`.

**Edit repo** — Same form as Add, pre-populated with current values. Submit calls
`PATCH /api/repos/:owner/:repo`. `register_repo` is an idempotent upsert
(`API.md §8.4`).

**Allowlist quick-add** — An inline text field on each repo card in the list allows an
operator to type a single GitHub username and add it to the allowlist without navigating
to the full edit form. Fires `PATCH /api/repos/:owner/:repo` immediately on submit.

Cross-reference: `API.md §8.2 RepoConfig` for the full field definitions and
`API.md §8.4` for `register_repo`, `unregister_repo`, `pause_repo`, `resume_repo`,
and `list_repos`.

### §5.4 PR / Converge Detail

**Purpose**: Deep inspection of a specific PR's converge lifecycle. Accessed from the
dashboard (via escalated counts), from the Runs list, or via a direct link in a push
notification.

**Data sources**:
- PR state: `GET /api/runs/:id` returning `RunDetail`, supplemented by forge PR
  metadata surfaced through the run detail API
- Converge round verdicts: `.converge-verdict-rN.json` files on the PR branch, surfaced
  through the run detail API
- Active run events: `GET /api/runs/:id/stream` (SSE, §5.5)

**Content**:

Header section:
- PR title, number, repo, and link to the forge PR page
- Current state badge: `BUILDING`, `CONVERGING`, `APPROVED`, `ESCALATED`, or `MERGED`
  (`API.md §2 PRState`, `STATE_MACHINE.md §2`)
- Draft flag
- Current CI status across the six blocking checks (`API.md §2 BLOCKING_CI_CHECKS`,
  `STATE_MACHINE.md §7`)
- Closing issue reference if present in the PR body

Converge rounds section (displayed for PRs that have entered or completed converge):

For each round R1, R2, R3 where a verdict file exists (`STATE_MACHINE.md §5`):
- Round number and outcome (fix / approve / escalate token)
- From `.converge-verdict-rN.json`: blocker count, suggestion count, nit list, and
  blocker signatures rendered as human-readable slugs
- Fixer dispatch record: whether a fixer agent was dispatched and the outcome
- Whether CI was green at decision time
- The `decide_round` decision token that resulted (`API.md §3.3`)

Active run section (when a run is in progress):
- Run handle, start time, and elapsed time
- "Stream log" button opening the SSE log panel (§5.5)
- "Cancel run" button calling `POST /api/runs/:id/cancel` with confirmation
- "Intervene" button opening a text input whose submission calls
  `POST /api/runs/:id/intervene` (delegates to `SessionPort.intervene`,
  `API.md §4.3`)

Escalation section (when state is `ESCALATED`):
- Escalation cause displayed with its code (E1 through E10) and a plain-language
  description of the trigger condition (`STATE_MACHINE.md §6`, `API.md §2
  EscalationCause`)
- Human action buttons:
  - "Open on forge" — link to the forge PR page for manual inspection and merge
  - "Re-queue" — removes `LABEL_NEEDS_HUMAN` and adds `LABEL_AGENT_WORK` to the
    linked issue, entering the re-dispatch path via
    `POST /api/triage/:owner/:repo/:issue/promote`. Visible only when a closing
    issue reference exists and the escalation cause supports re-queueing.
  - "Acknowledge / close" — closes the issue without re-queueing, for escalations
    that represent terminal failures

### §5.5 Runs

**Purpose**: Observe and manage all active and recent harness runs across repos. The
primary surface for operators monitoring pipeline activity or intervening in a run in
real time.

**Data source**: `GET /api/runs` returning `list<RunSummary>` (`API.md §8.4`,
`SessionPort` `API.md §4.3`).

**Run list**:

A table on desktop or a stacked card list on mobile, one entry per run. Columns or
fields:
- Run ID (abbreviated)
- Repo (`owner/name`)
- Issue or PR reference (linked)
- Run type: `dispatch`, `converge`, `intake`, or `triage`
- Status: `in-flight`, `completed`, `failed`, or `cancelled`
- Start time (absolute) and elapsed or total duration
- Harness identifier

Filter controls:
- By repo (multi-select)
- By status (in-flight / completed / failed / cancelled)
- By run type
- Time range (last 1h / 6h / 24h / 7d)

**Run detail view** (navigated to from any run row):

Log streaming:
- Live log tail via `GET /api/runs/:id/stream` (server-sent events delegating to
  `SessionPort.stream_events`, `API.md §4.3`)
- Auto-scroll to the latest line by default; a "Pause scroll" toggle allows the
  operator to inspect earlier output without losing the stream connection
- On mobile: single-column log view, monospace font, horizontal scroll for long lines

Run controls:
- "Cancel run" calls `POST /api/runs/:id/cancel`
  (`OrchestratorService.cancel_run`, `API.md §8.4`) with confirmation required.
  Cancellation does not alter forge label state; the entity remains in its
  last-written label state and the reconciler recovers it on the next cron tick
  (`API.md §4.3`, `API.md §6`).
- "Intervene" opens a text input whose "Send" button calls
  `POST /api/runs/:id/intervene` (`OrchestratorService.intervene_run`,
  `API.md §8.4`), injecting a message into the in-flight run if the harness
  supports mid-run injection. Does not alter forge label state.

Both controls are active only when the run status is `in-flight`. Completed, failed,
and cancelled runs display the final log in read-only mode.

### §5.6 Settings

**Purpose**: Full configuration control. Every `Config` field, `SwarmLimits` field,
notification setting, operator account, and secret indicator is managed here.

**Global swarm limits** — Form fields for the three `SwarmLimits` fields
(`API.md §8.2`):
- `max_concurrent_runs_global` — integer input, minimum 1; sane default 10
- `max_concurrent_runs_per_repo` — integer input, minimum 1; sane default 4
- `max_concurrent_reconciles` — integer input, minimum 1; sane default 4

"Save" calls `PATCH /api/config` with the updated `SwarmLimits`.

**Reconcile schedule** — Cron expression input field with a plain-English preview that
updates as the operator types (for example `*/15 * * * *` renders as "every 15
minutes"). Validates against standard five-field cron syntax before enabling Save.
Saves `Config.reconcile_cron` (`API.md §8.2`). Default: `*/15 * * * *`
(`API.md §2` Constants). A "Reconcile now" shortcut calls `POST /api/reconcile`
without changing the schedule.

`Config.dedup_window` (the `delivery_id` LRU ring buffer size, `API.md §8.2`) is
presented in an "Advanced" disclosure section for operators who need to tune it.

**Notifications** — Web push controls:
- Enable / disable push notifications for this device (toggle)
- When enabled: notification category checkboxes for escalations, promotion requests,
  and merge-ready approvals, corresponding to the three push types defined in §3.2
- "Send test notification" calls `POST /api/push/test`, delivering a test push to the
  current device immediately
- Device list showing all registered push subscriptions for the current operator
  account (device name derived from user agent, registration date); each entry has a
  "Remove" button calling `DELETE /api/push/subscribe`

Subscribing the current device calls `POST /api/push/subscribe` with the browser's
`PushSubscription` object.

**Secrets** — Handle indicators only. Secret values are never displayed in the UI
(`THREAT_MODEL.md §4 I3`, `THREAT_MODEL.md §3 T4`). This section shows a configured /
not-configured indicator for each of:
- Forge token
- Harness API key
- Webhook secret
- VAPID public/private key pair
- Operator secret key (`OPERATOR_SECRET_KEY`)

Each indicator links to `DEPLOYMENT.md §3.4` for rotation instructions. No secret
value is ever rendered, logged, or transmitted to the browser.

**Operators** — Operator account management:
- List of operator accounts: username, creation date, last login date
- "Add operator" form: username and initial password
- "Remove operator" with confirmation; cannot remove the last operator account
- "Change my password" form for the currently authenticated operator

All operator mutations require re-confirmation of the current operator's password before
the change is applied.

**About** — Informational panel:
- Orchestrator version and commit SHA
- Aggregate `pipeline_health` verdict across all repos
- Links to key specification documents: `ARCHITECTURE.md`, `THREAT_MODEL.md`,
  `API.md`, `STATE_MACHINE.md`, and this document
- Current time and server-reported time (to assist with timezone debugging)

### §5.7 Login Screen

The unauthenticated entry point. Displayed when the JWT is absent or expired and when
a `401` is received from any API request.

Content:
- Application name and logo or wordmark
- Username field
- Password field
- "Remember me" checkbox — when checked, the issued JWT carries an extended TTL
  (a reasonable default is 30 days for "remember me" versus 8 hours for a
  session-scoped token; the specific duration is deployment-configurable)
- "Sign in" button calling `POST /api/auth`

Error handling:
- Invalid credentials: display "Invalid credentials" with no additional detail. The
  error message must not distinguish between an unknown username and an incorrect
  password (`THREAT_MODEL.md §3 T7`).
- Network error: display "Unable to reach the server" with a retry button.

After successful authentication: redirect to the URL the operator was attempting to
reach, or to the Dashboard if no prior URL is recorded.

The login screen is the only screen accessible without authentication. All other routes
redirect here if the JWT is absent or invalid.

---

## §6 Control-Plane API Extensions

The base `OrchestratorService` method-to-HTTP mapping is illustrated in `API.md §8.6`.
The PWA requires the following additional endpoints. These are illustrative and
non-binding in the same spirit as `API.md §8.6`; they represent intent and surface,
not a mandated URL scheme.

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/auth` | Authenticate operator; issue JWT or session cookie |
| `POST` | `/api/auth/refresh` | Silently refresh a valid JWT before expiry |
| `GET` | `/api/repos` | `list_repos()` — all registered `RepoConfig` records |
| `POST` | `/api/repos` | `register_repo(cfg)` — create or upsert a repo |
| `PATCH` | `/api/repos/:owner/:repo` | Update `RepoConfig` fields (allowlist, enabled, intake_enabled, SwarmLimits override) |
| `DELETE` | `/api/repos/:owner/:repo` | `unregister_repo()` |
| `POST` | `/api/repos/:owner/:repo/pause` | `pause_repo()` — set `enabled=false` |
| `POST` | `/api/repos/:owner/:repo/resume` | `resume_repo()` — set `enabled=true` |
| `GET` | `/api/status` | `status(null)` — `HealthReport` for all enabled repos (`API.md §3.9`) |
| `GET` | `/api/runs` | `list_runs(repo?, status?, type?, since?)` |
| `GET` | `/api/runs/:id` | `get_run(handle)` — full `RunDetail` |
| `GET` | `/api/runs/:id/stream` | SSE stream of run events via `SessionPort.stream_events` (`API.md §4.3`) |
| `POST` | `/api/runs/:id/cancel` | `cancel_run(handle)` |
| `POST` | `/api/runs/:id/intervene` | `intervene_run(handle, message)` |
| `POST` | `/api/reconcile` | `reconcile_now(null)` — immediate reconcile for all repos |
| `POST` | `/api/reconcile/:owner/:repo` | `reconcile_now(repo)` — single-repo reconcile |
| `GET` | `/api/triage` | Issues with `LABEL_AWAITING_PROMOTION` across all enabled repos, with triager comment |
| `POST` | `/api/triage/:owner/:repo/:issue/promote` | Remove `LABEL_AWAITING_PROMOTION`, add `LABEL_AGENT_WORK`; write audit record (`THREAT_MODEL.md §4 I6`) |
| `POST` | `/api/triage/:owner/:repo/:issue/decline` | Close issue on forge; optional comment; write audit record (`THREAT_MODEL.md §4 I6`) |
| `GET` | `/api/config` | Full `Config` object, excluding all secret values (`API.md §8.2`) |
| `PATCH` | `/api/config` | Update `SwarmLimits`, `reconcile_cron`, and `dedup_window` |
| `GET` | `/api/operators` | List operator accounts (username, created-at, last-login; no password hashes) |
| `POST` | `/api/operators` | Add a new operator account |
| `DELETE` | `/api/operators/:id` | Remove an operator account; rejected if it is the last account |
| `POST` | `/api/operators/:id/password` | Change an operator's password; self only; requires current password confirmation |
| `POST` | `/api/push/subscribe` | Register a web push `PushSubscription` for the current device |
| `DELETE` | `/api/push/subscribe` | Unregister the current device's push subscription |
| `GET` | `/api/push/subscriptions` | List all push subscriptions for the current operator |
| `POST` | `/api/push/test` | Send a test push notification to the current device |

All endpoints except `POST /api/auth` require a valid JWT or session cookie.

The audit log entries written by the promote and decline endpoints must capture: operator
username, issue ref, action (`promote` or `decline`), timestamp, and the state of
`RepoConfig.allowlist` at the time of the decision. This satisfies
`THREAT_MODEL.md §4 I6`.

---

## §7 Accessibility and Performance

### §7.1 Accessibility

Minimum standard: WCAG 2.1 AA across all screens and interactive states.

Specific requirements:

- All interactive elements must have a minimum touch and click target of 44x44 pixels.
  This applies to icon-only buttons (cancel, dismiss, expand toggles), toggle switches,
  and action buttons on triage queue cards.

- Icon-only buttons must carry ARIA labels that describe the action, not the icon. A
  cancel button that shows only an X must carry `aria-label="Cancel run"`, not
  `aria-label="X"`.

- Focus management: when a modal or overlay opens (the Intervene dialog, the Decline
  comment field), focus must move to the first interactive element inside it. When the
  overlay closes, focus must return to the triggering element.

- High-contrast mode: the UI must render without loss of information when the OS or
  browser requests high contrast. Status badges (`ON_TRACK`, `AT_RISK`, `BLOCKED`)
  must not rely on color alone; each must include a text label, icon, or pattern
  alongside the color.

- Live regions: real-time updates (push notifications displayed in-app, run status
  changes, new triage queue entries arriving) must be announced to screen readers via
  ARIA live regions with appropriate politeness levels. Use `aria-live="polite"` for
  non-urgent updates and `aria-live="assertive"` for escalation alerts.

- Tables on the Runs screen must use proper `<table>` semantics with column headers,
  or equivalent ARIA roles when a custom implementation is used.

### §7.2 Performance

Time-to-interactive target for the Dashboard on a mobile device with a 3G connection
(approximately 1.6 Mbps): 3 seconds or less. This budget is achievable because:
- Static assets are cached by the service worker after the first install; subsequent
  loads serve from cache with no network round-trip
- The Dashboard requires one API call (`GET /api/status`) after JWT validation
- The application shell and navigation are rendered before the API response arrives

Achieving this budget requires:
- Content-addressed static assets cached with a long-lived `Cache-Control` header
- The application shell (navigation, card skeletons) rendered immediately on load before
  the API response arrives
- Skeleton placeholders shown while per-repo data is loading, rather than a blank
  screen or spinner blocking the full layout
- No blocking render-time requests beyond the single `GET /api/status`

API response caching: API responses must not be cached by the service worker or the
browser. Every `/api/*` request must go to the network. The UI must always reflect live
pipeline state.

Log streaming performance: the SSE stream for run logs (§5.5) on mobile must not block
the main thread. Long log lines should be rendered in a virtualized list when the log
exceeds a few hundred lines. Auto-scroll must not cause frame drops on mobile;
scheduling scroll updates via `requestAnimationFrame` or equivalent is recommended.
