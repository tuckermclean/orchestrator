"""Unit tests for ExecutionBackend implementations.

Tests covered:
  - SubprocessBackend parity with original ClaudeCodeHarnessPort subprocess logic
    (all observable behaviours preserved after the ExecutionBackend refactor).
  - SubprocessBackend now owns the clone: clone is called, hook is written when
    allowed_agent_refs is set, clone failure marks run failed without spawning.
  - K8sJobBackend Job-spec construction — shell-script command (clone + hook +
    claude), correct image, env keys, I3 (master creds ABSENT, token not literal
    in manifest), I9 (ORCHESTRATOR_ALLOWED_AGENT_REFS present when set).
  - K8sJobBackend entry-script content: contains clone URL, GH_TOKEN as env ref
    (not literal), hook setup guarded by ORCHESTRATOR_ALLOWED_AGENT_REFS, exec
    of claude_args.
  - K8sJobBackend watch/poll loop: success, failure, timeout, read-error paths.
  - K8sJobBackend cancel: Job is deleted from the cluster.
  - Regression: dispatch() in harness does NOT clone for the k8s backend —
    all clone activity happens inside the Job pod.
  - Backend factory: HARNESS_EXECUTION_BACKEND env-var selects correct backend.
  - FakeExecutionBackend: completeness smoke-test.

Real K8s end-to-end tests are in tests/integration/test_k8s_backend_real.py and
skip when no cluster is available (env-gated, @pytest.mark.integration_real).

Security invariants asserted here:
  I3 — master credentials (App private key, FORGE_TOKEN, OPERATOR_SECRET_KEY)
       ABSENT from K8s Job pod env (test_k8s_i3_master_creds_absent_from_job_env);
       GH_TOKEN appears only as a pod env var at runtime — NEVER as a literal
       string in the Job manifest or the entry script (test_k8s_i3_token_not_literal_in_manifest).
  I9 — ORCHESTRATOR_ALLOWED_AGENT_REFS env var present in Job env when
       allowed_agent_refs is set (test_k8s_i9_allowed_refs_in_job_env);
       hook exit-code-2 denies out-of-set spawns (asserted via existing
       test_harness_i9_hook tests — the hook script itself is unchanged).
  Regression — control-plane never clones for k8s backend
       (test_k8s_dispatch_does_not_clone_in_control_plane).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.types import DispatchContext
from src.ports.execution_backend import (
    FakeExecutionBackend,
    FakeKubeClient,
    K8sJobBackend,
    SubprocessBackend,
    make_execution_backend,
)
from src.ports.harness import ClaudeCodeHarnessPort, ProcessResult, RunEventStore, RunStatus

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


def _make_fake_harness(
    *,
    clone_raises: Exception | None = None,
    contract_raises: Exception | None = None,
) -> MagicMock:
    """Return a fake harness with _clone_repo, _write_spawn_hook,
    _materialize_contract, and _configure_git_identity mocked."""
    harness = MagicMock()
    if clone_raises is not None:
        harness._clone_repo = AsyncMock(side_effect=clone_raises)
    else:
        harness._clone_repo = AsyncMock(return_value=None)
    harness._write_spawn_hook = MagicMock(return_value=None)
    if contract_raises is not None:
        harness._materialize_contract = MagicMock(side_effect=contract_raises)
    else:
        harness._materialize_contract = MagicMock(return_value=None)
    harness._configure_git_identity = AsyncMock(return_value=None)
    harness._repo_owner = "acme"
    harness._repo_name = "myrepo"
    return harness


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
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    # Runner must have been called
    assert calls, "ProcessRunner was never called"
    assert calls[0]["args"] == ["claude", "-p", "hello"]


@pytest.mark.covers("§9.2", "subprocess-backend-dispatch")
async def test_subprocess_backend_clones_repo_before_running() -> None:
    """SubprocessBackend.dispatch() clones the repo via harness._clone_repo."""
    runner, _, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch="feature/foo",
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    # _clone_repo must have been called with the branch
    harness._clone_repo.assert_called_once()
    _, _, clone_branch = harness._clone_repo.call_args[0]
    assert clone_branch == "feature/foo"


@pytest.mark.covers("§9.2", "subprocess-backend-dispatch")
async def test_subprocess_backend_writes_hook_when_refs_set() -> None:
    """SubprocessBackend writes the I9 hook when allowed_agent_refs is set."""
    runner, _, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=["engineering-code-reviewer.md"],
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    harness._write_spawn_hook.assert_called_once()


@pytest.mark.covers("§9.2", "subprocess-backend-dispatch")
async def test_subprocess_backend_no_hook_when_refs_none() -> None:
    """SubprocessBackend does NOT write the I9 hook when allowed_agent_refs is None."""
    runner, _, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    harness._write_spawn_hook.assert_not_called()


@pytest.mark.covers("§9.2", "subprocess-backend-dispatch")
async def test_subprocess_backend_clone_failure_marks_failed() -> None:
    """SubprocessBackend marks run as failed when clone raises, without spawning."""
    runner, calls, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness(clone_raises=RuntimeError("auth error"))

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    # ProcessRunner must NOT have been called — process should not spawn on clone failure
    assert not calls, "ProcessRunner called despite clone failure"
    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "failure"
    error_events = [e for e in store.get_events(run_id) if e.event_type == "error"]
    assert error_events, "No error event recorded on clone failure"


@pytest.mark.covers("§9.2", "subprocess-backend-status-in-progress")
async def test_subprocess_backend_sets_in_progress() -> None:
    """SubprocessBackend sets status to in_progress while process runs."""
    runner, _, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    await asyncio.sleep(0.1)
    queue = store.get_queue(run_id)
    assert queue is not None
    items: list[object] = []
    while not queue.empty():
        items.append(queue.get_nowait())
    assert None in items, "No completion sentinel in event queue"


# ===========================================================================
# K8sJobBackend — entry script construction
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


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-command")
def test_k8s_entry_script_contains_clone() -> None:
    """K8sJobBackend entry script contains a git clone for the correct repo."""
    backend = _make_k8s_backend()
    script = backend._build_entry_script(
        "acme", "myrepo", None, ["claude", "-p", "hello"]
    )
    assert "git" in script
    assert "clone" in script
    assert "acme/myrepo" in script


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-command")
def test_k8s_entry_script_uses_env_token_not_literal() -> None:
    """I3: entry script uses ${GH_TOKEN} shell variable — not a literal token value."""
    backend = _make_k8s_backend()
    literal_token = "ghp_secret_literal_token_xyz"
    script = backend._build_entry_script(
        "acme", "myrepo", None, ["claude", "-p", "hello"]
    )
    # Script must reference the env var, not a literal value
    assert "${GH_TOKEN}" in script or "$GH_TOKEN" in script, (
        "Entry script must reference GH_TOKEN via shell variable"
    )
    assert literal_token not in script, (
        "Literal token must NOT appear in the entry script (I3)"
    )


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-command")
def test_k8s_entry_script_contains_branch_when_set() -> None:
    """K8sJobBackend entry script includes --branch when branch is specified."""
    backend = _make_k8s_backend()
    script = backend._build_entry_script(
        "acme", "myrepo", "feature/my-branch", ["claude", "-p", "x"]
    )
    assert "--branch" in script
    assert "feature/my-branch" in script


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-command")
def test_k8s_entry_script_no_branch_flag_when_none() -> None:
    """K8sJobBackend entry script has no --branch flag when branch is None."""
    backend = _make_k8s_backend()
    script = backend._build_entry_script("acme", "myrepo", None, ["claude", "-p", "x"])
    assert "--branch" not in script


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-command")
def test_k8s_entry_script_contains_hook_setup_conditional() -> None:
    """K8sJobBackend entry script has conditional hook setup block."""
    backend = _make_k8s_backend()
    script = backend._build_entry_script(
        "acme", "myrepo", None, ["claude", "-p", "hello"]
    )
    # Must guard hook setup on ORCHESTRATOR_ALLOWED_AGENT_REFS being set
    assert "ORCHESTRATOR_ALLOWED_AGENT_REFS" in script
    assert "i9_spawn_hook.py" in script
    assert "settings.json" in script


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-command")
def test_k8s_entry_script_contains_claude_invocation() -> None:
    """K8sJobBackend entry script contains the claude invocation."""
    backend = _make_k8s_backend()
    claude_args = ["claude", "-p", "hello world", "--output-format", "stream-json"]
    script = backend._build_entry_script("acme", "myrepo", None, claude_args)
    # All args must appear (shell-quoted)
    assert "claude" in script
    assert "stream-json" in script


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-image")
def test_k8s_build_job_spec_has_correct_image() -> None:
    """K8sJobBackend._build_job_spec uses the configured image."""
    backend = _make_k8s_backend()
    script = "set -e\necho hello\n"
    spec = backend._build_job_spec("run-abc123", script, {"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
    containers = spec["spec"]["template"]["spec"]["containers"]
    assert len(containers) == 1
    assert containers[0]["image"] == "ghcr.io/test/orchestrator-agent-runner:test"


@pytest.mark.covers("§9.2", "k8s-backend-job-spec-command")
def test_k8s_build_job_spec_command_is_sh_c_script() -> None:
    """K8sJobBackend._build_job_spec command is ['sh', '-c', <script>]."""
    backend = _make_k8s_backend()
    script = "set -e\necho hello\n"
    spec = backend._build_job_spec("run-abc", script, {})
    containers = spec["spec"]["template"]["spec"]["containers"]
    cmd = containers[0]["command"]
    assert cmd[0] == "sh"
    assert cmd[1] == "-c"
    assert cmd[2] == script


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
    script = "set -e\necho hello\n"
    spec = backend._build_job_spec("run-i3test", script, child_env)
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


@pytest.mark.covers("§9.2", "k8s-i3-master-creds-absent")
def test_k8s_i3_token_not_literal_in_manifest() -> None:
    """I3: the scoped GH token value is NOT a literal string in the Job manifest.

    The token is in the pod env (which is correct — it must be there for git to
    use it).  This test asserts it does NOT appear as a literal in the entry
    script (the command field).  The entry script references ${GH_TOKEN} as a
    shell variable expanded at runtime — not interpolated at build time.
    """
    backend = _make_k8s_backend()
    scoped_token = "ghp_scoped_token_must_not_be_literal_in_script"
    child_env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "tok",
        "GH_TOKEN": scoped_token,
    }
    # Build the full dispatch to get the actual entry script used in the spec
    script = backend._build_entry_script(
        "acme", "myrepo", None, ["claude", "-p", "hello"]
    )
    spec = backend._build_job_spec("run-i3lit", script, child_env)
    containers = spec["spec"]["template"]["spec"]["containers"]
    command_str = str(containers[0]["command"])

    # The literal token value must NOT appear in the command/script
    assert scoped_token not in command_str, (
        f"Literal GH token '{scoped_token}' found in Job command (I3 violation)"
    )

    # The token must be in the pod env (that's correct — the script reads it via ${GH_TOKEN})
    env_list: list[dict[str, str]] = containers[0]["env"]
    gh_entries = [e for e in env_list if e["name"] == "GH_TOKEN"]
    assert gh_entries, "GH_TOKEN must be in pod env so the script can read it"
    assert gh_entries[0]["value"] == scoped_token


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
    script = "set -e\necho hello\n"
    spec = backend._build_job_spec("run-i9test", script, child_env)
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
    script = "set -e\necho hello\n"
    spec = backend._build_job_spec("run-no-refs", script, child_env)
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
    script = "set -e\necho hello\n"
    spec = backend._build_job_spec(
        "run-structtest",
        script,
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
    harness = _make_fake_harness()

    # Configure immediate success
    fake_client.configure_job_outcome(
        f"orch-agent-{run_id[:16]}",
        statuses=[{"metadata": {"name": f"orch-agent-{run_id[:16]}"}, "status": {"succeeded": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )

    assert len(fake_client.created_jobs) == 1
    body = fake_client.created_jobs[0]["body"]
    assert body["kind"] == "Job"
    assert fake_client.created_jobs[0]["namespace"] == "test-ns"


@pytest.mark.covers("§9.2", "k8s-backend-dispatch-creates-job")
async def test_k8s_dispatch_does_not_clone_in_control_plane() -> None:
    """Regression: K8sJobBackend.dispatch() does NOT call harness._clone_repo.

    The clone happens inside the pod (entry script).  The control-plane image
    has no git and must never attempt a clone for k8s dispatches.
    """
    fake_client = FakeKubeClient()
    backend = _make_k8s_backend(fake_client)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness()

    fake_client.configure_job_outcome(
        f"orch-agent-{run_id[:16]}",
        statuses=[{"metadata": {"name": f"orch-agent-{run_id[:16]}"}, "status": {"succeeded": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )

    # _clone_repo must NOT have been called — the Job does the clone inside the pod
    harness._clone_repo.assert_not_called(), "K8s backend must not clone in control-plane"


@pytest.mark.covers("§9.2", "k8s-backend-success")
async def test_k8s_backend_watch_success() -> None:
    """K8sJobBackend records success when Job.status.succeeded > 0."""
    fake_client = FakeKubeClient()
    backend = _make_k8s_backend(fake_client, poll_interval_s=0.001)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness()

    job_name = f"orch-agent-{run_id[:16]}"
    fake_client.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"succeeded": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    job_name = f"orch-agent-{run_id[:16]}"
    fake_client.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"failed": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    # No success/failure status — job stays pending forever
    # (FakeKubeClient returns empty status by default)

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    job_name = f"orch-agent-{run_id[:16]}"
    fake_client.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"succeeded": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    job_name = f"orch-agent-{run_id[:16]}"
    fake_client.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"succeeded": 1}}],
    )

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    await asyncio.sleep(0.1)

    assert job_name in fake_client.deleted_jobs, "Job not deleted after successful completion"


# ===========================================================================
# ClaudeCodeHarnessPort — backend wiring (regression: existing behaviour intact)
# ===========================================================================


@pytest.mark.covers("§9.2", "harness-delegates-to-backend")
async def test_harness_dispatch_delegates_to_backend() -> None:
    """ClaudeCodeHarnessPort.dispatch() delegates to the configured backend."""
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

    ctx = DispatchContext(
        contract="agents/implementer.md",
        model="claude-sonnet-4-6",
        max_turns=30,
        forge_token_scope="repo-branch",
    )
    with patch(
        "src.ports.harness._mint_scoped_installation_token",
        new=AsyncMock(return_value="scoped-token"),
    ):
        handle = await port.dispatch(ctx)

    assert handle is not None
    assert len(fake_backend.dispatched) == 1, "Backend was not called"
    dispatched = fake_backend.dispatched[0]
    assert dispatched["run_id"] == handle.run_id
    assert "claude" in dispatched["claude_args"]
    # Backend receives repo info, not a cloned dir
    assert dispatched["repo_owner"] == "acme"
    assert dispatched["repo_name"] == "myrepo"


@pytest.mark.covers("§9.2", "harness-delegates-to-backend")
async def test_harness_dispatch_does_not_clone_for_k8s_backend() -> None:
    """Regression: harness.dispatch() does NOT call _clone_repo when using K8s backend.

    The K8sJobBackend is responsible for cloning inside the pod.  Cloning in
    the control-plane is wrong (no git in that image) and would produce a
    temp dir that the Job pod never sees.
    """
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

    ctx = DispatchContext(
        contract="agents/implementer.md",
        model="claude-sonnet-4-6",
        max_turns=30,
        forge_token_scope="repo-branch",
    )

    clone_called = False

    async def _spy_clone(gh_token: str, work_dir: str, branch: str | None) -> None:
        nonlocal clone_called
        clone_called = True

    with patch(
        "src.ports.harness._mint_scoped_installation_token",
        new=AsyncMock(return_value="scoped-token"),
    ), patch.object(ClaudeCodeHarnessPort, "_clone_repo", side_effect=_spy_clone):
        await port.dispatch(ctx)

    assert not clone_called, (
        "harness._clone_repo must NOT be called when using a custom (K8s-like) backend — "
        "the backend owns the clone"
    )


@pytest.mark.covers("§9.2", "harness-cancel-delegates-to-backend")
async def test_harness_cancel_delegates_to_backend() -> None:
    """ClaudeCodeHarnessPort.cancel() delegates to the configured backend.

    We register a run manually (not via dispatch) with in_progress status so
    that the harness's terminal-guard doesn't block the cancel() call.
    """
    from src.ports.harness import RunHandle

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
    harness = _make_fake_harness()

    await fake.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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
    harness = _make_fake_harness()

    await fake.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
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


# ===========================================================================
# Issue #111 — contract materialisation
# ===========================================================================


@pytest.mark.covers("§9.2", "k8s-contract-materialisation")
def test_k8s_entry_script_materialises_contract() -> None:
    """#111: K8s entry script copies the baked contract into the clone (#111).

    When a contract is provided, the script must:
      - Check the baked contract exists at /app/agents/<basename> and exit 1 if absent.
      - Copy it into /workspace/repo/agents/<basename>.
    """
    backend = _make_k8s_backend()
    script = backend._build_entry_script(
        "acme", "myrepo", None, ["claude", "-p", "hello"],
        contract="agents/orchestrator.md",
    )
    # Must reference the baked contract dir
    assert "/app/agents/orchestrator.md" in script, (
        "Entry script must reference the baked contract at /app/agents/"
    )
    # Must create the agents/ dir and copy the contract
    assert "mkdir -p /workspace/repo/agents" in script
    assert "cp" in script and "orchestrator.md" in script
    # Must fail loudly if the baked contract is absent
    assert "FATAL" in script or "exit 1" in script, (
        "Entry script must fail loudly if the contract file is absent (#111)"
    )


@pytest.mark.covers("§9.2", "k8s-contract-materialisation")
def test_k8s_entry_script_gitignores_contract() -> None:
    """#111: entry script git-ignores the materialised contract.

    agents/** is a PROTECTED_PATH; if the agent's `git add -A` swept the copied
    contract into the PR, the converge protected-path check (E1) would escalate
    and stall a greenfield run. The script must append the repo-relative contract
    path to .git/info/exclude so untracked copies are never staged.
    """
    backend = _make_k8s_backend()
    script = backend._build_entry_script(
        "acme", "myrepo", None, ["claude", "-p", "hello"],
        contract="agents/orchestrator.md",
    )
    assert ".git/info/exclude" in script, (
        "Entry script must add the contract to .git/info/exclude (#111)"
    )
    assert "/agents/orchestrator.md" in script


@pytest.mark.covers("§9.2", "k8s-contract-materialisation")
def test_k8s_entry_script_no_contract_step_when_empty() -> None:
    """#111: when no contract provided, entry script has no contract materialisation."""
    backend = _make_k8s_backend()
    script = backend._build_entry_script(
        "acme", "myrepo", None, ["claude", "-p", "hello"],
        contract="",
    )
    # No /app/agents reference when contract is empty
    assert "/app/agents/" not in script, (
        "Entry script must not reference /app/agents/ when contract is empty"
    )


@pytest.mark.covers("§9.2", "k8s-contract-materialisation")
def test_k8s_entry_script_contract_path_is_basename_only() -> None:
    """#111: the contract step uses only the basename, not the full path."""
    backend = _make_k8s_backend()
    script = backend._build_entry_script(
        "acme", "myrepo", None, ["claude", "-p", "hello"],
        contract="agents/converge-reviewer.md",
    )
    # The baked path must use the basename
    assert "/app/agents/converge-reviewer.md" in script
    # The destination must be agents/<basename> (not agents/agents/)
    assert "/workspace/repo/agents/converge-reviewer.md" in script or (
        "converge-reviewer.md" in script and "/workspace/repo/agents" in script
    )


@pytest.mark.covers("§9.2", "subprocess-contract-materialisation")
async def test_subprocess_backend_materialises_contract() -> None:
    """#111: SubprocessBackend calls harness._materialize_contract when contract is set."""
    runner, _, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/orchestrator.md",
        event_store=store,
        harness=harness,
    )
    harness._materialize_contract.assert_called_once()
    call_args = harness._materialize_contract.call_args[0]
    assert call_args[0] == "agents/orchestrator.md", (
        "_materialize_contract must receive the full contract path"
    )


@pytest.mark.covers("§9.2", "subprocess-contract-materialisation")
async def test_subprocess_backend_no_materialise_when_contract_empty() -> None:
    """#111: SubprocessBackend skips contract materialisation when contract is empty."""
    runner, _, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="",
        event_store=store,
        harness=harness,
    )
    harness._materialize_contract.assert_not_called()


@pytest.mark.covers("§9.2", "subprocess-contract-materialisation")
async def test_subprocess_backend_contract_failure_marks_failed() -> None:
    """#111: SubprocessBackend marks run failed and emits error when contract absent.

    Fail-loud: a missing contract is a hard error — never silently let the agent
    run without its governing instructions.
    """
    runner, calls, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness(
        contract_raises=FileNotFoundError("contract not found: /repo/agents/missing.md")
    )

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/missing.md",
        event_store=store,
        harness=harness,
    )
    # Process must NOT be spawned — error must surface before running the agent
    assert not calls, "ProcessRunner must not be called when contract is absent (#111)"
    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "failure"
    error_events = [e for e in store.get_events(run_id) if e.event_type == "error"]
    assert error_events, "No error event recorded for missing contract (#111)"
    assert "contract" in error_events[0].data["message"].lower(), (
        "Error message must mention 'contract'"
    )


# ===========================================================================
# Issue #112 — git identity and push auth
# ===========================================================================


@pytest.mark.covers("§9.2", "k8s-git-identity")
def test_k8s_entry_script_sets_git_identity() -> None:
    """#112: K8s entry script configures git user.name and user.email globally."""
    backend = _make_k8s_backend()
    script = backend._build_entry_script(
        "acme", "myrepo", None, ["claude", "-p", "hello"]
    )
    assert "git config --global user.name" in script, (
        "Entry script must set git user.name globally (#112)"
    )
    assert "git config --global user.email" in script, (
        "Entry script must set git user.email globally (#112)"
    )
    # Must use a recognisable identity (not an empty string)
    assert "Orchestrator Agent" in script or "orchestrator" in script.lower(), (
        "Entry script must set a non-empty git identity (#112)"
    )


@pytest.mark.covers("§9.2", "k8s-git-push-auth")
def test_k8s_entry_script_configures_push_auth() -> None:
    """#112: K8s entry script configures push auth via url.insteadOf using ${GH_TOKEN}."""
    backend = _make_k8s_backend()
    script = backend._build_entry_script(
        "acme", "myrepo", None, ["claude", "-p", "hello"]
    )
    # Must configure push auth — insteadOf for the token
    assert "insteadOf" in script, (
        "Entry script must configure git url.insteadOf for push auth (#112)"
    )
    # I3: token must be referenced via shell variable, not literal
    assert "${GH_TOKEN}" in script or "$GH_TOKEN" in script, (
        "Push auth must use ${GH_TOKEN} shell variable, not a literal token (#112 / I3)"
    )
    # The push-auth insteadOf must cover github.com (not just the clone step)
    assert "git config" in script and "url." in script, (
        "Entry script must use git config for push url.insteadOf (#112)"
    )


@pytest.mark.covers("§9.2", "subprocess-git-identity")
async def test_subprocess_backend_calls_configure_git_identity() -> None:
    """#112: SubprocessBackend calls harness._configure_git_identity after clone."""
    runner, _, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    harness = _make_fake_harness()

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh-token"},
        allowed_agent_refs=None,
        contract="agents/orchestrator.md",
        event_store=store,
        harness=harness,
    )
    harness._configure_git_identity.assert_called_once()
    call_kwargs = harness._configure_git_identity.call_args[0]
    # Must pass the GH_TOKEN for the push credential
    assert "gh-token" in call_kwargs, (
        "_configure_git_identity must receive the GH_TOKEN (#112)"
    )


@pytest.mark.covers("§9.2", "subprocess-git-identity")
async def test_subprocess_backend_git_identity_called_before_hook() -> None:
    """#112: _configure_git_identity is called before the I9 hook is written.

    Order matters: identity must be set before the agent runs, and the hook
    is written last so its installation order doesn't affect identity setup.
    """
    runner, _, _ = _make_runner()
    backend = SubprocessBackend(process_runner=runner)
    store = RunEventStore()
    run_id = _run_id()
    store.register(run_id)
    call_order: list[str] = []

    harness = _make_fake_harness()

    async def _record_git_identity(*args: Any) -> None:
        call_order.append("git_identity")

    def _record_spawn_hook(*args: Any) -> None:
        call_order.append("spawn_hook")

    harness._configure_git_identity = AsyncMock(side_effect=_record_git_identity)
    harness._write_spawn_hook = MagicMock(side_effect=_record_spawn_hook)

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="myrepo",
        branch=None,
        claude_args=["claude", "-p", "hello"],
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "tok", "GH_TOKEN": "gh"},
        allowed_agent_refs=["engineering-code-reviewer.md"],
        contract="agents/orchestrator.md",
        event_store=store,
        harness=harness,
    )
    # git identity must be configured before the spawn hook is written
    assert call_order.index("git_identity") < call_order.index("spawn_hook"), (
        "_configure_git_identity must be called before _write_spawn_hook (#112)"
    )
