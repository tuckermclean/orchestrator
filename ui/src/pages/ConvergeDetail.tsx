import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import {
  api,
  type ConvergeDetail as ConvergeDetailType,
  type ConvergeFixerRun,
  type ConvergeRound,
} from "../api";

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

function FixerRunBadge({ fixer }: { fixer: ConvergeFixerRun }) {
  const color = fixer.timed_out ? "#f85149" : "#58a6ff";
  const label = fixer.timed_out ? "fixer: timed out (E11)" : "fixer: completed";
  return (
    <div
      style={{
        marginTop: "12px",
        padding: "8px 12px",
        borderRadius: "6px",
        background: "#0d1117",
        border: `1px solid ${color}`,
        fontSize: "13px",
        color,
        display: "flex",
        alignItems: "center",
        gap: "8px",
      }}
    >
      <span
        role="img"
        aria-label={fixer.timed_out ? "fixer timed out" : "fixer completed"}
      >
        {fixer.timed_out ? "⏱" : "🔧"}
      </span>
      <span>{label}</span>
      {fixer.run_id && (
        <code
          style={{
            background: "#161b22",
            borderRadius: "4px",
            padding: "1px 5px",
            fontSize: "11px",
            color: "#8b949e",
          }}
        >
          {fixer.run_id}
        </code>
      )}
      <span style={{ marginLeft: "auto", color: "#8b949e", fontSize: "12px" }}>
        model: {fixer.model}
      </span>
    </div>
  );
}

function RoundAccordion({ round }: { round: ConvergeRound }) {
  const [open, setOpen] = useState(round.round === 1);
  const v = round.verdict;
  const panelId = `converge-round-panel-${round.round}`;
  return (
    <div style={card}>
      <button
        type="button"
        className="converge-accordion-toggle"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={panelId}
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
          <span aria-hidden="true">{open ? "▾" : "▸"}</span> Round {round.round}
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
        <div
          id={panelId}
          role="region"
          aria-label={`Round ${round.round} details`}
          style={{ marginTop: "16px" }}
        >
          <div style={{ display: "flex", gap: "16px", fontSize: "14px", marginBottom: "12px" }}>
            <span>
              <span aria-hidden="true">🔴</span> {v.blockers} blockers
            </span>
            <span>
              <span aria-hidden="true">🟡</span> {v.suggestions} suggestions
            </span>
            <span>
              <span aria-hidden="true">💬</span> {v.nits.length} nits
            </span>
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
                    <span role="img" aria-label={ciGreen(c.conclusion) ? "passed" : "failed"}>
                      {ciGreen(c.conclusion) ? "✓" : "✗"}
                    </span>{" "}
                    {c.conclusion ?? c.state}
                  </span>
                </div>
              ))}
            </div>
          </div>

          {/* Fixer run — present when the token was "fix" and a fixer was dispatched */}
          {round.fixer_run && <FixerRunBadge fixer={round.fixer_run} />}
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
      {/* :focus-visible cannot be expressed inline; scope a class to the accordion toggle. */}
      <style>{`
        .converge-accordion-toggle:focus-visible {
          outline: 2px solid #58a6ff;
          outline-offset: 2px;
          border-radius: 4px;
        }
      `}</style>
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
            <div style={{ display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" }}>
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
              {detail.escalation_cause && (
                <span
                  aria-label={`Escalation cause: ${detail.escalation_cause}`}
                  style={{
                    padding: "2px 10px",
                    borderRadius: "12px",
                    fontSize: "12px",
                    fontWeight: 600,
                    background: "#3d1a1a",
                    color: "#f85149",
                    border: "1px solid #f8514944",
                  }}
                >
                  {detail.escalation_cause}
                </span>
              )}
            </div>
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
