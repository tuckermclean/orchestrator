# syntax=docker/dockerfile:1.9
# ---------------------------------------------------------------------------
# Orchestrator control-plane image
# ---------------------------------------------------------------------------
# Multi-stage build:
#   builder    — installs Python deps + fetches the specialist agent pack
#   ui-builder — builds the Vite PWA (ui/dist) served by the control-plane (#31)
#   runtime    — slim final image, non-root, readOnlyRootFilesystem-safe
#
# The claude CLI and git are NOT in this image.  Agent subprocesses run in
# separate Kubernetes Job pods (see deploy/agent-runner.Dockerfile, issue #51).
# In local dev the ClaudeCodeHarnessPort subprocess backend can be used when
# git and claude are available on the host; in production the K8s Job backend
# (#51) is used instead.
#
# Multi-arch: build for linux/amd64 + linux/arm64 via:
#   docker buildx build --platform linux/amd64,linux/arm64 .
# ---------------------------------------------------------------------------

# ---- base layer ----
# Pinned by tag; update the sha256 digest comment when bumping.
# To re-pin: docker pull python:3.12-slim-bookworm && docker inspect --format='{{index .RepoDigests 0}}' python:3.12-slim-bookworm
FROM python:3.12-slim-bookworm AS base
LABEL org.opencontainers.image.source="https://github.com/tuckermclean/orchestrator"
LABEL org.opencontainers.image.description="Orchestrator control-plane"
LABEL org.opencontainers.image.licenses="MIT"

# ---- builder stage ----
FROM base AS builder

# git is needed only in builder (for agent-pack clone)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Package manifest first for layer-cache efficiency
COPY pyproject.toml ./
# Stub src/__init__.py so hatchling can resolve the package at install time
RUN mkdir -p src && touch src/__init__.py

# Install runtime dependencies into an isolated venv.
# Include the [k8s] extra: the control-plane builds K8sJobBackend at boot when
# HARNESS_EXECUTION_BACKEND=k8s, which imports the kubernetes client (#51).
RUN python -m venv /app/venv \
 && /app/venv/bin/pip install --no-cache-dir --upgrade pip \
 && /app/venv/bin/pip install --no-cache-dir ".[k8s]"

# Copy full source and install (no-deps: deps already installed above)
COPY . .
RUN /app/venv/bin/pip install --no-cache-dir --no-deps -e .

# ---------------------------------------------------------------------------
# Fetch and bake the specialist agent pack (AGENTS.md §8 Phase 7)
# ---------------------------------------------------------------------------
ARG AGENT_PACK_REPO_URL="https://github.com/msitarzewski/agency-agents"
ARG AGENT_PACK_PINNED_REF="d6553e261e595c651064f899a6c33dd5aa71c9e3"
ARG AGENT_PACK_DEST_DIR=".agents"

LABEL org.opencontainers.image.agent-pack.source="${AGENT_PACK_REPO_URL}"
LABEL org.opencontainers.image.agent-pack.ref="${AGENT_PACK_PINNED_REF}"

# --filter=blob:none: blobless clone works reliably at any pinned SHA (AGENTS.md §8)
# rev-parse assertion: fails the build if checkout landed on the wrong commit
# Bake specialist agent files flat (basename = AgentRef, AGENTS.md §7). Per-directory
# README.md files are NOT agents and are the one duplicate basename in the pack — exclude
# them. The collision guard then still fails loudly on genuine duplicate AgentRefs.
RUN git clone --no-tags --filter=blob:none "${AGENT_PACK_REPO_URL}" /tmp/agency-agents \
 && git -C /tmp/agency-agents checkout "${AGENT_PACK_PINNED_REF}" \
 && [ "$(git -C /tmp/agency-agents rev-parse HEAD)" = "${AGENT_PACK_PINNED_REF}" ] \
 && mkdir -p "/app/${AGENT_PACK_DEST_DIR}" \
 && find /tmp/agency-agents -mindepth 2 -name "*.md" ! -iname "README.md" | while IFS= read -r f; do \
      target="/app/${AGENT_PACK_DEST_DIR}/$(basename "$f")"; \
      [ -e "$target" ] && { echo "ERROR: basename collision: $f" >&2; exit 1; }; \
      cp "$f" "$target"; \
    done \
 && rm -rf /tmp/agency-agents

# ---- UI builder stage (Vite PWA, #31) ----
# The control-plane serves the built SPA at / (main.py mounts ui/dist when present).
# ui/dist is git-ignored and never committed, so it MUST be built here — a clean
# checkout has no dist, and there is no node in the python runtime image.
FROM node:22-slim AS ui-builder
WORKDIR /ui
# Lockfile-first for layer caching; package-lock.json is committed so `npm ci` is reproducible.
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build   # tsc && vite build → /ui/dist

# ---- runtime stage ----
FROM base AS runtime

# Non-root user (ARCHITECTURE.md §5/§6 security context)
RUN groupadd --gid 1001 orch \
 && useradd --uid 1001 --gid orch --no-create-home --shell /usr/sbin/nologin orch

# Copy the venv, installed source, and agent pack from builder
COPY --from=builder /app/venv /app/venv
COPY --from=builder /build /app
COPY --from=builder /app/.agents /app/.agents
# Built PWA — main.py mounts /app/ui/dist at / when present (#31)
COPY --from=ui-builder /ui/dist /app/ui/dist

WORKDIR /app

# /data — SQLite DB mount (PVC in k8s, or emptyDir for ephemeral dev)
# /tmp  — temp space; harness subprocess clones write here (emptyDir in k8s)
# Both must be writable; declared here so chown runs as root before USER switch
RUN mkdir -p /data /tmp && chown orch:orch /data /tmp

USER orch

ENV PATH="/app/venv/bin:${PATH}" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED="1" \
    PORT="8080"

EXPOSE 8080 9090

# Docker HEALTHCHECK uses /healthz (liveness) — same probe the kubelet uses
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" \
    || exit 1

CMD ["uvicorn", "src.api.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--log-level", "info"]
