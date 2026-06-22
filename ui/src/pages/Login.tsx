/**
 * Login screen (WEBUI.md §5.7)
 *
 * Accessibility: form has labelled inputs, error announced via aria-live,
 * focus management on error, WCAG 2.1 AA contrast.
 */
import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { setToken } from "../auth";

const styles = {
  page: {
    minHeight: "100vh",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    background: "var(--bg, #0d1117)",
    padding: "24px",
  } as React.CSSProperties,
  card: {
    background: "var(--surface, #161b22)",
    border: "1px solid var(--border, #30363d)",
    borderRadius: "12px",
    padding: "40px",
    width: "100%",
    maxWidth: "400px",
  } as React.CSSProperties,
  heading: {
    fontSize: "24px",
    fontWeight: 700,
    marginBottom: "8px",
    color: "var(--text, #e6edf3)",
  } as React.CSSProperties,
  subtext: {
    color: "var(--text-muted, #8b949e)",
    marginBottom: "32px",
    fontSize: "14px",
  } as React.CSSProperties,
  label: {
    display: "block",
    fontSize: "14px",
    fontWeight: 600,
    marginBottom: "6px",
    color: "var(--text, #e6edf3)",
  } as React.CSSProperties,
  input: {
    width: "100%",
    padding: "10px 14px",
    background: "#010409",
    border: "1px solid var(--border, #30363d)",
    borderRadius: "6px",
    color: "var(--text, #e6edf3)",
    fontSize: "16px",
    marginBottom: "20px",
    minHeight: "44px",
  } as React.CSSProperties,
  checkboxRow: {
    display: "flex",
    alignItems: "center",
    gap: "10px",
    marginBottom: "24px",
  } as React.CSSProperties,
  checkbox: {
    width: "18px",
    height: "18px",
    cursor: "pointer",
    accentColor: "var(--blue, #58a6ff)",
  } as React.CSSProperties,
  button: {
    width: "100%",
    padding: "12px",
    background: "var(--blue, #58a6ff)",
    color: "#000",
    border: "none",
    borderRadius: "6px",
    fontSize: "16px",
    fontWeight: 600,
    cursor: "pointer",
    minHeight: "44px",
  } as React.CSSProperties,
  error: {
    background: "rgba(248,81,73,0.15)",
    border: "1px solid var(--red, #f85149)",
    borderRadius: "6px",
    padding: "12px",
    marginBottom: "20px",
    color: "var(--red, #f85149)",
    fontSize: "14px",
  } as React.CSSProperties,
};

export default function Login() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const errorRef = useRef<HTMLDivElement>(null);
  const usernameRef = useRef<HTMLInputElement>(null);

  // Focus username on mount
  useEffect(() => {
    usernameRef.current?.focus();
  }, []);

  // Focus error message when it appears
  useEffect(() => {
    if (error) {
      errorRef.current?.focus();
    }
  }, [error]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    setIsLoading(true);
    try {
      const data = await api.login(username, password, rememberMe);
      setToken(data.access_token, rememberMe);
      const next = params.get("next") || "/";
      navigate(next, { replace: true });
    } catch {
      // Error message: no distinction between unknown username and wrong password (WEBUI.md §5.7)
      setError("Invalid credentials");
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div style={styles.page}>
      <main style={styles.card} aria-label="Login">
        <h1 style={styles.heading}>Orchestrator</h1>
        <p style={styles.subtext}>Sign in to the operator interface</p>

        {error && (
          <div
            ref={errorRef}
            role="alert"
            aria-live="assertive"
            aria-atomic="true"
            style={styles.error}
            tabIndex={-1}
          >
            {error}
          </div>
        )}

        <form onSubmit={(e) => void handleSubmit(e)} noValidate>
          <div>
            <label htmlFor="username" style={styles.label}>
              Username
            </label>
            <input
              ref={usernameRef}
              id="username"
              name="username"
              type="text"
              autoComplete="username"
              required
              aria-required="true"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              style={styles.input}
              aria-describedby={error ? "login-error" : undefined}
            />
          </div>

          <div>
            <label htmlFor="password" style={styles.label}>
              Password
            </label>
            <input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              required
              aria-required="true"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              style={styles.input}
            />
          </div>

          <div style={styles.checkboxRow}>
            <input
              id="remember-me"
              name="remember_me"
              type="checkbox"
              checked={rememberMe}
              onChange={(e) => setRememberMe(e.target.checked)}
              style={styles.checkbox}
              aria-describedby="remember-me-desc"
            />
            <label htmlFor="remember-me" style={{ fontSize: "14px", cursor: "pointer" }}>
              Remember me
            </label>
            <span id="remember-me-desc" className="sr-only" style={{ display: "none" }}>
              Extends session to 30 days
            </span>
          </div>

          <button
            type="submit"
            disabled={isLoading}
            style={{
              ...styles.button,
              opacity: isLoading ? 0.7 : 1,
              cursor: isLoading ? "not-allowed" : "pointer",
            }}
            aria-busy={isLoading}
          >
            {isLoading ? "Signing in…" : "Sign in"}
          </button>
        </form>
      </main>
    </div>
  );
}
