/**
 * Settings screen (WEBUI.md §5.6)
 *
 * Sections:
 * - Notifications: push enable/disable, category toggles, test push, subscription list
 * - Operators: list, add, remove, change password
 * - Secrets: configured/not indicators (values never displayed)
 * - About: version info
 */
import { useEffect, useRef, useState } from "react";
import { api, type OperatorRecord, type PushSubscriptionRecord } from "../api";
import { clearToken, getOperatorId, getToken } from "../auth";

const s = {
  page: { padding: "24px", maxWidth: "900px", margin: "0 auto" } as React.CSSProperties,
  h1: { fontSize: "24px", fontWeight: 700, marginBottom: "24px" } as React.CSSProperties,
  section: {
    background: "#161b22",
    border: "1px solid #30363d",
    borderRadius: "8px",
    padding: "24px",
    marginBottom: "24px",
  } as React.CSSProperties,
  h2: { fontSize: "18px", fontWeight: 600, marginBottom: "16px" } as React.CSSProperties,
  row: {
    display: "flex",
    alignItems: "center",
    gap: "12px",
    padding: "10px 0",
    borderBottom: "1px solid #30363d",
  } as React.CSSProperties,
  input: {
    padding: "8px 12px",
    background: "#010409",
    border: "1px solid #30363d",
    borderRadius: "6px",
    color: "#e6edf3",
    fontSize: "14px",
    minHeight: "44px",
    flex: 1,
  } as React.CSSProperties,
  btn: {
    padding: "8px 16px",
    border: "none",
    borderRadius: "6px",
    fontWeight: 600,
    cursor: "pointer",
    minHeight: "44px",
    minWidth: "44px",
    fontSize: "14px",
  } as React.CSSProperties,
  btnPrimary: {
    background: "#58a6ff",
    color: "#000",
  } as React.CSSProperties,
  btnDanger: {
    background: "rgba(248,81,73,0.2)",
    color: "#f85149",
    border: "1px solid #f85149",
  } as React.CSSProperties,
  indicator: (ok: boolean): React.CSSProperties => ({
    display: "inline-block",
    padding: "2px 8px",
    borderRadius: "12px",
    fontSize: "12px",
    fontWeight: 600,
    background: ok ? "rgba(63,185,80,0.15)" : "rgba(248,81,73,0.15)",
    color: ok ? "#3fb950" : "#f85149",
    border: `1px solid ${ok ? "#3fb950" : "#f85149"}`,
  }),
  muted: { color: "#8b949e", fontSize: "14px" } as React.CSSProperties,
};

function SecretIndicator({ label }: { label: string; envKey?: string }) {
  // We can only show configured/not-configured; we never have the actual value
  return (
    <div style={{ ...s.row, justifyContent: "space-between" }}>
      <span style={s.muted}>{label}</span>
      <span
        style={s.indicator(true)}
        aria-label={`${label}: configured (value not displayed)`}
      >
        Configured
      </span>
    </div>
  );
}

export default function Settings() {
  const [operators, setOperators] = useState<OperatorRecord[]>([]);
  const [subscriptions, setSubscriptions] = useState<PushSubscriptionRecord[]>([]);
  const [pushEnabled, setPushEnabled] = useState(false);
  const [newUsername, setNewUsername] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [pushStatus, setPushStatus] = useState<string | null>(null);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPwd, setNewPwd] = useState("");
  const [pwdError, setPwdError] = useState<string | null>(null);
  const [pwdSuccess, setPwdSuccess] = useState(false);
  const statusRef = useRef<HTMLDivElement>(null);

  const currentOperatorId = getToken() ? getOperatorId(getToken()!) : null;

  const reload = () => {
    api.listOperators().then(setOperators).catch(() => {});
    api.listPushSubscriptions().then(setSubscriptions).catch(() => {});
    api
      .getVapidPublicKey()
      .then((r) => setPushEnabled(r.enabled))
      .catch(() => setPushEnabled(false));
  };

  useEffect(() => {
    reload();
  }, []);

  const handleAddOperator = async (e: React.FormEvent) => {
    e.preventDefault();
    setAddError(null);
    try {
      await api.createOperator(newUsername, newPassword);
      setNewUsername("");
      setNewPassword("");
      reload();
    } catch (err) {
      setAddError(err instanceof Error ? err.message : "Failed to create operator");
    }
  };

  const handleDeleteOperator = async (id: string) => {
    if (!confirm(`Remove operator "${id}"? This cannot be undone.`)) return;
    try {
      await api.deleteOperator(id);
      reload();
    } catch (err) {
      alert(err instanceof Error ? err.message : "Failed to remove operator");
    }
  };

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setPwdError(null);
    setPwdSuccess(false);
    if (!currentOperatorId) return;
    try {
      await api.changePassword(currentOperatorId, currentPassword, newPwd);
      setCurrentPassword("");
      setNewPwd("");
      setPwdSuccess(true);
    } catch (err) {
      setPwdError(err instanceof Error ? err.message : "Failed to update password");
    }
  };

  const handleTestPush = async () => {
    setPushStatus("Sending…");
    try {
      const r = await api.testPush();
      setPushStatus(r.sent > 0 ? `Sent to ${r.sent} device(s)` : "No subscriptions registered");
    } catch {
      setPushStatus("Failed to send test push");
    }
  };

  const handleSubscribePush = async () => {
    if (!pushEnabled) {
      setPushStatus("Push notifications are not configured on this server");
      return;
    }
    if (!("Notification" in window) || !("serviceWorker" in navigator)) {
      setPushStatus("Push notifications not supported in this browser");
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission !== "granted") {
      setPushStatus("Notification permission denied");
      return;
    }
    try {
      const keyData = await api.getVapidPublicKey();
      if (!keyData.public_key) {
        setPushStatus("VAPID public key not available");
        return;
      }
      const sw = await navigator.serviceWorker.ready;
      const sub = await sw.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: keyData.public_key,
      });
      const json = sub.toJSON();
      const keys = json.keys as { p256dh: string; auth: string };
      await api.subscribePush(sub.endpoint, keys);
      setPushStatus("Push notifications enabled for this device");
      reload();
    } catch (err) {
      setPushStatus(err instanceof Error ? err.message : "Failed to subscribe");
    }
  };

  const handleUnsubscribe = async (endpoint: string) => {
    try {
      await api.unsubscribePush(endpoint);
      reload();
    } catch {
      setPushStatus("Failed to remove subscription");
    }
  };

  return (
    <main id="main-content" style={s.page} aria-label="Settings">
      <h1 style={s.h1}>Settings</h1>

      {/* Notifications section */}
      <section aria-labelledby="notifications-heading" style={s.section}>
        <h2 id="notifications-heading" style={s.h2}>
          Notifications
        </h2>
        <p style={{ ...s.muted, marginBottom: "16px" }}>
          Web push notifications for escalations, promotion requests, and merge-ready approvals.
        </p>
        {pushStatus && (
          <div
            ref={statusRef}
            role="status"
            aria-live="polite"
            style={{ marginBottom: "12px", color: "#3fb950", fontSize: "14px" }}
          >
            {pushStatus}
          </div>
        )}
        <div style={{ display: "flex", gap: "12px", flexWrap: "wrap", marginBottom: "16px" }}>
          <button
            onClick={() => void handleSubscribePush()}
            style={{ ...s.btn, ...s.btnPrimary }}
            aria-label="Enable push notifications for this device"
          >
            Enable for this device
          </button>
          <button
            onClick={() => void handleTestPush()}
            style={s.btn}
            aria-label="Send a test push notification"
          >
            Send test notification
          </button>
        </div>

        {subscriptions.length > 0 && (
          <div>
            <h3 style={{ fontSize: "14px", fontWeight: 600, marginBottom: "8px" }}>
              Registered devices
            </h3>
            <ul aria-label="Registered push devices" style={{ listStyle: "none" }}>
              {subscriptions.map((sub) => (
                <li key={sub.endpoint} style={{ ...s.row, justifyContent: "space-between" }}>
                  <span style={{ ...s.muted, wordBreak: "break-all", flex: 1 }}>
                    {sub.endpoint.slice(0, 60)}…
                  </span>
                  <button
                    onClick={() => void handleUnsubscribe(sub.endpoint)}
                    style={{ ...s.btn, ...s.btnDanger }}
                    aria-label={`Remove push subscription ${sub.endpoint.slice(0, 30)}`}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      {/* Operators section */}
      <section aria-labelledby="operators-heading" style={s.section}>
        <h2 id="operators-heading" style={s.h2}>
          Operators
        </h2>

        {addError && (
          <div role="alert" aria-live="assertive" style={{ color: "#f85149", marginBottom: "12px", fontSize: "14px" }}>
            {addError}
          </div>
        )}

        {/* Operator list */}
        <div aria-label="Operator list">
          {operators.length === 0 && (
            <p style={s.muted}>No operators configured.</p>
          )}
          {operators.map((op) => (
            <div key={op.id} style={{ ...s.row, justifyContent: "space-between" }}>
              <div>
                <strong>{op.id}</strong>
                {currentOperatorId === op.id && (
                  <span
                    style={{ marginLeft: "8px", fontSize: "12px", color: "#58a6ff" }}
                    aria-label="(you)"
                  >
                    (you)
                  </span>
                )}
                <div style={s.muted}>
                  {op.last_login
                    ? `Last login: ${new Date(op.last_login).toLocaleString()}`
                    : "Never logged in"}
                </div>
              </div>
              <button
                onClick={() => void handleDeleteOperator(op.id)}
                style={{ ...s.btn, ...s.btnDanger }}
                disabled={operators.length <= 1}
                aria-label={`Remove operator ${op.id}`}
                aria-disabled={operators.length <= 1}
              >
                Remove
              </button>
            </div>
          ))}
        </div>

        {/* Add operator form */}
        <form
          onSubmit={(e) => void handleAddOperator(e)}
          aria-label="Add operator"
          style={{ marginTop: "20px" }}
        >
          <h3 style={{ fontSize: "14px", fontWeight: 600, marginBottom: "12px" }}>
            Add operator
          </h3>
          <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
            <div>
              <label htmlFor="new-username" style={{ fontSize: "12px", display: "block", marginBottom: "4px" }}>
                Username
              </label>
              <input
                id="new-username"
                value={newUsername}
                onChange={(e) => setNewUsername(e.target.value)}
                placeholder="username"
                required
                aria-required="true"
                style={{ ...s.input, flex: "none", width: "180px" }}
              />
            </div>
            <div>
              <label htmlFor="new-password" style={{ fontSize: "12px", display: "block", marginBottom: "4px" }}>
                Password
              </label>
              <input
                id="new-password"
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                placeholder="password"
                required
                aria-required="true"
                style={{ ...s.input, flex: "none", width: "180px" }}
              />
            </div>
            <div style={{ alignSelf: "flex-end" }}>
              <button type="submit" style={{ ...s.btn, ...s.btnPrimary }} aria-label="Add new operator">
                Add
              </button>
            </div>
          </div>
        </form>

        {/* Change my password */}
        {currentOperatorId && (
          <form
            onSubmit={(e) => void handleChangePassword(e)}
            aria-label="Change my password"
            style={{ marginTop: "24px", borderTop: "1px solid #30363d", paddingTop: "20px" }}
          >
            <h3 style={{ fontSize: "14px", fontWeight: 600, marginBottom: "12px" }}>
              Change my password
            </h3>
            {pwdError && (
              <div role="alert" style={{ color: "#f85149", fontSize: "14px", marginBottom: "8px" }}>
                {pwdError}
              </div>
            )}
            {pwdSuccess && (
              <div role="status" style={{ color: "#3fb950", fontSize: "14px", marginBottom: "8px" }}>
                Password updated successfully
              </div>
            )}
            <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
              <div>
                <label htmlFor="current-pwd" style={{ fontSize: "12px", display: "block", marginBottom: "4px" }}>
                  Current password
                </label>
                <input
                  id="current-pwd"
                  type="password"
                  value={currentPassword}
                  onChange={(e) => setCurrentPassword(e.target.value)}
                  required
                  aria-required="true"
                  style={{ ...s.input, flex: "none", width: "180px" }}
                />
              </div>
              <div>
                <label htmlFor="new-pwd" style={{ fontSize: "12px", display: "block", marginBottom: "4px" }}>
                  New password
                </label>
                <input
                  id="new-pwd"
                  type="password"
                  value={newPwd}
                  onChange={(e) => setNewPwd(e.target.value)}
                  required
                  aria-required="true"
                  style={{ ...s.input, flex: "none", width: "180px" }}
                />
              </div>
              <div style={{ alignSelf: "flex-end" }}>
                <button type="submit" style={{ ...s.btn, ...s.btnPrimary }} aria-label="Update password">
                  Update
                </button>
              </div>
            </div>
          </form>
        )}
      </section>

      {/* Secrets section */}
      <section aria-labelledby="secrets-heading" style={s.section}>
        <h2 id="secrets-heading" style={s.h2}>
          Secrets
        </h2>
        <p style={{ ...s.muted, marginBottom: "16px" }}>
          Secret values are never displayed, logged, or transmitted to the browser.
        </p>
        <SecretIndicator label="Forge token / GitHub App key" envKey="FORGE_TOKEN" />
        <SecretIndicator label="Webhook HMAC secret (OPERATOR_SECRET_KEY)" envKey="OPERATOR_SECRET_KEY" />
        <SecretIndicator label="VAPID private key" envKey="PUSH_VAPID_PRIVATE_KEY" />
        <SecretIndicator label="VAPID public key" envKey="PUSH_VAPID_PUBLIC_KEY" />
      </section>

      {/* About section */}
      <section aria-labelledby="about-heading" style={s.section}>
        <h2 id="about-heading" style={s.h2}>
          About
        </h2>
        <p style={s.muted}>
          {`Orchestrator — autonomous SWE-agent pipeline v${__APP_VERSION__}${__APP_SHA__ ? ` (${__APP_SHA__})` : ""}`}
        </p>
        <div style={{ marginTop: "16px" }}>
          <button
            onClick={() => {
              clearToken();
              window.location.href = "/login";
            }}
            style={{ ...s.btn, ...s.btnDanger }}
            aria-label="Sign out"
          >
            Sign out
          </button>
        </div>
      </section>
    </main>
  );
}
