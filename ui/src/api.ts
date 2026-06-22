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
