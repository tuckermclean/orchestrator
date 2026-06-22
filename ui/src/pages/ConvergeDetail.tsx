import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api, type ConvergeDetail as ConvergeDetailType, type ConvergeRound } from "../api";

const card: React.CSSProperties = {
  background: "#161b22",
  border: "1px solid #30363d",
  borderRadius: "8px",
  padding: "20px",
  marginBottom: "16px",
};

const tokenColor = (token: string): string => {
  if (token === "approve") return "#3fb950";
  if (token === "fix") return "#58a6ff";
  return "#f85149"; // escalate:*
};

const ciGreen = (conclusion: string | null): boolean =>
  conclusion === "success" || conclusion === "skipped" || conclusion === "neutral";

function RoundAccordion({ round }: { round: ConvergeRound }) {
  const [open, setOpen] = useState(round.round === 1);
  const v = round.verdict;
  return (
    <div style={card}>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          width: "100%",
          background: "transparent",
          border: "none",
          color: "#e6edf3",
          cursor: "pointer",
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          fontSize: "16px",
          fontWeight: 600,
        }}
      >
        <span>
          {open ? "▾" : "▸"} Round {round.round}
          <span style={{ color: "#8b949e", fontWeight: 400, marginLeft: "12px", fontSize: "13px" }}>
            model: {round.model}
          </span>
        </span>
        <span
          style={{
            padding: "2px 10px",
            borderRadius: "12px",
            fontSize: "12px",
            fontWeight: 600,
            background: "#21262d",
            color: tokenColor(round.decide_round_token),
          }}
        >
          {round.decide_round_token}
        </span>
      </button>

      {open && (
        <div style={{ marginTop: "16px" }}>
          <div style={{ display: "flex", gap: "16px", fontSize: "14px", marginBottom: "12px" }}>
            <span>🔴 {v.blockers} blockers</span>
            <span>🟡 {v.suggestions} suggestions</span>
            <span>💬 {v.nits.length} nits</span>
          </div>

          {v.blocker_signatures.length > 0 && (
            <div style={{ marginBottom: "12px" }}>
              <div style={{ color: "#8b949e", fontSize: "13px", marginBottom: "4px" }}>
                Blocker signatures
              </div>
              {v.blocker_signatures.map((sig) => (
                <code
                  key={sig}
                  style={{
                    display: "inline-block",
                    background: "#0d1117",
                    border: "1px solid #21262d",
                    borderRadius: "4px",
                    padding: "2px 6px",
                    marginRight: "6px",
                    marginBottom: "4px",
                    fontSize: "12px",
                  }}
                >
                  {sig}
                </code>
              ))}
            </div>
          )}

          <div style={{ marginBottom: "12px" }}>
            <div style={{ color: "#8b949e", fontSize: "13px", marginBottom: "4px" }}>
              Specialists
            </div>
            {round.specialists.map((s) => (
              <span
                key={s}
                style={{
                  display: "inline-block",
                  background: "#0d1117",
                  border: "1px solid #21262d",
                  borderRadius: "12px",
                  padding: "2px 10px",
                  marginRight: "6px",
                  marginBottom: "4px",
                  fontSize: "12px",
                  color: "#d2a8ff",
                }}
              >
                {s.replace(/\.md$/, "")}
              </span>
            ))}
          </div>

          <div>
            <div style={{ color: "#8b949e", fontSize: "13px", marginBottom: "4px" }}>
              CI checks
            </div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
                gap: "8px",
              }}
            >
              {round.ci_checks.map((c) => (
                <div
                  key={c.name}
                  style={{
                    background: "#0d1117",
                    border: "1px solid #21262d",
                    borderRadius: "6px",
                    padding: "8px 10px",
                    fontSize: "12px",
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                  }}
                >
                  <span>{c.name}</span>
                  <span style={{ color: ciGreen(c.conclusion) ? "#3fb950" : "#f85149" }}>
                    {ciGreen(c.conclusion) ? "✓" : "✗"} {c.conclusion ?? c.state}
                  </span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function ConvergeDetail() {
  const { owner, repo, number } = useParams<{ owner: string; repo: string; number: string }>();
  const [detail, setDetail] = useState<ConvergeDetailType | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!owner || !repo || !number) return;
    api
      .getConverge(owner, repo, number)
      .then(setDetail)
      .catch((e: Error) => setError(e.message));
  }, [owner, repo, number]);

  return (
    <div>
      <h1 style={{ fontSize: "24px", marginBottom: "20px" }}>
        Converge: {owner}/{repo} #{number}
      </h1>

      {error && (
        <div style={{ ...card, borderColor: "#f85149", color: "#f85149" }}>Error: {error}</div>
      )}

      {detail && (
        <>
          <div style={card}>
            <div style={{ fontSize: "16px", fontWeight: 600, marginBottom: "8px" }}>
              {detail.pr_title}
            </div>
            <span
              style={{
                padding: "2px 10px",
                borderRadius: "12px",
                fontSize: "12px",
                fontWeight: 600,
                background: "#21262d",
                color: "#58a6ff",
              }}
            >
              {detail.state}
            </span>
          </div>

          {detail.rounds.length === 0 && (
            <div style={card}>
              <span style={{ color: "#6e7681" }}>No converge rounds recorded yet.</span>
            </div>
          )}
          {detail.rounds.map((r) => (
            <RoundAccordion key={r.round} round={r} />
          ))}
        </>
      )}
    </div>
  );
}
