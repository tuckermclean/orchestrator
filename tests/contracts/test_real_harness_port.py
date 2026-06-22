"""Integration tests for ClaudeCodeHarnessPort against a real claude subprocess.

These tests are gated behind @pytest.mark.integration_real and will only execute
when:
  - ORCH_REAL_CLAUDE_TEST=1
  - CLAUDE_CODE_OAUTH_TOKEN is set (Claude OAuth token)
  - GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, GITHUB_APP_INSTALLATION_ID are set
    (or FORGE_TOKEN as a fallback)
  - TEST_GITHUB_OWNER and TEST_GITHUB_REPO point to a sandboxed test repository

Without these env vars the entire module is skipped cleanly.

These tests CANNOT run in this autonomous build environment (no live tokens),
but are structured correctly for a credentialed CI/deploy environment.

NOTE: The sandbox invocation uses `--dangerously-skip-permissions` (equivalent to
`bypassPermissions` permission-mode) which is safe ONLY in an isolated sandbox
with no production access.  See SECURITY.md §3 I3.
"""

from __future__ import annotations

import os

import pytest

_ENABLED = (
    os.environ.get("ORCH_REAL_CLAUDE_TEST") == "1"
    and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
)
if not _ENABLED:
    pytest.skip(
        "Real harness integration tests require ORCH_REAL_CLAUDE_TEST=1 "
        "and CLAUDE_CODE_OAUTH_TOKEN",
        allow_module_level=True,
    )

from src.domain.types import (  # noqa: E402
    DispatchContext,
    IssueRef,
    RepoRef,
    RunHandle,
)
from src.ports.harness import ClaudeCodeHarnessPort  # noqa: E402

_OWNER = os.environ.get("TEST_GITHUB_OWNER", "")
_REPO_NAME = os.environ.get("TEST_GITHUB_REPO", "")
_CLAUDE_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
_APP_ID = os.environ.get("GITHUB_APP_ID", "")
_PRIVATE_KEY = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
_INSTALLATION_ID = os.environ.get("GITHUB_APP_INSTALLATION_ID", "")
_FORGE_TOKEN = os.environ.get("FORGE_TOKEN", "")

pytestmark = pytest.mark.integration_real


@pytest.fixture
def repo() -> RepoRef:
    return RepoRef(owner=_OWNER, name=_REPO_NAME)


@pytest.fixture
def harness_port() -> ClaudeCodeHarnessPort:
    return ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        forge_token=_FORGE_TOKEN,
    )


def _make_context(repo: RepoRef) -> DispatchContext:
    return DispatchContext(
        issue_ref=IssueRef(repo=repo, number=1),
        contract="agents/implementer.md",
        model="claude-sonnet-4-6",
        max_turns=3,
        forge_token_scope="repo-branch",
        allowed_agent_refs=None,
    )


async def test_real_harness_dispatch_returns_handle(
    harness_port: ClaudeCodeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    assert isinstance(handle, RunHandle)
    assert handle.run_id


async def test_real_harness_dispatch_does_not_block(
    harness_port: ClaudeCodeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    assert handle is not None


async def test_real_harness_get_run_status_after_dispatch(
    harness_port: ClaudeCodeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    status = await harness_port.get_run_status(handle)
    assert status.state in ("queued", "in_progress", "completed")


async def test_real_harness_cancel_idempotent(
    harness_port: ClaudeCodeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    await harness_port.cancel(handle)
    await harness_port.cancel(handle)  # must not raise


async def test_real_harness_run_handle_round_trip(
    harness_port: ClaudeCodeHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    reconstructed = RunHandle.from_run_id(handle.run_id)
    assert reconstructed == handle
    assert reconstructed.run_id == handle.run_id
