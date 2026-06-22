// Typed fetch wrappers for all API endpoints + SSE helper

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

export interface ConvergeRound {
  round: 1 | 2 | 3;
  model: string;
  decide_round_token: string;
  specialists: string[];
  verdict: ConvergeVerdict;
  ci_checks: ConvergeCiCheck[];
}

export interface ConvergeDetail {
  pr_ref: { repo: { owner: string; name: string }; number: number };
  pr_title: string;
  state: string;
  rounds: ConvergeRound[];
}

const BASE = "";

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`);
  return res.json() as Promise<T>;
}

export const api = {
  getStatus: () => json<HealthReport>("/api/status"),
  listRuns: () => json<RunSummary[]>("/api/runs"),
  getRun: (runId: string) => json<RunDetail>(`/api/runs/${runId}`),
  getConverge: (owner: string, repo: string, number: string) =>
    json<ConvergeDetail>(`/api/prs/${owner}/${repo}/${number}/converge`),
  devDispatch: () =>
    json<{ run_id: string }>("/api/dev/dispatch", { method: "POST" }),
};

/** Subscribe to SSE stream for a run. Returns an unsubscribe function. */
export function streamRunEvents(
  runId: string,
  onEvent: (event: RunEvent) => void,
  onError?: (err: Event) => void,
): () => void {
  const es = new EventSource(`/api/runs/${runId}/stream`);
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
