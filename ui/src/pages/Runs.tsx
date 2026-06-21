import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type RunSummary } from "../api";

const statusColor: Record<string, string> = {
  completed: "#3fb950",
  in_progress: "#58a6ff",
  queued: "#8b949e",
  cancelled: "#f85149",
};

const card: React.CSSProperties = {
  background: "#161b22",
  border: "1px solid #30363d",
  borderRadius: "8px",
  padding: "16px",
  marginBottom: "12px",
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  textDecoration: "none",
  color: "inherit",
  transition: "border-color 0.15s",
};

export default function Runs() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = () => {
    api
      .listRuns()
      .then((r) => {
        setRuns(r);
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
  };

  useEffect(() => {
    load();
  }, []);

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
        <h1 style={{ fontSize: "24px" }}>Runs</h1>
        <button
          onClick={load}
          style={{
            background: "#21262d",
            color: "#e6edf3",
            border: "1px solid #30363d",
            borderRadius: "6px",
            padding: "6px 16px",
            cursor: "pointer",
            fontSize: "14px",
          }}
        >
          Refresh
        </button>
      </div>

      {error && (
        <div style={{ ...card, borderColor: "#f85149", color: "#f85149" }}>
          Error: {error}
        </div>
      )}

      {runs.length === 0 && !error && (
        <div style={{ color: "#8b949e", padding: "24px", textAlign: "center" }}>
          No runs yet. Try dispatching one from the Run Detail page.
        </div>
      )}

      {runs.map((run) => {
        const color = statusColor[run.status] ?? "#8b949e";
        return (
          <Link key={run.run_id} to={`/runs/${run.run_id}`} style={card}>
            <div>
              <div style={{ fontWeight: 600, marginBottom: "4px" }}>{run.run_id}</div>
              <div style={{ fontSize: "13px", color: "#8b949e" }}>
                {run.type} · {run.repo.owner}/{run.repo.name}
              </div>
              <div style={{ fontSize: "12px", color: "#6e7681", marginTop: "4px" }}>
                Started: {new Date(run.started_at).toLocaleString()}
              </div>
            </div>
            <span
              style={{
                background: color,
                color: "#0d1117",
                fontWeight: 600,
                padding: "3px 10px",
                borderRadius: "4px",
                fontSize: "12px",
              }}
            >
              {run.status}
            </span>
          </Link>
        );
      })}
    </div>
  );
}
