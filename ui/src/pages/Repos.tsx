/**
 * Repos screen (WEBUI.md §5.3)
 *
 * Manage the repo registry. Stub implementation — list view with
 * placeholder for add/edit (registry API endpoints are Step 9 backend scope).
 */
import { useEffect, useState } from "react";

const s = {
  page: { padding: "24px", maxWidth: "900px", margin: "0 auto" } as React.CSSProperties,
  h1: { fontSize: "24px", fontWeight: 700, marginBottom: "24px" } as React.CSSProperties,
  card: {
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: "8px",
    padding: "24px",
    marginBottom: "16px",
  } as React.CSSProperties,
  muted: { color: "#8b949e", fontSize: "14px" } as React.CSSProperties,
  badge: (enabled: boolean): React.CSSProperties => ({
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: "12px",
    fontSize: "12px",
    fontWeight: 600,
    background: enabled ? "rgba(63,185,80,0.15)" : "rgba(139,148,158,0.15)",
    color: enabled ? "#3fb950" : "#8b949e",
    border: `1px solid ${enabled ? "#3fb950" : "#8b949e"}`,
  }),
};

interface RepoEntry {
  owner: string;
  name: string;
  enabled: boolean;
  allowlist_size: number;
}

export default function Repos() {
  const [repos, setRepos] = useState<RepoEntry[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Repos registry API (GET /api/repos) is a planned endpoint.
    // For now, surface a stub with the demo repo from session context.
    setRepos([
      { owner: "demo", name: "repo", enabled: true, allowlist_size: 0 },
    ]);
    setLoading(false);
  }, []);

  return (
    <main id="main-content" style={s.page} aria-label="Repos">
      <h1 style={s.h1}>Repos</h1>

      {loading && (
        <div role="status" aria-live="polite" aria-busy="true" style={s.muted}>
          Loading repos…
        </div>
      )}

      {!loading && repos.length === 0 && (
        <div style={s.card}>
          <p style={s.muted}>
            No repos registered. Use the API or Helm chart to configure repos.
          </p>
        </div>
      )}

      <ul aria-label="Registered repos" style={{ listStyle: "none" }}>
        {repos.map((repo) => (
          <li key={`${repo.owner}/${repo.name}`} style={s.card}>
            <div style={{ display: "flex", alignItems: "center", gap: "12px", flexWrap: "wrap" }}>
              <strong style={{ fontSize: "16px" }}>
                {repo.owner}/{repo.name}
              </strong>
              <span
                style={s.badge(repo.enabled)}
                role="status"
                aria-label={`Status: ${repo.enabled ? "enabled" : "paused"}`}
              >
                {repo.enabled ? "Enabled" : "Paused"}
              </span>
            </div>
            <div style={{ ...s.muted, marginTop: "8px" }}>
              Allowlist: {repo.allowlist_size === 0 ? "empty (gate disabled)" : `${repo.allowlist_size} users`}
            </div>
          </li>
        ))}
      </ul>
    </main>
  );
}
