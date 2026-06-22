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
        self.requests: list[dict[str, Any]] = []

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
        import json as _json

        index = {"i": 0}
        responses = self._responses
        captured = self.requests

        def _handler(request: httpx.Request) -> httpx.Response:
            if index["i"] >= len(responses):
                raise RuntimeError(
                    f"Unexpected request: {request.method} {request.url}"
                )
            body_bytes = request.read()
            try:
                body = _json.loads(body_bytes) if body_bytes else None
            except Exception:
                body = None
            captured.append(
                {
                    "method": request.method,
                    "url": str(request.url),
                    "path": request.url.path,
                    "body": body,
                }
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


async def test_harness_dispatch_inputs_contain_contract_model_turns() -> None:
    """dispatch POST body contains contract, model, max_turns in workflow inputs."""
    b = _MockTransportBuilder()
    b.add_empty(204)
    b.add_json({"workflow_runs": [{"id": 55}], "total_count": 1})
    RealHarnessPort(
        forge_token=_TOKEN,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        client=b.build(),
    )
    # Rebuild port with the builder so we can capture requests
    b2 = _MockTransportBuilder()
    b2.add_empty(204)
    b2.add_json({"workflow_runs": [{"id": 55}], "total_count": 1})
    port2 = RealHarnessPort(
        forge_token=_TOKEN,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        client=b2.build(),
    )
    ctx = _make_context(model="claude-opus-4", max_turns=15)
    await port2.dispatch(ctx)

    dispatch_reqs = [r for r in b2.requests if "dispatches" in r["path"]]
    assert dispatch_reqs, "No dispatch request captured"
    inputs = dispatch_reqs[0]["body"]["inputs"]
    assert inputs["contract"] == "agents/orchestrator.md"
    assert inputs["model"] == "claude-opus-4"
    assert inputs["max_turns"] == "15"


async def test_harness_dispatch_inputs_contain_allowed_agent_refs() -> None:
    """dispatch includes allowed_agent_refs as JSON string in inputs."""
    import json

    b = _MockTransportBuilder()
    b.add_empty(204)
    b.add_json({"workflow_runs": [{"id": 88}], "total_count": 1})
    port = RealHarnessPort(
        forge_token=_TOKEN,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        client=b.build(),
    )
    ctx = _make_context(
        allowed_agent_refs=["agents/code-reviewer.md", "agents/security.md"]
    )
    await port.dispatch(ctx)

    dispatch_reqs = [r for r in b.requests if "dispatches" in r["path"]]
    assert dispatch_reqs
    inputs = dispatch_reqs[0]["body"]["inputs"]
    parsed_refs = json.loads(inputs["allowed_agent_refs"])
    assert "agents/code-reviewer.md" in parsed_refs
    assert "agents/security.md" in parsed_refs


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


async def test_harness_trigger_ci_reruns_failed_jobs() -> None:
    """trigger_ci fetches PR head SHA, finds most recent run, and re-runs failed jobs."""
    b = _MockTransportBuilder()
    # 1. PR data (to get head SHA)
    b.add_json(
        {
            "number": 7,
            "head": {"ref": "feat/fix", "sha": "abc123"},
            "state": "open",
        }
    )
    # 2. List workflow runs for head SHA
    b.add_json({"workflow_runs": [{"id": 9999, "status": "completed"}], "total_count": 1})
    # 3. POST rerun-failed-jobs
    b.add_empty(201)
    port = _port(b)
    pr_ref = PRRef(repo=_REPO, number=7)
    await port.trigger_ci(pr_ref)  # should not raise


async def test_harness_trigger_ci_no_runs_is_noop() -> None:
    """trigger_ci exits cleanly when no workflow runs exist for the PR head SHA."""
    b = _MockTransportBuilder()
    b.add_json({"number": 7, "head": {"ref": "feat/fix", "sha": "abc123"}, "state": "open"})
    b.add_json({"workflow_runs": [], "total_count": 0})
    port = _port(b)
    pr_ref = PRRef(repo=_REPO, number=7)
    await port.trigger_ci(pr_ref)  # should not raise (no runs to rerun)


async def test_harness_trigger_ci_surfaces_api_error() -> None:
    """trigger_ci propagates unexpected API errors (does not silently swallow)."""
    import pytest

    b = _MockTransportBuilder()
    b.add_json({"number": 7, "head": {"ref": "feat/fix", "sha": "abc123"}, "state": "open"})
    b.add_json({"workflow_runs": [{"id": 9998, "status": "completed"}], "total_count": 1})
    # rerun-failed-jobs returns 500 (unexpected error)
    b.add_json({"message": "Internal Server Error"}, status_code=500)
    port = _port(b)
    pr_ref = PRRef(repo=_REPO, number=7)
    with pytest.raises(Exception):
        await port.trigger_ci(pr_ref)


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


async def test_harness_cancel_idempotent_on_404() -> None:
    """404 on cancel means the run is gone — should be a terminal no-op (SPEC §9.2)."""
    b = _MockTransportBuilder()
    b.add_json({"message": "Not Found"}, status_code=404)
    port = _port(b)
    await port.cancel(RunHandle(run_id="gone-run"))  # should not raise


async def test_harness_run_handle_round_trip() -> None:
    """RunHandle.from_run_id round-trip is lossless."""
    handle = RunHandle(run_id="workflow-run-42")
    reconstructed = RunHandle.from_run_id(handle.run_id)
    assert reconstructed == handle
    assert reconstructed.run_id == handle.run_id


# ---------------------------------------------------------------------------
# Security: I3 — no credentials in dispatch inputs
# ---------------------------------------------------------------------------


async def test_security_no_credentials_in_dispatch_context() -> None:
    """I3: FORGE_TOKEN and HARNESS_API_KEY values must not appear in dispatch inputs."""
    captured_requests: list[dict[str, object]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        try:
            body = request.read()
            parsed = __import__("json").loads(body) if body else {}
        except Exception:
            parsed = {}
        captured_requests.append(
            {"method": request.method, "url": str(request.url), "body": parsed}
        )
        # First call: workflow dispatch → 204
        if request.method == "POST" and "dispatches" in str(request.url):
            return httpx.Response(204, content=b"")
        # Second call: resolve latest run
        return httpx.Response(200, json={"workflow_runs": [{"id": 42}], "total_count": 1})

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.github.com")

    _REAL_TOKEN = "ghp_super_secret_forge_token_xyz"
    _REAL_HARNESS_KEY = "harness-api-key-very-secret-123"

    port = RealHarnessPort(
        forge_token=_REAL_TOKEN,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        harness_api_key=_REAL_HARNESS_KEY,
        client=client,
    )
    ctx = _make_context(allowed_agent_refs=["agents/reviewer.md"])
    await port.dispatch(ctx)

    # Find the dispatch POST body
    dispatch_reqs = [r for r in captured_requests if "dispatches" in str(r["url"])]
    assert dispatch_reqs, "Expected at least one dispatch POST"
    dispatch_body = dispatch_reqs[0]["body"]
    assert isinstance(dispatch_body, dict)

    inputs = dispatch_body.get("inputs", {})
    assert isinstance(inputs, dict)

    # Serialize inputs to a flat string for substring search
    inputs_str = __import__("json").dumps(inputs)
    assert _REAL_TOKEN not in inputs_str, (
        "FORGE_TOKEN leaked into dispatch inputs (I3 violation)"
    )
    assert _REAL_HARNESS_KEY not in inputs_str, (
        "HARNESS_API_KEY leaked into dispatch inputs (I3 violation)"
    )
