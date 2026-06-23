/**
 * JWT authentication utilities.
 *
 * Tokens are stored in sessionStorage (session-scoped) or localStorage
 * (remember-me / 30-day TTL).  The service worker refresh path calls
 * POST /api/auth/refresh before expiry.
 */

const TOKEN_KEY = "orchestrator_token";
const REMEMBER_KEY = "orchestrator_remember";

export function getToken(): string | null {
  return (
    localStorage.getItem(TOKEN_KEY) || sessionStorage.getItem(TOKEN_KEY) || null
  );
}

export function setToken(token: string, remember: boolean): void {
  clearToken();
  if (remember) {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(REMEMBER_KEY, "1");
  } else {
    sessionStorage.setItem(TOKEN_KEY, token);
  }
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(REMEMBER_KEY);
  sessionStorage.removeItem(TOKEN_KEY);
}

export function isRemembered(): boolean {
  return localStorage.getItem(REMEMBER_KEY) === "1";
}

/** Decode a JWT payload without verifying (client-side display only). */
function decodePayload(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    const payload = atob(parts[1].replace(/-/g, "+").replace(/_/g, "/"));
    return JSON.parse(payload) as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** Return seconds until JWT expiry, or 0 if expired / invalid. */
export function secondsUntilExpiry(token: string): number {
  const payload = decodePayload(token);
  if (!payload || typeof payload.exp !== "number") return 0;
  return Math.max(0, payload.exp - Math.floor(Date.now() / 1000));
}

/** Return the operator username from the JWT sub claim. */
export function getOperatorId(token: string): string | null {
  const payload = decodePayload(token);
  if (!payload || typeof payload.sub !== "string") return null;
  return payload.sub;
}

/** Attempt a silent token refresh; returns new token or null on failure. */
export async function refreshToken(): Promise<string | null> {
  const current = getToken();
  if (!current) return null;
  try {
    const resp = await fetch("/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: current }),
    });
    if (!resp.ok) return null;
    const data = (await resp.json()) as { access_token?: string };
    if (data.access_token) {
      setToken(data.access_token, isRemembered());
      return data.access_token;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * Threshold (seconds) below which a proactive background refresh is triggered.
 * Refresh when less than 5 minutes remain on the token — well before any 401.
 */
export const PROACTIVE_REFRESH_THRESHOLD_SECS = 5 * 60;

/**
 * Interval (ms) at which the proactive refresh check runs.
 * Checking every 60 s is sufficient — at the 5-minute threshold there are
 * 5 check cycles before expiry, so the window is wide enough.
 */
export const PROACTIVE_REFRESH_INTERVAL_MS = 60_000;

/**
 * Start a proactive refresh timer that keeps active sessions alive.
 *
 * On each tick the current token's remaining TTL is checked; if it falls
 * below PROACTIVE_REFRESH_THRESHOLD_SECS a silent refresh is attempted.
 * Call the returned cleanup function (e.g. from a useEffect return) to
 * stop the interval — typically on logout or component unmount.
 *
 * Also fires once immediately so users who load the app with a nearly-
 * expired token are refreshed before the first API call completes.
 */
export function startProactiveRefresh(): () => void {
  let active = true;

  async function tick() {
    if (!active) return;
    const token = getToken();
    if (!token) return;
    if (secondsUntilExpiry(token) < PROACTIVE_REFRESH_THRESHOLD_SECS) {
      await refreshToken();
    }
  }

  // Immediate check — handles the "loaded app with near-expiry token" case.
  void tick();

  const id = setInterval(() => void tick(), PROACTIVE_REFRESH_INTERVAL_MS);

  return () => {
    active = false;
    clearInterval(id);
  };
}
