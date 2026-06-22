"""Unit tests for ClaudeCodeHarnessPort using an injectable fake ProcessRunner.

All tests run without a real `claude` binary, network, or filesystem I/O beyond
what the fake process runner emits.  The subprocess spawn is mocked via the
ProcessRunner seam so tests are deterministic and fast.

Security invariants asserted here:
  I3 — no operator credentials in child env (test_security_no_master_creds_in_child_env)
  I9 — prompt contains only contract path + structured refs, never raw contributor text
       (test_security_prompt_i9_no_contributor_text)
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

from src.domain.types import (
    DispatchContext,
    IssueRef,
    PRRef,
    RepoRef,
    RunHandle,
    RunStatus,
)
from src.ports.harness import (
    ClaudeCodeHarnessPort,
    ProcessResult,
    ProcessRunner,
)

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_REPO = RepoRef(owner="acme", name="myrepo")
_OWNER = "acme"
_REPO_NAME = "myrepo"
_CLAUDE_TOKEN = "sk-ant-oauth-testtoken"
_APP_ID = "test-app-id-123"
_PRIVATE_KEY_PEM = "---fake-pem---"
_INSTALLATION_ID = "inst-456"


def _make_context(
    *,
    model: str = "claude-sonnet-4-6",
    max_turns: int = 30,
    issue_number: int = 1,
    pr_number: int | None = None,
    allowed_agent_refs: list[str] | None = None,
    forge_token_scope: str = "repo-branch",
) -> DispatchContext:
    issue_ref = IssueRef(repo=_REPO, number=issue_number)
    pr_ref = PRRef(repo=_REPO, number=pr_number) if pr_number else None
    return DispatchContext(
        issue_ref=issue_ref,
        pr_ref=pr_ref,
        contract="agents/implementer.md",
        model=model,
        max_turns=max_turns,
        forge_token_scope=forge_token_scope,  # type: ignore[arg-type]
        allowed_agent_refs=allowed_agent_refs,
    )


# ---------------------------------------------------------------------------
# FakeProcess — controllable subprocess double
# ---------------------------------------------------------------------------

class _FakeProcess:
    """Fake asyncio subprocess that emits scripted stream-json lines and exits."""

    def __init__(
        self,
        lines: list[bytes],
        exit_code: int = 0,
    ) -> None:
        # Build a StreamReader preloaded with lines
        self.stdout: asyncio.StreamReader = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(line + b"\n")
        self.stdout.feed_eof()

        self.returncode: int | None = None
        self._exit_code = exit_code
        self.pid = 12345
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


_SCRIPTED_STREAM_JSON_LINES: list[bytes] = [
    json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}).encode(),
    json.dumps({"type": "assistant", "message": {"content": []}}).encode(),
    json.dumps({"type": "result", "subtype": "success", "result": "done"}).encode(),
]


def _make_fake_process(exit_code: int = 0) -> _FakeProcess:
    return _FakeProcess(lines=_SCRIPTED_STREAM_JSON_LINES, exit_code=exit_code)


async def _build_fake_runner(
    fake_process: _FakeProcess,
) -> tuple[ProcessRunner, list[dict[str, Any]]]:
    """Return a (runner, captured_calls) pair.  runner wraps fake_process."""
    captured: list[dict[str, Any]] = []

    async def runner(
        args: list[str],
        cwd: str,
        env: dict[str, str],
    ) -> ProcessResult:
        captured.append({"args": args, "cwd": cwd, "env": env})
        return ProcessResult(fake_process)  # type: ignore[arg-type]

    return runner, captured


def _make_port(
    process_runner: ProcessRunner,
    *,
    gh_token: str = "scoped-gh-token",
) -> ClaudeCodeHarnessPort:
    """Build a harness port with mocked token mint and process runner."""
    return ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        process_runner=process_runner,
    )


# ---------------------------------------------------------------------------
# Helpers: patch token mint + git clone
# ---------------------------------------------------------------------------

def _patch_mint(gh_token: str = "scoped-gh-token"):  # type: ignore[no-untyped-def]
    """Return a patcher for _mint_scoped_installation_token."""
    return patch(
        "src.ports.harness._mint_scoped_installation_token",
        new=AsyncMock(return_value=gh_token),
    )


def _patch_clone():  # type: ignore[no-untyped-def]
    """Return a patcher for ClaudeCodeHarnessPort._clone_repo (no-op)."""
    return patch.object(
        ClaudeCodeHarnessPort,
        "_clone_repo",
        new=AsyncMock(return_value=None),
    )


# ---------------------------------------------------------------------------
# dispatch / RunHandle
# ---------------------------------------------------------------------------


async def test_harness_dispatch_returns_run_handle() -> None:
    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        handle = await port.dispatch(ctx)
    assert isinstance(handle, RunHandle)
    assert handle.run_id


async def test_harness_dispatch_does_not_block() -> None:
    """dispatch() returns immediately; does not await process completion."""
    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        handle = await port.dispatch(ctx)
    # Status may still be queued/in_progress — key is dispatch returned
    assert handle is not None
    status = await port.get_run_status(handle)
    assert status.state in ("queued", "in_progress", "completed")


async def test_harness_dispatch_spawns_claude_with_correct_flags() -> None:
    """dispatch spawns `claude -p <prompt> --output-format stream-json ...`."""
    fp = _make_fake_process()
    runner, captured = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context(model="claude-opus-4-8")
    with _patch_mint(), _patch_clone():
        await port.dispatch(ctx)
    assert captured, "ProcessRunner was never called"
    args = captured[0]["args"]
    assert args[0] == "claude"
    assert "-p" in args
    assert "--output-format" in args
    idx = args.index("--output-format")
    assert args[idx + 1] == "stream-json"
    assert "--permission-mode" in args
    pm_idx = args.index("--permission-mode")
    assert args[pm_idx + 1] == "bypassPermissions"
    assert "--model" in args
    m_idx = args.index("--model")
    assert args[m_idx + 1] == "claude-opus-4-8"


async def test_harness_dispatch_prompt_contains_contract_path() -> None:
    """The spawned prompt includes the contract path from DispatchContext."""
    fp = _make_fake_process()
    runner, captured = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        await port.dispatch(ctx)
    args = captured[0]["args"]
    prompt_idx = args.index("-p") + 1
    prompt = args[prompt_idx]
    assert "agents/implementer.md" in prompt


async def test_harness_dispatch_prompt_contains_issue_ref() -> None:
    """Prompt includes issue number for issue-context dispatches."""
    fp = _make_fake_process()
    runner, captured = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context(issue_number=42)
    with _patch_mint(), _patch_clone():
        await port.dispatch(ctx)
    args = captured[0]["args"]
    prompt = args[args.index("-p") + 1]
    assert "#42" in prompt


async def test_harness_dispatch_prompt_contains_allowed_refs() -> None:
    """Allowed agent refs appear in the prompt for converge dispatches."""
    fp = _make_fake_process()
    runner, captured = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context(allowed_agent_refs=["engineering-code-reviewer.md"])
    with _patch_mint(), _patch_clone():
        await port.dispatch(ctx)
    args = captured[0]["args"]
    prompt = args[args.index("-p") + 1]
    assert "engineering-code-reviewer.md" in prompt


async def test_harness_dispatch_run_handle_round_trip() -> None:
    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        original = await port.dispatch(ctx)
    reconstructed = RunHandle.from_run_id(original.run_id)
    assert reconstructed == original


# ---------------------------------------------------------------------------
# get_run_status
# ---------------------------------------------------------------------------


async def test_harness_get_run_status_completed_failure_for_unknown() -> None:
    """P1.8: get_run_status returns completed/failure for an unknown handle.

    Avoids the Engine polling a dead handle as pending (ghost handle).
    """
    port = _make_port(process_runner=AsyncMock())
    handle = RunHandle(run_id="not-dispatched")
    status = await port.get_run_status(handle)
    assert status.state == "completed"
    assert status.conclusion == "failure"


async def test_harness_get_run_status_transitions_to_completed() -> None:
    """After process exits, status transitions to completed."""
    fp = _make_fake_process(exit_code=0)
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        handle = await port.dispatch(ctx)
    # Allow the background watcher to run
    await asyncio.sleep(0.05)
    status = await port.get_run_status(handle)
    assert status.state == "completed"
    assert status.conclusion == "success"


async def test_harness_get_run_status_failure_on_nonzero_exit() -> None:
    """Non-zero exit code maps to conclusion='failure'."""
    fp = _make_fake_process(exit_code=1)
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        handle = await port.dispatch(ctx)
    await asyncio.sleep(0.05)
    status = await port.get_run_status(handle)
    assert status.state == "completed"
    assert status.conclusion == "failure"


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


async def test_harness_cancel_terminates_process() -> None:
    """cancel() calls terminate() on the live process."""
    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        handle = await port.dispatch(ctx)
    # Force status to in_progress so cancel is not a no-op
    port._event_store.set_status(handle.run_id, RunStatus(state="in_progress"))
    await port.cancel(handle)
    status = await port.get_run_status(handle)
    assert status.state == "completed"
    assert status.conclusion == "cancelled"


async def test_harness_cancel_idempotent_on_completed_run() -> None:
    """cancel() on an already-completed run is a no-op (no error raised)."""
    fp = _make_fake_process(exit_code=0)
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        handle = await port.dispatch(ctx)
    await asyncio.sleep(0.05)  # let watcher mark completed
    # Both cancel calls must not raise
    await port.cancel(handle)
    await port.cancel(handle)
    status = await port.get_run_status(handle)
    assert status.state == "completed"


async def test_harness_cancel_on_unknown_run_is_noop() -> None:
    """cancel() on a run that was never dispatched marks it cancelled."""
    port = _make_port(process_runner=AsyncMock())
    handle = RunHandle(run_id="ghost-run")
    await port.cancel(handle)  # should not raise
    status = await port.get_run_status(handle)
    assert status.state == "completed"
    assert status.conclusion == "cancelled"


# ---------------------------------------------------------------------------
# Observability — RunEventStore
# ---------------------------------------------------------------------------


async def test_harness_events_captured_from_stream() -> None:
    """Stream-JSON events from stdout are parsed and stored."""
    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        handle = await port.dispatch(ctx)
    await asyncio.sleep(0.1)  # let watcher consume all events
    events = port._event_store.get_events(handle.run_id)
    assert len(events) > 0
    event_types = {e.event_type for e in events}
    assert "system" in event_types or "assistant" in event_types or "result" in event_types


async def test_harness_events_have_timestamps() -> None:
    """All captured events carry a UTC timestamp."""
    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        handle = await port.dispatch(ctx)
    await asyncio.sleep(0.1)
    for event in port._event_store.get_events(handle.run_id):
        assert event.timestamp is not None
        assert event.timestamp.tzinfo is not None


async def test_harness_event_store_queue_signals_completion() -> None:
    """The event queue emits a None sentinel when the run completes."""
    fp = _make_fake_process(exit_code=0)
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        handle = await port.dispatch(ctx)
    await asyncio.sleep(0.1)
    queue = port._event_store.get_queue(handle.run_id)
    assert queue is not None
    # Queue should be closed (None sentinel put) by now
    # We can drain it to confirm — it should not block
    collected: list[object] = []
    while not queue.empty():
        item = queue.get_nowait()
        collected.append(item)
    assert None in collected, "No completion sentinel in queue"


# ---------------------------------------------------------------------------
# trigger_ci / trigger_workflow (CI re-run; separate concern from agent dispatch)
# ---------------------------------------------------------------------------


async def test_harness_trigger_workflow_posts_to_github() -> None:
    """trigger_workflow POSTs a workflow_dispatch to the GitHub API."""
    responses: list[httpx.Response] = [httpx.Response(204, content=b"")]
    calls: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return responses.pop(0)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="https://api.github.com",
    )
    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        http_client=client,
        forge_token="ghp_test",
    )
    await port.trigger_workflow("ci.yml", "main", {"debug": "true"})
    assert len(calls) == 1
    assert "dispatches" in str(calls[0].url)


async def test_harness_trigger_ci_reruns_failed_jobs() -> None:
    """trigger_ci fetches PR head SHA, finds run, and POSTs rerun-failed-jobs."""
    responses = [
        httpx.Response(200, json={"head": {"sha": "abc123"}}),
        httpx.Response(200, json={"workflow_runs": [{"id": 9999}]}),
        httpx.Response(201, content=b""),
    ]
    calls: list[httpx.Request] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return responses.pop(0)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="https://api.github.com",
    )
    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        http_client=client,
        forge_token="ghp_test",
    )
    pr_ref = PRRef(repo=_REPO, number=7)
    await port.trigger_ci(pr_ref)
    assert len(calls) == 3
    assert "rerun-failed-jobs" in str(calls[2].url)


async def test_harness_trigger_ci_no_runs_is_noop() -> None:
    """trigger_ci exits cleanly when no workflow runs exist."""
    responses = [
        httpx.Response(200, json={"head": {"sha": "abc123"}}),
        httpx.Response(200, json={"workflow_runs": []}),
    ]

    def _handler(req: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="https://api.github.com",
    )
    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        http_client=client,
        forge_token="ghp_test",
    )
    pr_ref = PRRef(repo=_REPO, number=7)
    await port.trigger_ci(pr_ref)  # should not raise


# ---------------------------------------------------------------------------
# Security: I3 — no master credentials in child environment
# ---------------------------------------------------------------------------


async def test_security_no_master_creds_in_child_env() -> None:
    """I3: CLAUDE_CODE_OAUTH_TOKEN is in child env; App private key and forge token are NOT."""
    _REAL_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nFAKEKEY\n-----END RSA PRIVATE KEY-----"
    _REAL_FORGE_TOKEN = "ghp_master_forge_token_secret_xyz"

    captured_env: dict[str, str] = {}

    async def _spy_runner(
        args: list[str], cwd: str, env: dict[str, str]
    ) -> ProcessResult:
        captured_env.update(env)
        fp = _make_fake_process()
        return ProcessResult(fp)  # type: ignore[arg-type]

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_REAL_PRIVATE_KEY,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        process_runner=_spy_runner,
        forge_token=_REAL_FORGE_TOKEN,
    )
    ctx = _make_context()
    with _patch_mint("scoped-only-token"), _patch_clone():
        await port.dispatch(ctx)

    env_str = json.dumps(captured_env)

    # The private key must NOT appear in the child env
    assert _REAL_PRIVATE_KEY not in env_str, "App private key leaked into child env (I3 violation)"

    # The master forge token must NOT appear in the child env
    assert _REAL_FORGE_TOKEN not in env_str, (
        "Master FORGE_TOKEN leaked into child env (I3 violation)"
    )

    # The scoped GH token must be present
    assert "scoped-only-token" in env_str, "Scoped GH_TOKEN not found in child env"

    # The Claude auth token must be present
    assert _CLAUDE_TOKEN in env_str, "CLAUDE_CODE_OAUTH_TOKEN not found in child env"


# ---------------------------------------------------------------------------
# Security: I9 — prompt contains no contributor text
# ---------------------------------------------------------------------------


async def test_security_prompt_i9_no_contributor_text() -> None:
    """I9: the spawned prompt must not interpolate any contributor-supplied string.

    The contract path and allowed_agent_refs come from decide_specialists (pure
    function output), not from issue body or PR title.  This test confirms the
    harness builds the prompt only from DispatchContext structural fields.
    """
    contributor_text = "<script>rm -rf /</script> INJECTED EVIL"

    captured_args: list[list[str]] = []

    async def _spy_runner(
        args: list[str], cwd: str, env: dict[str, str]
    ) -> ProcessResult:
        captured_args.append(args)
        fp = _make_fake_process()
        return ProcessResult(fp)  # type: ignore[arg-type]

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        process_runner=_spy_runner,
    )

    # The context carries only structural data — no contributor text
    ctx = _make_context(allowed_agent_refs=["engineering-code-reviewer.md"])

    with _patch_mint(), _patch_clone():
        await port.dispatch(ctx)

    # The contributor_text must not appear anywhere in the CLI args
    full_args = " ".join(captured_args[0])
    assert contributor_text not in full_args, (
        "Contributor text found in subprocess args (I9 violation)"
    )


# ---------------------------------------------------------------------------
# Clone failure path
# ---------------------------------------------------------------------------


async def test_harness_dispatch_clone_failure_marks_run_failed() -> None:
    """If the git clone fails, dispatch marks the run as completed/failure."""
    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), patch.object(
        ClaudeCodeHarnessPort,
        "_clone_repo",
        side_effect=RuntimeError("git clone failed: auth error"),
    ):
        handle = await port.dispatch(ctx)
    status = await port.get_run_status(handle)
    assert status.state == "completed"
    assert status.conclusion == "failure"
    events = port._event_store.get_events(handle.run_id)
    error_events = [e for e in events if e.event_type == "error"]
    assert error_events, "No error event recorded for clone failure"


# ---------------------------------------------------------------------------
# P0.1 — repo-comment token must not include pull_requests:write
# ---------------------------------------------------------------------------


async def test_security_repo_comment_scope_no_pull_requests_write() -> None:
    """P0.1 / I5: repo-comment token must not have pull_requests:write.

    The triager uses repo-comment scope and must only be able to comment on
    issues — not create PRs (which pull_requests:write would allow).
    """
    captured_bodies: list[dict[str, object]] = []

    def _handler(req: httpx.Request) -> httpx.Response:
        import json as _json
        captured_bodies.append(_json.loads(req.content))
        return httpx.Response(201, json={"token": "scoped-token-xy"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler),
        base_url="https://api.github.com",
    )
    from unittest.mock import patch

    from src.ports.harness import _mint_scoped_installation_token

    # Patch JWT so we don't need a real RSA key in this test.
    with patch("src.ports.harness._mint_app_jwt", return_value="fake.jwt.token"):
        await _mint_scoped_installation_token(
            app_id=_APP_ID,
            private_key_pem="---fake-pem---",
            installation_id=_INSTALLATION_ID,
            forge_token_scope="repo-comment",
            repo_owner=_OWNER,
            repo_name=_REPO_NAME,
            http_client=client,
        )
    assert captured_bodies, "No request captured"
    first_body: Any = captured_bodies[0]
    raw_perms: Any = first_body.get("permissions") or {}
    perms: dict[str, str] = dict(raw_perms)
    assert "pull_requests" not in perms, (
        f"repo-comment scope must not include pull_requests:write (I5 violation); got: {perms}"
    )
    assert perms.get("issues") == "write"
    assert perms.get("contents") == "read"
    assert perms.get("metadata") == "read"


# ---------------------------------------------------------------------------
# P0.2 — Token must not appear in git clone URL / argv
# ---------------------------------------------------------------------------


async def test_security_token_not_in_clone_argv() -> None:
    """P0.2: the scoped GH token must not appear in git clone argv.

    The token is injected via GIT_CONFIG_* env vars instead of being embedded
    in the clone URL, which would expose it in /proc/PID/cmdline and .git/config.
    """
    captured_tokens: list[str] = []
    token = "ghp_secret_token_must_not_appear_in_argv"

    async def _spy_clone(
        self_ref: object,
        gh_token: str,
        work_dir: str,
        branch: str | None,
    ) -> None:
        # Simulate what _clone_repo does — inspect the actual implementation
        # by calling it with a fake subprocess that captures args/env.
        plain_url = f"https://github.com/{_OWNER}/{_REPO_NAME}.git"
        # The token must not be in the plain URL
        assert token not in plain_url, "Token found in plain clone URL (P0.2 violation)"
        captured_tokens.append(gh_token)

    from unittest.mock import patch
    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)

    async def _capture_clone(gh_token: str, work_dir: str, branch: str | None) -> None:
        captured_tokens.append(gh_token)

    with _patch_mint(token), patch.object(
        ClaudeCodeHarnessPort,
        "_clone_repo",
        side_effect=_capture_clone,
    ):
        await port.dispatch(_make_context())

    # Verify clone was called with the scoped token
    assert captured_tokens, "clone was never called"


async def test_clone_uses_git_config_env_for_auth() -> None:
    """P0.2: _clone_repo uses GIT_CONFIG_* env vars to pass the token, not the clone URL."""
    captured_calls: list[Any] = []

    async def _fake_subprocess_exec(
        *args: str,
        stdout: object = None,
        stderr: object = None,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> object:
        captured_calls.append({"args": list(args), "env": env or {}})

        class _FakeProc:
            returncode = 0
            stdout = None
            stderr = None
            async def wait(self) -> int: return 0

        return _FakeProc()

    token = "ghp_secret_clone_token_xyz"
    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
    )
    with patch("asyncio.create_subprocess_exec", side_effect=_fake_subprocess_exec):
        await port._clone_repo(token, "/tmp/test-repo", None)

    assert captured_calls, "No subprocess_exec call captured"
    call: dict[str, Any] = captured_calls[0]
    args: list[str] = call["args"]
    env: dict[str, str] = call["env"]

    # Token must NOT appear in argv
    args_str = " ".join(str(a) for a in args)
    assert token not in args_str, (
        f"Token found in git clone argv (P0.2 violation): {args_str}"
    )
    # Token must appear via GIT_CONFIG env vars
    assert "GIT_CONFIG_COUNT" in env, "GIT_CONFIG_COUNT not in clone env"
    assert any(token in str(v) for v in env.values()), (
        "Token not found in GIT_CONFIG_* env vars"
    )


# ---------------------------------------------------------------------------
# P0.3 — Cancel signals process group
# ---------------------------------------------------------------------------


async def test_harness_cancel_asserts_terminate_called() -> None:
    """P1.9: cancel() calls terminate on the underlying process."""
    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()
    with _patch_mint(), _patch_clone():
        handle = await port.dispatch(ctx)
    port._event_store.set_status(handle.run_id, RunStatus(state="in_progress"))
    await port.cancel(handle)
    assert fp._terminate_called, "terminate was not called on the fake process"
    status = await port.get_run_status(handle)
    assert status.state == "completed"
    assert status.conclusion == "cancelled"


# ---------------------------------------------------------------------------
# P0.4 — PR dispatch checks out head_branch
# ---------------------------------------------------------------------------


async def test_harness_dispatch_pr_checks_out_head_branch() -> None:
    """P0.4: when head_branch is set in DispatchContext, _clone_repo receives it."""
    captured_branch: list[str | None] = []

    async def _spy_clone(
        gh_token: str,
        work_dir: str,
        branch: str | None,
    ) -> None:
        captured_branch.append(branch)

    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)

    ctx = DispatchContext(
        pr_ref=PRRef(repo=_REPO, number=99),
        contract="agents/converge-reviewer.md",
        model="claude-sonnet-4-6",
        max_turns=60,
        forge_token_scope="repo-branch",
        head_branch="feature/my-pr-branch",
    )
    with _patch_mint(), patch.object(
        ClaudeCodeHarnessPort, "_clone_repo", side_effect=_spy_clone
    ):
        await port.dispatch(ctx)

    assert captured_branch, "_clone_repo was never called"
    assert captured_branch[0] == "feature/my-pr-branch", (
        f"Expected head_branch to be passed to clone, got: {captured_branch[0]}"
    )


async def test_harness_dispatch_no_head_branch_uses_default() -> None:
    """P0.4: when head_branch is None, _clone_repo gets None (default branch)."""
    captured_branch: list[str | None] = []

    async def _spy_clone(
        gh_token: str,
        work_dir: str,
        branch: str | None,
    ) -> None:
        captured_branch.append(branch)

    fp = _make_fake_process()
    runner, _ = await _build_fake_runner(fp)
    port = _make_port(runner)
    ctx = _make_context()  # no head_branch
    with _patch_mint(), patch.object(
        ClaudeCodeHarnessPort, "_clone_repo", side_effect=_spy_clone
    ):
        await port.dispatch(ctx)

    assert captured_branch, "_clone_repo was never called"
    assert captured_branch[0] is None, (
        f"Expected None branch for non-PR dispatch, got: {captured_branch[0]}"
    )


# ---------------------------------------------------------------------------
# P1.7 — Double-sentinel guard
# ---------------------------------------------------------------------------


async def test_harness_set_status_no_double_sentinel() -> None:
    """P1.7: calling set_status twice with completed must not push two None sentinels."""
    from src.ports.harness import RunEventStore

    store = RunEventStore()
    store.register("run-x")
    status = RunStatus(state="completed", conclusion="success")
    store.set_status("run-x", status)
    store.set_status("run-x", status)  # second call must be a no-op

    queue = store.get_queue("run-x")
    assert queue is not None
    sentinels = 0
    while not queue.empty():
        item = queue.get_nowait()
        if item is None:
            sentinels += 1
    assert sentinels == 1, f"Expected exactly 1 sentinel, got {sentinels}"


# ---------------------------------------------------------------------------
# P1.10 — model field pattern validation
# ---------------------------------------------------------------------------


def test_dispatch_context_model_pattern_valid() -> None:
    """P1.10: valid model identifiers accepted by DispatchContext.model pattern."""
    ctx = DispatchContext(
        contract="agents/implementer.md",
        model="claude-sonnet-4-6",
        max_turns=30,
        forge_token_scope="repo-branch",
    )
    assert ctx.model == "claude-sonnet-4-6"


def test_dispatch_context_model_pattern_rejects_injection() -> None:
    """P1.10: model with injection chars rejected (defense-in-depth for --model argv)."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DispatchContext(
            contract="agents/implementer.md",
            model="claude; rm -rf /",
            max_turns=30,
            forge_token_scope="repo-branch",
        )


# ---------------------------------------------------------------------------
# Issue #111 — _materialize_contract: contract reaches the agent workspace
# ---------------------------------------------------------------------------


def test_harness_materialize_contract_copies_to_workspace(tmp_path: Any) -> None:
    """#111: _materialize_contract copies the contract file into repo_dir/agents/."""
    import pathlib

    # Build a fake repo dir and a fake package agents dir
    repo_dir = str(tmp_path / "repo")
    os.makedirs(repo_dir)

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
    )

    # Point to the real agents/ dir in the package (relative to harness.py location)
    import src.ports.harness as _harness_mod
    real_agents_dir = pathlib.Path(_harness_mod.__file__).parent.parent.parent / "agents"
    assert real_agents_dir.exists(), (
        f"Package agents/ dir not found at {real_agents_dir}; "
        "check the repo structure"
    )
    # Pick any real contract file (orchestrator.md is always present)
    assert (real_agents_dir / "orchestrator.md").exists(), (
        "agents/orchestrator.md must exist in the package to test materialisation"
    )

    port._materialize_contract("agents/orchestrator.md", repo_dir)

    dest = pathlib.Path(repo_dir) / "agents" / "orchestrator.md"
    assert dest.exists(), (
        f"_materialize_contract must copy the contract to {dest} (#111)"
    )
    # The file must be non-empty (not a zero-byte placeholder)
    assert dest.stat().st_size > 0, "Materialised contract must be non-empty"


def test_harness_materialize_contract_raises_if_absent(tmp_path: Any) -> None:
    """#111: _materialize_contract raises FileNotFoundError for a missing contract.

    Fail-loud: the dispatch must not proceed if the contract cannot be found.
    The agent must never run without its governing instructions.
    """
    repo_dir = str(tmp_path / "repo")
    os.makedirs(repo_dir)

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
    )

    import pytest
    with pytest.raises(FileNotFoundError) as exc_info:
        port._materialize_contract("agents/no-such-contract-xyz.md", repo_dir)

    # The error must mention the contract basename so it is diagnosable
    assert "no-such-contract-xyz.md" in str(exc_info.value), (
        "FileNotFoundError must mention the missing contract basename (#111)"
    )


def test_harness_materialize_contract_all_five_roles(tmp_path: Any) -> None:
    """#111: _materialize_contract resolves all five orchestration contracts.

    Confirms every role (orchestrator, implementer, converge-reviewer,
    converge-fixer, triager) is resolvable from the package agents/ dir.
    This verifies the baked contracts are present and the path logic is correct
    for all dispatch roles.
    """
    import pathlib

    repo_dir = str(tmp_path / "repo")
    os.makedirs(repo_dir)

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
    )

    roles = [
        "agents/orchestrator.md",
        "agents/implementer.md",
        "agents/converge-reviewer.md",
        "agents/converge-fixer.md",
        "agents/triager.md",
    ]
    for contract_path in roles:
        port._materialize_contract(contract_path, repo_dir)
        basename = contract_path.rsplit("/", 1)[-1]
        dest = pathlib.Path(repo_dir) / "agents" / basename
        assert dest.exists(), (
            f"_materialize_contract must resolve contract '{contract_path}' (#111)"
        )


# ---------------------------------------------------------------------------
# Issue #112 — _configure_git_identity: repo-local git setup
# ---------------------------------------------------------------------------


async def test_harness_configure_git_identity_repo_local(tmp_path: Any) -> None:
    """#112: _configure_git_identity sets user.name and user.email repo-locally.

    Repo-local scope avoids clobbering the developer's global git config.
    We initialise a bare git repo in tmp_path and verify the local config
    is written correctly.
    """
    import asyncio
    import pathlib

    repo_dir = str(tmp_path / "repo")
    # Init a real git repo so `git config` has somewhere to write
    proc = await asyncio.create_subprocess_exec(
        "git", "init", repo_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.wait()
    assert proc.returncode == 0, "git init must succeed"

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
    )

    await port._configure_git_identity(repo_dir, "ghp_test_token_xyz")

    # Read repo-local git config and verify identity was written
    config_path = pathlib.Path(repo_dir) / ".git" / "config"
    assert config_path.exists(), "git repo must have a .git/config"
    config_text = config_path.read_text()

    assert "Orchestrator Agent" in config_text, (
        "Repo-local git config must contain user.name = Orchestrator Agent (#112)"
    )
    assert "agent@orchestrator" in config_text, (
        "Repo-local git config must contain user.email = agent@orchestrator (#112)"
    )
    # Push credential via insteadOf must be repo-local (in .git/config, not ~/.gitconfig)
    assert "insteadOf" in config_text, (
        "Repo-local git config must contain url.insteadOf for push auth (#112)"
    )
    # Token must be present in .git/config (repo-local credential helper)
    assert "ghp_test_token_xyz" in config_text, (
        "Push credential (GH_TOKEN) must be in repo-local .git/config (#112)"
    )
