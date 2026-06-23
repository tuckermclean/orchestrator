"""agent_transcript.py — JSONL → RunEvent mapping for Claude Code CLI output.

Claude Code (``claude --output-format stream-json``) emits one JSON object per
line on stdout.  Each object has a ``type`` field that classifies the message.
This module converts the meaningful message types into compact RunEvents that
the RunEventStore can stream to the UI via SSE, while:

  - Dropping high-noise / low-signal lines (``system``, ``thinking_tokens``).
  - Extracting readable summaries for ``assistant`` messages (text, tool_use,
    thinking blocks), ``user`` messages (tool results), and ``result`` lines.
  - Redacting / truncating to enforce I3 and keep SSE payloads small.

Security (I3):
  All agent output is UNTRUSTED.  This module:
    1. Redacts secret-like strings (``ghp_*``, ``sk-ant-*``, ``Bearer <token>``
       patterns) from text payloads before emitting them.
    2. Caps every text field at ``_MAX_TEXT_BYTES`` (4 KiB) so a single verbose
       tool output cannot inflate SSE frames to unbounded size.
    3. Never evaluates agent output as code or HTML — treated as opaque text.

Event types emitted (new taxonomy — consistent with existing RunEvent.event_type
strings):
  ``agent_message``       — assistant text block (readable prose)
  ``agent_thinking``      — assistant thinking block (collapsed in UI by default)
  ``agent_tool_use``      — tool call with name + truncated input summary
  ``agent_tool_result``   — tool result content (truncated)
  ``agent_result``        — top-level result line (subtype + short result text)

Dropped (returns None):
  ``system``              — harness startup noise (session ID, env info, etc.)
  ``thinking_tokens``     — streaming token deltas, high volume, no user value
  Any line that does not parse as JSON.
  Any assistant message with no meaningful content blocks.

Verdict extraction (SPEC §5, §8.2):
  ``extract_verdict_from_events`` scans a completed run's events for a fenced
  JSON verdict block (```json ... ```) embedded in an ``agent_message`` or
  ``agent_result`` event.  The reviewer emits one such block as its final output;
  the engine reads it from the run result rather than from a committed file.
  Returns a ``Verdict`` or ``None`` when no parseable verdict is found
  (crash fail-safe: the engine treats ``None`` as ``"unknown"`` blockers).

This module contains only pure functions (no async, no I/O) — consistent with
the spec's decision-function purity contract (AGENTS.md §5).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from src.domain.types import RunEvent, Verdict

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum bytes for any single text payload before truncation.
# 4 KiB is generous for a UI line while preventing runaway SSE frames.
_MAX_TEXT_BYTES = 4096

# Truncation suffix appended when a payload is cut short.
_TRUNCATION_SUFFIX = "…[truncated]"

# Event types that carry no actionable signal for the run view — DROP them.
_DROPPED_TYPES: frozenset[str] = frozenset(
    {
        "system",
        "thinking_tokens",
        # Internal harness / control-plane lifecycle events are also in the
        # stream from k8s and subprocess watchers; keep only agent-originated.
    }
)

# Regex patterns for secret-like strings (I3 — never echo credentials).
# Patterns are intentionally broad to catch token variants.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    # GitHub PATs and app installation tokens
    re.compile(r"gh[ps]_[A-Za-z0-9]{36,}", re.IGNORECASE),
    # Anthropic Claude OAuth / API tokens
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}", re.IGNORECASE),
    # Bearer tokens in HTTP headers or plain text
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
    # AWS-style access keys
    re.compile(r"AKIA[A-Z0-9]{16}", re.IGNORECASE),
    # Generic high-entropy base64 runs prefixed by "token" or "key" (heuristic)
    re.compile(r"(?:token|key|secret)[=:\s]+[A-Za-z0-9+/=_\-]{32,}", re.IGNORECASE),
)

_REDACTION_PLACEHOLDER = "[REDACTED]"


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------


def redact_secrets(text: str) -> str:
    """Replace secret-like strings in *text* with ``[REDACTED]``.

    Applied to all text payloads before they leave this module (I3 defence
    in depth — the harness already excludes secrets from child_env, but
    agent output may echo back tokens it received through tool results).

    Pure function: same input → same output; no side effects.
    """
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_REDACTION_PLACEHOLDER, text)
    return text


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def truncate_text(text: str, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes.

    Truncation is suffix-marked so the UI can display a visual indicator.
    Operates on encoded byte length to protect against large Unicode code points.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Cut at max_bytes, decode with replacement, append suffix.
    cut = encoded[:max_bytes].decode("utf-8", errors="replace")
    return cut + _TRUNCATION_SUFFIX


def _safe_text(value: object, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    """Coerce *value* to a string, redact secrets, and truncate."""
    text = str(value) if not isinstance(value, str) else value
    text = redact_secrets(text)
    return truncate_text(text, max_bytes)


# ---------------------------------------------------------------------------
# Content-block extractors
# ---------------------------------------------------------------------------


def _extract_text_block(block: dict[str, Any]) -> str | None:
    """Extract the text payload from an assistant ``text`` content block.

    Returns None if the block carries no non-empty text.
    """
    text = block.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return None
    return _safe_text(text)


def _extract_thinking_block(block: dict[str, Any]) -> str | None:
    """Extract a summary from an assistant ``thinking`` content block.

    Thinking blocks can be large; we truncate aggressively (1 KiB) and prefix
    with a clear label so the UI can collapse them by default.
    """
    thinking = block.get("thinking", "")
    if not isinstance(thinking, str) or not thinking.strip():
        return None
    return _safe_text(thinking, max_bytes=1024)


def _extract_tool_use_block(block: dict[str, Any]) -> dict[str, object] | None:
    """Extract name + truncated input summary from an assistant ``tool_use`` block.

    The input dict can be large (e.g. full file contents for Write).  We
    serialize a compact JSON summary and truncate it.
    """
    name = block.get("name", "")
    if not isinstance(name, str) or not name:
        return None
    tool_input = block.get("input", {})
    try:
        input_summary = json.dumps(tool_input, separators=(",", ":"))
    except (TypeError, ValueError):
        input_summary = str(tool_input)
    return {
        "name": name,
        "input_summary": _safe_text(input_summary, max_bytes=512),
    }


# ---------------------------------------------------------------------------
# Top-level JSONL line parser
# ---------------------------------------------------------------------------


def parse_jsonl_line(line: str) -> RunEvent | None:
    """Parse one JSONL line from claude stdout into a RunEvent, or return None.

    None means the line should be dropped (noise, or non-JSON).

    This is a pure synchronous function — no I/O, no async (AGENTS.md §5).

    Message type handling:

    ``assistant``
      Iterates the ``message.content`` array.  Each ``text`` block becomes an
      ``agent_message`` event; each ``thinking`` block an ``agent_thinking``
      event; each ``tool_use`` block an ``agent_tool_use`` event.  If multiple
      content blocks are present, the *first meaningful block* is returned (the
      watcher calls this function once per line — one event per call).  A line
      with no useful content blocks returns None.

    ``user``
      Looks for ``tool_results`` in ``message.content`` (the format claude uses
      for tool-result messages).  Returns a single ``agent_tool_result`` event
      with the truncated content.

    ``result``
      Returns an ``agent_result`` event with subtype and a brief result summary.

    All others (``system``, ``thinking_tokens``, unknown)
      Dropped — returns None.
    """
    if not line:
        return None

    try:
        data: dict[str, Any] = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    msg_type = str(data.get("type", ""))

    if msg_type in _DROPPED_TYPES:
        return None

    ts = datetime.now(tz=UTC)

    # ------------------------------------------------------------------
    # assistant — extract the first useful content block
    # ------------------------------------------------------------------
    if msg_type == "assistant":
        message = data.get("message", {})
        if not isinstance(message, dict):
            return None
        content = message.get("content", [])
        if not isinstance(content, list):
            return None

        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type", ""))

            if block_type == "text":
                text = _extract_text_block(block)
                if text is not None:
                    return RunEvent(
                        event_type="agent_message",
                        data={"text": text},
                        timestamp=ts,
                    )

            elif block_type == "thinking":
                thinking = _extract_thinking_block(block)
                if thinking is not None:
                    return RunEvent(
                        event_type="agent_thinking",
                        data={"thinking": thinking},
                        timestamp=ts,
                    )

            elif block_type == "tool_use":
                tool_data = _extract_tool_use_block(block)
                if tool_data is not None:
                    return RunEvent(
                        event_type="agent_tool_use",
                        data=tool_data,
                        timestamp=ts,
                    )

        # No meaningful block found.
        return None

    # ------------------------------------------------------------------
    # user — tool results
    # ------------------------------------------------------------------
    if msg_type == "user":
        message = data.get("message", {})
        if not isinstance(message, dict):
            return None
        content = message.get("content", [])
        if not isinstance(content, list):
            return None

        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type", "")) == "tool_result":
                raw_content = block.get("content", "")
                if isinstance(raw_content, list):
                    # Array of content blocks — join text items.
                    texts = [
                        str(c.get("text", ""))
                        for c in raw_content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    content_str = " ".join(t for t in texts if t)
                else:
                    content_str = str(raw_content) if raw_content else ""

                if not content_str.strip():
                    return None

                return RunEvent(
                    event_type="agent_tool_result",
                    data={"content": _safe_text(content_str, max_bytes=512)},
                    timestamp=ts,
                )

        return None

    # ------------------------------------------------------------------
    # result — top-level terminal message
    # ------------------------------------------------------------------
    if msg_type == "result":
        subtype = str(data.get("subtype", ""))
        result_text = data.get("result", "")
        if isinstance(result_text, str):
            result_text = _safe_text(result_text, max_bytes=1024)
        else:
            result_text = _safe_text(str(result_text), max_bytes=1024)

        return RunEvent(
            event_type="agent_result",
            data={
                "subtype": subtype,
                "result": result_text,
            },
            timestamp=ts,
        )

    # ------------------------------------------------------------------
    # Unknown type — drop
    # ------------------------------------------------------------------
    return None


# ---------------------------------------------------------------------------
# Verdict extraction from run events (SPEC §5, §8.2)
# ---------------------------------------------------------------------------

# Matches a fenced ```json ... ``` block (non-greedy, DOTALL).
# The reviewer emits exactly one such block as its final output.
_VERDICT_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# Reserved sentinel signature — a verdict containing only this slug is not a
# real verdict (it is the init sentinel the Engine formerly seeded on the
# branch before dispatching the reviewer).  It must not be treated as a valid
# reviewer result.  Since the sentinel commit path is now removed, a block
# containing this slug means the reviewer reproduced the sentinel verbatim
# (which should not happen in practice, but the check is retained as defence
# in depth and for backward compatibility with SPEC §5 sentinel semantics).
_SENTINEL_SIG = "verdict-file-not-written"


def _is_sentinel_verdict(v: Verdict) -> bool:
    """True when the verdict is the init sentinel (not a real reviewer output)."""
    return v.blocker_signatures == [_SENTINEL_SIG]


def extract_verdict_from_events(events: list[RunEvent]) -> Verdict | None:
    """Scan a completed run's events for a fenced JSON verdict block.

    The converge reviewer emits its ``Verdict`` as a single fenced JSON block
    (``` ``json`` ... ```) in its final message.  This function scans all
    ``agent_message`` and ``agent_result`` events in reverse order (most-recent
    first) and returns the first parseable, non-sentinel ``Verdict`` found.

    Returns ``None`` when no parseable verdict is found — the engine treats this
    as ``"unknown"`` blockers (crash fail-safe, SPEC §5).

    Pure function: no I/O, no async (AGENTS.md §5).

    Security (I3): the events contain UNTRUSTED agent output.  This function
    only JSON-parses the fenced block and validates it through the ``Verdict``
    Pydantic model; it does not evaluate any code or interpret the output as HTML.
    """
    # Scan in reverse order so the reviewer's final (most-recent) message wins.
    for event in reversed(events):
        if event.event_type not in ("agent_message", "agent_result"):
            continue

        # Extract the text payload from the event data.
        text: str | None = None
        if event.event_type == "agent_message":
            raw = event.data.get("text", "")
            text = str(raw) if raw else None
        elif event.event_type == "agent_result":
            raw = event.data.get("result", "")
            text = str(raw) if raw else None

        if not text:
            continue

        # Search for a fenced JSON block within the text.
        match = _VERDICT_FENCE_RE.search(text)
        if match is None:
            continue

        json_str = match.group(1)
        try:
            verdict = Verdict.model_validate_json(json_str)
        except (ValueError, TypeError):
            # Malformed JSON or schema mismatch — try the next event.
            continue

        if _is_sentinel_verdict(verdict):
            # Sentinel reproduced verbatim by the reviewer — not a real verdict.
            continue

        return verdict

    return None
