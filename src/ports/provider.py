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
"""

from __future__ import annotations

import os

from src.domain.types import RepoRef
from src.ports.base import ForgePort, HarnessPort, SessionPort
from src.ports.github import GitHubForgePort
from src.ports.harness import RealHarnessPort


class PortProvider:
    """Holds credentials and constructs port instances per repo.

    Credentials come from environment variables only:
      FORGE_TOKEN      — GitHub personal access token (or GitHub App installation token)
      HARNESS_API_KEY  — Harness API key (reserved; may be None for forge-based dispatch)
    """

    def __init__(self, forge_token: str, harness_api_key: str | None = None) -> None:
        # Tokens are stored only here; they never enter DispatchContext or forge state.
        self._forge_token = forge_token
        self._harness_api_key = harness_api_key

    @classmethod
    def from_env(cls) -> PortProvider:
        """Construct from environment variables (raises if FORGE_TOKEN is absent)."""
        forge_token = os.environ.get("FORGE_TOKEN", "")
        if not forge_token:
            raise RuntimeError(
                "FORGE_TOKEN environment variable is required for production use"
            )
        harness_api_key = os.environ.get("HARNESS_API_KEY") or None
        return cls(forge_token=forge_token, harness_api_key=harness_api_key)

    def ports(self, repo: RepoRef) -> tuple[ForgePort, HarnessPort, SessionPort]:
        """Return (ForgePort, HarnessPort, SessionPort) for the given repo.

        SessionPort is not yet backed by a persistent store; callers that need
        session persistence should inject their own SessionPort via dependency
        injection rather than relying on this factory.
        """
        from src.ports.fakes import FakeSessionPort  # SessionPort DB backend in Step 9

        forge: ForgePort = GitHubForgePort(token=self._forge_token)
        harness: HarnessPort = RealHarnessPort(
            forge_token=self._forge_token,
            repo_owner=repo.owner,
            repo_name=repo.name,
            harness_api_key=self._harness_api_key,
        )
        # SessionPort backed by FakeSessionPort until the persistent session store
        # (Step 9 DB layer) is implemented.  Swap this out in the DB migration step.
        session: SessionPort = FakeSessionPort()
        return forge, harness, session
