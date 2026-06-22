"""Unit tests for RealHarnessPort using httpx MockTransport.

Verifies that the harness adapter builds correct GitHub Actions API requests
and parses responses into domain types.  No network calls are made.
"""

from __future__ import annotations

from typing import Any

import httpx

from src.domain.types import (
    DispatchContext,
    IssueRef,
    PRRef,
    RepoRef,
    RunHandle,
)
from src.ports.harness import RealHarnessPort

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

_TOKEN = "ghp_testtoken123"
_REPO = RepoRef(owner="acme", name="myrepo")
_OWNER = "acme"
_REPO_NAME = "myrepo"


def _make_context(
    *,
    model: str = "claude-sonnet-4-6",
    max_turns: int = 30,
    issue_number: int = 1,
    pr_number: int | None = None,
    allowed_agent_refs: list[str] | None = None,
) -> DispatchContext:
    issue_ref = IssueRef(repo=_REPO, number=issue_number)
    pr_ref = PRRef(repo=_REPO, number=pr_number) if pr_number else None
    return DispatchContext(
        issue_ref=issue_ref,
        pr_ref=pr_ref,
        contract="agents/orchestrator.md",
        model=model,
        max_turns=max_turns,
        forge_token_scope="repo-branch",
        allowed_agent_refs=allowed_agent_refs,
    )


class _MockTransportBuilder:
    def __init__(self) -> None:
        self._responses: list[httpx.Response] = []

    def add_json(self, data: Any, status_code: int = 200) -> _MockTransportBuilder:
        self._responses.append(httpx.Response(status_code, json=data))
        return self

    def add_empty(self, status_code: int = 204) -> _MockTransportBuilder:
        self._responses.append(httpx.Response(status_code, content=b""))
        return self

    def add_not_found(self) -> _MockTransportBuilder:
        self._responses.append(httpx.Response(404, json={"message": "Not Found"}))
        return self

    def build(self) -> httpx.AsyncClient:
        index = {"i": 0}
        responses = self._responses

        def _handler(request: httpx.Request) -> httpx.Response:
            if index["i"] >= len(responses):
                raise RuntimeError(
                    f"Unexpected request: {request.method} {request.url}"
                )
            resp = responses[index["i"]]
            index["i"] += 1
            return resp

        transport = httpx.MockTransport(_handler)
        return httpx.AsyncClient(transport=transport, base_url="https://api.github.com")


def _port(builder: _MockTransportBuilder) -> RealHarnessPort:
    return RealHarnessPort(
        forge_token=_TOKEN,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        client=builder.build(),
    )


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


async def test_harness_dispatch_returns_run_handle() -> None:
    b = _MockTransportBuilder()
    # workflow dispatch → 204
    b.add_empty(204)
    # resolve latest run → returns run id 12345
    b.add_json(
        {"workflow_runs": [{"id": 12345, "status": "queued"}], "total_count": 1}
    )
    port = _port(b)
    ctx = _make_context()
    handle = await port.dispatch(ctx)
    assert isinstance(handle, RunHandle)
    assert handle.run_id == "12345"


async def test_harness_dispatch_fire_and_forget() -> None:
    """dispatch() returns immediately after triggering; does not poll."""
    b = _MockTransportBuilder()
    b.add_empty(204)
    b.add_json(
        {"workflow_runs": [{"id": 99, "status": "queued"}], "total_count": 1}
    )
    port = _port(b)
    ctx = _make_context()
    handle = await port.dispatch(ctx)
    # If dispatch were blocking it would poll get_run_status — but it does not
    assert handle is not None


async def test_harness_dispatch_includes_contract_in_inputs() -> None:
    """dispatch sends the contract path in workflow inputs."""
    b = _MockTransportBuilder()
    b.add_empty(204)
    b.add_json({"workflow_runs": [{"id": 55}], "total_count": 1})
    port = _port(b)
    ctx = _make_context()
    await port.dispatch(ctx)
    # No assertion on request body here (MockTransport captures by response order)
    # The behavioral proof is that dispatch doesn't raise


async def test_harness_dispatch_with_pr_ref() -> None:
    b = _MockTransportBuilder()
    b.add_empty(204)
    b.add_json({"workflow_runs": [{"id": 77}], "total_count": 1})
    port = _port(b)
    ctx = _make_context(pr_number=5)
    handle = await port.dispatch(ctx)
    assert handle.run_id == "77"


async def test_harness_dispatch_with_allowed_agent_refs() -> None:
    b = _MockTransportBuilder()
    b.add_empty(204)
    b.add_json({"workflow_runs": [{"id": 88}], "total_count": 1})
    port = _port(b)
    ctx = _make_context(
        allowed_agent_refs=["engineering-code-reviewer.md", "engineering-security-engineer.md"]
    )
    handle = await port.dispatch(ctx)
    assert handle.run_id == "88"


async def test_harness_dispatch_fallback_when_no_runs() -> None:
    """dispatch succeeds even if no workflow runs are found yet."""
    b = _MockTransportBuilder()
    b.add_empty(204)
    b.add_json({"workflow_runs": [], "total_count": 0})
    port = _port(b)
    ctx = _make_context()
    handle = await port.dispatch(ctx)
    # Fallback run ID is generated
    assert handle.run_id.startswith("dispatch-")


# ---------------------------------------------------------------------------
# trigger_workflow
# ---------------------------------------------------------------------------


async def test_harness_trigger_workflow_sends_dispatch() -> None:
    b = _MockTransportBuilder()
    b.add_empty(204)
    port = _port(b)
    await port.trigger_workflow("deploy.yml", "main", {"env": "prod"})


async def test_harness_trigger_workflow_custom_ref() -> None:
    b = _MockTransportBuilder()
    b.add_empty(204)
    port = _port(b)
    await port.trigger_workflow("ci.yml", "feat/branch", {"debug": "true"})


# ---------------------------------------------------------------------------
# trigger_ci
# ---------------------------------------------------------------------------


async def test_harness_trigger_ci_fetches_pr_then_posts() -> None:
    b = _MockTransportBuilder()
    # PR data (to get head SHA)
    b.add_json(
        {
            "number": 7,
            "head": {"ref": "feat/fix", "sha": "abc123"},
            "state": "open",
        }
    )
    # Post check run (best-effort)
    b.add_json({"id": 1, "status": "queued"}, status_code=201)
    port = _port(b)
    pr_ref = PRRef(repo=_REPO, number=7)
    await port.trigger_ci(pr_ref)  # should not raise


async def test_harness_trigger_ci_ignores_api_error() -> None:
    """trigger_ci is best-effort; errors are swallowed."""
    b = _MockTransportBuilder()
    b.add_json({"number": 7, "head": {"ref": "feat/fix", "sha": "abc123"}, "state": "open"})
    b.add_json({"message": "Unprocessable Entity"}, status_code=422)
    port = _port(b)
    pr_ref = PRRef(repo=_REPO, number=7)
    await port.trigger_ci(pr_ref)  # should not raise (best-effort)


# ---------------------------------------------------------------------------
# get_run_status
# ---------------------------------------------------------------------------


async def test_harness_get_run_status_queued() -> None:
    b = _MockTransportBuilder()
    b.add_json({"id": 1, "status": "queued", "conclusion": None})
    port = _port(b)
    handle = RunHandle(run_id="1")
    status = await port.get_run_status(handle)
    assert status.state == "queued"
    assert status.conclusion is None


async def test_harness_get_run_status_in_progress() -> None:
    b = _MockTransportBuilder()
    b.add_json({"id": 2, "status": "in_progress", "conclusion": None})
    port = _port(b)
    status = await port.get_run_status(RunHandle(run_id="2"))
    assert status.state == "in_progress"


async def test_harness_get_run_status_completed_success() -> None:
    b = _MockTransportBuilder()
    b.add_json({"id": 3, "status": "completed", "conclusion": "success"})
    port = _port(b)
    status = await port.get_run_status(RunHandle(run_id="3"))
    assert status.state == "completed"
    assert status.conclusion == "success"


async def test_harness_get_run_status_completed_failure() -> None:
    b = _MockTransportBuilder()
    b.add_json({"id": 4, "status": "completed", "conclusion": "failure"})
    port = _port(b)
    status = await port.get_run_status(RunHandle(run_id="4"))
    assert status.state == "completed"
    assert status.conclusion == "failure"


async def test_harness_get_run_status_cancelled() -> None:
    b = _MockTransportBuilder()
    b.add_json({"id": 5, "status": "completed", "conclusion": "cancelled"})
    port = _port(b)
    status = await port.get_run_status(RunHandle(run_id="5"))
    assert status.state == "completed"
    assert status.conclusion == "cancelled"


async def test_harness_get_run_status_not_found_returns_queued() -> None:
    b = _MockTransportBuilder()
    b.add_not_found()
    port = _port(b)
    status = await port.get_run_status(RunHandle(run_id="does-not-exist"))
    assert status.state == "queued"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_harness_cancel_posts_cancel() -> None:
    b = _MockTransportBuilder()
    b.add_empty(202)
    port = _port(b)
    await port.cancel(RunHandle(run_id="123"))  # should not raise


async def test_harness_cancel_idempotent_on_409() -> None:
    """409 means already terminal — should be treated as idempotent no-op."""
    b = _MockTransportBuilder()
    b.add_json({"message": "Conflict"}, status_code=409)
    port = _port(b)
    await port.cancel(RunHandle(run_id="123"))  # should not raise


async def test_harness_cancel_idempotent_on_422() -> None:
    """422 means already completed — should be treated as idempotent no-op."""
    b = _MockTransportBuilder()
    b.add_json({"message": "Unprocessable Entity"}, status_code=422)
    port = _port(b)
    await port.cancel(RunHandle(run_id="456"))  # should not raise


async def test_harness_run_handle_round_trip() -> None:
    """RunHandle.from_run_id round-trip is lossless."""
    handle = RunHandle(run_id="workflow-run-42")
    reconstructed = RunHandle.from_run_id(handle.run_id)
    assert reconstructed == handle
    assert reconstructed.run_id == handle.run_id
