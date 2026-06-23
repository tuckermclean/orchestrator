"""I9 spawn allow-set hook — Claude Code PreToolUse gate for Task tool spawns.

This script is materialized into the per-dispatch working tree and referenced
from .claude/settings.json as a PreToolUse hook for the Task tool.

Protocol (Claude Code hook contract):
  - Receives tool input JSON on stdin.
  - Exits 0   → allow the Task spawn.
  - Exits 2   → deny the Task spawn. Per the Claude Code PreToolUse contract ONLY
                exit code 2 blocks the tool (stderr is fed back to Claude); exit
                code 1 is a NON-blocking error and the spawn would proceed. Every
                deny path below returns 2 — do not "fix" these back to 1.

Validation model (AGENTS.md §7.4, SECURITY.md §3 I9):
  Correct specialist spawns always use subagent_type "general-purpose" and embed
  the AgentRef in the prompt:
      "Act as the agent defined in .agents/<AgentRef>. Read that file first."
  So the hook:
    1. Requires subagent_type == "general-purpose" (any other → DENY, fail-closed).
    2. Parses the AgentRef from the prompt: the token after ".agents/" in the prompt
       text (requires the ".agents/" marker; robust to surrounding text).
    3. ALLOWs (exit 0) iff that AgentRef is in the allow-set; else DENY (exit 2).

Fail-closed semantics (SECURITY.md §3 I9):
  - If ORCHESTRATOR_ALLOWED_AGENT_REFS is not set          → DENY.
  - If stdin cannot be parsed as JSON                       → DENY.
  - If allow-set is empty                                   → DENY (no spawns allowed).
  - If tool_input absent or not a dict                      → DENY.
  - If subagent_type absent                                 → DENY.
  - If subagent_type is not "general-purpose"               → DENY.
  - If prompt absent or not a string                        → DENY.
  - If no ".agents/<ref>" pattern found in prompt           → DENY.
  - If the parsed AgentRef is NOT in the allow-set          → DENY.
  - Only if all checks pass                                 → ALLOW (exit 0).

Allow-set is injected as a comma-separated env var:
  ORCHESTRATOR_ALLOWED_AGENT_REFS=engineering-security-engineer.md,engineering-code-reviewer.md

The env var is populated by the harness from DispatchContext.allowed_agent_refs at
dispatch time.  An empty string ("") means an empty allow-set (deny all Task spawns).
"""

from __future__ import annotations

import json
import os
import re
import sys

# Matches ".agents/<AgentRef>" anywhere in the prompt.
# AgentRef is a flat ".md" filename (no directory separators).  The pattern
# captures the filename including its ".md" extension and stops there, so that a
# sentence-ending period appended by the caller (e.g. "...engineer.md. Read")
# is not mistakenly included in the captured group.
_AGENT_REF_RE = re.compile(r"\.agents/([^\s/\"']*\.md)")

# The only valid subagent_type for specialist spawns (AGENTS.md §7.4).
_REQUIRED_SUBAGENT_TYPE = "general-purpose"


def _parse_agent_ref(prompt: str) -> str | None:
    """Extract the AgentRef from the prompt string.

    Looks for the first ".agents/<ref>" occurrence.  Returns the ref
    (e.g. "engineering-security-engineer.md") or None if absent.
    """
    m = _AGENT_REF_RE.search(prompt)
    if m:
        return m.group(1)
    return None


def main() -> int:
    """Return 0 (allow) or 2 (deny — the Claude Code PreToolUse blocking code)."""
    # 1. Read allow-set from env — fail closed if missing.
    raw_refs = os.environ.get("ORCHESTRATOR_ALLOWED_AGENT_REFS")
    if raw_refs is None:
        # Env var absent: deny (fail closed — missing control is a deny).
        sys.stderr.write(
            "I9 hook: ORCHESTRATOR_ALLOWED_AGENT_REFS not set — spawn DENIED\n"
        )
        return 2

    # 2. Parse allow-set — empty string means empty list (deny all spawns).
    allowed: set[str] = set(filter(None, raw_refs.split(",")))
    if not allowed:
        sys.stderr.write(
            "I9 hook: allow-set is empty — all Task spawns DENIED\n"
        )
        return 2

    # 3. Parse tool input from stdin — fail closed on parse error.
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception as exc:
        sys.stderr.write(f"I9 hook: failed to parse stdin JSON ({exc}) — spawn DENIED\n")
        return 2

    # 4. Extract tool_input — fail closed if absent or wrong type.
    # Claude Code delivers: {"tool_name": "Task", "tool_input": {...}}
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        sys.stderr.write("I9 hook: tool_input is not a dict — spawn DENIED\n")
        return 2

    # 5. Require subagent_type == "general-purpose" — any other type is DENIED.
    # Specialist spawns per AGENTS.md §7.4 ALWAYS use "general-purpose"; a different
    # subagent_type is either a misconfigured spawn or an injection attempt.
    subagent_type = tool_input.get("subagent_type")
    if subagent_type is None:
        sys.stderr.write("I9 hook: subagent_type absent from tool_input — spawn DENIED\n")
        return 2
    if subagent_type != _REQUIRED_SUBAGENT_TYPE:
        sys.stderr.write(
            f"I9 hook: subagent_type '{subagent_type}' is not 'general-purpose' — spawn DENIED\n"
        )
        return 2

    # 6. Parse the AgentRef from the prompt's ".agents/<AgentRef>" marker.
    # The prompt field carries the canonical AgentRef (AGENTS.md §7.4):
    #   "Act as the agent defined in .agents/<AgentRef>. Read that file first."
    prompt = tool_input.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        sys.stderr.write("I9 hook: prompt absent or not a string — spawn DENIED\n")
        return 2

    agent_ref = _parse_agent_ref(prompt)
    if agent_ref is None:
        sys.stderr.write(
            "I9 hook: no '.agents/<ref>' pattern found in prompt — spawn DENIED\n"
        )
        return 2

    # 7. Enforce allow-set membership on the parsed AgentRef.
    if agent_ref not in allowed:
        sys.stderr.write(
            f"I9 hook: AgentRef '{agent_ref}' not in allow-set "
            f"{sorted(allowed)} — spawn DENIED\n"
        )
        return 2

    # 8. All checks passed — permit.
    return 0


if __name__ == "__main__":
    sys.exit(main())
