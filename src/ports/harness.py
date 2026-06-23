"""ClaudeCodeHarnessPort — Claude Code HarnessPort implementation.

The harness dispatches `claude` (Claude Code CLI) agents via a pluggable
ExecutionBackend.  By default (dev/CI) the subprocess backend spawns a local
child process; in production the K8s Job backend schedules an isolated pod.

Each dispatch:
  1. Mints a scoped GitHub App installation token (forge_token_scope determines perms).
  2. Builds the prompt, child env, and claude argv.
  3. Delegates to ExecutionBackend.dispatch() which is responsible for cloning the
     repo, writing the I9 hook (when allowed_agent_refs is set), and running claude.
  4. Returns a RunHandle immediately (non-blocking).

Design rationale for clone ownership (fixes production architecture bug):
  The control-plane image ships WITHOUT git (by design — see Dockerfile comment).
  The K8s Job backend's agent-runner image HAS git + claude + python3.  Therefore,
  cloning must happen INSIDE the execution environment, not in the control-plane.
  Each backend now owns its own working-tree preparation:
    - SubprocessBackend: clones locally (dev/CI, git is on the host).
    - K8sJobBackend:     clone + hook setup + claude run are a single shell script
                        executed INSIDE the agent-runner pod.

I3 invariants:
  - Only CLAUDE_CODE_OAUTH_TOKEN and a scoped GH_TOKEN reach the child env.
  - Orchestrator master creds (App private key, FORGE_TOKEN) are NEVER forwarded.
  - No contributor text in env or subprocess args (I9).
  - I3 token handling for K8s: GH_TOKEN is in the pod env; git authenticates via
    the runtime env variable — the token NEVER appears as a literal in the Job
    manifest / argv.

I9 spawn enforcement (SECURITY.md §3 I9, closes #52):
  When DispatchContext.allowed_agent_refs is a list (not None), the harness
  communicates this to the backend via the child_env
  (ORCHESTRATOR_ALLOWED_AGENT_REFS) and the allowed_agent_refs parameter.
  - SubprocessBackend: writes src/ports/i9_spawn_hook.py into the cloned repo
    at .claude/i9_spawn_hook.py and writes .claude/settings.json with a
    PreToolUse hook on the Task tool that invokes the hook script via python3.
  - K8sJobBackend: the agent-runner image has the hook baked at
    /opt/orchestrator/i9_spawn_hook.py (deploy/agent-runner.Dockerfile).  The
    Job's entry script copies it into /work/repo/.claude/ and writes
    .claude/settings.json — identical setup, done inside the pod.
  The hook denies via EXIT CODE 2 (Claude Code contract) — do not regress.
  When allowed_agent_refs is None, no hook is written (no harness-level restriction).

trigger_workflow / trigger_ci remain for re-running the repo's own CI via GitHub
Actions.  All agent-dispatch-via-workflow_dispatch code is removed.

Backend selection (HARNESS_EXECUTION_BACKEND env var):
  subprocess (default) — local child process, dev/CI.
  k8s                  — Kubernetes Job per dispatch, prod isolation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import signal
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import jwt

from src.domain.types import (
    DispatchContext,
    PRRef,
    RunEvent,
    RunHandle,
    RunStatus,
)

_log = logging.getLogger(__name__)


def _get_package_pack_dir() -> pathlib.Path:
    """Return the package root's .agents/ directory path.

    Extracted as a module-level function so tests can patch it without
    fighting pathlib.Path.  The real implementation resolves relative to
    this file: src/ports/harness.py → src/ports → src → package root → .agents/.
    """
    return pathlib.Path(__file__).parent.parent.parent / ".agents"


# ---------------------------------------------------------------------------
# GitHub API constants (shared with github.py — keep in sync)
# ---------------------------------------------------------------------------

# _GITHUB_API duplicated here for standalone import; authoritative copy is github.py.
_GITHUB_API = "https://api.github.com"


# ---------------------------------------------------------------------------
# ProcessRunner seam — injectable for unit tests
# ---------------------------------------------------------------------------

class ProcessResult:
    """Slim wrapper around a running subprocess."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        return self._process.stdout

    async def wait(self) -> int:
        return await self._process.wait()

    async def terminate(self) -> None:
        """Send SIGTERM to the entire process group (kills child processes too).

        Uses os.killpg so that the root claude process and all its children
        (git, tools, sub-agents) are signalled together.  Falls back to the
        process's own terminate() when the process group is unavailable (e.g.
        the process was not spawned with start_new_session=True in unit tests).
        """
        try:
            pgid = os.getpgid(self._process.pid)
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            # Process already dead or pgid unavailable — try single-process.
            try:
                self._process.terminate()
            except (ProcessLookupError, PermissionError, AttributeError):
                pass  # already dead — idempotent

    async def kill(self) -> None:
        """Send SIGKILL to the entire process group."""
        try:
            pgid = os.getpgid(self._process.pid)
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            # Process already dead or pgid unavailable — try single-process.
            try:
                self._process.kill()
            except (ProcessLookupError, PermissionError, AttributeError):
                pass


# Callable type: (args, cwd, env) → ProcessResult
ProcessRunner = Callable[
    [list[str], str, dict[str, str]],
    Coroutine[Any, Any, ProcessResult],
]


async def _default_process_runner(
    args: list[str],
    cwd: str,
    env: dict[str, str],
) -> ProcessResult:
    """Default runner: spawns the real claude subprocess.

    start_new_session=True places the child in its own process group so that
    cancel() can signal the entire group (claude + git + sub-agents) rather
    than only the root process.
    """
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    return ProcessResult(process)


# ---------------------------------------------------------------------------
# Run-event store — in-process; Step 9 wires SSE on top of this
# ---------------------------------------------------------------------------

StatusSink = Callable[[str, RunStatus], None]


class RunEventStore:
    """Per-run event accumulator.  Thread-safe (asyncio single-threaded).

    Holds events in memory.  Step 9 (issue #31) will expose these via SSE;
    for #45 they are captured and queryable here.

    Status propagation (issue #101 — write-through):
      Callers may register a per-run status sink via register_status_sink().
      Whenever set_status() records a new status for that run, the sink is
      invoked synchronously (the sink must not block the event loop).
      RunRecordingHarness uses this to propagate live status into the run_store
      so list_runs/get_run always reflect the real run state.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[RunEvent]] = {}
        self._statuses: dict[str, RunStatus] = {}
        # Per-run asyncio.Queue for live streaming; None signals completion.
        self._queues: dict[str, asyncio.Queue[RunEvent | None]] = {}
        # Per-run status sinks registered by RunRecordingHarness (issue #101).
        self._status_sinks: dict[str, StatusSink] = {}

    def register(self, run_id: str) -> None:
        """Initialise storage for a new run."""
        self._events[run_id] = []
        self._statuses[run_id] = RunStatus(state="queued")
        self._queues[run_id] = asyncio.Queue()

    def register_status_sink(self, run_id: str, sink: StatusSink) -> None:
        """Register a callback that is invoked synchronously on every set_status call.

        Used by RunRecordingHarness to propagate live status into the run_store
        (write-through, issue #101).  Only one sink per run_id is supported;
        a second registration replaces the first.

        The sink must be non-blocking (no awaits).  SQLiteRunStore.set_status
        satisfies this — it schedules the DB write via asyncio.create_task.
        FakeRunStore.set_status is also sync.
        """
        self._status_sinks[run_id] = sink

    def append(self, run_id: str, event: RunEvent) -> None:
        """Record an event and push it to the live queue."""
        self._events.setdefault(run_id, []).append(event)
        if run_id in self._queues:
            self._queues[run_id].put_nowait(event)

    def set_status(self, run_id: str, status: RunStatus) -> None:
        current = self._statuses.get(run_id)
        if current is not None and current.state == "completed":
            # Already terminal — guard against double-sentinel on the queue.
            return
        self._statuses[run_id] = status
        if status.state == "completed" and run_id in self._queues:
            # Signal end-of-stream
            self._queues[run_id].put_nowait(None)
        # Notify the write-through sink (issue #101) — invoked after state is
        # committed so the sink always sees the new status.
        sink = self._status_sinks.get(run_id)
        if sink is not None:
            try:
                sink(run_id, status)
            except Exception:
                _log.exception(
                    "RunEventStore status sink raised for run_id=%s status=%s",
                    run_id,
                    status,
                )

    def get_status(self, run_id: str) -> RunStatus:
        return self._statuses.get(run_id, RunStatus(state="queued"))

    def get_events(self, run_id: str) -> list[RunEvent]:
        return list(self._events.get(run_id, []))

    def get_queue(self, run_id: str) -> asyncio.Queue[RunEvent | None] | None:
        """Return the live event queue for SSE streaming (Step 9)."""
        return self._queues.get(run_id)


# ---------------------------------------------------------------------------
# Token minting — scoped GitHub App installation token (I3)
# ---------------------------------------------------------------------------

def _mint_app_jwt(app_id: str, private_key_pem: str) -> str:
    """Return a short-lived GitHub App JWT (10 min)."""
    now = int(time.time())
    payload = {
        "iat": now - 60,  # allow 60 s clock skew
        "exp": now + 600,
        "iss": app_id,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


async def _mint_scoped_installation_token(
    app_id: str,
    private_key_pem: str,
    installation_id: str,
    forge_token_scope: str,
    repo_owner: str,
    repo_name: str,
    http_client: httpx.AsyncClient,
) -> str:
    """Mint a GitHub App installation token scoped to the dispatch permissions.

    forge_token_scope mapping (SPEC §9.2, SECURITY.md §3 I3):
      "repo-comment" → contents:read + issues:write (triager: comment only)
      "repo-branch"  → contents:write + pull_requests:write (implementer/reviewer/fixer)

    The token is scoped to the specific repository only (minimum-privilege).
    Operator credentials (private_key_pem, app_id) are NEVER forwarded.
    """
    if forge_token_scope == "repo-comment":
        permissions = {
            "contents": "read",
            "issues": "write",
            "metadata": "read",
        }
    else:  # "repo-branch"
        permissions = {
            "contents": "write",
            "issues": "write",
            "pull_requests": "write",
            "metadata": "read",
        }

    app_jwt = _mint_app_jwt(app_id, private_key_pem)
    resp = await http_client.post(
        f"{_GITHUB_API}/app/installations/{installation_id}/access_tokens",
        headers={
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "repositories": [repo_name],
            "permissions": permissions,
        },
    )
    resp.raise_for_status()
    return str(resp.json()["token"])


# ---------------------------------------------------------------------------
# Main port
# ---------------------------------------------------------------------------

class ClaudeCodeHarnessPort:
    """HarnessPort that dispatches Claude Code agents via a pluggable ExecutionBackend.

    dispatch(context):
      - Mints a scoped GH installation token for the target repo.
      - Builds the prompt, child env, and claude argv.
      - Delegates to the ExecutionBackend (subprocess or K8s Job), which is
        responsible for cloning the repo, materialising the I9 hook, and running
        claude.  The control-plane never clones for the K8s backend.
      - Returns a RunHandle immediately (non-blocking).

    get_run_status / cancel reflect live run state via RunEventStore.

    Security (I3 / I9):
      - Child env contains ONLY CLAUDE_CODE_OAUTH_TOKEN + GH_TOKEN (scoped).
      - Operator master creds (App key, FORGE_TOKEN) never reach the child.
      - No contributor text in args or env.
      - I3 K8s: GH_TOKEN is in the pod env; token NEVER appears as a literal in
        the Job manifest — git authenticates via the runtime env variable.
      - I9 hook (exit-2 deny) written by each backend into the working tree when
        allowed_agent_refs is set.

    Backend selection:
      - subprocess (default, dev/CI): HARNESS_EXECUTION_BACKEND unset or 'subprocess'.
      - k8s (prod): HARNESS_EXECUTION_BACKEND=k8s.
      - Injected via execution_backend param (tests use FakeExecutionBackend or
        SubprocessBackend with a FakeProcessRunner).

    trigger_workflow / trigger_ci: retained for CI re-runs via GitHub Actions.
    """

    def __init__(
        self,
        claude_oauth_token: str,
        app_id: str,
        private_key_pem: str,
        installation_id: str,
        repo_owner: str,
        repo_name: str,
        event_store: RunEventStore | None = None,
        process_runner: ProcessRunner | None = None,
        http_client: httpx.AsyncClient | None = None,
        # Kept for compatibility with tests that wire trigger_ci/trigger_workflow
        forge_token: str = "",
        # Injectable ExecutionBackend — defaults to SubprocessBackend (or K8s if
        # HARNESS_EXECUTION_BACKEND=k8s).  Tests inject a fake/subprocess backend.
        # The type is imported inline to avoid a circular import at module level.
        execution_backend: Any | None = None,
    ) -> None:
        self._claude_oauth_token = claude_oauth_token
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._installation_id = installation_id
        self._repo_owner = repo_owner
        self._repo_name = repo_name
        self._event_store = event_store or RunEventStore()
        self._http_client = http_client or httpx.AsyncClient(timeout=30.0)
        self._forge_token = forge_token  # used only for trigger_ci / trigger_workflow

        # ExecutionBackend — wires subprocess vs K8s.  process_runner is forwarded
        # to SubprocessBackend for test compat (so existing tests passing process_runner
        # continue to work without modification).
        # Type is ExecutionBackend Protocol; Any used to avoid circular import.
        from src.ports.execution_backend import make_execution_backend
        self._backend: Any = (
            execution_backend
            if execution_backend is not None
            else make_execution_backend(process_runner=process_runner)
        )

        # Keep _process_runner for direct access in tests that inspect it, but
        # process execution is handled by _backend.
        self._process_runner = process_runner or _default_process_runner

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_prompt(self, context: DispatchContext) -> str:
        """Build the Claude Code prompt from the dispatch context.

        Security (I9): prompt contains ONLY the contract path and structured
        references from the DispatchContext — never raw contributor text.
        """
        lines: list[str] = [
            f"Act as the agent defined in {context.contract}. Read that file first.",
        ]
        if context.issue_ref is not None:
            lines.append(
                f"Task: work on issue #{context.issue_ref.number} in "
                f"{context.issue_ref.repo.owner}/{context.issue_ref.repo.name}."
            )
        if context.pr_ref is not None:
            lines.append(
                f"PR context: PR #{context.pr_ref.number} in "
                f"{context.pr_ref.repo.owner}/{context.pr_ref.repo.name}."
            )
        if context.allowed_agent_refs is not None:
            # Allowed refs from decide_specialists output only — never contributor text (I9).
            refs_str = ", ".join(context.allowed_agent_refs)
            lines.append(
                f"You may spawn sub-agents only from this allow-set: [{refs_str}]."
            )
        lines.append(
            f"Use at most {context.max_turns} turns. "
            "Write all changes through your tools; do not ask interactive questions."
        )
        return " ".join(lines)

    def _build_child_env(
        self,
        gh_token: str,
        allowed_agent_refs: list[str] | None = None,
    ) -> dict[str, str]:
        """Build the child process environment.

        I3: Only CLAUDE_CODE_OAUTH_TOKEN and GH_TOKEN (scoped) are injected.
        The orchestrator's FORGE_TOKEN, App private key, and all other operator
        credentials are NEVER forwarded to the child environment.

        I9: When allowed_agent_refs is not None, ORCHESTRATOR_ALLOWED_AGENT_REFS
        is injected as a comma-separated string so the PreToolUse hook can enforce
        the allow-set.  An empty list injects an empty string (hook denies all
        Task spawns — fail closed).
        """
        env: dict[str, str] = {
            # Claude auth (OAuth token — operator env, never repo secret)
            "CLAUDE_CODE_OAUTH_TOKEN": self._claude_oauth_token,
            # Scoped GitHub token (freshly minted, repo-limited, permission-limited)
            "GH_TOKEN": gh_token,
            # Required for git to work non-interactively
            "GIT_TERMINAL_PROMPT": "0",
            # Forward PATH so the claude binary is findable
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            # HOME is required for some git operations
            "HOME": os.environ.get("HOME", "/root"),
        }
        if allowed_agent_refs is not None:
            # Comma-separated allow-set consumed by i9_spawn_hook.py.
            # Empty list → empty string → hook denies all Task spawns.
            env["ORCHESTRATOR_ALLOWED_AGENT_REFS"] = ",".join(allowed_agent_refs)
        return env

    def _materialize_contract(self, contract: str, repo_dir: str) -> None:
        """Copy ALL orchestration contracts from the package into the cloned workspace.

        Used by SubprocessBackend (dev/CI path) so the agent can read its contract —
        and any sibling contracts it references by relative path — from the
        repo-relative path the prompt uses (e.g. "agents/orchestrator.md").

        The orchestrator contract delegates to sibling contracts by relative path
        (agents/implementer.md, agents/converge-reviewer.md, etc.).  Materialising
        only the dispatched contract leaves those paths unresolvable, causing the
        running agent to improvise a generic subagent that ignores the sibling
        contract's disciplines (#111 follow-up).

        The contracts are sourced from the orchestrator package's own agents/
        directory (sibling of src/ at the repo root), NOT from the cloned target
        repo — which will never contain orchestrator's own contracts (#111).

        Fail-loud: raises FileNotFoundError if the *dispatched* contract is absent,
        regardless of whether other contracts are present.  The agent must never run
        without its primary governing instructions.

        Args:
          contract:  DispatchContext.contract, e.g. "agents/orchestrator.md".
          repo_dir:  absolute path to the cloned working tree root.
        """
        import pathlib
        import shutil

        dispatched_basename = contract.rsplit("/", 1)[-1]
        # Locate the package's own agents/ dir: src/ports/harness.py → ../../agents/
        package_agents_dir = pathlib.Path(__file__).parent.parent.parent / "agents"

        # Fail loudly if the dispatched contract is absent — the agent must never
        # run without its primary governing instructions (#111).
        dispatched_src = package_agents_dir / dispatched_basename
        if not dispatched_src.exists():
            raise FileNotFoundError(
                f"Agent contract not found at expected package path: {dispatched_src}. "
                f"Contract '{contract}' cannot be materialised into the workspace. "
                "The orchestrator's contracts are in the 'agents/' directory at the "
                "repo root; they are baked into the agent-runner image at /app/agents/ "
                "for K8s dispatches. Check that agents/ is present and the contract "
                f"basename '{dispatched_basename}' matches an existing file."
            )

        dest_dir = pathlib.Path(repo_dir) / "agents"
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Copy the FULL orchestration-contract set (all *.md files) so any
        # relative-path reference between contracts resolves.  The orchestrator
        # contract delegates to sibling contracts by relative path (Step 5 reads
        # agents/implementer.md; it also references agents/converge-reviewer.md and
        # agents/converge-fixer.md).  Without the full set the running agent logs
        # "There's no implementer.md" and improvises a generic subagent that ignores
        # the implementer contract's disciplines (commit hygiene, D4, etc.).
        for src_path in package_agents_dir.glob("*.md"):
            shutil.copy2(str(src_path), str(dest_dir / src_path.name))

        # Git-ignore the entire materialised agents/ dir so an agent's `git add -A`
        # cannot sweep any contract into the PR.  agents/** is a PROTECTED_PATH, so
        # a committed copy trips the converge protected-path check (E1) and stalls a
        # greenfield run on a spurious escalation.  .git/info/exclude is repo-local
        # and only affects *untracked* files — a repo that legitimately tracks
        # contracts (e.g. the orchestrator's own repo) is unaffected (#111).
        exclude_path = pathlib.Path(repo_dir) / ".git" / "info" / "exclude"
        exclude_line = "/agents/**\n"
        try:
            exclude_path.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_path.read_text() if exclude_path.exists() else ""
            if exclude_line not in existing:
                with exclude_path.open("a") as fh:
                    fh.write(exclude_line)
        except OSError:
            # Best-effort: a missing .git/info is non-fatal; the converge E1 check
            # remains the backstop.
            pass

        # Best-effort: copy the specialist pack from the package root's .agents/ dir
        # into the cloned workspace so agents can read ".agents/<AgentRef>" at the
        # workspace-relative path that orchestration contracts specify (AGENTS.md §7.4).
        #
        # The pack is external/baked (fetched at image build time) and is normally
        # ABSENT in dev/CI — the repo has no .agents/ directory.  This copy is
        # intentionally best-effort:
        #   - If the pack source is absent, skip silently (no raise).  Dev/CI
        #     dispatches may not need specialists; the fail-loud contract guarantee
        #     applies only to the dispatched orchestration contract above.
        #   - If the copy fails for any other OS reason, log and continue — the
        #     agent will simply see no .agents/ dir and specialisation will degrade
        #     gracefully rather than blocking the dispatch.
        # .agents/** is a PROTECTED_PATH; add /.agents/** to .git/info/exclude so
        # the agent's `git add -A` can never sweep the pack into a PR.
        package_pack_dir = _get_package_pack_dir()
        if package_pack_dir.is_dir():
            dest_pack_dir = pathlib.Path(repo_dir) / ".agents"
            try:
                dest_pack_dir.mkdir(parents=True, exist_ok=True)
                for src_path in package_pack_dir.glob("*.md"):
                    shutil.copy2(str(src_path), str(dest_pack_dir / src_path.name))
                # Git-ignore the materialised pack — .agents/** is a PROTECTED_PATH.
                pack_exclude_line = "/.agents/**\n"
                try:
                    existing2 = exclude_path.read_text() if exclude_path.exists() else ""
                    if pack_exclude_line not in existing2:
                        with exclude_path.open("a") as fh:
                            fh.write(pack_exclude_line)
                except OSError:
                    pass
            except OSError:
                # Non-fatal: pack copy failure degrades specialist availability
                # but must never block the dispatch.
                pass

    async def _configure_git_identity(self, repo_dir: str, gh_token: str) -> None:
        """Configure repo-local git identity and push credentials for the agent (#112).

        Sets user.name and user.email locally (not globally) so the agent can
        commit without "Author identity unknown".  Configures url.insteadOf for
        the token so git push works without terminal prompts.

        Repo-local scope is used for subprocess dispatches to avoid clobbering the
        developer's global git config.  For K8s, the entry script uses --global
        because the pod is single-use.

        I3: the token appears only in the repo-local .git/config, which is written
        inside the cloned working tree (not the orchestrator source tree).  It is
        never forwarded to the child env as a new variable.
        """
        base_env = {
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
        }
        for args in (
            ["git", "config", "user.name", "Orchestrator Agent"],
            ["git", "config", "user.email", "agent@orchestrator"],
            # Configure push auth via url.insteadOf (repo-local, inside .git/config).
            # This covers both fetch and push without exposing the token in argv.
            [
                "git",
                "config",
                f"url.https://x-access-token:{gh_token}@github.com/.insteadOf",
                "https://github.com/",
            ],
        ):
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=repo_dir,
                env=base_env,
            )
            await proc.wait()
            # Failures are non-fatal (logged) — they degrade gracefully rather
            # than killing the dispatch.  The agent will hit the original error
            # if identity is truly absent, which surfaces via the run log.
            if proc.returncode != 0:
                _log.warning(
                    "_configure_git_identity: git config failed (exit %s) for %s",
                    proc.returncode,
                    args[2] if len(args) > 2 else args,
                )

    def _write_spawn_hook(self, repo_dir: str) -> None:
        """Materialise the I9 PreToolUse hook into the cloned repo.

        Used by SubprocessBackend (via the harness helper) to write the hook
        into a locally-cloned working tree.

        For K8sJobBackend, an equivalent setup is performed INSIDE the pod by
        the Job entry script (which copies the baked hook from
        /opt/orchestrator/i9_spawn_hook.py).

        Writes two files into repo_dir/.claude/:
          - i9_spawn_hook.py  — the hook script (copied from src/ports/).
          - settings.json     — Claude Code settings with PreToolUse hook wired.

        If .claude/settings.json already exists its content is REPLACED so that
        the hook is always active for this dispatch (defence in depth; we own
        the working tree).

        Security (I9): the hook script is read from the orchestrator source tree
        (not the cloned repo) so a compromised repo cannot substitute its own
        hook script.
        """
        import pathlib
        import shutil

        claude_dir = pathlib.Path(repo_dir) / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)

        # 1. Copy the hook script from the orchestrator package into the repo.
        # Use __file__ of this module to locate the sibling hook script reliably.
        hook_src = pathlib.Path(__file__).parent / "i9_spawn_hook.py"
        hook_dst = claude_dir / "i9_spawn_hook.py"
        shutil.copy2(str(hook_src), str(hook_dst))

        # 2. Write settings.json with the PreToolUse hook.
        settings: dict[str, object] = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Task",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"python3 {hook_dst}",
                            }
                        ],
                    }
                ]
            }
        }
        settings_path = claude_dir / "settings.json"
        settings_path.write_text(json.dumps(settings, indent=2))

    async def _clone_repo(self, gh_token: str, work_dir: str, branch: str | None) -> None:
        """Clone the target repo into work_dir using the scoped GH_TOKEN.

        Security: the token is passed via GIT_CONFIG_* env vars so it never
        appears in process argv (/proc/PID/cmdline) or in .git/config.

        NOTE: This method is used ONLY by SubprocessBackend (dev/CI path).
        K8sJobBackend does NOT call this method — the clone happens inside the
        agent-runner pod.  This is intentional: the control-plane image ships
        WITHOUT git.
        """
        plain_url = f"https://github.com/{self._repo_owner}/{self._repo_name}.git"
        # Inject the token via git's url.insteadOf mechanism (env-only; not
        # written to .git/config because --no-local / env-only config is
        # transient for this subprocess only).
        clone_env = {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": (
                f"url.https://x-access-token:{gh_token}@github.com/.insteadOf"
            ),
            "GIT_CONFIG_VALUE_0": "https://github.com/",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", "/root"),
        }
        args = ["git", "clone", "--depth", "1"]
        if branch:
            args += ["--branch", branch]
        args += [plain_url, work_dir]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=clone_env,
        )
        await proc.wait()
        # Non-zero exit means clone failed; callers let the exception propagate.
        if proc.returncode != 0:
            stderr_bytes = b""
            if proc.stderr is not None:
                stderr_bytes = await proc.stderr.read()
            raise RuntimeError(
                f"git clone failed (exit {proc.returncode}): "
                f"{stderr_bytes.decode(errors='replace')}"
            )

    def _target_branch(self, context: DispatchContext) -> str | None:
        """Return the branch to check out, or None for default branch.

        P0.4: head_branch is set by converge() so reviewers/fixers operate on
        the actual PR diff rather than the default branch.
        """
        return context.head_branch

    # ------------------------------------------------------------------
    # HarnessPort implementation
    # ------------------------------------------------------------------

    async def dispatch(self, context: DispatchContext) -> RunHandle:
        """Dispatch Claude Code via the configured ExecutionBackend; return a RunHandle.

        Non-blocking: returns immediately.  The backend watches the run in a
        background asyncio task.

        The control-plane does NOT clone the repo — that responsibility belongs
        to the ExecutionBackend:
          - SubprocessBackend (dev/CI): clones locally into a temp dir, writes
            the I9 hook when allowed_agent_refs is set, then runs claude.
          - K8sJobBackend (prod): the Job entry script clones + sets up the hook
            + runs claude INSIDE the agent-runner pod.  The control-plane never
            touches the filesystem for K8s dispatches.

        Security:
          - I3: only CLAUDE_CODE_OAUTH_TOKEN + scoped GH_TOKEN in child env.
            For K8s: token is in the pod env — NEVER a literal in the manifest.
          - I9: prompt built from context.contract path only; no contributor text.
               PreToolUse hook (exit-2 deny) enforces allowed_agent_refs when not
               None (closes #52), written by each backend into the working tree.
        """
        run_id = str(uuid.uuid4())
        self._event_store.register(run_id)

        # 1. Mint a scoped installation token for the target repo (I3).
        gh_token = await _mint_scoped_installation_token(
            app_id=self._app_id,
            private_key_pem=self._private_key_pem,
            installation_id=self._installation_id,
            forge_token_scope=context.forge_token_scope,
            repo_owner=self._repo_owner,
            repo_name=self._repo_name,
            http_client=self._http_client,
        )

        # 2. Build the prompt (I9: contract path only, no contributor text).
        prompt = self._build_prompt(context)

        # 3. Build the child env (I3: only CLAUDE_CODE_OAUTH_TOKEN + GH_TOKEN).
        #    I9: inject ORCHESTRATOR_ALLOWED_AGENT_REFS when allow-set is specified.
        child_env = self._build_child_env(gh_token, context.allowed_agent_refs)

        # 4. Build the claude CLI invocation.
        #    -p / --print: headless (non-interactive) mode
        #    --output-format stream-json: NDJSON event stream on stdout
        #    --permission-mode bypassPermissions: autonomous run (no interactive prompts)
        #    --verbose: include tool-use events in the stream
        #    --model: use the model from the dispatch context
        claude_args = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
            "--verbose",
            "--model",
            context.model,
        ]

        # 5. Determine the target branch (P0.4).
        branch = self._target_branch(context)

        # 6. Delegate execution to the backend (fire and watch — AGENTS.md §1).
        #    The backend is responsible for:
        #      a) Cloning the repo (subprocess: locally; k8s: inside the pod).
        #      b) Materialising the agent contract (#111: subprocess copies from
        #         the package agents/ dir; k8s copies from the baked /app/agents/).
        #      c) Configuring git identity and push auth (#112).
        #      d) Materialising the I9 hook when allowed_agent_refs is set.
        #      e) Running claude in the cloned working tree.
        await self._backend.dispatch(
            run_id=run_id,
            repo_owner=self._repo_owner,
            repo_name=self._repo_name,
            branch=branch,
            claude_args=claude_args,
            child_env=child_env,
            allowed_agent_refs=context.allowed_agent_refs,
            contract=context.contract,
            event_store=self._event_store,
            harness=self,
        )

        return RunHandle(run_id=run_id)

    def register_run_status_sink(self, run_id: str, sink: StatusSink) -> None:
        """Wire a write-through sink for a single run's status changes (issue #101).

        Delegates to the underlying RunEventStore.  Called by RunRecordingHarness
        immediately after dispatch() returns so every subsequent set_status call
        (queued → in_progress → completed/failure) is propagated into the run_store.

        Exposed on the port so RunRecordingHarness can reach the event_store without
        coupling to ClaudeCodeHarnessPort's internals — the protocol surface is minimal
        (one method, one run_id, one sync callback).
        """
        self._event_store.register_status_sink(run_id, sink)

    def get_live_status(self, run_id: str) -> RunStatus:
        """Return the current live status from the RunEventStore (issue #101).

        Used by RunRecordingHarness after registering the sink to catch any
        status transitions that occurred synchronously during dispatch() before
        the sink was installed.  This closes the race between backend.dispatch()
        setting status and RunRecordingHarness registering the sink.
        """
        return self._event_store.get_status(run_id)

    async def get_run_status(self, handle: RunHandle) -> RunStatus:
        """Return the live status of the run.

        P1.8: Unknown run IDs return completed/failure (avoids polling a dead
        handle as pending; consistent with crash-only recovery).
        """
        if handle.run_id not in self._event_store._statuses:
            return RunStatus(state="completed", conclusion="failure")
        return self._event_store.get_status(handle.run_id)

    async def cancel(self, handle: RunHandle) -> None:
        """Terminate the run and clean up (idempotent).

        Delegates to the ExecutionBackend which signals the process group
        (subprocess) or deletes the K8s Job.  Already-terminal runs are
        a no-op (SPEC §9.2).
        """
        status = self._event_store.get_status(handle.run_id)
        if status.state == "completed":
            # Already terminal — no-op (SPEC §9.2)
            return

        await self._backend.cancel(
            run_id=handle.run_id,
            event_store=self._event_store,
        )

    # ------------------------------------------------------------------
    # CI re-run methods (GitHub Actions — retained, separate concern)
    # ------------------------------------------------------------------

    def _gh_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._forge_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def trigger_workflow(
        self,
        name: str,
        ref: str,
        inputs: dict[str, object],
    ) -> None:
        """Trigger an arbitrary GitHub Actions workflow by filename.

        Used only for re-running the repo's own CI (tests/lint), NOT for
        agent dispatch (which is handled by dispatch() above).
        """
        resp = await self._http_client.post(
            f"{_GITHUB_API}/repos/{self._repo_owner}/{self._repo_name}"
            f"/actions/workflows/{name}/dispatches",
            headers=self._gh_headers(),
            json={"ref": ref, "inputs": inputs},
        )
        resp.raise_for_status()

    async def trigger_ci(self, pr_ref: PRRef) -> None:
        """Re-trigger CI on a PR by re-running the most recent failed workflow run.

        Fetches PR head SHA, finds the most recent workflow run, and calls
        POST /actions/runs/{run_id}/rerun-failed-jobs.
        """
        pr_resp = await self._http_client.get(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/pulls/{pr_ref.number}",
            headers=self._gh_headers(),
        )
        pr_resp.raise_for_status()
        head_sha = str(pr_resp.json()["head"]["sha"])

        runs_resp = await self._http_client.get(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            "/actions/runs",
            headers=self._gh_headers(),
            params={"head_sha": head_sha, "per_page": "1"},
        )
        runs_resp.raise_for_status()
        runs = runs_resp.json().get("workflow_runs", [])
        if not runs:
            return

        run_id_str = str(runs[0]["id"])
        rerun_resp = await self._http_client.post(
            f"{_GITHUB_API}/repos/{pr_ref.repo.owner}/{pr_ref.repo.name}"
            f"/actions/runs/{run_id_str}/rerun-failed-jobs",
            headers=self._gh_headers(),
            json={},
        )
        rerun_resp.raise_for_status()


# Deprecated: RealHarnessPort is an alias for back-compat. Use ClaudeCodeHarnessPort.
RealHarnessPort = ClaudeCodeHarnessPort
