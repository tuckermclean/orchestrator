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
}

export interface RunEvent {
  event_type: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface RunDetail extends RunSummary {
  events: RunEvent[];
  pr_ref?: { repo: { owner: string; name: string }; number: number } | null;
  issue_ref?: { repo: { owner: string; name: string }; number: number } | null;
  build_status?: string | null;
  changed_files?: number | null;
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

  let resp = await fetch(`${BASE}${path}`, { ...init, headers });

  // Attempt a concurrency-safe silent refresh on 401.
  // All concurrent 401s share the same refresh promise; only one network
  // request is made, and all callers retry with the new token once it lands.
  if (resp.status === 401 && token) {
    const newToken = await getOrStartRefresh();
    if (newToken) {
      headers["Authorization"] = `Bearer ${newToken}`;
      resp = await fetch(`${BASE}${path}`, { ...init, headers });
    }
  }

  if (resp.status === 401) {
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
