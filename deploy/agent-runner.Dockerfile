# syntax=docker/dockerfile:1.9
# ---------------------------------------------------------------------------
# Agent-runner image
# ---------------------------------------------------------------------------
# This image is executed by Kubernetes Job pods (issue #51 — K8s Job harness
# backend).  It contains the toolchain needed to run a Claude Code agent:
#
#   - git          (repo clone + commits)
#   - claude CLI   (anthropics/claude-code; installed via npm)
#   - gh CLI       (optional; useful for PR operations from within the agent)
#   - Python 3.12  (some agent tooling may invoke Python scripts)
#   - The orchestration agent contracts (/app/agents/*.md)
#   - The specialist agent pack (/app/.agents/*.md, SHA-pinned, baked at build)
#
# Security model:
#   - Non-root user (uid 1001 "agent")
#   - No orchestrator master credentials — only CLAUDE_CODE_OAUTH_TOKEN and a
#     freshly-minted, repository-scoped GH_TOKEN are injected at Job runtime
#     (I3, ARCHITECTURE.md §2 HarnessPort).
#   - The image itself has no secrets baked in.
#
# Build:
#   docker buildx build \
#     --platform linux/amd64,linux/arm64 \
#     -f deploy/agent-runner.Dockerfile \
#     -t ghcr.io/<owner>/orchestrator-agent-runner:<tag> .
# ---------------------------------------------------------------------------

FROM node:22-slim AS node-base

# Install system packages: git, gh CLI, ca-certificates, Python 3.12
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git \
      ca-certificates \
      curl \
      gnupg \
      python3 \
      python3-pip \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
 && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

# Install the claude CLI globally via npm
# Pin to a known version; bump intentionally after reviewing changelog
RUN npm install -g @anthropic-ai/claude-code@latest \
 && claude --version

# ---------------------------------------------------------------------------
# Fetch and bake the specialist agent pack (AGENTS.md §8 Phase 7)
# ---------------------------------------------------------------------------
ARG AGENT_PACK_REPO_URL="https://github.com/msitarzewski/agency-agents"
ARG AGENT_PACK_PINNED_REF="d6553e261e595c651064f899a6c33dd5aa71c9e3"
ARG AGENT_PACK_DEST_DIR=".agents"

LABEL org.opencontainers.image.source="https://github.com/tuckermclean/orchestrator"
LABEL org.opencontainers.image.description="Orchestrator agent-runner (K8s Job executor)"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.agent-pack.source="${AGENT_PACK_REPO_URL}"
LABEL org.opencontainers.image.agent-pack.ref="${AGENT_PACK_PINNED_REF}"

RUN git clone --no-tags --filter=blob:none "${AGENT_PACK_REPO_URL}" /tmp/agency-agents \
 && git -C /tmp/agency-agents checkout "${AGENT_PACK_PINNED_REF}" \
 && [ "$(git -C /tmp/agency-agents rev-parse HEAD)" = "${AGENT_PACK_PINNED_REF}" ] \
 && mkdir -p "/app/${AGENT_PACK_DEST_DIR}" \
 && find /tmp/agency-agents -mindepth 2 -name "*.md" | while IFS= read -r f; do \
      target="/app/${AGENT_PACK_DEST_DIR}/$(basename "$f")"; \
      [ -e "$target" ] && { echo "ERROR: basename collision: $f" >&2; exit 1; }; \
      cp "$f" "$target"; \
    done \
 && rm -rf /tmp/agency-agents

# Copy orchestration agent contracts (five *.md files from agents/)
# These are PROTECTED_PATHS — baked read-only; never modified at runtime
COPY agents/ /app/agents/

# Non-root user
RUN groupadd --gid 1001 agent \
 && useradd --uid 1001 --gid agent --no-create-home --shell /usr/sbin/nologin agent \
 && chown -R agent:agent /app

# Working directory for agent runs (K8s Job mounts the cloned repo here)
WORKDIR /workspace
RUN chown agent:agent /workspace

USER agent

ENV GIT_TERMINAL_PROMPT="0" \
    PYTHONUNBUFFERED="1"

# Default entrypoint: the K8s Job (#51) overrides CMD per dispatch with:
#   claude -p "<prompt>" --output-format stream-json --permission-mode bypassPermissions --verbose --model <model>
# This default allows quick smoke-testing of the image.
ENTRYPOINT ["claude"]
CMD ["--version"]
