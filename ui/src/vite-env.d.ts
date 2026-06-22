/// <reference types="vite/client" />

// Globals injected by Vite at build time (vite.config.ts → define).
// In dev mode these resolve to the env vars VITE_APP_VERSION / VITE_APP_GIT_SHA,
// falling back to "0.0.0-dev" / "" when unset.
declare const __APP_VERSION__: string;
declare const __APP_SHA__: string;
