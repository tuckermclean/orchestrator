# Authentik Forward-Auth Configuration for the Orchestrator PWA

## Root cause of the 503 / CORS error

`orch.dcxxiv.com` is deployed behind an authentik forward-auth proxy.  When
authentik intercepts a request to a public asset (e.g. `/manifest.json`) from
an unauthenticated browser, it responds with an HTTP 302 redirect to
`auth.dcxxiv.com`.  The browser fetches a cross-origin redirect instead of the
asset, producing:

- `GET /manifest.json` → **503** (nginx upstream error) or a silent redirect
  that the browser treats as a CORS failure
- The PWA manifest never loads, so `Add to Home Screen` and install prompts are
  broken
- The service worker registration at `/sw.js` fails before it can cache the app
  shell, breaking offline support

The fix is **not** to modify auth.py or the ingress — the API already does its
own JWT authentication.  The fix is to tell authentik which paths are public so
those requests bypass the forward-auth check entirely.

---

## Path classification

### Paths that MUST bypass forward-auth (unauthenticated public assets)

These assets must be fetchable before the user has a session.  They contain no
sensitive data.

| Path pattern | Why it must be public |
|---|---|
| `/` | SPA entry point; browser fetches this before any login redirect |
| `/index.html` | Same document served for all SPA routes |
| `/manifest.json` | PWA web app manifest; fetched by the browser at install time, before auth |
| `/sw.js` | Service worker script; the browser registers it with `navigator.serviceWorker.register('/sw.js')` on page load, before any token is present |
| `/icons/**` | App icons referenced in `manifest.json` (`/icons/icon-192.png`, `/icons/icon-512.png`); fetched by the browser and the OS during PWA install |
| `/assets/**` | Vite build output: content-addressed JS/CSS bundles (e.g. `/assets/index-<hash>.js`, `/assets/index-<hash>.css`), fonts, and other static resources |

The service worker and manifest filenames are confirmed from
`ui/public/sw.js`, `ui/public/manifest.json`, and the Vite build config
(`ui/vite.config.ts` sets `outDir: "dist"`; Vite copies `public/` files to
`dist/` unchanged and emits hashed bundles into `dist/assets/`).

> **SPA catch-all:** `/` must be exempt so nginx can serve `index.html` for
> every client-side route (`/triage`, `/runs/*`, etc.) before login.  Authentik
> will redirect the unauthenticated browser to the login page after it has
> loaded the app shell.  The shell then performs the login flow via
> `POST /api/auth` (which is also exempt — see below).

### Paths that MUST stay authenticated via the application layer

These endpoints enforce their own authentication and must NOT be exempted from
forward-auth unless you have a specific reason — removing authentik protection
here does not weaken security (the app enforces auth independently), but
exempting them unnecessarily widens the attack surface.

| Path pattern | Auth mechanism | Notes |
|---|---|---|
| `/api/**` | JWT Bearer (HS256) via `src/api/auth.py` | All API calls require a Bearer token issued by `POST /api/auth` |
| `/api/auth` | Open (login endpoint) | Issues the JWT; exempt from JWT auth in `auth.py` but does not need a session — safe to also exempt from forward-auth if desired, since it authenticates itself |
| `/api/auth/refresh` | Open (refresh endpoint) | Same rationale as `/api/auth` |
| `/api/webhook` | HMAC-SHA256 | Authenticated by `OPERATOR_SECRET_KEY`; must be reachable by GitHub, not by a browser session |
| `/healthz` | Open | Kubernetes liveness probe; no sensitive data |
| `/readyz` | Open | Kubernetes readiness probe; no sensitive data |
| `/api/runs/{id}/stream` | JWT Bearer | SSE endpoint; the UI sends the Bearer token via the `Authorization` header in the `EventSource` fetch call — stays under `/api/**`, stays authed |

> **Do not** add `/api/**` to the authentik skip-path list.  The application
> already enforces JWT auth on every `/api/` route except the login/refresh
> endpoints and the HMAC-protected webhook.  The Kubernetes probes (`/healthz`,
> `/readyz`) are open by design — no credentials are needed.

---

## Authentik proxy provider configuration

In the authentik admin UI, open the **Proxy Provider** for `orch.dcxxiv.com`
and add the following paths to the **"Unauthenticated Paths"** (also called
**"Skip-path regex"** depending on your authentik version).

Each line is a Python `re.search`-compatible regex matched against the request
path.

```
^/$
^/index\.html$
^/manifest\.json$
^/sw\.js$
^/icons/
^/assets/
^/healthz$
^/readyz$
^/api/auth(/.*)?$
^/api/webhook(/.*)?$
```

**What each pattern exempts:**

| Pattern | Exempts |
|---|---|
| `^/$` | SPA root (serves `index.html`) |
| `^/index\.html$` | Explicit `/index.html` fetch |
| `^/manifest\.json$` | PWA web app manifest |
| `^/sw\.js$` | Service worker script |
| `^/icons/` | All icon assets under `/icons/` |
| `^/assets/` | All Vite-emitted hashed bundles under `/assets/` |
| `^/healthz$` | Kubernetes liveness probe |
| `^/readyz$` | Kubernetes readiness probe |
| `^/api/auth(/.*)?$` | Login + refresh (app enforces its own auth) |
| `^/api/webhook(/.*)?$` | GitHub webhook (HMAC-authenticated) |

> **Tip:** authentik's "Unauthenticated Paths" field accepts one regex per line
> in most versions, or a single pipe-delimited regex in older versions.  Test
> your change by opening `https://orch.dcxxiv.com/manifest.json` in an
> incognito window — you should get JSON, not a redirect to `auth.dcxxiv.com`.

### Outpost / forward-auth annotation (nginx ingress)

If you are using the authentik embedded outpost with nginx ingress, the
`nginx.ingress.kubernetes.io/auth-url` and
`nginx.ingress.kubernetes.io/auth-signin` annotations drive forward-auth.
Unauthenticated-path exemptions live in the **Proxy Provider** settings in
authentik, not in the ingress annotations — the ingress passes every request to
the outpost; the outpost decides whether to allow or redirect based on the
provider's skip-path list.

### Alternative: per-path ingress annotations

If you prefer to keep the authentik Proxy Provider strict and instead exempt
paths at the ingress level, create a second Ingress resource (or a separate
`nginx.ingress.kubernetes.io/configuration-snippet`) that omits the auth
annotations for public asset paths.  This approach is more verbose and harder
to maintain; the Proxy Provider skip-path approach above is preferred.

---

## Verification checklist

After applying the skip-path configuration:

1. Open `https://orch.dcxxiv.com/manifest.json` in an incognito window.
   Expected: JSON response, HTTP 200, no redirect to `auth.dcxxiv.com`.

2. Open `https://orch.dcxxiv.com/sw.js` in an incognito window.
   Expected: JavaScript response, HTTP 200.

3. Open `https://orch.dcxxiv.com/icons/icon-192.png` in an incognito window.
   Expected: PNG image, HTTP 200.

4. Open `https://orch.dcxxiv.com/` in an incognito window.
   Expected: the app shell loads (HTML/CSS/JS), then authentik redirects to
   login for any authenticated action.

5. Confirm `/api/runs` still requires auth:
   ```bash
   curl -I https://orch.dcxxiv.com/api/runs
   # Expected: HTTP 401 (from the app, not a 302 from authentik)
   ```

6. Confirm the webhook is reachable without a session (GitHub calls this):
   ```bash
   curl -I -X POST https://orch.dcxxiv.com/api/webhook
   # Expected: HTTP 400 or 422 (missing HMAC), not 302
   ```
