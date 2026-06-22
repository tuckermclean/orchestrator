"""Contract tests for RealHarnessPort against the real GitHub Actions harness.

These tests run the SAME contract suite as test_harness_port.py (FakeHarnessPort)
against the real RealHarnessPort.  They are gated behind @pytest.mark.integration_real
and will only execute when:
  - ORCH_REAL_GITHUB_TEST=1
  - FORGE_TOKEN is set (GitHub token with repo/workflow scope)
  - TEST_GITHUB_OWNER and TEST_GITHUB_REPO point to a sandboxed test repository
    that has the claude-code-action.yml workflow deployed

Without these env vars the entire module is skipped cleanly.

These tests CANNOT run in this autonomous build environment (no live tokens),
but are structured correctly for a credentialed CI/deploy environment.
See ROADMAP.md Step 8 Definition of Done.
"""

from __future__ import annotations

import os

import pytest

_ENABLED = (
    os.environ.get("ORCH_REAL_GITHUB_TEST") == "1"
    and os.environ.get("FORGE_TOKEN")
)
if not _ENABLED:
    pytest.skip(
        "Real harness integration tests require ORCH_REAL_GITHUB_TEST=1 and FORGE_TOKEN",
        allow_module_level=True,
    )

from src.domain.types import (  # noqa: E402
    DispatchContext,
    IssueRef,
    RepoRef,
    RunHandle,
)
from src.ports.harness import RealHarnessPort  # noqa: E402

_OWNER = os.environ.get("TEST_GITHUB_OWNER", "")
_REPO_NAME = os.environ.get("TEST_GITHUB_REPO", "")
_TOKEN = os.environ.get("FORGE_TOKEN", "")

pytestmark = pytest.mark.integration_real


@pytest.fixture
def repo() -> RepoRef:
    return RepoRef(owner=_OWNER, name=_REPO_NAME)


@pytest.fixture
def harness_port() -> RealHarnessPort:
    return RealHarnessPort(
        forge_token=_TOKEN,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
    )


def _make_context(repo: RepoRef) -> DispatchContext:
    return DispatchContext(
        issue_ref=IssueRef(repo=repo, number=1),
        contract="agents/orchestrator.md",
        model="claude-sonnet-4-6",
        max_turns=5,
        forge_token_scope="repo-branch",
        allowed_agent_refs=None,
    )


async def test_real_harness_dispatch_returns_handle(
    harness_port: RealHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    assert isinstance(handle, RunHandle)
    assert handle.run_id


async def test_real_harness_dispatch_does_not_block(
    harness_port: RealHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    # Returns immediately
    assert handle is not None


async def test_real_harness_get_run_status_after_dispatch(
    harness_port: RealHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    status = await harness_port.get_run_status(handle)
    assert status.state in ("queued", "in_progress", "completed")


async def test_real_harness_cancel_idempotent(
    harness_port: RealHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    # Cancel once
    await harness_port.cancel(handle)
    # Cancel again — must not raise
    await harness_port.cancel(handle)


async def test_real_harness_run_handle_round_trip(
    harness_port: RealHarnessPort,
    repo: RepoRef,
) -> None:
    ctx = _make_context(repo)
    handle = await harness_port.dispatch(ctx)
    reconstructed = RunHandle.from_run_id(handle.run_id)
    assert reconstructed == handle
    assert reconstructed.run_id == handle.run_id
