/**
 * Orchestrator Service Worker
 *
 * Caching strategy:
 * - Cache-first: static assets (JS/CSS/fonts with content-addressed filenames)
 * - Network-first: /api/* (never cached; always live state)
 * - Network-first: navigation requests (HTML shell)
 *
 * Background JWT refresh: runs before expiry to silently renew tokens.
 * Web push: handles push events and shows OS notifications.
 */

const CACHE_NAME = "orchestrator-v1";
const ASSET_CACHE_NAME = "orchestrator-assets-v1";

// Assets matched by content-addressed filename patterns (hash in filename)
const STATIC_ASSET_PATTERN = /\.(js|css|woff2?|ttf|svg|png|ico)$/;
const API_PATTERN = /^\/api\//;

// ---------------------------------------------------------------------------
// Install: pre-cache the app shell
// ---------------------------------------------------------------------------

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      cache.addAll(["/", "/index.html"]).catch(() => {
        // Non-fatal: shell may not exist yet on first install
      })
    )
  );
  // Activate immediately — don't wait for old clients to close
  self.skipWaiting();
});

// ---------------------------------------------------------------------------
// Activate: clean old caches
// ---------------------------------------------------------------------------

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME && k !== ASSET_CACHE_NAME)
          .map((k) => caches.delete(k))
      )
    )
  );
  // Take control of all open clients immediately
  self.clients.claim();
});

// ---------------------------------------------------------------------------
// Fetch: routing logic
// ---------------------------------------------------------------------------

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // API requests: network-first, no caching
  if (API_PATTERN.test(url.pathname)) {
    event.respondWith(
      fetch(event.request).catch(() =>
        new Response(JSON.stringify({ error: "offline" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        })
      )
    );
    return;
  }

  // Static assets with content-addressed filenames: cache-first
  if (STATIC_ASSET_PATTERN.test(url.pathname) && url.origin === self.location.origin) {
    event.respondWith(
      caches.open(ASSET_CACHE_NAME).then(async (cache) => {
        const cached = await cache.match(event.request);
        if (cached) return cached;
        const response = await fetch(event.request);
        if (response.ok) {
          cache.put(event.request, response.clone());
        }
        return response;
      })
    );
    return;
  }

  // Navigation (HTML): network-first with app shell fallback
  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(() =>
        caches.match("/index.html").then((r) => r || new Response("Offline", { status: 503 }))
      )
    );
    return;
  }

  // Default: network-first
  event.respondWith(
    fetch(event.request).catch(() => caches.match(event.request).then((r) => r || new Response("Offline", { status: 503 })))
  );
});

// ---------------------------------------------------------------------------
// Push notifications
// ---------------------------------------------------------------------------

self.addEventListener("push", (event) => {
  if (!event.data) return;

  let data;
  try {
    data = event.data.json();
  } catch {
    data = { type: "notification", title: "Orchestrator", body: event.data.text() };
  }

  const type = data.type || "notification";
  const repo = data.repo || "";
  const num = data.issue_or_pr_number ? `#${data.issue_or_pr_number}` : "";
  const url = data.url || "/";

  const titles = {
    escalation: `⚠️ Escalation: ${repo} ${num}`,
    promotion: `📋 Promotion Request: ${repo} ${num}`,
    approval: `✅ Approved: ${repo} ${num}`,
    test: "🔔 Test notification",
  };

  const bodies = {
    escalation: data.title || "An entity needs human attention",
    promotion: data.title || "A new issue is awaiting promotion",
    approval: data.title || "A PR is ready to merge",
    test: "Push notifications are working correctly.",
  };

  const title = titles[type] || data.title || "Orchestrator";
  const body = bodies[type] || data.body || "";

  const options = {
    body,
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-192.png",
    tag: `${type}-${repo}-${num}`,
    requireInteraction: type === "escalation",
    data: { url, type },
    actions:
      type !== "test"
        ? [
            { action: "open", title: "Open" },
            { action: "dismiss", title: "Dismiss" },
          ]
        : [],
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

// ---------------------------------------------------------------------------
// Notification click
// ---------------------------------------------------------------------------

self.addEventListener("notificationclick", (event) => {
  event.notification.close();

  if (event.action === "dismiss") return;

  const url = event.notification.data?.url || "/";

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      // Focus existing window if available
      for (const client of clients) {
        if (client.url.startsWith(self.location.origin) && "focus" in client) {
          client.focus();
          client.navigate(url);
          return;
        }
      }
      // Otherwise open a new window
      if (self.clients.openWindow) {
        return self.clients.openWindow(url);
      }
    })
  );
});
