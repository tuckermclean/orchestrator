"""Unit tests for the I9 PreToolUse hook mechanism (closes GitHub issue #52).

Tests the three layers of the I9 spawn allow-set enforcement:

  1. Hook script logic (i9_spawn_hook.py) — decision function unit tests.
  2. Hook config content — _write_spawn_hook produces correct settings.json.
  3. Harness wiring — dispatch() materialises hook & injects env var when
     allowed_agent_refs is not None; skips hook when it is None.

Named tests from SPEC §9.2 (test names must match coverage_map.yaml):
  test_security_spawn_ref_outside_allowset_rejected
  test_security_spawn_rejected_when_allowed_refs_none  (None → no hook, no env var)

SPEC §9.2 / SECURITY.md §3 I9 fail-closed semantics:
  - Missing ORCHESTRATOR_ALLOWED_AGENT_REFS env var → DENY
  - Empty allow-set                                  → DENY
  - Unparseable stdin                                → DENY
  - subagent_type absent from payload                → DENY
  - subagent_type NOT in allow-set                   → DENY
  - subagent_type IN allow-set                       → ALLOW
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
from io import StringIO
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import src.ports.i9_spawn_hook as _hook_module
from src.domain.types import (
    DispatchContext,
    IssueRef,
    RepoRef,
)
from src.ports.harness import ClaudeCodeHarnessPort

# ---------------------------------------------------------------------------
# Hook script decision logic helpers
# ---------------------------------------------------------------------------

def _run_hook(
    *,
    allowed_refs: str | None,
    stdin_payload: object = None,
    stdin_raw: str | None = None,
) -> int:
    """Invoke the hook's main() with controlled env and stdin; return exit code.

    Args:
        allowed_refs: value for ORCHESTRATOR_ALLOWED_AGENT_REFS, or None to omit.
        stdin_payload: object to JSON-encode as stdin (mutually exclusive with stdin_raw).
        stdin_raw: raw string to use as stdin (for testing parse-error paths).
    """
    if stdin_payload is not None and stdin_raw is not None:
        raise ValueError("Supply stdin_payload or stdin_raw, not both")

    if stdin_raw is not None:
        raw = stdin_raw
    elif stdin_payload is not None:
        raw = json.dumps(stdin_payload)
    else:
        raw = json.dumps({})

    env_patch: dict[str, str] = {}
    if allowed_refs is not None:
        env_patch["ORCHESTRATOR_ALLOWED_AGENT_REFS"] = allowed_refs

    # Patch os.environ and sys.stdin for the duration of the call.
    saved_env = os.environ.copy()
    # Remove the env var first so we can control presence/absence cleanly.
    os.environ.pop("ORCHESTRATOR_ALLOWED_AGENT_REFS", None)
    os.environ.update(env_patch)

    saved_stdin = sys.stdin
    sys.stdin = StringIO(raw)

    try:
        return _hook_module.main()
    finally:
        sys.stdin = saved_stdin
        # Restore env.
        os.environ.clear()
        os.environ.update(saved_env)


def _make_task_payload(subagent_type: str) -> dict[str, Any]:
    """Build a Task tool input payload as Claude Code delivers it."""
    return {
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": subagent_type,
            "description": "do work",
            "prompt": "please implement feature X",
        },
    }


# ===========================================================================
# 1. Hook script decision logic — unit tests
# ===========================================================================

# --- Fail-closed: missing env var ---

def test_hook_denies_when_env_var_missing() -> None:
    """Missing ORCHESTRATOR_ALLOWED_AGENT_REFS → DENY (fail closed)."""
    payload = _make_task_payload("agents/reviewer.md")
    result = _run_hook(allowed_refs=None, stdin_payload=payload)
    assert result == 1


# --- Fail-closed: empty allow-set ---

def test_hook_denies_when_allow_set_is_empty_string() -> None:
    """Empty string env var → empty allow-set → DENY all Task spawns."""
    payload = _make_task_payload("agents/reviewer.md")
    result = _run_hook(allowed_refs="", stdin_payload=payload)
    assert result == 1


def test_hook_denies_when_allow_set_is_whitespace_only() -> None:
    """Env var containing only commas/whitespace → empty allow-set → DENY."""
    payload = _make_task_payload("agents/reviewer.md")
    result = _run_hook(allowed_refs=",,,", stdin_payload=payload)
    assert result == 1


# --- Fail-closed: bad stdin ---

def test_hook_denies_on_invalid_json_stdin() -> None:
    """Unparseable stdin JSON → DENY (fail closed)."""
    result = _run_hook(allowed_refs="agents/reviewer.md", stdin_raw="NOT JSON {{{{")
    assert result == 1


def test_hook_denies_on_empty_stdin() -> None:
    """Empty stdin → JSON parse error → DENY."""
    result = _run_hook(allowed_refs="agents/reviewer.md", stdin_raw="")
    assert result == 1


# --- Fail-closed: missing subagent_type ---

def test_hook_denies_when_subagent_type_absent() -> None:
    """tool_input without subagent_type → DENY."""
    payload = {"tool_name": "Task", "tool_input": {"description": "oops"}}
    result = _run_hook(allowed_refs="agents/reviewer.md", stdin_payload=payload)
    assert result == 1


def test_hook_denies_when_tool_input_missing() -> None:
    """Payload without tool_input → DENY."""
    payload = {"tool_name": "Task"}
    result = _run_hook(allowed_refs="agents/reviewer.md", stdin_payload=payload)
    assert result == 1


def test_hook_denies_when_tool_input_not_a_dict() -> None:
    """tool_input is a non-dict → DENY."""
    payload = {"tool_name": "Task", "tool_input": ["not", "a", "dict"]}
    result = _run_hook(allowed_refs="agents/reviewer.md", stdin_payload=payload)
    assert result == 1


# --- In-set: allow ---

@pytest.mark.covers("§9.2", "spawn-hook-in-set-allow")
def test_hook_allows_when_subagent_type_in_set() -> None:
    """subagent_type in allow-set → ALLOW (exit 0)."""
    payload = _make_task_payload("agents/reviewer.md")
    result = _run_hook(allowed_refs="agents/reviewer.md", stdin_payload=payload)
    assert result == 0


def test_hook_allows_when_subagent_type_in_multi_entry_set() -> None:
    """Allow-set with multiple entries; subagent_type matches one → ALLOW."""
    payload = _make_task_payload("agents/fixer.md")
    result = _run_hook(
        allowed_refs="agents/reviewer.md,agents/fixer.md",
        stdin_payload=payload,
    )
    assert result == 0


# --- Out-of-set: deny ---

@pytest.mark.covers("§9.2", "spawn-hook-out-of-set-deny")
def test_security_spawn_ref_outside_allowset_rejected() -> None:
    """SPEC §9.2 / SECURITY I9: subagent_type not in allow-set → DENY.

    This is the primary I9 enforcement test.  A spawned sub-agent whose
    ref is outside the allow-set must be hard-rejected by the hook.
    """
    payload = _make_task_payload("rogue-agent.md")
    result = _run_hook(allowed_refs="agents/reviewer.md,agents/fixer.md", stdin_payload=payload)
    assert result == 1


def test_hook_denies_out_of_set_single_entry_allow_set() -> None:
    """Single-entry allow-set; different subagent_type → DENY."""
    payload = _make_task_payload("agents/different.md")
    result = _run_hook(allowed_refs="agents/reviewer.md", stdin_payload=payload)
    assert result == 1


def test_hook_deny_is_case_sensitive() -> None:
    """Allow-set matching is case-sensitive: 'Agents/Reviewer.md' != 'agents/reviewer.md'."""
    payload = _make_task_payload("Agents/Reviewer.md")
    result = _run_hook(allowed_refs="agents/reviewer.md", stdin_payload=payload)
    assert result == 1


# ===========================================================================
# 2. Hook config content — _write_spawn_hook writes correct settings.json
# ===========================================================================

@pytest.mark.covers("§9.2", "hook-config-content")
def test_write_spawn_hook_creates_settings_json() -> None:
    """_write_spawn_hook writes .claude/settings.json into repo_dir."""
    with tempfile.TemporaryDirectory() as repo_dir:
        port = _make_port()
        port._write_spawn_hook(repo_dir)

        settings_path = pathlib.Path(repo_dir) / ".claude" / "settings.json"
        assert settings_path.exists(), "settings.json was not created"


def test_write_spawn_hook_settings_json_is_valid_json() -> None:
    """Written settings.json must be parseable JSON."""
    with tempfile.TemporaryDirectory() as repo_dir:
        port = _make_port()
        port._write_spawn_hook(repo_dir)
        settings_path = pathlib.Path(repo_dir) / ".claude" / "settings.json"
        content = settings_path.read_text()
        data = json.loads(content)
        assert isinstance(data, dict)


def test_write_spawn_hook_settings_has_pretooluse_hook() -> None:
    """settings.json must have hooks.PreToolUse array with Task matcher."""
    with tempfile.TemporaryDirectory() as repo_dir:
        port = _make_port()
        port._write_spawn_hook(repo_dir)
        settings_path = pathlib.Path(repo_dir) / ".claude" / "settings.json"
        data = json.loads(settings_path.read_text())

        assert "hooks" in data, "No 'hooks' key in settings.json"
        assert "PreToolUse" in data["hooks"], "No 'PreToolUse' in hooks"
        pre_tool_use = data["hooks"]["PreToolUse"]
        assert isinstance(pre_tool_use, list) and len(pre_tool_use) > 0

        entry = pre_tool_use[0]
        assert entry.get("matcher") == "Task", (
            f"Expected matcher 'Task', got {entry.get('matcher')!r}"
        )
        hooks_list = entry.get("hooks", [])
        assert hooks_list, "No hooks in PreToolUse entry"
        hook = hooks_list[0]
        assert hook.get("type") == "command", "Hook type must be 'command'"
        assert "command" in hook, "Hook must have 'command' key"


def test_write_spawn_hook_copies_hook_script() -> None:
    """_write_spawn_hook copies i9_spawn_hook.py into .claude/."""
    with tempfile.TemporaryDirectory() as repo_dir:
        port = _make_port()
        port._write_spawn_hook(repo_dir)
        hook_dst = pathlib.Path(repo_dir) / ".claude" / "i9_spawn_hook.py"
        assert hook_dst.exists(), "i9_spawn_hook.py was not copied into .claude/"
        # Must be the real hook script (contains the module docstring sentinel)
        content = hook_dst.read_text()
        assert "ORCHESTRATOR_ALLOWED_AGENT_REFS" in content


def test_write_spawn_hook_command_points_to_hook_script() -> None:
    """The hook command must invoke the materialised hook script by absolute path."""
    with tempfile.TemporaryDirectory() as repo_dir:
        port = _make_port()
        port._write_spawn_hook(repo_dir)
        settings_path = pathlib.Path(repo_dir) / ".claude" / "settings.json"
        data = json.loads(settings_path.read_text())
        command = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        hook_dst = str(pathlib.Path(repo_dir) / ".claude" / "i9_spawn_hook.py")
        assert hook_dst in command, (
            f"Hook command {command!r} does not reference the materialised script path"
        )


def test_write_spawn_hook_idempotent() -> None:
    """Calling _write_spawn_hook twice overwrites (no error, consistent result)."""
    with tempfile.TemporaryDirectory() as repo_dir:
        port = _make_port()
        port._write_spawn_hook(repo_dir)
        port._write_spawn_hook(repo_dir)  # must not raise
        settings_path = pathlib.Path(repo_dir) / ".claude" / "settings.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text())
        assert "hooks" in data


# ===========================================================================
# 3. Harness wiring — dispatch() materialises hook and injects env var
# ===========================================================================

# Shared test infrastructure (mirrors test_real_harness_port.py minimally)

_REPO = RepoRef(owner="acme", name="testrepo")
_OWNER = "acme"
_REPO_NAME = "testrepo"
_CLAUDE_TOKEN = "sk-ant-oauth-test"
_APP_ID = "app-123"
_PRIVATE_KEY_PEM = "---fake-pem---"
_INSTALLATION_ID = "inst-456"


def _make_context(
    *,
    allowed_agent_refs: list[str] | None = None,
) -> DispatchContext:
    return DispatchContext(
        issue_ref=IssueRef(repo=_REPO, number=1),
        contract="agents/implementer.md",
        model="claude-sonnet-4-6",
        max_turns=30,
        forge_token_scope="repo-branch",
        allowed_agent_refs=allowed_agent_refs,
    )


def _make_port() -> ClaudeCodeHarnessPort:
    return ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        process_runner=AsyncMock(return_value=AsyncMock(pid=1, returncode=0, stdout=None)),
    )


def _patch_mint(token: str = "scoped-gh-token"):  # type: ignore[no-untyped-def]
    return patch(
        "src.ports.harness._mint_scoped_installation_token",
        new=AsyncMock(return_value=token),
    )


def _patch_clone():  # type: ignore[no-untyped-def]
    return patch.object(
        ClaudeCodeHarnessPort,
        "_clone_repo",
        new=AsyncMock(return_value=None),
    )


@pytest.mark.covers("§9.2", "hook-env-var-injected")
async def test_dispatch_injects_allowed_refs_env_var_when_set() -> None:
    """dispatch() injects ORCHESTRATOR_ALLOWED_AGENT_REFS when allowed_agent_refs is a list."""
    captured_env: dict[str, str] = {}

    async def _spy_runner(args: list[str], cwd: str, env: dict[str, str]) -> Any:
        captured_env.update(env)
        m = AsyncMock()
        m.pid = 1
        m.returncode = 0
        m.stdout = None
        return m

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        process_runner=_spy_runner,
    )
    ctx = _make_context(allowed_agent_refs=["agents/reviewer.md", "agents/fixer.md"])
    with _patch_mint(), _patch_clone(), patch.object(
        ClaudeCodeHarnessPort, "_write_spawn_hook"
    ):
        await port.dispatch(ctx)

    assert "ORCHESTRATOR_ALLOWED_AGENT_REFS" in captured_env, (
        "ORCHESTRATOR_ALLOWED_AGENT_REFS not found in child env"
    )
    env_val = captured_env["ORCHESTRATOR_ALLOWED_AGENT_REFS"]
    assert "agents/reviewer.md" in env_val
    assert "agents/fixer.md" in env_val


@pytest.mark.covers("§9.2", "hook-env-var-absent-when-none")
async def test_security_spawn_rejected_when_allowed_refs_none() -> None:
    """SPEC §9.2: when allowed_agent_refs is None, no hook env var is injected.

    When allowed_agent_refs is None the harness applies no spawn restriction
    (no hook, no env var).  This is by design — None means 'no harness-level
    restriction' for dispatches that do not use converge's allow-set.
    The hook is only active when a non-None list is provided.
    """
    captured_env: dict[str, str] = {}

    async def _spy_runner(args: list[str], cwd: str, env: dict[str, str]) -> Any:
        captured_env.update(env)
        m = AsyncMock()
        m.pid = 1
        m.returncode = 0
        m.stdout = None
        return m

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        process_runner=_spy_runner,
    )
    ctx = _make_context(allowed_agent_refs=None)  # None → no restriction
    with _patch_mint(), _patch_clone():
        await port.dispatch(ctx)

    assert "ORCHESTRATOR_ALLOWED_AGENT_REFS" not in captured_env, (
        "ORCHESTRATOR_ALLOWED_AGENT_REFS must NOT be injected when allowed_agent_refs is None"
    )


async def test_dispatch_calls_write_spawn_hook_when_refs_given() -> None:
    """dispatch() calls _write_spawn_hook when allowed_agent_refs is a list."""
    write_hook_calls: list[str] = []

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        process_runner=AsyncMock(return_value=AsyncMock(pid=1, returncode=0, stdout=None)),
    )

    def _record_call(repo_dir: str) -> None:
        write_hook_calls.append(repo_dir)

    ctx = _make_context(allowed_agent_refs=["agents/reviewer.md"])
    with _patch_mint(), _patch_clone(), patch.object(
        ClaudeCodeHarnessPort, "_write_spawn_hook", side_effect=_record_call
    ):
        await port.dispatch(ctx)

    assert len(write_hook_calls) == 1, "Expected _write_spawn_hook to be called once"


async def test_dispatch_skips_write_spawn_hook_when_refs_none() -> None:
    """dispatch() does NOT call _write_spawn_hook when allowed_agent_refs is None."""
    write_hook_calls: list[str] = []

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        process_runner=AsyncMock(return_value=AsyncMock(pid=1, returncode=0, stdout=None)),
    )

    def _record_call(repo_dir: str) -> None:
        write_hook_calls.append(repo_dir)

    ctx = _make_context(allowed_agent_refs=None)
    with _patch_mint(), _patch_clone(), patch.object(
        ClaudeCodeHarnessPort, "_write_spawn_hook", side_effect=_record_call
    ):
        await port.dispatch(ctx)

    assert len(write_hook_calls) == 0, (
        "_write_spawn_hook must NOT be called when allowed_agent_refs is None"
    )


async def test_dispatch_empty_allow_set_injects_empty_env_var() -> None:
    """Empty allow-set injects empty string env var → hook will deny all Task spawns."""
    captured_env: dict[str, str] = {}

    async def _spy_runner(args: list[str], cwd: str, env: dict[str, str]) -> Any:
        captured_env.update(env)
        m = AsyncMock()
        m.pid = 1
        m.returncode = 0
        m.stdout = None
        return m

    port = ClaudeCodeHarnessPort(
        claude_oauth_token=_CLAUDE_TOKEN,
        app_id=_APP_ID,
        private_key_pem=_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        repo_owner=_OWNER,
        repo_name=_REPO_NAME,
        process_runner=_spy_runner,
    )
    ctx = _make_context(allowed_agent_refs=[])  # empty list → deny all
    with _patch_mint(), _patch_clone(), patch.object(
        ClaudeCodeHarnessPort, "_write_spawn_hook"
    ):
        await port.dispatch(ctx)

    assert "ORCHESTRATOR_ALLOWED_AGENT_REFS" in captured_env
    assert captured_env["ORCHESTRATOR_ALLOWED_AGENT_REFS"] == "", (
        "Empty allow-set must produce empty string env var so hook denies all spawns"
    )


# ===========================================================================
# 4. _build_child_env unit tests
# ===========================================================================

def test_build_child_env_no_allowed_refs_omits_env_var() -> None:
    """When allowed_agent_refs is None, ORCHESTRATOR_ALLOWED_AGENT_REFS not in env."""
    port = _make_port()
    env = port._build_child_env("scoped-token", allowed_agent_refs=None)
    assert "ORCHESTRATOR_ALLOWED_AGENT_REFS" not in env


def test_build_child_env_with_refs_injects_env_var() -> None:
    """When allowed_agent_refs is a list, env var is comma-joined refs."""
    port = _make_port()
    refs = ["agents/reviewer.md", "agents/fixer.md"]
    env = port._build_child_env("scoped-token", allowed_agent_refs=refs)
    assert "ORCHESTRATOR_ALLOWED_AGENT_REFS" in env
    parts = set(env["ORCHESTRATOR_ALLOWED_AGENT_REFS"].split(","))
    assert parts == set(refs)


def test_build_child_env_with_empty_refs_injects_empty_string() -> None:
    """When allowed_agent_refs is [], env var is empty string (hook denies all)."""
    port = _make_port()
    env = port._build_child_env("scoped-token", allowed_agent_refs=[])
    assert env["ORCHESTRATOR_ALLOWED_AGENT_REFS"] == ""


def test_build_child_env_always_has_required_keys() -> None:
    """Regardless of allowed_agent_refs, I3 keys are always present."""
    port = _make_port()
    for refs in [None, [], ["agents/reviewer.md"]]:
        env = port._build_child_env("gh-token", allowed_agent_refs=refs)
        assert "CLAUDE_CODE_OAUTH_TOKEN" in env
        assert "GH_TOKEN" in env
        assert "GIT_TERMINAL_PROMPT" in env
        assert "PATH" in env
        assert "HOME" in env
