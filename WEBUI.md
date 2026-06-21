# WEBUI.md — Progressive Web Application Specification

---

## §1 Overview

The orchestrator PWA is the primary operator interface — a mobile-first, responsive,
installable Progressive Web Application served by the orchestrator process itself. The same
binary handles forge webhooks, drives the agent pipeline, and serves PWA static assets at
`/` and the control-plane API at `/api/*`.

The PWA speaks the same control-plane API as the CLI. Every `OrchestratorService` method
is reachable from the UI. Operators should be able to stand up, configure, monitor, and
intervene in the full pipeline without touching the CLI.

**Primary mobile use cases**: triage approvals and escalation acknowledgment on a phone.
**Desktop**: configuration and log inspection.

Implementation technology is not mandated. Any modern PWA stack capable of producing a
web app manifest, service worker, SSE consumption, and responsive layout is suitable.

---

## §2 Authentication and Authorization

Operators authenticate via `POST /api/auth` (username + password) and receive a JWT or
session cookie. The JWT carries a standard expiry claim. The service worker silently
refreshes before expiry via `POST /api/auth/refresh`; a refresh failure redirects to the
login screen, preserving the current URL.

All authenticated operators have full access. No RBAC in this version. Unauthenticated
requests to any `/api/*` endpoint return `401` and redirect to the login screen (§5.7).

Contributor-level access control (allowlist gate) is governed by `decide_intake` and the
per-repo allowlist (`SPEC.md §8.11`) — contributors have no PWA access and no path to
modify the allowlist.

---

## §3 PWA Technical Requirements

### §3.1 Installability

Web App Manifest required fields: `name`, `short_name` (≤12 chars), `icons` (192×192 and
512×512 PNG), `display: standalone`, `start_url: /`, `theme_color`, `background_color`,
`orientation: any`.

Service worker must implement:
- **Cache-first** for static assets (content-addressed filenames; cached indefinitely).
- **Network-first** for `/api/*` (API responses never cached; UI always reflects live
  state).
- Background JWT refresh.
- Web push event handling (§3.2).

HTTPS is required for service worker and push subscription. Enforced at the Kubernetes
Ingress layer (`ARCHITECTURE.md §6`).

### §3.2 Web Push Notifications

VAPID-based web push for time-sensitive operator alerts. Push subscriptions are per-device
and managed in the Settings screen (§5.6). Three categories, each configurable
independently:

1. **Escalation alerts** — any entity receiving `LABEL_NEEDS_HUMAN` (E1–E10,
   `SPEC.md §6`). Highest-priority.
2. **Promotion requests** — a new non-allowlisted issue lands in the triage queue with
   `LABEL_AWAITING_PROMOTION`.
3. **Merge-ready approvals** — a PR transitions to `APPROVED` / `LABEL_READY`.

Push notification payload:
```json
{
  "type": "escalation" | "promotion" | "approval",
  "repo": "owner/name",
  "issue_or_pr_number": 42,
  "title": "...",
  "url": "..."
}
```

Action buttons where the platform supports them (Android, desktop Chrome): "Open" →
relevant PWA screen; "Dismiss" → close without navigation.

### §3.3 Responsive Breakpoints

| Tier | Width | Layout |
|---|---|---|
| **Mobile** | ≤640px | Single-column; bottom navigation bar (5 items); ≥44×44px touch targets; pull-to-refresh |
| **Tablet** | 641–1024px | Two-column where useful; bottom bar or compact top bar |
| **Desktop** | ≥1025px | Persistent sidebar navigation; expanded table views; multi-panel list+detail |

---

## §4 Navigation Structure

Five top-level destinations (bottom bar on mobile, sidebar on desktop):

1. **Dashboard** — Multi-repo pipeline health overview
2. **Triage** — Human gate for non-allowlisted issues
3. **Repos** — Repo registry management
4. **Runs** — Harness run observation and control
5. **Settings** — Full operator configuration

Deep links (from notification taps) must resolve to the correct destination with the
correct detail state pre-loaded.

---

## §5 Screen Specifications

### §5.1 Dashboard

**Data source:** `GET /api/status` → `list<HealthReport>` per enabled repo (`SPEC.md §11`).
Called on load and pull-to-refresh.

**Global summary bar:**
- Total in-flight runs across all repos (implementing + converge PRs)
- Total escalations pending (`needs-human` count across all repos)
- Last reconcile timestamp
- "Reconcile now" button → `POST /api/reconcile`

**Per-repo health card:**
- Repo name (`owner/name`)
- Health badge: `ON_TRACK` (green) / `AT_RISK` (amber) / `BLOCKED` (red), from
  `HealthReport.verdict` (`SPEC.md §8.9`)
- Counts: in-flight, queued, converging, escalated, triage queue
- Last reconcile timestamp for this repo

`BLOCKED` (any `needs-human` entities) → prominent visual indicator. `AT_RISK`
(`in_flight >= AT_RISK_THRESHOLD = 5`) → amber. Badges must not rely on color alone
(include text label or icon).

**Interactions:** tap repo card → Repo detail; tap escalation count → Runs filtered to
escalated; tap triage queue count → Triage screen filtered to that repo.

**Empty state:** prompt to add a repo via the Repos screen.

### §5.2 Triage Queue

The human gate for non-allowlisted public issues. Every promotion or decline is
audit-logged (`SECURITY.md §3 I6`).

**Data source:** `GET /api/triage` → issues with `LABEL_AWAITING_PROMOTION` across all
enabled repos, each including the triager's structured comment (`ARCHITECTURE.md §3`).

**Filter and sort:** by repo (all by default); sort by oldest/newest/risk level; count
badge showing total pending.

**Per-issue card:**
- Repo name, issue number, title
- Author (visually flagged as not on allowlist), time in queue
- Triager summary and risk flags
- "Show issue" toggle for full issue body

**Three actions (full-width buttons on mobile, ≥44×44px):**
- **Promote** — Remove `LABEL_AWAITING_PROMOTION`, add `LABEL_AGENT_WORK`. Fires
  `issues:labeled agent-work` → `Engine.dispatch` (I2, P1). Writes audit record.
  Card exits queue.
- **Decline** — Close issue on forge; optional comment field. Writes audit record.
  Card exits queue.
- **Defer** — Local operator state only; no forge action. Issue remains but is
  visually de-emphasized. Deferred state resets on refresh.

After Promote or Decline, card animates out and next issue receives focus.

**Empty state:** "No issues awaiting promotion." Triage navigation destination shows no
badge when empty.

**Notification integration:** escalation push notification navigates directly to this
screen; relevant issue scrolled into view.

### §5.3 Repos

**Purpose:** manage the repo registry and per-repo configuration.

**Repo list:** one row per registered repo showing repo name, enabled toggle
(`pause_repo`/`resume_repo`), `intake_enabled` indicator, allowlist size, "Edit" and
"Unregister" (with confirmation) buttons.

**Add / Edit repo form:**
- Repository name (`owner/repo` format)
- `intake_enabled` toggle (default: on)
- Allowlist textarea — one GitHub username per line; empty = gate disabled
- `SwarmLimits` per-repo override (collapsed by default)

Submit → `POST /api/repos` (`register_repo`, idempotent upsert). Edit → `PATCH
/api/repos/:owner/:repo`.

**Allowlist quick-add:** inline text field on each repo card for single-username addition
without navigating to the full edit form.

### §5.4 PR / Converge Detail

**Data sources:**
- PR state: `GET /api/runs/:id` → `RunDetail`
- Converge round verdicts: `.converge-verdict-rN.json` from the PR branch, surfaced
  through run detail API
- Active run events: `GET /api/runs/:id/stream` (SSE)

**Header:** PR title, number, repo, link to forge PR page, state badge (BUILDING /
CONVERGING / APPROVED / ESCALATED / MERGED), draft flag, CI status (6 blocking checks),
closing issue reference.

**Converge rounds:** for each R1/R2/R3 where a verdict file exists: round number,
outcome token, blocker/suggestion/nit counts, blocker signatures, fixer dispatch record,
CI status at decision time, `decide_round` token.

**Active run section:** run handle, start time, elapsed; "Stream log" (opens SSE panel);
"Cancel" → `POST /api/runs/:id/cancel` (confirmation required); "Intervene" → text input
→ `POST /api/runs/:id/intervene`. Controls active only when status is `in-flight`.

**Escalation section:** escalation cause code (E1–E10) with plain-language description;
"Open on forge", "Re-queue" (removes `LABEL_NEEDS_HUMAN`, adds `LABEL_AGENT_WORK` on
linked issue, only when closing issue exists and cause supports re-queueing), "Acknowledge
/ close", **"Resume"** (shown when the PR carries `converge` OR `agent:implementing` label
— i.e. P16/P17 eligible by label state; calls
`POST /api/prs/:owner/:repo/:number/deescalate` → `OrchestratorService.deescalate_pr`;
removes `LABEL_NEEDS_HUMAN` from the PR, resets stale-pr and converge-retry counters, and
writes an audit record; the reconciler recovers the PR on its next tick (P16 via RC-3) or
within `STALE_DRAFT_THRESHOLD_S` (P17 via RC-1); requires operator confirmation.
_Note:_ For E1 (protected-path) escalations, the Engine re-checks PROTECTED_PATHS on
converge re-entry and immediately re-escalates if the change is still present.).

Cancellation and intervention do not alter forge label state; the entity remains in its
last-written state and the reconciler recovers it on the next tick.

### §5.5 Runs

**Data source:** `GET /api/runs` → `list<RunSummary>`.

**Run list:** one entry per run, table on desktop or stacked cards on mobile. Fields:
run ID (abbreviated), repo, issue/PR reference (linked), run type (`dispatch`/`converge`/
`intake`/`triage`), status, start time, elapsed/total duration, harness ID.

**Filters:** by repo (multi-select), by status, by run type, by time range (1h/6h/24h/7d).

**Run detail view:**
- Live log tail via SSE (`GET /api/runs/:id/stream` → `SessionPort.stream_events`).
  Auto-scroll with "Pause scroll" toggle. On mobile: monospace font, horizontal scroll
  for long lines; virtualized list when log exceeds a few hundred lines.
- "Cancel" → `POST /api/runs/:id/cancel` (confirmation required).
- "Intervene" → `POST /api/runs/:id/intervene` (mid-run message injection).
- Both controls active only for `in-flight` runs.

### §5.6 Settings

**Global swarm limits:** form fields for `SwarmLimits` (`SPEC.md §11`):
`max_concurrent_runs_global` (default 10), `max_concurrent_runs_per_repo` (default 4),
`max_concurrent_reconciles` (default 4). "Save" → `PATCH /api/config`.

**Reconcile schedule:** cron expression input with plain-English preview. Validates
5-field cron syntax. Saves `Config.reconcile_cron`. Default: `*/15 * * * *`.
"Reconcile now" shortcut → `POST /api/reconcile`.

`Config.dedup_window` in an "Advanced" disclosure section.

**Notifications:** web push enable/disable toggle; category checkboxes (escalations,
promotion requests, merge-ready approvals); "Send test notification" → `POST
/api/push/test`; device list with "Remove" per subscription.

**Secrets:** configured/not-configured indicators only — secret values never displayed,
logged, or transmitted to the browser. Indicators for: forge token, harness API key,
webhook secret, VAPID key pair, operator secret key.

**Operators:** list with username / creation date / last login; "Add operator" form;
"Remove" with confirmation (cannot remove last account); "Change my password" (requires
current password confirmation).

**About:** version + commit SHA; aggregate `pipeline_health` across all repos; links to
key spec documents.

### §5.7 Login Screen

Unauthenticated entry point. Shown when JWT is absent, expired, or when `401` is received.

Fields: username, password, "Remember me" checkbox (extended JWT TTL; default 30 days vs.
8 hours for session-scoped). Error message: "Invalid credentials" with no distinction
between unknown username and wrong password. Network error: "Unable to reach the server"
with retry button.

After successful auth: redirect to original URL or Dashboard.

---

## §6 Control-Plane API Endpoints

All endpoints except `POST /api/auth` require a valid JWT or session cookie.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/auth` | Authenticate; issue JWT |
| `POST` | `/api/auth/refresh` | Silently refresh JWT |
| `GET` | `/api/repos` | List all registered `RepoConfig` records |
| `POST` | `/api/repos` | Create/upsert a repo (`register_repo`) |
| `PATCH` | `/api/repos/:owner/:repo` | Update `RepoConfig` fields |
| `DELETE` | `/api/repos/:owner/:repo` | Unregister repo |
| `POST` | `/api/repos/:owner/:repo/pause` | Set `enabled=false` |
| `POST` | `/api/repos/:owner/:repo/resume` | Set `enabled=true` |
| `GET` | `/api/status` | `HealthReport` for all enabled repos |
| `GET` | `/api/runs` | List runs (filters: repo, status, type, since) |
| `GET` | `/api/runs/:id` | Full `RunDetail` |
| `GET` | `/api/runs/:id/stream` | SSE stream via `SessionPort.stream_events` |
| `POST` | `/api/runs/:id/cancel` | Cancel in-flight run |
| `POST` | `/api/runs/:id/intervene` | Inject message into in-flight run |
| `POST` | `/api/reconcile` | Immediate reconcile for all repos |
| `POST` | `/api/reconcile/:owner/:repo` | Single-repo reconcile |
| `GET` | `/api/triage` | Issues with `LABEL_AWAITING_PROMOTION` across all repos |
| `POST` | `/api/triage/:owner/:repo/:issue/promote` | Promote issue (remove `AWAITING_PROMOTION`, add `AGENT_WORK`; write audit record) |
| `POST` | `/api/triage/:owner/:repo/:issue/decline` | Close issue; optional comment; write audit record |
| `POST` | `/api/prs/:owner/:repo/:number/deescalate` | Remove `LABEL_NEEDS_HUMAN` from PR; reset stale-pr and converge-retry counters; write audit record with escalation cause + label snapshot; reconciler recovers on next tick (P16/P17) |
| `GET` | `/api/config` | Full `Config` (no secret values) |
| `PATCH` | `/api/config` | Update `SwarmLimits`, `reconcile_cron`, `dedup_window` |
| `GET` | `/api/operators` | List operator accounts (no password hashes) |
| `POST` | `/api/operators` | Add operator account |
| `DELETE` | `/api/operators/:id` | Remove operator (rejected if last account) |
| `POST` | `/api/operators/:id/password` | Change password (self only; requires current password) |
| `POST` | `/api/push/subscribe` | Register push subscription for current device |
| `DELETE` | `/api/push/subscribe` | Unregister current device |
| `GET` | `/api/push/subscriptions` | List all subscriptions for current operator |
| `POST` | `/api/push/test` | Send test push to current device |

Promote and decline audit records must capture: operator username, issue ref, action,
timestamp, and `RepoConfig.allowlist` state at decision time (satisfies `SECURITY.md §3
I6`).

---

## §7 Accessibility and Performance

### §7.1 Accessibility

Minimum: WCAG 2.1 AA across all screens and interactive states.

- All interactive elements: minimum 44×44px touch/click target.
- Icon-only buttons: ARIA label describing the action (`aria-label="Cancel run"`,
  not `aria-label="X"`).
- Focus management: when a modal opens, focus moves to the first interactive element
  inside; when it closes, focus returns to the triggering element.
- High-contrast mode: no information loss; status badges must not rely on color alone
  (text label or icon alongside color).
- Live regions: real-time updates (push notifications in-app, run status changes, new
  triage entries) announced via ARIA live regions. `aria-live="polite"` for non-urgent;
  `aria-live="assertive"` for escalation alerts.
- Tables on the Runs screen: proper `<table>` semantics or equivalent ARIA roles.

### §7.2 Performance

Time-to-interactive target on mobile with 3G (~1.6 Mbps): **3 seconds or less**.

Requirements:
- Content-addressed static assets cached by service worker after first install.
- Application shell (navigation, card skeletons) rendered before API response arrives.
- Skeleton placeholders while per-repo data loads; no blank-screen block.
- Dashboard requires one API call (`GET /api/status`) after JWT validation.
- API responses not cached — every `/api/*` request goes to the network.
- Log streaming (§5.5): SSE rendering must not block the main thread; virtualize when
  log exceeds a few hundred lines; auto-scroll via `requestAnimationFrame` or equivalent.
