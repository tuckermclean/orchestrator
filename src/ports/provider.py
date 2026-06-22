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
  - GitHub App mode: set GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY +
    GITHUB_APP_INSTALLATION_ID.  Installation access tokens are minted and
    cached automatically; FORGE_TOKEN is still required to drive the harness.
  - Static PAT mode (dev/fallback): set only FORGE_TOKEN.
"""

from __future__ import annotations

import os

from src.domain.types import RepoRef
from src.ports.base import ForgePort, HarnessPort, SessionPort
from src.ports.github import GitHubAppForgePort, GitHubForgePort
from src.ports.harness import RealHarnessPort


class PortProvider:
    """Holds credentials and constructs port instances per repo.

    Credentials come from environment variables only (I3):
      FORGE_TOKEN                  — GitHub PAT (dev/fallback; also drives the harness)
      HARNESS_API_KEY              — Harness API key (reserved; may be None)
      GITHUB_APP_ID                — GitHub App ID (App auth mode)
      GITHUB_APP_PRIVATE_KEY       — PEM-encoded RS256 private key (App auth mode)
      GITHUB_APP_INSTALLATION_ID   — Installation ID for the target org/repo (App auth mode)
    """

    def __init__(
        self,
        forge_token: str,
        harness_api_key: str | None = None,
        github_app_id: str | None = None,
        github_app_private_key: str | None = None,
        github_app_installation_id: str | None = None,
    ) -> None:
        # Tokens are stored only here; they never enter DispatchContext or forge state.
        self._forge_token = forge_token
        self._harness_api_key = harness_api_key
        self._github_app_id = github_app_id
        self._github_app_private_key = github_app_private_key
        self._github_app_installation_id = github_app_installation_id

    @classmethod
    def from_env(cls) -> PortProvider:
        """Construct from environment variables.

        GitHub App mode is activated when GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY,
        and GITHUB_APP_INSTALLATION_ID are all present.  In that mode, the forge
        token (FORGE_TOKEN) is used for the harness adapter; the App token is used
        for all ForgePort operations.

        In static PAT mode (App vars absent), FORGE_TOKEN is required and used
        for both forge and harness operations.
        """
        forge_token = os.environ.get("FORGE_TOKEN", "")
        harness_api_key = os.environ.get("HARNESS_API_KEY") or None

        github_app_id = os.environ.get("GITHUB_APP_ID") or None
        github_app_private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY") or None
        github_app_installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID") or None

        app_mode = all([github_app_id, github_app_private_key, github_app_installation_id])

        if not app_mode and not forge_token:
            raise RuntimeError(
                "Either FORGE_TOKEN (PAT mode) or GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY"
                " + GITHUB_APP_INSTALLATION_ID (App mode) are required for production use"
            )

        return cls(
            forge_token=forge_token,
            harness_api_key=harness_api_key,
            github_app_id=github_app_id,
            github_app_private_key=github_app_private_key,
            github_app_installation_id=github_app_installation_id,
        )

    def ports(self, repo: RepoRef) -> tuple[ForgePort, HarnessPort, SessionPort]:
        """Return (ForgePort, HarnessPort, SessionPort) for the given repo.

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

        harness: HarnessPort = RealHarnessPort(
            forge_token=self._forge_token,
            repo_owner=repo.owner,
            repo_name=repo.name,
            harness_api_key=self._harness_api_key,
        )
        session: SessionPort = FakeSessionPort()  # persistent store added in Step 9
        return forge, harness, session
