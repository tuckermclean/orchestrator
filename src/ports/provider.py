"""PortProvider — credential-holding factory for real port instances.

Credentials are sourced exclusively from environment variables.  They are NEVER
accepted from DispatchContext, contributor input, or any other caller-supplied
value (invariants I3/I9).

Usage (production):
    provider = PortProvider.from_env()
    forge, harness, session = provider.ports(repo_ref)

Usage (tests):
    Inject FakeForgePort / FakeHarnessPort / FakeSessionPort directly — do not
    instantiate PortProvider in unit/integration tests.

Auth modes (selected automatically by from_env):
  - GitHub App mode (required for harness): set GITHUB_APP_ID +
    GITHUB_APP_PRIVATE_KEY + GITHUB_APP_INSTALLATION_ID.  The harness mints a
    fresh, scoped installation token per dispatch (I3).  FORGE_TOKEN may still be
    set for trigger_ci / trigger_workflow.
  - Static PAT mode (dev/fallback): set only FORGE_TOKEN.  The harness cannot
    mint scoped tokens in this mode; it falls back to the PAT for CI re-runs.

The harness requires CLAUDE_CODE_OAUTH_TOKEN for the Claude auth credential that
is injected into the agent sandbox (operator env, never contributor-supplied).

Multi-harness mode (SPEC §14.6):
  Set HARNESSES_JSON to a JSON array of harness-config objects.  Each entry's
  ``id`` must correspond to suffixed credential env vars that this provider reads:
    CLAUDE_CODE_OAUTH_TOKEN_<ID_UPPER>  — Claude auth for this harness slot
  All App credential vars (GITHUB_APP_ID, etc.) are shared across harnesses in the
  current implementation (single GitHub App, multiple Claude accounts).  Future
  work may add per-harness App credential namespacing.
  When HARNESSES_JSON is absent the provider falls back to the single-harness path.
"""

from __future__ import annotations

import os

from src.domain.types import HARNESSES_JSON_ENV, RepoRef
from src.ports.base import ForgePort, HarnessPort, SessionPort
from src.ports.github import GitHubAppForgePort, GitHubForgePort
from src.ports.harness import ClaudeCodeHarnessPort
from src.ports.harness_registry import (
    FailoverHarnessPort,
    HarnessConfig,
    HarnessRegistry,
    HarnessRegistryEntry,
)


class PortProvider:
    """Holds credentials and constructs port instances per repo.

    Credentials come from environment variables only (I3):
      FORGE_TOKEN                  — GitHub PAT (dev/fallback; trigger_ci / trigger_workflow)
      CLAUDE_CODE_OAUTH_TOKEN      — Claude auth token injected into the agent sandbox
      GITHUB_APP_ID                — GitHub App ID (App auth mode)
      GITHUB_APP_PRIVATE_KEY       — PEM-encoded RS256 private key (App auth mode)
      GITHUB_APP_INSTALLATION_ID   — Installation ID for the target org/repo (App auth mode)
      HARNESSES_JSON               — (optional) JSON array for multi-harness config (SPEC §14.6)
    """

    def __init__(
        self,
        forge_token: str,
        claude_oauth_token: str = "",
        github_app_id: str | None = None,
        github_app_private_key: str | None = None,
        github_app_installation_id: str | None = None,
        harnesses_json: str | None = None,
    ) -> None:
        # Tokens are stored only here; they never enter DispatchContext or forge state.
        self._forge_token = forge_token
        self._claude_oauth_token = claude_oauth_token
        self._github_app_id = github_app_id
        self._github_app_private_key = github_app_private_key
        self._github_app_installation_id = github_app_installation_id
        self._harnesses_json = harnesses_json

    @classmethod
    def from_env(cls) -> PortProvider:
        """Construct from environment variables.

        GitHub App mode is activated when GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY,
        and GITHUB_APP_INSTALLATION_ID are all present.  In that mode the forge
        uses App tokens; the harness mints per-dispatch scoped installation tokens.

        In static PAT mode (App vars absent), FORGE_TOKEN is required and used
        for ForgePort operations; the harness uses it for trigger_ci / trigger_workflow.
        """
        forge_token = os.environ.get("FORGE_TOKEN", "")
        claude_oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")

        github_app_id = os.environ.get("GITHUB_APP_ID") or None
        github_app_private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY") or None
        github_app_installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID") or None
        harnesses_json = os.environ.get(HARNESSES_JSON_ENV) or None

        app_mode = all([github_app_id, github_app_private_key, github_app_installation_id])

        if not app_mode and not forge_token:
            raise RuntimeError(
                "Either FORGE_TOKEN (PAT mode) or GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY"
                " + GITHUB_APP_INSTALLATION_ID (App mode) are required for production use"
            )

        return cls(
            forge_token=forge_token,
            claude_oauth_token=claude_oauth_token,
            github_app_id=github_app_id,
            github_app_private_key=github_app_private_key,
            github_app_installation_id=github_app_installation_id,
            harnesses_json=harnesses_json,
        )

    def _make_harness_port(
        self,
        repo: RepoRef,
        claude_oauth_token: str,
    ) -> HarnessPort:
        """Construct a single ClaudeCodeHarnessPort for the given repo and token.

        I3: credentials (claude_oauth_token) are passed as direct arguments from
        this factory only — never from DispatchContext or contributor input.
        """
        return ClaudeCodeHarnessPort(
            claude_oauth_token=claude_oauth_token,
            app_id=self._github_app_id or "",
            private_key_pem=self._github_app_private_key or "",
            installation_id=self._github_app_installation_id or "",
            repo_owner=repo.owner,
            repo_name=repo.name,
            forge_token=self._forge_token,
        )

    def _build_harness_registry(self, repo: RepoRef) -> HarnessRegistry:
        """Build a HarnessRegistry from HARNESSES_JSON or single-harness fallback.

        When HARNESSES_JSON is present: each entry's id is used to look up a
        per-harness CLAUDE_CODE_OAUTH_TOKEN_<ID_UPPER> env var.  If absent for
        a given id, the base CLAUDE_CODE_OAUTH_TOKEN is used as a fallback so
        that a new harness entry does not silently break in dev/CI.

        When HARNESSES_JSON is absent: builds a one-entry registry using the
        base CLAUDE_CODE_OAUTH_TOKEN (backward-compatible single-harness path).
        """
        if self._harnesses_json:
            def port_factory(config: HarnessConfig) -> HarnessPort:
                # Look up per-harness token: CLAUDE_CODE_OAUTH_TOKEN_<ID_UPPER>
                # Falls back to base token when per-harness var is absent.
                env_key = f"CLAUDE_CODE_OAUTH_TOKEN_{config.id.upper().replace('-', '_')}"
                token = os.environ.get(env_key) or self._claude_oauth_token
                return self._make_harness_port(repo, token)

            return HarnessRegistry.from_json(self._harnesses_json, port_factory)

        # Single-harness backward-compat path.
        port = self._make_harness_port(repo, self._claude_oauth_token)
        entry = HarnessRegistryEntry(
            config=HarnessConfig(id="default", priority=1),
            port=port,
        )
        return HarnessRegistry([entry])

    def ports(self, repo: RepoRef) -> tuple[ForgePort, HarnessPort, SessionPort]:
        """Return (ForgePort, HarnessPort, SessionPort) for the given repo.

        The returned HarnessPort is a FailoverHarnessPort backed by a HarnessRegistry
        (SPEC §14.4).  With a single harness in the registry it behaves identically to
        a bare ClaudeCodeHarnessPort; the failover layer is transparent.

        SessionPort is not yet backed by a persistent store; callers that need
        session persistence should inject their own SessionPort via dependency
        injection rather than relying on this factory.  Track via issue #30.
        """
        from src.ports.fakes import FakeSessionPort

        app_mode = all(
            [self._github_app_id, self._github_app_private_key, self._github_app_installation_id]
        )
        if app_mode:
            forge: ForgePort = GitHubAppForgePort(
                app_id=self._github_app_id or "",
                private_key_pem=self._github_app_private_key or "",
                installation_id=self._github_app_installation_id or "",
            )
        else:
            forge = GitHubForgePort(token=self._forge_token)

        registry = self._build_harness_registry(repo)
        harness: HarnessPort = FailoverHarnessPort(registry)
        session: SessionPort = FakeSessionPort()  # persistent store added in Step 9
        return forge, harness, session
