import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type EscalationSummary, type HealthReport } from "../api";

const verdictColor: Record<string, string> = {
  BLOCKED: "#f85149",
  AT_RISK: "#d29922",
  ON_TRACK: "#3fb950",
};

const card: React.CSSProperties = {
  background: "#161b22",
  border: "1px solid #30363d",
  borderRadius: "8px",
  padding: "24px",
  marginBottom: "16px",
};

// Default demo owner/repo for escalation listing
const _DEMO_OWNER = "demo";
const _DEMO_REPO = "repo";

export default function Dashboard() {
  const [health, setHealth] = useState<HealthReport | null>(null);
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [escalations, setEscalations] = useState<EscalationSummary[]>([]);

  const load = () => {
    api
      .getStatus()
      .then((h) => {
        setHealth(h);
        setLastUpdated(new Date().toLocaleTimeString());
        setError(null);
      })
      .catch((e: Error) => setError(e.message));
    api
      .listEscalations(_DEMO_OWNER, _DEMO_REPO)
      .then(setEscalations)
      .catch(() => {
        // non-fatal: escalation list is best-effort in the dashboard
      });
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 15000);
    return () => clearInterval(interval);
  }, []);

  if (error) {
    return (
      <div style={card}>
        <p style={{ color: "#f85149" }}>Error: {error}</p>
      </div>
    );
  }

  if (!health) {
    return (
      <div style={card}>
        <p style={{ color: "#8b949e" }}>Loading pipeline health…</p>
      </div>
    );
  }

  const color = verdictColor[health.verdict] ?? "#8b949e";

  return (
    <div>
      <h1 style={{ marginBottom: "16px", fontSize: "24px" }}>Pipeline Dashboard</h1>

      <div style={{ ...card, borderLeft: `4px solid ${color}` }}>
        <div style={{ display: "flex", alignItems: "center", gap: "12px", marginBottom: "16px" }}>
          <span
            style={{
              background: color,
              color: "#0d1117",
              fontWeight: 700,
              padding: "4px 12px",
              borderRadius: "4px",
              fontSize: "16px",
            }}
          >
            {health.verdict}
          </span>
          {lastUpdated && (
            <span style={{ color: "#8b949e", fontSize: "13px" }}>
              Last updated: {lastUpdated}
            </span>
          )}
        </div>

        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px" }}>
          {[
            ["Implementing", health.implementing],
            ["Converging", health.converge],
            ["Ready", health.ready],
            ["Needs Human", health.needs_human],
            ["Stale Drafts", health.stale_drafts],
            ["In Flight", health.in_flight],
          ].map(([label, value]) => (
            <div
              key={String(label)}
              style={{
                background: "#0d1117",
                border: "1px solid #21262d",
                borderRadius: "6px",
                padding: "12px",
              }}
            >
              <div style={{ fontSize: "24px", fontWeight: 700 }}>{value}</div>
              <div style={{ fontSize: "13px", color: "#8b949e" }}>{label}</div>
            </div>
          ))}
        </div>
      </div>

      <div style={{ marginTop: "16px" }}>
        <Link
          to="/runs"
          style={{
            color: "#58a6ff",
            textDecoration: "none",
            fontSize: "14px",
          }}
        >
          → View all runs
        </Link>
      </div>

      {/* Escalation list — shown only when there are blocked PRs */}
      {escalations.length > 0 && (
        <div style={{ ...card, marginTop: "24px", borderLeft: "4px solid #f85149" }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "10px",
              marginBottom: "12px",
            }}
          >
            <span
              style={{
                background: "#f85149",
                color: "#0d1117",
                fontWeight: 700,
                padding: "2px 10px",
                borderRadius: "4px",
                fontSize: "13px",
              }}
            >
              BLOCKED
            </span>
            <span style={{ fontWeight: 600, fontSize: "15px" }}>
              {escalations.length} escalated PR{escalations.length !== 1 ? "s" : ""}
            </span>
            {health?.verdict === "AT_RISK" && (
              <span
                style={{
                  background: "#d29922",
                  color: "#0d1117",
                  fontWeight: 700,
                  padding: "2px 8px",
                  borderRadius: "4px",
                  fontSize: "12px",
                }}
              >
                AT_RISK
              </span>
            )}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
            {escalations.map((e) => (
              <div
                key={e.pr_number}
                style={{
                  background: "#0d1117",
                  border: "1px solid #30363d",
                  borderRadius: "6px",
                  padding: "10px 12px",
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  fontSize: "13px",
                }}
              >
                <span>
                  <span style={{ fontWeight: 600 }}>#{e.pr_number}</span>{" "}
                  <span style={{ color: "#8b949e" }}>{e.title}</span>
                </span>
                <span
                  style={{
                    color: "#f85149",
                    fontFamily: "monospace",
                    fontSize: "12px",
                  }}
                >
                  {e.cause}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
