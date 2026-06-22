"""Unit tests for agent_transcript.py — JSONL → RunEvent mapping.

Covers:
  - All meaningful message types: assistant (text, thinking, tool_use),
    user (tool_result), result.
  - Dropped types: system, thinking_tokens, unknown.
  - Secret redaction (I3): GH_TOKEN patterns, sk-ant- tokens, Bearer tokens.
  - Payload truncation (I3): long text capped at _MAX_TEXT_BYTES.
  - Non-JSON lines dropped silently.
  - Empty / whitespace lines dropped.
  - Assistant messages with no meaningful blocks return None.
  - Edge cases: nested content lists, missing keys, non-dict input.
"""

from __future__ import annotations

import json

import pytest

from src.ports.agent_transcript import (
    _MAX_TEXT_BYTES,
    _TRUNCATION_SUFFIX,
    parse_jsonl_line,
    redact_secrets,
    truncate_text,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_assistant_text(text: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }
    )


def _make_assistant_thinking(thinking: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": thinking}]},
        }
    )


def _make_assistant_tool_use(name: str, tool_input: dict[str, object]) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "name": name, "input": tool_input}]
            },
        }
    )


def _make_user_tool_result(content: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "content": content}]
            },
        }
    )


def _make_result(subtype: str = "success", result: str = "done") -> str:
    return json.dumps({"type": "result", "subtype": subtype, "result": result})


def _make_system() -> str:
    return json.dumps(
        {"type": "system", "subtype": "init", "session_id": "abc123"}
    )


def _make_thinking_tokens() -> str:
    return json.dumps({"type": "thinking_tokens", "tokens": "some tokens here"})


# ===========================================================================
# assistant — text block
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-assistant-text")
def test_parse_assistant_text_returns_agent_message() -> None:
    """assistant text block → agent_message event."""
    event = parse_jsonl_line(_make_assistant_text("Hello from the agent"))
    assert event is not None
    assert event.event_type == "agent_message"
    assert event.data["text"] == "Hello from the agent"


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-assistant-text")
def test_parse_assistant_empty_text_returns_none() -> None:
    """assistant text block with empty text → None (dropped)."""
    event = parse_jsonl_line(_make_assistant_text(""))
    assert event is None


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-assistant-text")
def test_parse_assistant_whitespace_only_text_returns_none() -> None:
    """assistant text block with whitespace-only text → None."""
    event = parse_jsonl_line(_make_assistant_text("   \n  "))
    assert event is None


# ===========================================================================
# assistant — thinking block
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-assistant-thinking")
def test_parse_assistant_thinking_returns_agent_thinking() -> None:
    """assistant thinking block → agent_thinking event."""
    event = parse_jsonl_line(_make_assistant_thinking("reasoning step"))
    assert event is not None
    assert event.event_type == "agent_thinking"
    assert "reasoning step" in str(event.data["thinking"])


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-assistant-thinking")
def test_parse_assistant_empty_thinking_returns_none() -> None:
    """assistant thinking block with empty content → None."""
    event = parse_jsonl_line(_make_assistant_thinking(""))
    assert event is None


# ===========================================================================
# assistant — tool_use block
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-tool-use")
def test_parse_assistant_tool_use_returns_agent_tool_use() -> None:
    """assistant tool_use block → agent_tool_use event with name + input_summary."""
    event = parse_jsonl_line(
        _make_assistant_tool_use("Read", {"file_path": "/src/main.py"})
    )
    assert event is not None
    assert event.event_type == "agent_tool_use"
    assert event.data["name"] == "Read"
    assert "file_path" in str(event.data["input_summary"])


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-tool-use")
def test_parse_assistant_tool_use_no_name_returns_none() -> None:
    """assistant tool_use block without a name → None (malformed)."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "tool_use", "input": {}}]
            },
        }
    )
    event = parse_jsonl_line(line)
    assert event is None


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-tool-use")
def test_parse_assistant_tool_use_empty_name_returns_none() -> None:
    """assistant tool_use block with empty name string → None."""
    event = parse_jsonl_line(_make_assistant_tool_use("", {}))
    assert event is None


# ===========================================================================
# assistant — first block wins
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-first-block-wins")
def test_parse_assistant_first_text_block_returned() -> None:
    """When multiple content blocks are present, the first meaningful one wins."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "first"},
                    {"type": "text", "text": "second"},
                ]
            },
        }
    )
    event = parse_jsonl_line(line)
    assert event is not None
    assert event.data["text"] == "first"


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-first-block-wins")
def test_parse_assistant_skips_empty_blocks_to_find_content() -> None:
    """Empty text blocks are skipped; the parser continues to find the first non-empty."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": ""},  # empty → skip
                    {"type": "text", "text": "second"},  # non-empty → return
                ]
            },
        }
    )
    event = parse_jsonl_line(line)
    assert event is not None
    assert event.data["text"] == "second"


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-first-block-wins")
def test_parse_assistant_no_meaningful_blocks_returns_none() -> None:
    """assistant message with no meaningful content blocks → None."""
    line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "unknown_block_type"}]},
        }
    )
    event = parse_jsonl_line(line)
    assert event is None


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-first-block-wins")
def test_parse_assistant_empty_content_list_returns_none() -> None:
    """assistant message with empty content array → None."""
    line = json.dumps(
        {"type": "assistant", "message": {"content": []}}
    )
    event = parse_jsonl_line(line)
    assert event is None


# ===========================================================================
# user — tool_result
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-user-tool-result")
def test_parse_user_tool_result_returns_agent_tool_result() -> None:
    """user tool_result → agent_tool_result event."""
    event = parse_jsonl_line(_make_user_tool_result("file contents here"))
    assert event is not None
    assert event.event_type == "agent_tool_result"
    assert "file contents here" in str(event.data["content"])


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-user-tool-result")
def test_parse_user_tool_result_array_content_joined() -> None:
    """user tool_result with array content → text items are joined."""
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "content": [
                            {"type": "text", "text": "part1"},
                            {"type": "text", "text": "part2"},
                        ],
                    }
                ]
            },
        }
    )
    event = parse_jsonl_line(line)
    assert event is not None
    assert event.event_type == "agent_tool_result"
    assert "part1" in str(event.data["content"])
    assert "part2" in str(event.data["content"])


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-user-tool-result")
def test_parse_user_tool_result_empty_content_returns_none() -> None:
    """user tool_result with empty content → None."""
    event = parse_jsonl_line(_make_user_tool_result(""))
    assert event is None


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-user-tool-result")
def test_parse_user_no_tool_result_returns_none() -> None:
    """user message without tool_result blocks → None."""
    line = json.dumps(
        {"type": "user", "message": {"content": [{"type": "text", "text": "hi"}]}}
    )
    event = parse_jsonl_line(line)
    assert event is None


# ===========================================================================
# result
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-result")
def test_parse_result_returns_agent_result() -> None:
    """result line → agent_result event with subtype and result."""
    event = parse_jsonl_line(_make_result("success", "PR opened"))
    assert event is not None
    assert event.event_type == "agent_result"
    assert event.data["subtype"] == "success"
    assert "PR opened" in str(event.data["result"])


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-result")
def test_parse_result_empty_result_text() -> None:
    """result with empty result text emits event (result itself is meaningful)."""
    event = parse_jsonl_line(_make_result("success", ""))
    assert event is not None
    assert event.event_type == "agent_result"


# ===========================================================================
# Noise / dropped types
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-noise-dropped")
def test_parse_system_line_returns_none() -> None:
    """system lines are dropped."""
    event = parse_jsonl_line(_make_system())
    assert event is None


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-noise-dropped")
def test_parse_thinking_tokens_returns_none() -> None:
    """thinking_tokens lines are dropped."""
    event = parse_jsonl_line(_make_thinking_tokens())
    assert event is None


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-noise-dropped")
def test_parse_unknown_type_returns_none() -> None:
    """Unknown type lines are dropped."""
    event = parse_jsonl_line(json.dumps({"type": "some_future_type", "data": {}}))
    assert event is None


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-noise-dropped")
def test_parse_non_json_line_returns_none() -> None:
    """Non-JSON input is dropped silently."""
    event = parse_jsonl_line("not json at all")
    assert event is None


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-noise-dropped")
def test_parse_empty_string_returns_none() -> None:
    """Empty line is dropped."""
    event = parse_jsonl_line("")
    assert event is None


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-noise-dropped")
def test_parse_non_dict_json_returns_none() -> None:
    """JSON that is not a dict (e.g. array) is dropped."""
    event = parse_jsonl_line(json.dumps([1, 2, 3]))
    assert event is None


# ===========================================================================
# Secret redaction (I3)
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-secret-redaction")
def test_redact_github_pat_in_text() -> None:
    """GitHub PAT in agent text is redacted before emitting (I3)."""
    text = "token: ghp_abcdefghijklmnopqrstuvwxyzABCDEFGH"
    redacted = redact_secrets(text)
    assert "ghp_" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-secret-redaction")
def test_redact_github_server_token_in_text() -> None:
    """GitHub server token (ghs_...) in agent text is redacted."""
    text = "Authorization: ghs_abcdefghijklmnopqrstuvwxyz1234567890"
    redacted = redact_secrets(text)
    assert "ghs_" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-secret-redaction")
def test_redact_sk_ant_token_in_text() -> None:
    """Anthropic sk-ant-* token in agent text is redacted (I3)."""
    text = "key: sk-ant-api01-abcdefghijklmnopqrstuvwxyz"
    redacted = redact_secrets(text)
    assert "sk-ant-" not in redacted
    assert "[REDACTED]" in redacted


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-secret-redaction")
def test_redact_bearer_token_in_text() -> None:
    """Bearer token in agent text is redacted (I3)."""
    text = "Authorization: Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9abcdefghijk"
    redacted = redact_secrets(text)
    assert "[REDACTED]" in redacted


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-secret-redaction")
def test_redact_secrets_in_parsed_event() -> None:
    """Secret in assistant text is redacted in the emitted RunEvent."""
    token = "ghp_ExampleSecretTokenXYZ123456789ABCDEFGH"
    event = parse_jsonl_line(_make_assistant_text(f"found token: {token}"))
    assert event is not None
    assert token not in str(event.data)
    assert "[REDACTED]" in str(event.data)


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-secret-redaction")
def test_redact_secrets_in_tool_result() -> None:
    """Secret in tool result content is redacted before emitting."""
    token = "ghp_AnotherSecretTokenXYZ1234567890ABCDEFGH"
    event = parse_jsonl_line(_make_user_tool_result(f"response body: {token}"))
    assert event is not None
    assert token not in str(event.data)
    assert "[REDACTED]" in str(event.data)


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-secret-redaction")
def test_normal_text_not_redacted() -> None:
    """Non-secret text passes through without modification."""
    text = "The build succeeded with 0 errors."
    redacted = redact_secrets(text)
    assert redacted == text


# ===========================================================================
# Truncation (I3)
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-truncation")
def test_truncate_text_short_string_unchanged() -> None:
    """Short strings are returned unchanged."""
    text = "Hello"
    assert truncate_text(text) == text


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-truncation")
def test_truncate_text_exactly_at_limit_unchanged() -> None:
    """String at exactly max_bytes is returned unchanged."""
    text = "a" * _MAX_TEXT_BYTES
    result = truncate_text(text)
    assert result == text


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-truncation")
def test_truncate_text_over_limit_truncated() -> None:
    """String exceeding max_bytes is truncated with suffix."""
    text = "a" * (_MAX_TEXT_BYTES + 100)
    result = truncate_text(text)
    assert len(result.encode("utf-8")) <= _MAX_TEXT_BYTES + len(_TRUNCATION_SUFFIX.encode("utf-8"))
    assert result.endswith(_TRUNCATION_SUFFIX)


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-truncation")
def test_long_agent_message_is_truncated() -> None:
    """Long agent_message text is truncated in the emitted RunEvent."""
    long_text = "word " * 10000  # well over 4 KiB
    event = parse_jsonl_line(_make_assistant_text(long_text))
    assert event is not None
    text_bytes = str(event.data["text"]).encode("utf-8")
    assert len(text_bytes) <= _MAX_TEXT_BYTES + len(_TRUNCATION_SUFFIX.encode("utf-8")) + 10


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-truncation")
def test_long_tool_input_is_truncated() -> None:
    """Large tool_use input summary is truncated (capped at 512 bytes)."""
    large_input = {"data": "x" * 10000}
    event = parse_jsonl_line(_make_assistant_tool_use("Write", large_input))
    assert event is not None
    assert len(str(event.data["input_summary"]).encode("utf-8")) <= 512 + len(
        _TRUNCATION_SUFFIX.encode("utf-8")
    ) + 10


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-i3-truncation")
def test_long_tool_result_is_truncated() -> None:
    """Long tool_result content is truncated (capped at 512 bytes)."""
    long_content = "result line\n" * 1000
    event = parse_jsonl_line(_make_user_tool_result(long_content))
    assert event is not None
    assert len(str(event.data["content"]).encode("utf-8")) <= 512 + len(
        _TRUNCATION_SUFFIX.encode("utf-8")
    ) + 10


# ===========================================================================
# Background-task wiring — SubprocessBackend uses parser (not raw passthrough)
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-subprocess-integration")
async def test_subprocess_backend_emits_agent_message_events() -> None:
    """SubprocessBackend._watch() uses parse_jsonl_line and emits agent_message."""
    import asyncio
    import json as _json
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from src.ports.execution_backend import SubprocessBackend
    from src.ports.harness import ProcessResult, RunEventStore

    # Fake process that emits one assistant text line
    class _FakeStreamReader:
        def __init__(self) -> None:
            self._lines = [
                (_json.dumps({
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "I will fix the bug."}]}
                }) + "\n").encode(),
            ]
            self._idx = 0

        def __aiter__(self) -> _FakeStreamReader:
            return self

        async def __anext__(self) -> bytes:
            if self._idx >= len(self._lines):
                raise StopAsyncIteration
            val = self._lines[self._idx]
            self._idx += 1
            return val

    fake_proc = MagicMock()
    fake_proc.stdout = _FakeStreamReader()
    fake_proc.wait = AsyncMock(return_value=0)
    fake_proc.pid = 12345
    fake_proc.returncode = None

    store = RunEventStore()
    run_id = str(uuid.uuid4())
    store.register(run_id)

    async def _fake_runner(args: list[str], cwd: str, env: dict[str, str]) -> ProcessResult:
        return ProcessResult(fake_proc)

    harness = MagicMock()
    harness._clone_repo = AsyncMock(return_value=None)
    harness._materialize_contract = MagicMock(return_value=None)
    harness._configure_git_identity = AsyncMock(return_value=None)
    harness._write_spawn_hook = MagicMock(return_value=None)

    backend = SubprocessBackend(process_runner=_fake_runner)
    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="repo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    await asyncio.sleep(0.1)

    events = store.get_events(run_id)
    agent_msg_events = [e for e in events if e.event_type == "agent_message"]
    assert agent_msg_events, (
        "No agent_message events — subprocess backend must use parse_jsonl_line"
    )
    assert "fix the bug" in str(agent_msg_events[0].data.get("text", ""))


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-subprocess-integration")
async def test_subprocess_backend_drops_system_noise() -> None:
    """SubprocessBackend._watch() drops system lines — they must NOT appear as events."""
    import asyncio
    import json as _json
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from src.ports.execution_backend import SubprocessBackend
    from src.ports.harness import ProcessResult, RunEventStore

    class _FakeStreamReader:
        def __init__(self) -> None:
            self._lines = [
                (
                    _json.dumps({"type": "system", "subtype": "init", "session_id": "xyz"})
                    + "\n"
                ).encode(),
                (
                    _json.dumps({"type": "thinking_tokens", "tokens": "thinking..."}) + "\n"
                ).encode(),
            ]
            self._idx = 0

        def __aiter__(self) -> _FakeStreamReader:
            return self

        async def __anext__(self) -> bytes:
            if self._idx >= len(self._lines):
                raise StopAsyncIteration
            val = self._lines[self._idx]
            self._idx += 1
            return val

    fake_proc = MagicMock()
    fake_proc.stdout = _FakeStreamReader()
    fake_proc.wait = AsyncMock(return_value=0)
    fake_proc.pid = 12345
    fake_proc.returncode = None

    store = RunEventStore()
    run_id = str(uuid.uuid4())
    store.register(run_id)

    async def _fake_runner(args: list[str], cwd: str, env: dict[str, str]) -> ProcessResult:
        return ProcessResult(fake_proc)

    harness = MagicMock()
    harness._clone_repo = AsyncMock(return_value=None)
    harness._materialize_contract = MagicMock(return_value=None)
    harness._configure_git_identity = AsyncMock(return_value=None)
    harness._write_spawn_hook = MagicMock(return_value=None)

    backend = SubprocessBackend(process_runner=_fake_runner)
    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="repo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    await asyncio.sleep(0.1)

    events = store.get_events(run_id)
    noise_events = [e for e in events if e.event_type in ("system", "thinking_tokens", "raw")]
    assert not noise_events, (
        f"Noise events must be dropped, got: {[e.event_type for e in noise_events]}"
    )


# ===========================================================================
# K8s log streaming — FakeKubeLogClient wiring
# ===========================================================================


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-k8s-log-streaming")
async def test_k8s_backend_streams_agent_messages_via_log_client() -> None:
    """K8sJobBackend emits agent_message events when FakeKubeLogClient is wired."""
    import asyncio
    import json as _json
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from src.ports.execution_backend import FakeKubeClient, FakeKubeLogClient, K8sJobBackend
    from src.ports.harness import RunEventStore

    fake_kube = FakeKubeClient()
    fake_log = FakeKubeLogClient()
    fake_log.configure_log_lines([
        _json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "K8s agent says hello."}]},
        }),
        _json.dumps({"type": "system", "subtype": "init"}),  # should be dropped
        _json.dumps({"type": "result", "subtype": "success", "result": "done"}),
    ])

    run_id = str(uuid.uuid4())
    job_name = f"orch-agent-{run_id[:16]}"
    fake_kube.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"succeeded": 1}}],
    )

    backend = K8sJobBackend(
        image="ghcr.io/test/runner:test",
        namespace="test-ns",
        kube_client=fake_kube,
        kube_log_client=fake_log,
        poll_interval_s=0.001,
        job_timeout_s=10.0,
    )

    store = RunEventStore()
    store.register(run_id)

    harness = MagicMock()
    harness._clone_repo = AsyncMock(return_value=None)
    harness._materialize_contract = MagicMock(return_value=None)
    harness._configure_git_identity = AsyncMock(return_value=None)

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="repo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    await asyncio.sleep(0.2)

    events = store.get_events(run_id)
    agent_msgs = [e for e in events if e.event_type == "agent_message"]
    assert agent_msgs, "No agent_message events from K8s log streaming"
    assert "hello" in str(agent_msgs[0].data.get("text", ""))

    # system line must be dropped
    system_events = [e for e in events if e.event_type == "system"]
    assert not system_events, "system noise must be dropped in K8s log stream"

    # result must be surfaced
    result_events = [e for e in events if e.event_type == "agent_result"]
    assert result_events, "agent_result event must be emitted from K8s log stream"


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-k8s-no-log-client")
async def test_k8s_backend_without_log_client_no_transcript_events() -> None:
    """K8sJobBackend with no KubeLogPort does NOT emit transcript events (safe default)."""
    import asyncio
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from src.ports.execution_backend import FakeKubeClient, K8sJobBackend
    from src.ports.harness import RunEventStore

    fake_kube = FakeKubeClient()
    run_id = str(uuid.uuid4())
    job_name = f"orch-agent-{run_id[:16]}"
    fake_kube.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"succeeded": 1}}],
    )

    backend = K8sJobBackend(
        image="ghcr.io/test/runner:test",
        namespace="test-ns",
        kube_client=fake_kube,
        kube_log_client=None,  # no log streaming
        poll_interval_s=0.001,
        job_timeout_s=10.0,
    )

    store = RunEventStore()
    store.register(run_id)
    harness = MagicMock()
    harness._clone_repo = AsyncMock(return_value=None)

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="repo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    await asyncio.sleep(0.1)

    events = store.get_events(run_id)
    transcript_events = [
        e for e in events
        if e.event_type in ("agent_message", "agent_tool_use", "agent_tool_result", "agent_result")
    ]
    assert not transcript_events, (
        "No transcript events should be emitted when kube_log_client is None"
    )


@pytest.mark.covers("§9.2-agent-transcript", "agent-transcript-k8s-i3-secret-in-log")
async def test_k8s_log_stream_redacts_secrets() -> None:
    """Secrets in K8s pod log are redacted before reaching the event store (I3)."""
    import asyncio
    import json as _json
    import uuid
    from unittest.mock import AsyncMock, MagicMock

    from src.ports.execution_backend import FakeKubeClient, FakeKubeLogClient, K8sJobBackend
    from src.ports.harness import RunEventStore

    token = "ghp_SecretTokenThatMustBeRedactedXYZ123456"
    fake_kube = FakeKubeClient()
    fake_log = FakeKubeLogClient()
    fake_log.configure_log_lines([
        _json.dumps({
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"token={token}"}]},
        }),
    ])

    run_id = str(uuid.uuid4())
    job_name = f"orch-agent-{run_id[:16]}"
    fake_kube.configure_job_outcome(
        job_name,
        statuses=[{"metadata": {"name": job_name}, "status": {"succeeded": 1}}],
    )

    backend = K8sJobBackend(
        image="ghcr.io/test/runner:test",
        namespace="test-ns",
        kube_client=fake_kube,
        kube_log_client=fake_log,
        poll_interval_s=0.001,
        job_timeout_s=10.0,
    )

    store = RunEventStore()
    store.register(run_id)
    harness = MagicMock()
    harness._clone_repo = AsyncMock(return_value=None)

    await backend.dispatch(
        run_id=run_id,
        repo_owner="acme",
        repo_name="repo",
        branch=None,
        claude_args=["claude", "-p", "x"],
        child_env={"GH_TOKEN": "gh"},
        allowed_agent_refs=None,
        contract="agents/implementer.md",
        event_store=store,
        harness=harness,
    )
    await asyncio.sleep(0.2)

    events = store.get_events(run_id)
    all_data_str = str([e.data for e in events])
    assert token not in all_data_str, (
        "Literal secret token must NOT appear in event store (I3)"
    )
    assert "[REDACTED]" in all_data_str, "Redaction placeholder must appear"
