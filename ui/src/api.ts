// Typed fetch wrappers for all API endpoints + SSE helper

import { clearToken, getToken, refreshToken } from "./auth";

export interface HealthReport {
  implementing: number;
  converge: number;
  ready: number;
  needs_human: number;
  stale_drafts: number;
  in_flight: number;
  report_md: string;
  verdict: "BLOCKED" | "AT_RISK" | "ON_TRACK";
}

export interface RunSummary {
  run_id: string;
  repo: { owner: string; name: string };
  type: string;
  status: string;
  started_at: string;
  completed_at: string | null;
  model?: string | null;
  // ISO-8601 UTC timestamp at which the session/usage quota resets.
  // Non-null only when status === "awaiting_quota".
  quota_reset_at?: string | null;
}

export interface RunEvent {
  event_type: string;
  data: Record<string, unknown>;
  timestamp: string;
}

// Map a raw dispatch model id to a short friendly label for display.
// Unmapped / empty / null ids fall back to the raw string (or "" for null),
// so the UI never crashes on an unknown or missing model.
export function modelLabel(model: string | null | undefined): string {
  if (!model) return "";
  if (model === "claude-opus-4-8") return "Opus 4.8";
  if (model === "claude-sonnet-4-6") return "Sonnet 4.6";
  if (model.startsWith("claude-haiku-4-5")) return "Haiku 4.5";
  return model;
}

export interface RunDetail extends RunSummary {
  events: RunEvent[];
  pr_ref?: { repo: { owner: string; name: string }; number: number } | null;
  issue_ref?: { repo: { owner: string; name: string }; number: number } | null;
  build_status?: string | null;
  changed_files?: number | null;
  // Inherited from RunSummary: quota_reset_at?: string | null
}

// Converge detail (WEBUI.md §5.4) — per-round verdicts, specialists, CI grid.

export interface ConvergeVerdict {
  blockers: number;
  suggestions: number;
  nits: string[];
  blocker_signatures: string[];
}

export interface ConvergeCiCheck {
  name: string;
  state: string;
  conclusion: string | null;
}

export interface ConvergeFixerRun {
  /** run_id of the harness fixer dispatch for this round, null if fixer timed out */
  run_id: string | null;
  model: string;
  timed_out: boolean;
}

export interface ConvergeRound {
  round: 1 | 2 | 3;
  model: string;
  decide_round_token: string;
  specialists: string[];
  verdict: ConvergeVerdict;
  ci_checks: ConvergeCiCheck[];
  /** Fixer run details — present for rounds where a fixer was dispatched (R1, R2 with fix token) */
  fixer_run: ConvergeFixerRun | null;
}

export interface ConvergeDetail {
  pr_ref: { repo: { owner: string; name: string }; number: number };
  pr_title: string;
  state: string;
  /** SPEC §6 escalation code (E2–E11) when the PR is ESCALATED, null otherwise */
  escalation_cause: string | null;
  rounds: ConvergeRound[];
}

export interface EscalationSummary {
  pr_number: number;
  labels: string[];
  title: string;
  cause: string;
}

export interface ReconcileReport {
  stale_acted: number;
  conflicts_flagged: number;
  rearmed: number;
  redispatched: number;
  escalated: number;
}

// Mirrors the backend TriageItem (src/domain/types.py) exactly.
export interface TriageItem {
  issue_ref: { repo: { owner: string; name: string }; number: number };
  title: string;
  body: string;
  author: string;
  labels: string[];
  queued_at: string;
}

export interface RepoSummary {
  owner: string;
  name: string;
  enabled: boolean;
  intake_enabled: boolean;
}

export interface OperatorRecord {
  id: string;
  created_at: string;
  last_login: string | null;
}

export interface PushSubscriptionRecord {
  endpoint: string;
  created_at: string;
}

const BASE = "";

/** 401 redirect target — redirect to /login, preserving current URL */
function handle401(): never {
  clearToken();
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  window.location.href = `/login?next=${next}`;
  throw new Error("Redirecting to login");
}

/**
 * Auth endpoints that must NOT trigger re-auth redirect on failure — they
 * surface their own error messages (e.g. "Invalid credentials").
 */
const AUTH_ENDPOINTS = ["/api/auth/login", "/api/auth/refresh"];

/**
 * Classify a fetch Response as an auth-expiry signal.
 *
 * Covers all the ways an authentik ForwardAuth proxy can indicate a session
 * has expired, not just the plain HTTP 401 the app's own backend emits:
 *
 *   - 401 — app JWT expired (existing case)
 *   - 503 — proxy returned a gateway error while the session was being
 *            re-established; seen in practice as the symptom that sent users
 *            to the manual logout/login recovery loop
 *   - opaqueredirect — fetch(redirect:"manual") made the ForwardAuth 302
 *            surface as a type="opaqueredirect" response instead of being
 *            silently followed to the login HTML page
 *   - resp.redirected — browser followed a redirect before we could catch it
 *            with redirect:"manual" (defensive; should not occur with the flag)
 *   - HTML content-type on an /api/ path — the HTML login page leaked through
 *            (e.g. redirect:"manual" not honoured in some browsers, or a proxy
 *            returned 200 with a login page body)
 *
 * Auth endpoints themselves are excluded so login/refresh failures surface
 * their own errors rather than bouncing to the login page in a loop.
 */
function isAuthExpiry(resp: Response, path: string): boolean {
  if (AUTH_ENDPOINTS.some((ep) => path.startsWith(ep))) return false;
  if (resp.status === 401) return true;
  if (resp.status === 503) return true;
  if (resp.type === "opaqueredirect") return true;
  if (resp.redirected) return true;
  if (
    path.startsWith("/api/") &&
    !resp.ok &&
    (resp.headers.get("content-type") ?? "").startsWith("text/html")
  ) {
    return true;
  }
  return false;
}

/**
 * Single in-flight refresh promise shared across all concurrent callers.
 *
 * When multiple requests 401 simultaneously (e.g. on Dashboard mount), they
 * all race into authFetch. Without this guard each caller would independently
 * call refreshToken(), potentially hammering the auth endpoint and racing the
 * token store. Instead:
 *   - The first 401 creates the refresh promise and stores it here.
 *   - Subsequent 401s await the same promise rather than starting a new one.
 *   - Once resolved (success or failure) the slot is cleared so the next
 *     expiry cycle starts fresh.
 */
let _refreshInFlight: Promise<string | null> | null = null;

function getOrStartRefresh(): Promise<string | null> {
  if (!_refreshInFlight) {
    _refreshInFlight = refreshToken().finally(() => {
      _refreshInFlight = null;
    });
  }
  return _refreshInFlight;
}

async function authFetch(path: string, init?: RequestInit): Promise<Response> {
  const token = getToken();
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  // Use redirect:"manual" so a ForwardAuth 302 to the authentik login page
  // surfaces as an opaqueredirect response instead of being transparently
  // followed to an HTML login page that json() would choke on.
  let resp = await fetch(`${BASE}${path}`, { ...init, headers, redirect: "manual" });

  // Attempt a concurrency-safe silent refresh on any auth-expiry signal.
  // All concurrent auth-expiry responses share the same refresh promise; only
  // one network request is made, and all callers retry with the new token once
  // it lands.  This preserves the existing 401+refresh happy path and extends
  // it to cover the ForwardAuth 503/redirect/HTML cases.
  if (isAuthExpiry(resp, path) && token) {
    const newToken = await getOrStartRefresh();
    if (newToken) {
      headers["Authorization"] = `Bearer ${newToken}`;
      resp = await fetch(`${BASE}${path}`, { ...init, headers, redirect: "manual" });
    }
  }

  // If still auth-expiry after the refresh attempt (or there was no token to
  // refresh with), trigger a full-page re-auth navigation.  This is the same
  // recovery the original code applied to a naked 401 — a top-level
  // window.location navigation re-auths through authentik and re-establishes
  // the session, automating the user's former manual logout/login step.
  // Note: a genuine backend-down 503 (not auth-related) will also land here
  // and bounce to /login; after re-auth the user returns to their original
  // page, where the backend will either have recovered or show the real error.
  if (isAuthExpiry(resp, path)) {
    handle401();
  }

  return resp;
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authFetch(path, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`);
  return res.json() as Promise<T>;
}

export const api = {
  // Auth
  login: (username: string, password: string, remember_me = false) =>
    fetch(`${BASE}/api/auth`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, remember_me }),
    }).then((r) => {
      if (!r.ok) throw new Error("Invalid credentials");
      return r.json() as Promise<{ access_token: string }>;
    }),

  // Core API
  getStatus: () => json<HealthReport>("/api/status"),
  listRuns: () => json<RunSummary[]>("/api/runs"),
  getRun: (runId: string) => json<RunDetail>(`/api/runs/${runId}`),
  getConverge: (owner: string, repo: string, number: string) =>
    json<ConvergeDetail>(`/api/prs/${owner}/${repo}/${number}/converge`),
  devDispatch: () =>
    json<{ run_id: string }>("/api/dev/dispatch", { method: "POST" }),
  listEscalations: (owner: string, repo: string) =>
    json<EscalationSummary[]>(`/api/repos/${owner}/${repo}/escalations`),
  deescalatePr: (
    owner: string,
    repo: string,
    prNumber: number,
    intent: "resume" | "requeue" | "acknowledge",
    operator = "operator",
  ) =>
    json<{ status: string }>(
      `/api/repos/${owner}/${repo}/prs/${prNumber}/deescalate?operator=${encodeURIComponent(operator)}&intent=${encodeURIComponent(intent)}`,
      { method: "POST" },
    ),
  devReconcile: () =>
    json<ReconcileReport[]>("/api/dev/reconcile", { method: "POST" }),

  // Repos
  listRepos: () => json<RepoSummary[]>("/api/repos"),

  // Triage
  listTriage: () => json<TriageItem[]>("/api/triage"),
  promoteIssue: (issueNumber: number) =>
    json<{ status: string; run_id: string }>(
      `/api/triage/${issueNumber}/promote`,
      { method: "POST" },
    ),
  declineIssue: (issueNumber: number) =>
    json<{ status: string }>(`/api/triage/${issueNumber}/decline`, {
      method: "POST",
    }),

  // Operators
  listOperators: () => json<OperatorRecord[]>("/api/operators"),
  createOperator: (username: string, password: string) =>
    json<{ status: string; id: string }>("/api/operators", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  deleteOperator: (id: string) =>
    json<{ status: string }>(`/api/operators/${id}`, { method: "DELETE" }),
  changePassword: (id: string, current_password: string, new_password: string) =>
    json<{ status: string }>(`/api/operators/${id}/password`, {
      method: "POST",
      body: JSON.stringify({ current_password, new_password }),
    }),

  // Push subscriptions
  getVapidPublicKey: () =>
    json<{ enabled: boolean; public_key: string | null }>("/api/push/vapid-public-key"),
  subscribePush: (endpoint: string, keys: { p256dh: string; auth: string }) =>
    json<{ status: string }>("/api/push/subscribe", {
      method: "POST",
      body: JSON.stringify({ endpoint, keys }),
    }),
  unsubscribePush: (endpoint: string) =>
    authFetch("/api/push/subscribe", {
      method: "DELETE",
      body: JSON.stringify({ endpoint }),
    }),
  listPushSubscriptions: () =>
    json<PushSubscriptionRecord[]>("/api/push/subscriptions"),
  testPush: () => json<{ status: string; sent: number }>("/api/push/test", { method: "POST" }),
};

/**
 * Subscribe to a run's SSE stream. Uses fetch (not EventSource) so it goes through
 * `authFetch` — the SAME Bearer-token + refresh path as every other API call — instead
 * of a second auth mechanism. (EventSource can't set an Authorization header, which would
 * force a token in the URL.) Returns an unsubscribe function.
 */
export function streamRunEvents(
  runId: string,
  onEvent: (event: RunEvent) => void,
  onError?: (err: unknown) => void,
): () => void {
  const controller = new AbortController();
  void (async () => {
    try {
      const res = await authFetch(`/api/runs/${runId}/stream`, {
        signal: controller.signal,
        headers: { Accept: "text/event-stream" },
      });
      if (!res.ok || !res.body) {
        onError?.(new Error(`SSE ${res.status} ${res.statusText}`));
        return;
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line; collect the `data:` payload.
        let sep: number;
        while ((sep = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          const data = frame
            .split("\n")
            .filter((l) => l.startsWith("data:"))
            .map((l) => l.slice(5).replace(/^ /, ""))
            .join("\n");
          if (!data) continue;
          try {
            onEvent(JSON.parse(data) as RunEvent);
          } catch {
            // ignore malformed frames
          }
        }
      }
    } catch (err) {
      if (!controller.signal.aborted) onError?.(err);
    }
  })();
  return () => controller.abort();
}
