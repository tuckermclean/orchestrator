"""Unit tests for ExecutionBackend implementations.

Tests covered:
  - SubprocessBackend parity with original ClaudeCodeHarnessPort subprocess logic
    (all observable behaviours preserved after the ExecutionBackend refactor).
  - K8sJobBackend Job-spec construction — correct image, env keys, I3 (master
    creds ABSENT), I9 (ORCHESTRATOR_ALLOWED_AGENT_REFS present when set).
  - K8sJobBackend watch/poll loop: success, failure, timeout, read-error paths.
  - K8sJobBackend cancel: Job is deleted from the cluster.
  - Backend factory: HARNESS_EXECUTION_BACKEND env-var selects correct backend.
  - FakeExecutionBackend: completeness smoke-test.

Real K8s end-to-end tests are in tests/integration/test_k8s_backend_real.py and
skip when no cluster is available (env-gated, @pytest.mark.integration_real).

Security invariants asserted here:
  I3 — master credentials (App private key, FORGE_TOKEN, OPERATOR_SECRET_KEY)
       ABSENT from K8s Job pod env (test_k8s_i3_master_creds_absent_from_job_env).
  I9 — ORCHESTRATOR_ALLOWED_AGENT_REFS env var present in Job env when
       allowed_agent_refs is set (test_k8s_i9_allowed_refs_in_job_env);
       hook exit-code-2 denies out-of-set spawns (asserted via existing
       test_harness_i9_hook tests — the hook script itself is unchanged).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any
from unittest.mock import patch

import pytest

from src.ports.execution_backend import (
    FakeExecutionBackend,
    FakeKubeClient,
    K8sJobBackend,
    SubprocessBackend,
    make_execution_backend,
)
from src.ports.harness import ProcessResult, RunEventStore, RunStatus

# ---------------------------------------------------------------------------
# Helpers — fake process double (mirrors test_real_harness_port.py)
# ---------------------------------------------------------------------------

_SCRIPTED_LINES: list[bytes] = [
    json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}).encode(),
    json.dumps({"type": "assistant", "message": {"content": []}}).encode(),
    json.dumps({"type": "result", "subtype": "success", "result": "done"}).encode(),
]


class _FakeProcess:
    def __init__(self, exit_code: int = 0) -> None:
        self.stdout: asyncio.StreamReader = asyncio.StreamReader()
        for line in _SCRIPTED_LINES:
            self.stdout.feed_data(line + b"\n")
        self.stdout.feed_eof()
        self.returncode: int | None = None
        self._exit_code = exit_code
        self.pid = 99999
        self._terminate_called = False
        self._kill_called = False

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code

    def terminate(self) -> None:
        self._terminate_called = True
        self.returncode = -15

    def kill(self) -> None:
        self._kill_called = True
        self.returncode = -9


def _make_runner(
    exit_code: int = 0,
) -> tuple[Any, list[dict[str, Any]], _FakeProcess]:
    """Return (runner_coroutine, call_log, fake_process)."""
    fp = _FakeProcess(exit_code=exit_code)
    calls: list[dict[str, Any]] = []

    async def runner(args: list[str], cwd: str, env: dict[str, str]) -> ProcessResult:
        calls.append({"args": args, "cwd": cwd, "env": env})
        return ProcessResult(fp)  # type: ignore[arg-type]

    return runner, calls, fp


def _run_id() -> str:
    return str(uuid.uuid4())


# ===========================================================================
# SubprocessBackend — parity tests
# ===========================================================================


@pytest.mark.covers("§9.2", "subprocess-backend-dispatch")
async def test_subprocess_backend_dispatch_calls_runner() -> None:
    """SubprocessBackend.dispatch() calls the ProcessRunner with correct args."""
    runner, calls, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "hello"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        event_store=store,
    )
    # Runner must have been called
    assert calls, "ProcessRunner was never called"
    assert calls[0]["args"] == ["claude", "-p", "hello"]
    assert calls[0]["cwd"] == "/tmp/repo"


@pytest.mark.covers("§9.2", "subprocess-backend-status-in-progress")
async def test_subprocess_backend_sets_in_progress() -> None:
    """SubprocessBackend sets status to in_progress while process runs."""
    runner, _, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={},
        event_store=store,
    )
    # Give background watcher time to start and set in_progress
    await asyncio.sleep(0.01)
    status = store.get_status(run_id)
    assert status.state in ("in_progress", "completed")


@pytest.mark.covers("§9.2", "subprocess-backend-success")
async def test_subprocess_backend_success_on_zero_exit() -> None:
    """SubprocessBackend records success when process exits 0."""
    runner, _, _ = _make_runner(exit_code=0)
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={},
        event_store=store,
    )
    await asyncio.sleep(0.1)
    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "success"


@pytest.mark.covers("§9.2", "subprocess-backend-failure")
async def test_subprocess_backend_failure_on_nonzero_exit() -> None:
    """SubprocessBackend records failure when process exits non-zero."""
    runner, _, _ = _make_runner(exit_code=1)
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={},
        event_store=store,
    )
    await asyncio.sleep(0.1)
    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "failure"


@pytest.mark.covers("§9.2", "subprocess-backend-events")
async def test_subprocess_backend_captures_json_events() -> None:
    """SubprocessBackend parses stream-json lines and records events."""
    runner, _, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={},
        event_store=store,
    )
    await asyncio.sleep(0.1)
    events = store.get_events(run_id)
    assert len(events) > 0
    event_types = {e.event_type for e in events}
    assert event_types & {"system", "assistant", "result"}


@pytest.mark.covers("§9.2", "subprocess-backend-cancel")
async def test_subprocess_backend_cancel_marks_cancelled() -> None:
    """SubprocessBackend.cancel() marks run as completed/cancelled."""
    runner, _, fp = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={},
        event_store=store,
    )
    # Force in_progress so cancel is not a no-op at the harness level
    store.set_status(run_id, RunStatus(state="in_progress"))

    await backend.cancel(run_id=run_id, event_store=store)

    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "cancelled"


@pytest.mark.covers("§9.2", "subprocess-backend-queue-sentinel")
async def test_subprocess_backend_queue_sentinel_on_completion() -> None:
    """SubprocessBackend emits None sentinel in queue when run completes."""
    runner, _, _ = _make_runner(exit_code=0)
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={},
        event_store=store,
    )
    await asyncio.sleep(0.1)
    queue = store.get_queue(run_id)
    assert queue is not None
    items: list[object] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    assert None in items, "No completion sentinel in event queue"


# ===========================================================================
# K8sJobBackend — Job-spec construction
# ===========================================================================


def _make_k8s_backend(
    fake_client: FakeKubeClient | None = None,
    *,
    poll_interval_s: float = 0.001,
    job_timeout_s: float = 10.0,
) -> K8sJobBackend:
    return K8sJobBackend(
        image="ghcr.io/test/orchestrator-agent-runner:test",
        namespace="test-ns",
        kube_client=fake_client or FakeKubeClient(),
        poll_interval_s=poll_interval_s,
        job_timeout_s=job_timeout_s,
    )


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-image")
def test_k8s_build_job_spec_has_correct_image() -> None:
    """K8sJobBackend._build_job_spec uses the configured image."""
    backend = _make_k8s_backend()
    spec = backend._build_job_spec(
        "run-abc123",
        ["claude", "-p", "hello"],
        {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
    )
    containers = spec["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    assert containers[0]["image"] == "ghcr.io/test/orchestrator-agent-runner:test"


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-command")
def test_k8s_build_job_spec_has_correct_command() -> None:
    """K8sJobBackend._build_job_spec sets the claude command correctly."""
    backend = _make_k8s_backend()
    cmd = ["claude", "-p", "hello", "--output-format", "stream-json"]
    spec = backend._build_job_spec("run-abc", cmd, {})
    containers = spec["spec"]["template"]["spec"]["containers"]
    assert containers[0]["command"] == cmd


@pytest.mark.covers("§9.2", "k8s-i3-master-creds-absent")
def test_k8s_i3_master_creds_absent_from_job_env() -> None:
    """I3: master credentials MUST NOT appear in the K8s Job pod env.

    The harness's _build_child_env() guarantees child_env contains only
    CLAUDE_CODE_OAUTH_TOKEN and GH_TOKEN (scoped).  This test confirms that
    _build_job_spec forwards child_env verbatim and does not add any master
    credentials of its own.
    """
    _MASTER_APP_KEY = "-----BEGIN RSA PRIVATE KEY-----\nSECRET_KEY\n-----END RSA PRIVATE KEY-----"
    _MASTER_FORGE_TOKEN = "ghp_master_forge_secret"
    _MASTER_OPERATOR_KEY = "OPERATOR_SECRET_KEY_VALUE"

    backend = _make_k8s_backend()
    # Simulate the scoped child_env that the harness passes (I3 compliant)
    child_env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oauth-scoped",
        "GH_TOKEN": "ghp_scoped_token_only",
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": "/usr/bin:/bin",
        "HOME": "/workspace",
    }
    spec = backend._build_job_spec("run-i3test", ["claude", "-p", "x"], child_env)
    containers = spec["spec"]["template"]["spec"]["containers"]
    env_list: list[dict[str, str]] = containers[0]["env"]
    env_keys = {e["name"] for e in env_list}

    # Required scoped creds must be present
    assert "CLAUDE_CODE_OAUTH_TOKEN" in env_keys, "CLAUDE_CODE_OAUTH_TOKEN missing from pod env"
    assert "GH_TOKEN" in env_keys, "GH_TOKEN missing from pod env"

    # Master credentials must NOT appear anywhere in the env
    all_env_str = str(env_list)
    assert _MASTER_APP_KEY not in all_env_str, "App private key leaked into pod env (I3)"
    assert _MASTER_FORGE_TOKEN not in all_env_str, "Master FORGE_TOKEN leaked into pod env (I3)"
    assert _MASTER_OPERATOR_KEY not in all_env_str, "OPERATOR_SECRET_KEY leaked into pod env (I3)"

    # The scoped token must be the only GH_TOKEN value
    gh_entries = [e for e in env_list if e["name"] == "GH_TOKEN"]
    assert len(gh_entries) == 1
    assert gh_entries[0]["value"] == "ghp_scoped_token_only"


@pytest.mark.covers("§9.2", "k8s-i9-allowed-refs-env-var")
def test_k8s_i9_allowed_refs_in_job_env() -> None:
    """I9: ORCHESTRATOR_ALLOWED_AGENT_REFS present in Job env when set.

    The harness injects this env var into child_env when allowed_agent_refs is
    not None.  K8sJobBackend must carry it through to the pod.
    """
    backend = _make_k8s_backend()
    child_env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "tok",
        "GH_TOKEN": "gh",
        "ORCHESTRATOR_ALLOWED_AGENT_REFS": "agents/reviewer.md,agents/fixer.md",
    }
    spec = backend._build_job_spec("run-i9test", ["claude", "-p", "x"], child_env)
    containers = spec["spec"]["template"]["spec"]["containers"]
    env_list: list[dict[str, str]] = containers[0]["env"]
    env_map = {e["name"]: e["value"] for e in env_list}

    assert "ORCHESTRATOR_ALLOWED_AGENT_REFS" in env_map, (
        "ORCHESTRATOR_ALLOWED_AGENT_REFS must be in pod env (I9)"
    )
    assert "agents/reviewer.md" in env_map["ORCHESTRATOR_ALLOWED_AGENT_REFS"]
    assert "agents/fixer.md" in env_map["ORCHESTRATOR_ALLOWED_AGENT_REFS"]


@pytest.mark.covers("§9.2", "k8s-i9-no-refs-env-var-absent")
def test_k8s_i9_no_allowed_refs_env_var_absent() -> None:
    """I9: when no allowed_agent_refs, ORCHESTRATOR_ALLOWED_AGENT_REFS absent.

    The harness does not inject the env var when allowed_agent_refs is None,
    so child_env won't contain it.  K8sJobBackend must not add it either.
    """
    backend = _make_k8s_backend()
    child_env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "tok",
        "GH_TOKEN": "gh",
        # ORCHESTRATOR_ALLOWED_AGENT_REFS deliberately absent
    }
    spec = backend._build_job_spec("run-no-refs", ["claude", "-p", "x"], child_env)
    containers = spec["spec"]["template"]["spec"]["containers"]
    env_list: list[dict[str, str]] = containers[0]["env"]
    env_keys = {e["name"] for e in env_list}

    assert "ORCHESTRATOR_ALLOWED_AGENT_REFS" not in env_keys, (
        "ORCHESTRATOR_ALLOWED_AGENT_REFS must NOT appear in pod env when not set"
    )


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-structure")
def test_k8s_build_job_spec_structure() -> None:
    """K8sJobBackend._build_job_spec produces a valid K8s Job manifest."""
    backend = _make_k8s_backend()
    spec = backend._build_job_spec(
        "run-structtest",
        ["claude", "-p", "hello"],
        {"CLAUDE_CODE_OAUTH_TOKEN": "tok"},
    )
    # Top-level keys
    assert spec["apiVersion"] == "batch/v1"
    assert spec["kind"] == "Job"
    assert "metadata" in spec
    assert "spec" in spec
    # Job spec required fields
    job_spec = spec["spec"]
    assert job_spec["backoffLimit"] == 0  # no K8s retries
    assert "ttlSecondsAfterFinished" in job_spec
    # Pod template
    template = job_spec["template"]
    assert "spec" in template
    pod_spec = template["spec"]
    assert pod_spec["restartPolicy"] == "Never"
    assert len(pod_spec["containers"]) == 1
    # Security context — non-root
    container_ctx = pod_spec["containers"][0]["securityContext"]
    assert container_ctx["runAsNonRoot"] is True
    assert container_ctx["allowPrivilegeEscalation"] is False


# ===========================================================================
# K8sJobBackend — watch/poll loop
# ===========================================================================


@pytest.mark.covers("§9.2", "k8s-backend-dispatch-creates-job")
async def test_k8s_backend_dispatch_creates_job() -> None:
    """K8sJobBackend.dispatch() creates a Job via the kube client."""
    fake_client = FakeKubeClient()
    backend = _make_k8s_backend(fake_client)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    # Configure immediate success
    fake_client.configure_job_outcome(
        f"orch-agent-{run_id[:16]}",
        statuses=[{"metadata": {"name": f"orch-agent-{run_id[:16]}"}, "status": {"succeeded": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "hello"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        event_store=store,
    )

    assert len(fake_client.created_jobs) == 1
    body = fake_client.created_jobs[0]["body"]
    assert body["kind"] == "Job"
    assert fake_client.created_jobs[0]["namespace"] == "test-ns"


@pytest.mark.covers("§9.2", "k8s-backend-success")
async def test_k8s_backend_watch_success() -> None:
    """K8sJobBackend records success when Job.status.succeeded > 0."""
    fake_client = FakeKubeClient()
    backend = _make_k8s_backend(fake_client, poll_interval_s=0.001)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    job_name = f"orch-agent-{run_id[:16]}"
    fake_client.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"succeeded": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        event_store=store,
    )
    await asyncio.sleep(0.1)

    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "success"


@pytest.mark.covers("§9.2", "k8s-backend-failure")
async def test_k8s_backend_watch_failure() -> None:
    """K8sJobBackend records failure when Job.status.failed > 0."""
    fake_client = FakeKubeClient()
    backend = _make_k8s_backend(fake_client, poll_interval_s=0.001)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    job_name = f"orch-agent-{run_id[:16]}"
    fake_client.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"failed": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        event_store=store,
    )
    await asyncio.sleep(0.1)

    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "failure"


@pytest.mark.covers("§9.2", "k8s-backend-timeout")
async def test_k8s_backend_watch_timeout() -> None:
    """K8sJobBackend records failure and emits timeout event when deadline exceeded."""
    fake_client = FakeKubeClient()
    # Very short timeout to trigger quickly
    backend = _make_k8s_backend(fake_client, poll_interval_s=0.001, job_timeout_s=0.01)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    # No success/failure status — job stays pending forever
    # (FakeKubeClient returns empty status by default)

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        event_store=store,
    )
    await asyncio.sleep(0.2)

    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "failure"

    # Timeout event must be recorded
    events = store.get_events(run_id)
    timeout_events = [e for e in events if e.event_type == "k8s_job_timeout"]
    assert timeout_events, "No k8s_job_timeout event recorded"


@pytest.mark.covers("§9.2", "k8s-backend-read-error")
async def test_k8s_backend_watch_read_error_retries() -> None:
    """K8sJobBackend records k8s_read_error and retries on API read failure."""

    class _ErrorThenSuccessClient(FakeKubeClient):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0
            self._job_name: str = ""

        def read_namespaced_job(self, name: str, namespace: str) -> dict[str, Any]:
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("simulated API error")
            return {"metadata": {"name": name}, "status": {"succeeded": 1}}

    fake_client = _ErrorThenSuccessClient()
    backend = _make_k8s_backend(fake_client, poll_interval_s=0.001)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        event_store=store,
    )
    await asyncio.sleep(0.2)

    # Should eventually succeed after retry
    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "success"

    # Error event should be recorded for the failed read
    events = store.get_events(run_id)
    error_events = [e for e in events if e.event_type == "k8s_read_error"]
    assert error_events, "No k8s_read_error event recorded"


@pytest.mark.covers("§9.2", "k8s-backend-cancel")
async def test_k8s_backend_cancel_deletes_job() -> None:
    """K8sJobBackend.cancel() deletes the K8s Job and marks run cancelled."""
    fake_client = FakeKubeClient()
    # Long timeout so the watcher doesn't finish before cancel
    backend = _make_k8s_backend(fake_client, poll_interval_s=60.0, job_timeout_s=3600.0)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        event_store=store,
    )

    # Force in_progress so cancel doesn't no-op
    store.set_status(run_id, RunStatus(state="in_progress"))

    await backend.cancel(run_id=run_id, event_store=store)

    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "cancelled"

    # Job must be deleted
    job_name = f"orch-agent-{run_id[:16]}"
    assert job_name in fake_client.deleted_jobs, "Job not deleted on cancel"


@pytest.mark.covers("§9.2", "k8s-backend-job-created-event")
async def test_k8s_backend_dispatch_emits_job_created_event() -> None:
    """K8sJobBackend.dispatch() emits k8s_job_created event."""
    fake_client = FakeKubeClient()
    backend = _make_k8s_backend(fake_client, poll_interval_s=0.001)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    job_name = f"orch-agent-{run_id[:16]}"
    fake_client.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"succeeded": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        event_store=store,
    )
    await asyncio.sleep(0.05)

    events = store.get_events(run_id)
    created_events = [e for e in events if e.event_type == "k8s_job_created"]
    assert created_events, "No k8s_job_created event emitted"
    assert created_events[0].data["job_name"] == job_name


@pytest.mark.covers("§9.2", "k8s-backend-cleanup-job-on-success")
async def test_k8s_backend_cleanup_job_on_success() -> None:
    """K8sJobBackend deletes the Job after successful completion."""
    fake_client = FakeKubeClient()
    backend = _make_k8s_backend(fake_client, poll_interval_s=0.001)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    job_name = f"orch-agent-{run_id[:16]}"
    fake_client.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"succeeded": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/repo",
        work_dir="/tmp/work",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        event_store=store,
    )
    await asyncio.sleep(0.1)

    assert job_name in fake_client.deleted_jobs, "Job not deleted after successful completion"


# ===========================================================================
# ClaudeCodeHarnessPort — backend wiring (regression: existing behaviour intact)
# ===========================================================================


@pytest.mark.covers("§9.2", "harness-delegates-to-backend")
async def test_harness_dispatch_delegates_to_backend() -> None:
    """ClaudeCodeHarnessPort.dispatch() delegates to the configured backend."""
    from unittest.mock import AsyncMock, patch

    from src.ports.harness import ClaudeCodeHarnessPort

    fake_backend = FakeExecutionBackend()

    port = ClaudeCodeHarnessPort(
        claude_oauth_token="tok",
        app_id="app",
        private_key_pem="pem",
        installation_id="inst",
        repo_owner="acme",
        repo_name="myrepo",
        execution_backend=fake_backend,
    )

    from src.domain.types import DispatchContext

    ctx = DispatchContext(
        contract="agents/implementer.md",
        model="claude-sonnet-4-6",
        max_turns=30,
        forge_token_scope="repo-branch",
    )
    with patch(
        "src.ports.harness._mint_scoped_installation_token",
        new=AsyncMock(return_value="scoped-token"),
    ), patch.object(
        ClaudeCodeHarnessPort, "_clone_repo", new=AsyncMock(return_value=None)
    ):
        handle = await port.dispatch(ctx)

    assert handle is not None
    assert len(fake_backend.dispatched) == 1, "Backend was not called"
    dispatched = fake_backend.dispatched[0]
    assert dispatched["run_id"] == handle.run_id
    assert "claude" in dispatched["claude_args"]


@pytest.mark.covers("§9.2", "harness-cancel-delegates-to-backend")
async def test_harness_cancel_delegates_to_backend() -> None:
    """ClaudeCodeHarnessPort.cancel() delegates to the configured backend.

    We register a run manually (not via dispatch) with in_progress status so
    that the harness's terminal-guard doesn't block the cancel() call.
    """
    from src.ports.harness import ClaudeCodeHarnessPort, RunHandle

    fake_backend = FakeExecutionBackend()

    port = ClaudeCodeHarnessPort(
        claude_oauth_token="tok",
        app_id="app",
        private_key_pem="pem",
        installation_id="inst",
        repo_owner="acme",
        repo_name="myrepo",
        execution_backend=fake_backend,
    )

    # Register a run in in_progress state (simulates a live run)
    run_id = _run_id()
    port._event_store.register(run_id)
    port._event_store.set_status(run_id, RunStatus(state="in_progress"))

    handle = RunHandle(run_id=run_id)
    await port.cancel(handle)

    assert run_id in fake_backend.cancelled, "Backend cancel was not called"


# ===========================================================================
# Backend factory
# ===========================================================================


@pytest.mark.covers("§9.2", "backend-factory-subprocess-default")
def test_backend_factory_defaults_to_subprocess() -> None:
    """make_execution_backend() returns SubprocessBackend by default."""
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_EXECUTION_BACKEND"}
    with patch.dict(os.environ, env, clear=True):
        backend = make_execution_backend()
    assert isinstance(backend, SubprocessBackend)


@pytest.mark.covers("§9.2", "backend-factory-k8s-env")
def test_backend_factory_returns_k8s_when_env_set() -> None:
    """make_execution_backend() returns K8sJobBackend when HARNESS_EXECUTION_BACKEND=k8s."""
    fake_kube = FakeKubeClient()
    with patch.dict(os.environ, {"HARNESS_EXECUTION_BACKEND": "k8s"}):
        backend = make_execution_backend(kube_client=fake_kube)
    assert isinstance(backend, K8sJobBackend)


@pytest.mark.covers("§9.2", "backend-factory-subprocess-explicit")
def test_backend_factory_subprocess_explicit() -> None:
    """make_execution_backend() returns SubprocessBackend when explicitly set."""
    with patch.dict(os.environ, {"HARNESS_EXECUTION_BACKEND": "subprocess"}):
        backend = make_execution_backend()
    assert isinstance(backend, SubprocessBackend)


# ===========================================================================
# FakeExecutionBackend — smoke test
# ===========================================================================


async def test_fake_backend_dispatch_records_call() -> None:
    """FakeExecutionBackend records dispatched calls."""
    fake = FakeExecutionBackend()
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await fake.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/r",
        work_dir="/tmp/w",
        child_env={"GH_TOKEN": "gh"},
        event_store=store,
    )
    assert len(fake.dispatched) == 1
    assert fake.dispatched[0]["run_id"] == run_id
    assert store.get_status(run_id).conclusion == "success"


async def test_fake_backend_configure_fail() -> None:
    """FakeExecutionBackend.configure(fail_dispatch=True) marks run as failure."""
    fake = FakeExecutionBackend()
    fake.configure(fail_dispatch=True)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await fake.dispatch(
        run_id=run_id,
        claude_args=["claude", "-p", "x"],
        repo_dir="/tmp/r",
        work_dir="/tmp/w",
        child_env={},
        event_store=store,
    )
    assert store.get_status(run_id).conclusion == "failure"


async def test_fake_backend_cancel_records_call() -> None:
    """FakeExecutionBackend records cancel calls."""
    fake = FakeExecutionBackend()
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)

    await fake.cancel(run_id=run_id, event_store=store)
    assert run_id in fake.cancelled
    assert store.get_status(run_id).conclusion == "cancelled"
