"""I9 spawn allow-set hook — Claude Code PreToolUse gate for Task tool spawns.

This script is materialized into the per-dispatch working tree and referenced
from .claude/settings.json as a PreToolUse hook for the Task tool.

Protocol (Claude Code hook contract):
  - Receives tool input JSON on stdin.
  - Exits 0   → allow the Task spawn.
  - Exits 1   → deny the Task spawn (Claude Code blocks the tool call).

Fail-closed semantics (SECURITY.md §3 I9):
  - If ORCHESTRATOR_ALLOWED_AGENT_REFS is not set     → DENY.
  - If stdin cannot be parsed as JSON                  → DENY.
  - If allowed set is empty                            → DENY (no spawns allowed).
  - If subagent_type is absent from stdin payload      → DENY.
  - If subagent_type is NOT in the allow-set           → DENY.
  - Only if subagent_type IS in the allow-set          → ALLOW (exit 0).

Allow-set is injected as a comma-separated env var:
  ORCHESTRATOR_ALLOWED_AGENT_REFS=agents/reviewer.md,agents/fixer.md

The env var is populated by ClaudeCodeHarnessPort._build_child_env() from
DispatchContext.allowed_agent_refs at dispatch time.  An empty string
("") means an empty allow-set (deny all Task spawns).
"""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    """Return 0 (allow) or 1 (deny)."""
    # 1. Read allow-set from env — fail closed if missing.
    raw_refs = os.environ.get("ORCHESTRATOR_ALLOWED_AGENT_REFS")
    if raw_refs is None:
        # Env var absent: deny (fail closed — missing control is a deny).
        sys.stderr.write(
            "I9 hook: ORCHESTRATOR_ALLOWED_AGENT_REFS not set — spawn DENIED\n"
        )
        return 1

    # 2. Parse allow-set — empty string means empty list (deny all spawns).
    allowed: set[str] = set(filter(None, raw_refs.split(",")))
    if not allowed:
        sys.stderr.write(
            "I9 hook: allow-set is empty — all Task spawns DENIED\n"
        )
        return 1

    # 3. Parse tool input from stdin — fail closed on parse error.
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception as exc:
        sys.stderr.write(f"I9 hook: failed to parse stdin JSON ({exc}) — spawn DENIED\n")
        return 1

    # 4. Extract subagent_type — fail closed if absent.
    # Claude Code delivers: {"tool_name": "Task", "tool_input": {...}}
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        sys.stderr.write("I9 hook: tool_input is not a dict — spawn DENIED\n")
        return 1

    subagent_type = tool_input.get("subagent_type")
    if subagent_type is None:
        sys.stderr.write("I9 hook: subagent_type absent from tool_input — spawn DENIED\n")
        return 1

    # 5. Enforce allow-set membership.
    if subagent_type not in allowed:
        sys.stderr.write(
            f"I9 hook: subagent_type '{subagent_type}' not in allow-set "
            f"{sorted(allowed)} — spawn DENIED\n"
        )
        return 1

    # 6. In allow-set — permit.
    return 0


if __name__ == "__main__":
    sys.exit(main())
