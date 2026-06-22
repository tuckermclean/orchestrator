"""ClaudeCodeHarnessPort — Claude Code subprocess HarnessPort implementation.

The harness spawns `claude` (Claude Code CLI) as a supervised async child process.
Each dispatch:
  1. Mints a scoped GitHub App installation token (forge_token_scope determines perms).
  2. Clones the target repo into a temp dir with that token.
  3. Materialises a PreToolUse hook (I9 spawn allow-set gate) into repo_dir/.claude/
     when DispatchContext.allowed_agent_refs is not None.
  4. Spawns `claude -p "<prompt>" --output-format stream-json --permission-mode
     bypassPermissions --verbose` in the working tree.
  5. Streams JSON events from stdout → RunEventStore (queryable; Step 9 SSE).
  6. Returns a RunHandle immediately (PID + run_id); process is watched in background.

I3 invariants:
  - Only CLAUDE_CODE_OAUTH_TOKEN and a scoped GH_TOKEN reach the child env.
  - Orchestrator master creds (App private key, FORGE_TOKEN) are NEVER forwarded.
  - No contributor text in env or subprocess args (I9).

I9 spawn enforcement (SECURITY.md §3 I9, closes #52):
  When DispatchContext.allowed_agent_refs is a list (not None), the harness:
    - Writes src/ports/i9_spawn_hook.py into the cloned repo at
      .claude/i9_spawn_hook.py.
    - Writes .claude/settings.json with a PreToolUse hook on the Task tool
      that invokes the hook script via python3.
    - Injects ORCHESTRATOR_ALLOWED_AGENT_REFS into the child env (comma-
      separated AgentRef strings).
  The hook fails closed: missing env var, empty allow-set, or unparseable
  stdin all result in DENY.  When allowed_agent_refs is None, no hook is
  written (no harness-level restriction).

trigger_workflow / trigger_ci remain for re-running the repo's own CI via GitHub
Actions.  All agent-dispatch-via-workflow_dispatch code is removed.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import signal
import tempfile
import time
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
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

class RunEventStore:
    """Per-run event accumulator.  Thread-safe (asyncio single-threaded).

    Holds events in memory.  Step 9 (issue #31) will expose these via SSE;
    for #45 they are captured and queryable here.
    """

    def __init__(self) -> None:
        self._events: dict[str, list[RunEvent]] = {}
        self._statuses: dict[str, RunStatus] = {}
        # Per-run asyncio.Queue for live streaming; None signals completion.
        self._queues: dict[str, asyncio.Queue[RunEvent | None]] = {}

    def register(self, run_id: str) -> None:
        """Initialise storage for a new run."""
        self._events[run_id] = []
        self._statuses[run_id] = RunStatus(state="queued")
        self._queues[run_id] = asyncio.Queue()

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
    """HarnessPort that dispatches by spawning Claude Code as a supervised subprocess.

    dispatch(context):
      - Mints a scoped GH installation token for the target repo.
      - Clones the repo into a per-dispatch temp dir.
      - Spawns `claude -p <prompt> --output-format stream-json
          --permission-mode bypassPermissions --verbose` in the working tree.
      - Streams JSON events from stdout into the RunEventStore.
      - Returns a RunHandle immediately (non-blocking).

    get_run_status / cancel reflect live process state.

    Security (I3 / I9):
      - Child env contains ONLY CLAUDE_CODE_OAUTH_TOKEN + GH_TOKEN (scoped).
      - Operator master creds (App key, FORGE_TOKEN) never reach the child.
      - No contributor text in args or env.

    I9 spawn enforcement (#52 closed): when allowed_agent_refs is a list, the
    harness materialises a PreToolUse hook (i9_spawn_hook.py) into the cloned
    repo's .claude/ dir and injects ORCHESTRATOR_ALLOWED_AGENT_REFS into the
    child env.  The hook fails closed (deny on missing env var / empty set /
    parse error).  Prompt-level instruction is retained as defence in depth.
    Blast radius is bounded by the single-repo scoped token from
    _mint_scoped_installation_token.

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
    ) -> None:
        self._claude_oauth_token = claude_oauth_token
        self._app_id = app_id
        self._private_key_pem = private_key_pem
        self._installation_id = installation_id
        self._repo_owner = repo_owner
        self._repo_name = repo_name
        self._event_store = event_store or RunEventStore()
        self._process_runner = process_runner or _default_process_runner
        self._http_client = http_client or httpx.AsyncClient(timeout=30.0)
        self._forge_token = forge_token  # used only for trigger_ci / trigger_workflow

        # Active process map: run_id → (ProcessResult, temp_dir)
        self._processes: dict[str, tuple[ProcessResult, str]] = {}
        # Background watcher tasks — kept so cancel() can cancel them and to
        # prevent garbage collection before the task completes.
        self._background_tasks: set[asyncio.Task[None]] = set()

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

    def _write_spawn_hook(self, repo_dir: str) -> None:
        """Materialise the I9 PreToolUse hook into the cloned repo.

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

    async def _watch_process(
        self,
        run_id: str,
        process: ProcessResult,
        work_dir: str,
    ) -> None:
        """Background task: stream JSON events from stdout, update run status on exit."""
        self._event_store.set_status(run_id, RunStatus(state="in_progress"))

        if process.stdout is not None:
            async for raw_line in process.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    event_type = str(data.get("type", "stream"))
                    event = RunEvent(
                        event_type=event_type,
                        data={k: v for k, v in data.items() if k != "type"},
                        timestamp=datetime.now(tz=UTC),
                    )
                    self._event_store.append(run_id, event)
                except Exception:
                    # Non-JSON line (e.g. stderr bleed) — record as raw text event
                    event = RunEvent(
                        event_type="raw",
                        data={"line": line},
                        timestamp=datetime.now(tz=UTC),
                    )
                    self._event_store.append(run_id, event)

        exit_code = await process.wait()
        run_conclusion = "success" if exit_code == 0 else "failure"
        self._event_store.set_status(
            run_id,
            RunStatus(
                state="completed",
                conclusion="success" if run_conclusion == "success" else "failure",
            ),
        )

        # Clean up the working tree
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

        # Remove from active map
        self._processes.pop(run_id, None)

    # ------------------------------------------------------------------
    # HarnessPort implementation
    # ------------------------------------------------------------------

    async def dispatch(self, context: DispatchContext) -> RunHandle:
        """Spawn Claude Code as a supervised child process and return a RunHandle.

        Non-blocking: returns immediately after spawning.  The child is watched
        in a background asyncio task.

        Security:
          - I3: only CLAUDE_CODE_OAUTH_TOKEN + scoped GH_TOKEN in child env.
          - I9: prompt built from context.contract path only; no contributor text.
               PreToolUse hook enforces allowed_agent_refs when not None (closes #52).
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

        # 2. Clone the repo into a fresh temp dir (branch from context).
        work_dir = tempfile.mkdtemp(prefix=f"orch-run-{run_id[:8]}-")
        repo_dir = os.path.join(work_dir, "repo")
        os.makedirs(repo_dir, exist_ok=True)

        branch = self._target_branch(context)
        try:
            await self._clone_repo(gh_token, repo_dir, branch)
        except Exception as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            self._event_store.set_status(
                run_id,
                RunStatus(state="completed", conclusion="failure"),
            )
            event = RunEvent(
                event_type="error",
                data={"message": f"Clone failed: {exc}"},
                timestamp=datetime.now(tz=UTC),
            )
            self._event_store.append(run_id, event)
            return RunHandle(run_id=run_id)

        # 3. Materialise the I9 PreToolUse hook when an allow-set is specified.
        #    The hook is the technical control; prompt instruction is defence in depth.
        if context.allowed_agent_refs is not None:
            self._write_spawn_hook(repo_dir)

        # 4. Build the prompt (I9: contract path only, no contributor text).
        prompt = self._build_prompt(context)

        # 5. Build the child env (I3: only CLAUDE_CODE_OAUTH_TOKEN + GH_TOKEN).
        #    I9: inject ORCHESTRATOR_ALLOWED_AGENT_REFS when allow-set is specified.
        child_env = self._build_child_env(gh_token, context.allowed_agent_refs)

        # 6. Build the claude CLI invocation.
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

        # 7. Spawn the subprocess via the injectable runner.
        process = await self._process_runner(claude_args, repo_dir, child_env)

        # 8. Track the process.
        self._processes[run_id] = (process, work_dir)

        # 9. Watch in background — "fire and watch" (AGENTS.md §1).
        watcher = asyncio.create_task(
            self._watch_process(run_id, process, work_dir),
            name=f"harness-watch-{run_id[:8]}",
        )
        self._background_tasks.add(watcher)
        watcher.add_done_callback(self._background_tasks.discard)

        return RunHandle(run_id=run_id)

    async def get_run_status(self, handle: RunHandle) -> RunStatus:
        """Return the live status of the run.

        P1.8: Unknown run IDs return completed/failure (avoids polling a dead
        handle as pending; consistent with crash-only recovery).
        """
        if handle.run_id not in self._event_store._statuses:
            return RunStatus(state="completed", conclusion="failure")
        return self._event_store.get_status(handle.run_id)

    async def cancel(self, handle: RunHandle) -> None:
        """Terminate the child process group and clean up (idempotent)."""
        status = self._event_store.get_status(handle.run_id)
        if status.state == "completed":
            # Already terminal — no-op (SPEC §9.2)
            return

        # Cancel any outstanding background watcher task for this run.
        for task in list(self._background_tasks):
            if handle.run_id in (task.get_name() or ""):
                task.cancel()

        entry = self._processes.get(handle.run_id)
        if entry is not None:
            process, work_dir = entry
            await process.terminate()
            # Give the process group a moment to exit cleanly, then force-kill.
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                await process.kill()
            shutil.rmtree(work_dir, ignore_errors=True)
            self._processes.pop(handle.run_id, None)

        self._event_store.set_status(
            handle.run_id,
            RunStatus(state="completed", conclusion="cancelled"),
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
