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

export interface TriageItem {
  issue_number: number;
  title: string;
  author: string;
  body?: string;
  labels: string[];
  repo?: { owner: string; name: string };
  created_at?: string;
  triager_comment?: string;
}

export interface RepoSummary {
  owner: string;
  name: string;
  enabled: boolean;
  intake_enabled: boolean;
  required_checks: string[];
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

  // Attempt silent refresh on 401
  if (resp.status === 401 && token) {
    const newToken = await refreshToken();
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

/** Subscribe to SSE stream for a run. Returns an unsubscribe function. */
export function streamRunEvents(
  runId: string,
  onEvent: (event: RunEvent) => void,
  onError?: (err: Event) => void,
): () => void {
  const token = getToken();
  // EventSource doesn't support custom headers; pass token as query param
  const url = token
    ? `/api/runs/${runId}/stream?token=${encodeURIComponent(token)}`
    : `/api/runs/${runId}/stream`;
  const es = new EventSource(url);
  es.onmessage = (e) => {
    try {
      const parsed = JSON.parse(e.data as string) as RunEvent;
      onEvent(parsed);
    } catch {
      // ignore parse errors
    }
  };
  if (onError) es.onerror = onError;
  return () => es.close();
}
