import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type TriageItem } from "../api";

const styles = {
  container: {
    display: "flex",
    flexDirection: "column" as const,
    gap: "16px",
  },
  heading: {
    fontSize: "22px",
    fontWeight: 700,
    color: "#e6edf3",
    margin: "0 0 8px 0",
  },
  subheading: {
    fontSize: "14px",
    color: "#8b949e",
    margin: "0 0 24px 0",
  },
  card: {
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: "8px",
    padding: "20px",
    display: "flex",
    flexDirection: "column" as const,
    gap: "12px",
  },
  cardHeader: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "flex-start",
    gap: "12px",
  },
  title: {
    fontSize: "16px",
    fontWeight: 600,
    color: "#e6edf3",
    margin: 0,
  },
  issueNumber: {
    fontSize: "13px",
    color: "#8b949e",
    whiteSpace: "nowrap" as const,
  },
  meta: {
    fontSize: "13px",
    color: "#8b949e",
  },
  body: {
    fontSize: "14px",
    color: "#c9d1d9",
    lineHeight: 1.6,
    margin: 0,
    background: "#0d1117",
    padding: "12px",
    borderRadius: "4px",
    border: "1px solid #21262d",
    maxHeight: "80px",
    overflow: "hidden",
    display: "-webkit-box",
    WebkitLineClamp: 3,
    WebkitBoxOrient: "vertical" as const,
  },
  actions: {
    display: "flex",
    gap: "8px",
  },
  btnPromote: {
    padding: "6px 16px",
    background: "#238636",
    color: "#ffffff",
    border: "none",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "13px",
    fontWeight: 600,
  },
  btnDecline: {
    padding: "6px 16px",
    background: "transparent",
    color: "#f85149",
    border: "1px solid #f85149",
    borderRadius: "6px",
    cursor: "pointer",
    fontSize: "13px",
    fontWeight: 600,
  },
  empty: {
    textAlign: "center" as const,
    color: "#8b949e",
    padding: "48px 0",
    fontSize: "14px",
  },
  error: {
    color: "#f85149",
    background: "#1c1c1c",
    border: "1px solid #f85149",
    borderRadius: "6px",
    padding: "12px 16px",
    fontSize: "14px",
  },
};

export default function Triage() {
  const [items, setItems] = useState<TriageItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  async function fetchTriage() {
    try {
      const data = await api.listTriage();
      setItems(data);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchTriage();
  }, []);

  async function handlePromote(item: TriageItem) {
    try {
      await api.promoteIssue(item.issue_ref.number);
      await fetchTriage();
      navigate("/runs");
    } catch (e) {
      setError(`Promote failed: ${e}`);
    }
  }

  async function handleDecline(item: TriageItem) {
    try {
      await api.declineIssue(item.issue_ref.number);
      await fetchTriage();
    } catch (e) {
      setError(`Decline failed: ${e}`);
    }
  }

  return (
    <div style={styles.container}>
      <div>
        <h1 style={styles.heading}>Triage Queue</h1>
        <p style={styles.subheading}>
          Issues awaiting operator promotion before an agent is dispatched.
        </p>
      </div>

      {error && <div style={styles.error}>{error}</div>}

      {loading ? (
        <p style={{ color: "#8b949e" }}>Loading…</p>
      ) : items.length === 0 ? (
        <div style={styles.empty}>No issues awaiting triage.</div>
      ) : (
        items.map((item) => (
          <div
            key={`${item.issue_ref.repo.owner}/${item.issue_ref.repo.name}#${item.issue_ref.number}`}
            style={styles.card}
          >
            <div style={styles.cardHeader}>
              <h2 style={styles.title}>{item.title}</h2>
              <span style={styles.issueNumber}>
                #{item.issue_ref.number}
              </span>
            </div>

            <div style={styles.meta}>
              Submitted by <strong>{item.author}</strong>
            </div>

            {item.body && (
              <p style={styles.body}>{item.body}</p>
            )}

            <div style={styles.actions}>
              <button
                style={styles.btnPromote}
                onClick={() => handlePromote(item)}
              >
                Promote
              </button>
              <button
                style={styles.btnDecline}
                onClick={() => handleDecline(item)}
              >
                Decline
              </button>
            </div>
          </div>
        ))
      )}
    </div>
  );
}
