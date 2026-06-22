"""ExecutionBackend — seam between HarnessPort and the underlying execution substrate.

An ExecutionBackend abstracts "prepare a working tree and spawn a claude agent
dispatch, then surface its run state."  Each backend owns its own working-tree
preparation so the substrate (local subprocess vs Kubernetes Job) can be swapped
without touching the HarnessPort contract.

Two concrete implementations:
  SubprocessBackend — spawns the claude CLI as a local child process.  Dev/default.
                      Owns the local clone (git is on the dev/CI host).
  K8sJobBackend     — schedules a Kubernetes Job per dispatch.  Prod isolation.
                      The Job entry script does clone + I9 hook + claude INSIDE
                      the agent-runner pod (git + claude + python3 are in that image).

Backend selection:
  subprocess  — always, unless HARNESS_EXECUTION_BACKEND=k8s is set.
  k8s         — when HARNESS_EXECUTION_BACKEND=k8s (or detect in-cluster config).

Why each backend owns the clone (production architecture fix):
  The control-plane image ships WITHOUT git by design (Dockerfile comment).  The
  K8s agent-runner image HAS git + claude + python3.  Cloning in the control-plane
  would fail at runtime for k8s mode, and even if it succeeded the cloned tree
  would be on a DIFFERENT pod's filesystem — the Job pod never sees it.  The fix
  moves all working-tree preparation into the backend.

I3 invariant:
  Only CLAUDE_CODE_OAUTH_TOKEN and a scoped GH_TOKEN enter the execution
  environment.  Master credentials (App private key, FORGE_TOKEN) are NEVER
  forwarded to a child process or a K8s Job pod.
  For K8s: GH_TOKEN is in the pod env at runtime; the token NEVER appears as
  a literal string in the Job manifest or in argv.

I9 invariant (spawn hook — each backend installs it):
  SubprocessBackend: calls harness._write_spawn_hook() on the locally-cloned tree;
    ORCHESTRATOR_ALLOWED_AGENT_REFS is in child_env.
  K8sJobBackend: the agent-runner image bakes i9_spawn_hook.py at
    /opt/orchestrator/i9_spawn_hook.py (deploy/agent-runner.Dockerfile COPY).
    The Job entry script copies the hook and writes .claude/settings.json
    INSIDE the pod when ORCHESTRATOR_ALLOWED_AGENT_REFS is set in the pod env.
  The hook denies via EXIT CODE 2 (Claude Code's PreToolUse blocking contract —
  exit 1 does NOT block).

Kubernetes dependency:
  The kubernetes-client package is imported lazily inside K8sJobBackend only.
  The default subprocess path carries zero hard dependency on the kube SDK.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from src.domain.types import RunEvent, RunStatus
from src.ports.harness import (
    ProcessResult,
    ProcessRunner,
    RunEventStore,
    _default_process_runner,
)

# ---------------------------------------------------------------------------
# Protocol — the seam
# ---------------------------------------------------------------------------


class ExecutionBackend(Protocol):
    """Abstraction over "prepare a working tree, spawn a claude dispatch, surface run state."

    Implementors must be safe to call concurrently from multiple asyncio tasks.
    dispatch() must return immediately without blocking the event loop.

    Each backend is responsible for:
      a) Cloning the target repo (subprocess: locally; k8s: inside the pod).
      b) Materialising the I9 spawn-allow-set hook when allowed_agent_refs is set.
      c) Running the claude invocation in the cloned working tree.
    """

    async def dispatch(
        self,
        *,
        run_id: str,
        repo_owner: str,
        repo_name: str,
        branch: str | None,
        claude_args: list[str],
        child_env: dict[str, str],
        allowed_agent_refs: list[str] | None,
        contract: str,
        event_store: RunEventStore,
        harness: Any,
    ) -> None:
        """Start execution and record run state into event_store.

        Fires and forgets — returns immediately after the job/process is
        scheduled.  Progress is tracked via the RunEventStore.

        Parameters:
          run_id:             unique identifier for this run.
          repo_owner:         GitHub owner of the target repo.
          repo_name:          GitHub repo name.
          branch:             branch to check out, or None for the default.
          claude_args:        the full claude CLI invocation to run.
          child_env:          environment for the child (I3: only scoped creds).
          allowed_agent_refs: allow-set for I9 spawn gate, or None to skip.
          contract:           DispatchContext.contract path (e.g. "agents/orchestrator.md").
                              Passed to the backend so it can materialise the contract
                              into the workspace before running the agent (#111).
          event_store:        receives run events and status updates.
          harness:            ClaudeCodeHarnessPort instance (provides _clone_repo,
                              _write_spawn_hook, and _materialize_contract helpers
                              for SubprocessBackend).
        """
        ...

    async def cancel(
        self,
        *,
        run_id: str,
        event_store: RunEventStore,
    ) -> None:
        """Terminate an in-flight run.  Idempotent — no-op on completed runs."""
        ...


# ---------------------------------------------------------------------------
# FakeExecutionBackend — for unit testing backends (not the harness itself)
# ---------------------------------------------------------------------------


class FakeExecutionBackend:
    """Controllable ExecutionBackend double for unit-testing callers.

    Tracks dispatch/cancel calls.  By default dispatch() completes immediately
    with success.  Use configure() to inject failures.
    """

    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []
        self.cancelled: list[str] = []
        self._fail_dispatch: bool = False

    def configure(self, *, fail_dispatch: bool = False) -> None:
        self._fail_dispatch = fail_dispatch

    async def dispatch(
        self,
        *,
        run_id: str,
        repo_owner: str,
        repo_name: str,
        branch: str | None,
        claude_args: list[str],
        child_env: dict[str, str],
        allowed_agent_refs: list[str] | None,
        contract: str,
        event_store: RunEventStore,
        harness: Any,
    ) -> None:
        self.dispatched.append(
            {
                "run_id": run_id,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "branch": branch,
                "claude_args": claude_args,
                "child_env": child_env,
                "allowed_agent_refs": allowed_agent_refs,
                "contract": contract,
            }
        )
        if self._fail_dispatch:
            event_store.set_status(run_id, RunStatus(state="completed", conclusion="failure"))
            return
        event_store.set_status(run_id, RunStatus(state="completed", conclusion="success"))

    async def cancel(
        self,
        *,
        run_id: str,
        event_store: RunEventStore,
    ) -> None:
        self.cancelled.append(run_id)
        event_store.set_status(run_id, RunStatus(state="completed", conclusion="cancelled"))


# ---------------------------------------------------------------------------
# SubprocessBackend — dev / default
# ---------------------------------------------------------------------------


class SubprocessBackend:
    """Execute a claude dispatch as a supervised async child process.

    This is the dev/default backend.  It OWNS the working-tree preparation:
      1. Clones the target repo into a fresh temp dir (via harness._clone_repo).
      2. Materialises the I9 hook (via harness._write_spawn_hook) when
         allowed_agent_refs is not None.
      3. Spawns claude in the cloned tree and watches stdout → RunEventStore.

    On clone failure the run is immediately marked completed/failure without
    spawning any subprocess.

    Injectable process_runner seam allows unit tests to drive a fake subprocess
    without spawning a real one.
    """

    def __init__(
        self,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self._process_runner = process_runner or _default_process_runner
        # Active process map: run_id → (ProcessResult, work_dir)
        self._processes: dict[str, tuple[ProcessResult, str]] = {}
        # Background watcher tasks — kept to prevent GC before completion.
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def dispatch(
        self,
        *,
        run_id: str,
        repo_owner: str,
        repo_name: str,
        branch: str | None,
        claude_args: list[str],
        child_env: dict[str, str],
        allowed_agent_refs: list[str] | None,
        contract: str,
        event_store: RunEventStore,
        harness: Any,
    ) -> None:
        """Clone repo, materialise contract + git identity, write I9 hook, spawn claude."""
        # 1. Clone the repo into a fresh temp dir.
        work_dir = tempfile.mkdtemp(prefix=f"orch-run-{run_id[:8]}-")
        repo_dir = os.path.join(work_dir, "repo")
        os.makedirs(repo_dir, exist_ok=True)

        gh_token = child_env.get("GH_TOKEN", "")
        try:
            await harness._clone_repo(gh_token, repo_dir, branch)
        except Exception as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            event_store.set_status(
                run_id,
                RunStatus(state="completed", conclusion="failure"),
            )
            event_store.append(
                run_id,
                RunEvent(
                    event_type="error",
                    data={"message": f"Clone failed: {exc}"},
                    timestamp=datetime.now(tz=UTC),
                ),
            )
            return

        # 2. Materialise the agent contract into the clone (#111).
        #    The contract path (e.g. "agents/orchestrator.md") is repo-relative.
        #    Copy the file from the orchestrator package's own agents/ dir into
        #    the cloned workspace so the agent can read it at the expected path.
        if contract:
            try:
                harness._materialize_contract(contract, repo_dir)
            except Exception as exc:
                shutil.rmtree(work_dir, ignore_errors=True)
                event_store.set_status(
                    run_id,
                    RunStatus(state="completed", conclusion="failure"),
                )
                event_store.append(
                    run_id,
                    RunEvent(
                        event_type="error",
                        data={"message": f"Contract materialisation failed: {exc}"},
                        timestamp=datetime.now(tz=UTC),
                    ),
                )
                return

        # 3. Configure repo-local git identity and push credentials (#112).
        #    Repo-local scope avoids clobbering the developer's global git config.
        await harness._configure_git_identity(repo_dir, gh_token)

        # 4. Materialise the I9 PreToolUse hook when an allow-set is specified.
        #    The hook denies via EXIT CODE 2 — not 1 — per the Claude Code contract.
        if allowed_agent_refs is not None:
            harness._write_spawn_hook(repo_dir)

        # 5. Spawn claude in the cloned working tree.
        process = await self._process_runner(claude_args, repo_dir, child_env)
        self._processes[run_id] = (process, work_dir)

        watcher = asyncio.create_task(
            self._watch(run_id, process, work_dir, event_store),
            name=f"subprocess-watch-{run_id[:8]}",
        )
        self._background_tasks.add(watcher)
        watcher.add_done_callback(self._background_tasks.discard)

    async def _watch(
        self,
        run_id: str,
        process: ProcessResult,
        work_dir: str,
        event_store: RunEventStore,
    ) -> None:
        """Background: stream JSON events from stdout and update status on exit."""
        event_store.set_status(run_id, RunStatus(state="in_progress"))

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
                    event_store.append(run_id, event)
                except Exception:
                    event_store.append(
                        run_id,
                        RunEvent(
                            event_type="raw",
                            data={"line": line},
                            timestamp=datetime.now(tz=UTC),
                        ),
                    )

        exit_code = await process.wait()
        event_store.set_status(
            run_id,
            RunStatus(
                state="completed",
                conclusion="success" if exit_code == 0 else "failure",
            ),
        )

        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

        self._processes.pop(run_id, None)

    async def cancel(
        self,
        *,
        run_id: str,
        event_store: RunEventStore,
    ) -> None:
        """Terminate the child process group (idempotent)."""
        for task in list(self._background_tasks):
            if run_id in (task.get_name() or ""):
                task.cancel()

        entry = self._processes.get(run_id)
        if entry is not None:
            process, work_dir = entry
            await process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except TimeoutError:
                await process.kill()
            shutil.rmtree(work_dir, ignore_errors=True)
            self._processes.pop(run_id, None)

        event_store.set_status(
            run_id,
            RunStatus(state="completed", conclusion="cancelled"),
        )


# ---------------------------------------------------------------------------
# KubeClientPort — thin seam over kubernetes-client; injectable for testing
# ---------------------------------------------------------------------------


class KubeClientPort(Protocol):
    """Minimal Kubernetes API surface needed by K8sJobBackend.

    A real implementation wraps the official kubernetes-client.  Tests inject
    a FakeKubeClient without importing the kube SDK.
    """

    def create_namespaced_job(self, namespace: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create a Job in the given namespace; return the created object as a dict."""
        ...

    def read_namespaced_job(self, name: str, namespace: str) -> dict[str, Any]:
        """Return the Job object (status.conditions, status.succeeded, …)."""
        ...

    def delete_namespaced_job(
        self,
        name: str,
        namespace: str,
        *,
        propagation_policy: str = "Foreground",
    ) -> None:
        """Delete the Job (and its pods when propagation_policy='Foreground')."""
        ...


# ---------------------------------------------------------------------------
# FakeKubeClient — injectable double for K8sJobBackend unit tests
# ---------------------------------------------------------------------------


class FakeKubeClient:
    """Controllable fake Kubernetes client for K8sJobBackend unit tests.

    Records all API calls and allows the test to drive job outcomes via
    configure_job_outcome().
    """

    def __init__(self) -> None:
        self.created_jobs: list[dict[str, Any]] = []
        self.deleted_jobs: list[str] = []
        self.read_calls: list[str] = []
        # Map job_name → sequence of status dicts (popped on each read_namespaced_job call)
        self._job_status_sequence: dict[str, list[dict[str, Any]]] = {}

    def configure_job_outcome(
        self,
        job_name: str,
        *,
        statuses: list[dict[str, Any]],
    ) -> None:
        """Set the sequence of status dicts that read_namespaced_job will return.

        Each call pops the next status.  The last status is repeated if exhausted.
        """
        self._job_status_sequence[job_name] = list(statuses)

    def create_namespaced_job(self, namespace: str, body: dict[str, Any]) -> dict[str, Any]:
        self.created_jobs.append({"namespace": namespace, "body": body})
        name: str = body["metadata"]["name"]
        return {"metadata": {"name": name}, "status": {}}

    def read_namespaced_job(self, name: str, namespace: str) -> dict[str, Any]:
        self.read_calls.append(name)
        sequence = self._job_status_sequence.get(name, [])
        if not sequence:
            return {"metadata": {"name": name}, "status": {}}
        if len(sequence) == 1:
            return sequence[0]
        return sequence.pop(0)

    def delete_namespaced_job(
        self,
        name: str,
        namespace: str,
        *,
        propagation_policy: str = "Foreground",
    ) -> None:
        self.deleted_jobs.append(name)


# ---------------------------------------------------------------------------
# K8sJobBackend — prod isolation
# ---------------------------------------------------------------------------

# Baked path for the I9 hook script in the agent-runner image.
# This MUST match the COPY destination in deploy/agent-runner.Dockerfile.
_BAKED_HOOK_PATH = "/opt/orchestrator/i9_spawn_hook.py"

# Baked directory for the orchestration agent contracts in the agent-runner image.
# The Dockerfile does: COPY agents/ /app/agents/
# This MUST match that COPY destination (deploy/agent-runner.Dockerfile).
# Used by _build_entry_script to materialise the contract into the clone.
_BAKED_CONTRACT_DIR = "/app/agents"

# Default poll interval and timeout (seconds) — module-level so tests can override.
_K8S_POLL_INTERVAL_S: float = 5.0
_K8S_JOB_TIMEOUT_S: float = 1800.0  # 30 minutes


class K8sJobBackend:
    """Execute a claude dispatch as a Kubernetes Job (prod isolation backend).

    This backend OWNS working-tree preparation — it builds a shell script that
    runs INSIDE the agent-runner pod to:
      a) Clone the repo using the GH_TOKEN env var (I3: token in env, not in
         the manifest/argv).
      b) Materialise the I9 spawn-allow-set hook when ORCHESTRATOR_ALLOWED_AGENT_REFS
         is set in the pod env (copies the baked hook from _BAKED_HOOK_PATH and
         writes .claude/settings.json).
      c) cd into the cloned tree and exec the claude invocation.

    The control-plane never clones the repo for K8s dispatches — this is the
    correct behaviour because the control-plane image ships without git.

    I3 token handling:
      GH_TOKEN is in the pod env (child_env passed from harness).  The clone
      command authenticates via the runtime env variable:
        git -c "url.https://x-access-token:${GH_TOKEN}@github.com/.insteadOf=..."
              clone ...
      The token appears ONLY in the pod env at runtime — NEVER as a literal in
      the Job manifest.  This is validated by test_k8s_i3_token_not_literal_in_manifest.

    I9 hook in-pod setup:
      The agent-runner image bakes i9_spawn_hook.py at _BAKED_HOOK_PATH
      (deploy/agent-runner.Dockerfile: COPY src/ports/i9_spawn_hook.py /opt/orchestrator/).
      The entry script:
        1. Creates /workspace/repo/.claude/
        2. Copies _BAKED_HOOK_PATH → /workspace/repo/.claude/i9_spawn_hook.py
        3. Writes /workspace/repo/.claude/settings.json with the PreToolUse hook wired.
      This runs only when ORCHESTRATOR_ALLOWED_AGENT_REFS is set in the pod env.

    Per-dispatch behaviour:
      1. Build the entry shell script (clone + hook setup + claude run).
      2. Build a Job spec with ONE container using ["sh", "-c", "<script>"].
      3. Inject ONLY child_env into the pod (I3: master creds absent).
      4. Create the Job via the kube client.
      5. Poll the Job status in a background asyncio task; record events into
         RunEventStore.
      6. Delete the Job on completion or cancel.

    Kubernetes dependency is imported lazily:
      `from kubernetes import client as k8s_client, config as k8s_config`
      The import happens only inside _make_real_kube_client(), which is only
      called when K8sJobBackend is instantiated with kube_client=None (production).
      The subprocess path and all unit tests run without the kubernetes package.

    Unit-testability:
      Pass a FakeKubeClient as kube_client to test Job-spec construction,
      poll loop, failure, and timeout without a live cluster.

    Cluster-gated tests:
      Tests that need a real cluster are marked @pytest.mark.integration_real
      and skip when KUBECONFIG / in-cluster credentials are absent.
    """

    def __init__(
        self,
        *,
        image: str = "ghcr.io/tuckermclean/orchestrator-agent-runner:latest",
        namespace: str = "default",
        kube_client: KubeClientPort | None = None,
        poll_interval_s: float = _K8S_POLL_INTERVAL_S,
        job_timeout_s: float = _K8S_JOB_TIMEOUT_S,
        service_account: str = "orchestrator",
    ) -> None:
        self._image = image
        self._namespace = namespace
        self._kube_client: KubeClientPort = (
            kube_client if kube_client is not None else _make_real_kube_client()
        )
        self._poll_interval_s = poll_interval_s
        self._job_timeout_s = job_timeout_s
        self._service_account = service_account
        # Background watcher tasks
        self._background_tasks: set[asyncio.Task[None]] = set()
        # run_id → job_name mapping for cancel()
        self._job_names: dict[str, str] = {}

    def _build_entry_script(
        self,
        repo_owner: str,
        repo_name: str,
        branch: str | None,
        claude_args: list[str],
        contract: str = "",
    ) -> str:
        """Build the shell script that runs inside the agent-runner pod.

        The script:
          1. Clones the repo using the GH_TOKEN env var (I3: token in env,
             not in argv or manifest).
          2. Configures git identity (user.name / user.email) globally so
             the agent can commit without "Author identity unknown" (#112).
          3. Configures push authentication via url.insteadOf using ${GH_TOKEN}
             so git push succeeds without terminal prompts (#112).
          4. Materialises the orchestration agent contract from the baked
             path (_BAKED_CONTRACT_DIR/<basename>) into /workspace/repo/agents/
             so the agent can read it at its expected relative path (#111).
             Fails loudly with a clear error if the contract file is absent.
          5. When ORCHESTRATOR_ALLOWED_AGENT_REFS is set, installs the I9
             hook (baked at _BAKED_HOOK_PATH) into /workspace/repo/.claude/.
          6. cd into /workspace/repo and exec the claude invocation.

        I3 security: GH_TOKEN is referenced as ${GH_TOKEN} in the shell
        command — it is NEVER interpolated as a literal string here.  The
        token value reaches the script only via the pod env at runtime.

        Shell quoting: claude_args elements are individually shell-quoted via
        shlex.quote so argument injection from model or prompt is impossible.

        contract: the DispatchContext.contract value (e.g. "agents/orchestrator.md").
          When non-empty, the basename is used to copy the baked contract file from
          _BAKED_CONTRACT_DIR into the cloned workspace so the agent can read it.
        """
        import shlex

        clone_url = f"https://github.com/{repo_owner}/{repo_name}.git"

        # Build git clone command — GH_TOKEN via -c url.insteadOf (env, not argv).
        # The ${GH_TOKEN} shell variable is expanded at pod runtime, not here.
        branch_flag = ""
        if branch:
            # branch is context.head_branch — validated as a safe ref by DispatchContext
            branch_flag = f" --branch {shlex.quote(branch)}"

        # Shell-quote each claude argument to prevent word-splitting/injection.
        quoted_claude = " ".join(shlex.quote(a) for a in claude_args)

        hook_src = _BAKED_HOOK_PATH

        # GH_TOKEN injected via git's url.insteadOf mechanism so it never appears
        # in argv — the shell expands ${GH_TOKEN} at pod runtime, not here.
        gh_insteadof = (
            'git -c "url.https://x-access-token:${GH_TOKEN}'
            '@github.com/.insteadOf=https://github.com/"'
        )

        # Contract materialisation step (#111).
        # The contract path is repo-relative (e.g. "agents/orchestrator.md").
        # The baked file is at /app/agents/<basename>.
        # Copy it into the clone so the agent can read it at the expected path.
        contract_basename = contract.rsplit("/", 1)[-1] if contract else ""
        if contract_basename:
            baked_contract = shlex.quote(f"{_BAKED_CONTRACT_DIR}/{contract_basename}")
            contract_step = (
                # Fail loudly if the baked contract is absent — never silently
                # allow the agent to run without its governing contract (#111).
                f"[ -f {baked_contract} ] || "
                f'{{ echo "FATAL: contract not found: {baked_contract}" >&2; exit 1; }}\n'
                "mkdir -p /workspace/repo/agents\n"
                f"cp {baked_contract} /workspace/repo/agents/{shlex.quote(contract_basename)}\n"
            )
        else:
            contract_step = ""

        script = (
            "set -e\n"
            # HOME must be writable: the agent-runner user has no home dir
            # (useradd --no-create-home); claude's tools write under HOME (#95).
            "export HOME=/workspace\n"
            # Step 1: clone the repo (GH_TOKEN via env — not in argv).
            f"{gh_insteadof} "
            f"clone --depth 1{branch_flag} {shlex.quote(clone_url)} /workspace/repo\n"
            # Step 2: configure git identity globally so the agent can commit (#112).
            # Global config is safe here because this pod is single-use.
            'git config --global user.name "Orchestrator Agent"\n'
            'git config --global user.email "agent@orchestrator"\n'
            # Step 3: configure push auth via url.insteadOf using ${GH_TOKEN} (#112).
            # This ensures both git fetch and git push use the scoped token.
            # The token is expanded at runtime from the pod env — never a literal.
            'git config --global '
            '"url.https://x-access-token:${GH_TOKEN}@github.com/.insteadOf" '
            '"https://github.com/"\n'
            # Step 4: materialise the agent contract into the clone (#111).
            + contract_step
            # Step 5: install I9 hook if ORCHESTRATOR_ALLOWED_AGENT_REFS is set.
            + "if [ -n \"${ORCHESTRATOR_ALLOWED_AGENT_REFS}\" ]; then\n"
            "  mkdir -p /workspace/repo/.claude\n"
            f"  cp {shlex.quote(hook_src)} /workspace/repo/.claude/i9_spawn_hook.py\n"
            "  python3 -c \"\n"
            "import json, pathlib\n"
            "s = {'hooks': {'PreToolUse': [{'matcher': 'Task', 'hooks': [{"
            "'type': 'command', 'command': 'python3 /workspace/repo/.claude/i9_spawn_hook.py'"
            "}]}]}}\n"
            "p = pathlib.Path('/workspace/repo/.claude/settings.json')\n"
            "p.write_text(json.dumps(s, indent=2))\n"
            "\"\n"
            "fi\n"
            # Step 6: run claude in the cloned working tree.
            f"cd /workspace/repo\n"
            f"exec {quoted_claude}\n"
        )
        return script

    def _build_job_spec(
        self,
        run_id: str,
        entry_script: str,
        child_env: dict[str, str],
    ) -> dict[str, Any]:
        """Build a Kubernetes Job manifest for this dispatch.

        The container command is ["sh", "-c", "<entry_script>"] so the clone,
        hook setup, and claude invocation all happen INSIDE the pod.

        I3: Only env vars present in child_env are forwarded to the pod.
        The caller (_build_child_env in the harness) guarantees that
        child_env contains only CLAUDE_CODE_OAUTH_TOKEN, GH_TOKEN, and
        auxiliary non-credential vars.  Master credentials (App private key,
        FORGE_TOKEN) are absent from child_env before this method is called.

        I3 token: GH_TOKEN is in child_env and forwarded as a pod env var.
        The token NEVER appears as a literal string in the Job manifest — the
        entry script references it via ${GH_TOKEN} at runtime.
        """
        job_name = f"orch-agent-{run_id[:16]}"
        env_list = [{"name": k, "value": v} for k, v in child_env.items()]

        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "labels": {
                    "app": "orchestrator-agent",
                    "run-id": run_id[:63],  # label value max 63 chars
                },
            },
            "spec": {
                "backoffLimit": 0,  # no K8s-level retries; Engine handles retries
                "ttlSecondsAfterFinished": 600,  # auto-clean after 10 min
                "template": {
                    "metadata": {
                        "labels": {
                            "app": "orchestrator-agent",
                            "run-id": run_id[:63],
                        },
                    },
                    "spec": {
                        "serviceAccountName": self._service_account,
                        "restartPolicy": "Never",
                        "containers": [
                            {
                                "name": "agent",
                                "image": self._image,
                                # Entry-point is a shell script that clones, hooks,
                                # and runs claude — all inside the pod.
                                "command": ["sh", "-c", entry_script],
                                "env": env_list,
                                "resources": {
                                    "requests": {"cpu": "250m", "memory": "512Mi"},
                                    "limits": {"cpu": "2000m", "memory": "2Gi"},
                                },
                                "securityContext": {
                                    "runAsNonRoot": True,
                                    "allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": False,
                                },
                            }
                        ],
                        "securityContext": {
                            "runAsUser": 1001,
                            "runAsGroup": 1001,
                            "fsGroup": 1001,
                        },
                    },
                },
            },
        }

    async def dispatch(
        self,
        *,
        run_id: str,
        repo_owner: str,
        repo_name: str,
        branch: str | None,
        claude_args: list[str],
        child_env: dict[str, str],
        allowed_agent_refs: list[str] | None,
        contract: str,
        event_store: RunEventStore,
        harness: Any,
    ) -> None:
        """Build the entry script, create a K8s Job, and watch it in a background task.

        The control-plane does NOT clone the repo here — the entry script
        (built by _build_entry_script) does the clone inside the pod.
        The entry script also materialises the agent contract (#111) and
        configures git identity / push auth (#112) inside the pod.
        """
        entry_script = self._build_entry_script(
            repo_owner, repo_name, branch, claude_args, contract
        )
        job_spec = self._build_job_spec(run_id, entry_script, child_env)
        job_name: str = job_spec["metadata"]["name"]
        self._job_names[run_id] = job_name

        self._kube_client.create_namespaced_job(self._namespace, job_spec)
        event_store.set_status(run_id, RunStatus(state="in_progress"))
        event_store.append(
            run_id,
            RunEvent(
                event_type="k8s_job_created",
                data={"job_name": job_name, "namespace": self._namespace},
                timestamp=datetime.now(tz=UTC),
            ),
        )

        watcher = asyncio.create_task(
            self._watch(run_id, job_name, event_store),
            name=f"k8s-watch-{run_id[:8]}",
        )
        self._background_tasks.add(watcher)
        watcher.add_done_callback(self._background_tasks.discard)

    async def _watch(
        self,
        run_id: str,
        job_name: str,
        event_store: RunEventStore,
    ) -> None:
        """Poll the Job status until completion or timeout."""
        deadline = time.monotonic() + self._job_timeout_s

        while True:
            if time.monotonic() >= deadline:
                event_store.append(
                    run_id,
                    RunEvent(
                        event_type="k8s_job_timeout",
                        data={"job_name": job_name},
                        timestamp=datetime.now(tz=UTC),
                    ),
                )
                event_store.set_status(
                    run_id,
                    RunStatus(state="completed", conclusion="failure"),
                )
                self._cleanup_job(job_name)
                break

            try:
                job_obj = self._kube_client.read_namespaced_job(job_name, self._namespace)
            except Exception as exc:
                event_store.append(
                    run_id,
                    RunEvent(
                        event_type="k8s_read_error",
                        data={"error": str(exc)},
                        timestamp=datetime.now(tz=UTC),
                    ),
                )
                await asyncio.sleep(self._poll_interval_s)
                continue

            status = job_obj.get("status", {})
            succeeded = int(status.get("succeeded") or 0)
            failed = int(status.get("failed") or 0)

            if succeeded > 0:
                event_store.set_status(
                    run_id,
                    RunStatus(state="completed", conclusion="success"),
                )
                self._cleanup_job(job_name)
                break

            if failed > 0:
                event_store.set_status(
                    run_id,
                    RunStatus(state="completed", conclusion="failure"),
                )
                self._cleanup_job(job_name)
                break

            await asyncio.sleep(self._poll_interval_s)

    def _cleanup_job(self, job_name: str) -> None:
        """Delete the K8s Job (best-effort; TTL handles it if this fails)."""
        try:
            self._kube_client.delete_namespaced_job(
                job_name,
                self._namespace,
                propagation_policy="Foreground",
            )
        except Exception:
            pass

    async def cancel(
        self,
        *,
        run_id: str,
        event_store: RunEventStore,
    ) -> None:
        """Cancel an in-flight Job (delete it from the cluster)."""
        for task in list(self._background_tasks):
            if run_id in (task.get_name() or ""):
                task.cancel()

        job_name = self._job_names.get(run_id)
        if job_name is not None:
            self._cleanup_job(job_name)

        event_store.set_status(
            run_id,
            RunStatus(state="completed", conclusion="cancelled"),
        )


# ---------------------------------------------------------------------------
# Real kube client factory — lazy import so subprocess path is unaffected
# ---------------------------------------------------------------------------


def _make_real_kube_client() -> KubeClientPort:
    """Return a thin wrapper around the kubernetes-client BatchV1Api.

    Lazy-imports the kubernetes package so the default subprocess path and
    all non-k8s tests run without the package installed.

    Raises ImportError with a helpful message if kubernetes is not installed.
    """
    # Both imports are in a try/except so that a helpful error is raised when the
    # kubernetes package is not installed.  The type: ignore covers the mypy
    # import-not-found error when kubernetes is absent from the type-check env.
    try:
        import kubernetes as _kube_pkg  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "The 'kubernetes' package is required for K8sJobBackend. "
            "Install the k8s extra: pip install '.[k8s]' (baked into the control-plane image)."
        ) from exc

    k8s_client = _kube_pkg.client
    k8s_config = _kube_pkg.config

    try:
        k8s_config.load_incluster_config()
    except Exception:
        k8s_config.load_kube_config()

    batch_v1 = k8s_client.BatchV1Api()
    return _RealKubeClient(batch_v1)


class _RealKubeClient:
    """Thin wrapper adapting kubernetes BatchV1Api to KubeClientPort."""

    def __init__(self, batch_v1: Any) -> None:
        self._api = batch_v1

    def create_namespaced_job(self, namespace: str, body: dict[str, Any]) -> dict[str, Any]:
        result: Any = self._api.create_namespaced_job(namespace=namespace, body=body)
        if hasattr(result, "to_dict"):
            raw: dict[str, Any] = result.to_dict()
            return raw
        return dict(result)

    def read_namespaced_job(self, name: str, namespace: str) -> dict[str, Any]:
        result: Any = self._api.read_namespaced_job(name=name, namespace=namespace)
        if hasattr(result, "to_dict"):
            raw: dict[str, Any] = result.to_dict()
            return raw
        return dict(result)

    def delete_namespaced_job(
        self,
        name: str,
        namespace: str,
        *,
        propagation_policy: str = "Foreground",
    ) -> None:
        import kubernetes as _kube_pkg  # noqa: PLC0415

        self._api.delete_namespaced_job(
            name=name,
            namespace=namespace,
            body=_kube_pkg.client.V1DeleteOptions(propagation_policy=propagation_policy),
        )


# ---------------------------------------------------------------------------
# Backend factory — selects subprocess vs k8s based on env / config
# ---------------------------------------------------------------------------


def make_execution_backend(
    *,
    process_runner: ProcessRunner | None = None,
    kube_client: KubeClientPort | None = None,
) -> SubprocessBackend | K8sJobBackend:
    """Construct the appropriate ExecutionBackend based on HARNESS_EXECUTION_BACKEND.

    subprocess (default):
      Used unless HARNESS_EXECUTION_BACKEND=k8s.

    k8s:
      Used when HARNESS_EXECUTION_BACKEND=k8s.  The kubernetes package is imported
      lazily so the subprocess path never acquires the dependency.
    """
    backend_name = os.environ.get("HARNESS_EXECUTION_BACKEND", "subprocess").lower()
    if backend_name == "k8s":
        image = os.environ.get(
            "HARNESS_K8S_IMAGE",
            "ghcr.io/tuckermclean/orchestrator-agent-runner:latest",
        )
        namespace = os.environ.get("HARNESS_K8S_NAMESPACE", "default")
        service_account = os.environ.get("HARNESS_K8S_SERVICE_ACCOUNT", "orchestrator")
        return K8sJobBackend(
            image=image,
            namespace=namespace,
            kube_client=kube_client,
            service_account=service_account,
        )
    return SubprocessBackend(process_runner=process_runner)
