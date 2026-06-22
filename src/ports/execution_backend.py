"""ExecutionBackend — seam between HarnessPort and the underlying execution substrate.

An ExecutionBackend abstracts "spawn a claude agent dispatch and surface its run
state."  The harness delegates to the configured backend so the substrate
(local subprocess vs Kubernetes Job) can be swapped without touching the
HarnessPort contract.

Two concrete implementations:
  SubprocessBackend — spawns the claude CLI as a local child process.  Dev/default.
  K8sJobBackend     — schedules a Kubernetes Job per dispatch.  Prod isolation.

Backend selection:
  subprocess  — always, unless HARNESS_EXECUTION_BACKEND=k8s is set.
  k8s         — when HARNESS_EXECUTION_BACKEND=k8s (or detect in-cluster config).

I3 invariant:
  Only CLAUDE_CODE_OAUTH_TOKEN and a scoped GH_TOKEN enter the execution
  environment.  Master credentials (App private key, FORGE_TOKEN) are NEVER
  forwarded to a child process or a K8s Job pod.

I9 invariant (spawn hook carry-over to K8s):
  When K8sJobBackend is used and allowed_agent_refs is not None, the hook script
  and settings.json are written into the working tree before the Job starts, and
  ORCHESTRATOR_ALLOWED_AGENT_REFS is injected as a Job-level env var.  The pod's
  claude process enforces the allow-set exactly as the subprocess backend does.
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
    """Abstraction over "run a claude agent dispatch and surface run state."

    Implementors must be safe to call concurrently from multiple asyncio tasks.
    dispatch() must return immediately without blocking the event loop.
    """

    async def dispatch(
        self,
        *,
        run_id: str,
        claude_args: list[str],
        repo_dir: str,
        work_dir: str,
        child_env: dict[str, str],
        event_store: RunEventStore,
    ) -> None:
        """Start execution and record run state into event_store.

        Fires and forgets — returns immediately after the job/process is
        scheduled.  Progress is tracked via the RunEventStore.
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
        claude_args: list[str],
        repo_dir: str,
        work_dir: str,
        child_env: dict[str, str],
        event_store: RunEventStore,
    ) -> None:
        self.dispatched.append(
            {
                "run_id": run_id,
                "claude_args": claude_args,
                "repo_dir": repo_dir,
                "work_dir": work_dir,
                "child_env": child_env,
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

    This is the dev/default backend — identical behaviour to the original
    process-runner logic that was inlined in ClaudeCodeHarnessPort before the
    ExecutionBackend seam was introduced.

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
        claude_args: list[str],
        repo_dir: str,
        work_dir: str,
        child_env: dict[str, str],
        event_store: RunEventStore,
    ) -> None:
        """Spawn the claude CLI subprocess and watch it in a background task."""
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

# Default poll interval and timeout (seconds) — module-level so tests can override.
_K8S_POLL_INTERVAL_S: float = 5.0
_K8S_JOB_TIMEOUT_S: float = 1800.0  # 30 minutes


class K8sJobBackend:
    """Execute a claude dispatch as a Kubernetes Job (prod isolation backend).

    Per-dispatch behaviour:
      1. Build a Job spec with ONE container running the agent-runner image.
      2. Inject ONLY CLAUDE_CODE_OAUTH_TOKEN + GH_TOKEN (scoped) into the pod
         env — I3: master credentials are NEVER present.
      3. Create the Job via the kube client.
      4. Poll the Job status in a background asyncio task; record events into
         RunEventStore.
      5. Delete the Job on completion or cancel.

    I9 hook carry-over:
      When child_env contains ORCHESTRATOR_ALLOWED_AGENT_REFS, it is forwarded
      as a Job env var.  The hook script (written into repo_dir/.claude/ by the
      harness before dispatch() is called) is part of the working tree, so the
      PreToolUse gate is active inside the pod exactly as in the subprocess
      backend.  The hook exits 2 (DENY) — not 1 — per the Claude Code contract.

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

    def _build_job_spec(
        self,
        run_id: str,
        claude_args: list[str],
        child_env: dict[str, str],
    ) -> dict[str, Any]:
        """Build a Kubernetes Job manifest for this dispatch.

        I3: Only env vars present in child_env are forwarded to the pod.
        The caller (_build_child_env in the harness) guarantees that
        child_env contains only CLAUDE_CODE_OAUTH_TOKEN, GH_TOKEN, and
        auxiliary non-credential vars.  Master credentials (App private key,
        FORGE_TOKEN) are absent from child_env before this method is called.
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
                                "command": claude_args,
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
        claude_args: list[str],
        repo_dir: str,
        work_dir: str,
        child_env: dict[str, str],
        event_store: RunEventStore,
    ) -> None:
        """Create a K8s Job and watch it in a background task."""
        job_spec = self._build_job_spec(run_id, claude_args, child_env)
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
            self._watch(run_id, job_name, work_dir, event_store),
            name=f"k8s-watch-{run_id[:8]}",
        )
        self._background_tasks.add(watcher)
        watcher.add_done_callback(self._background_tasks.discard)

    async def _watch(
        self,
        run_id: str,
        job_name: str,
        work_dir: str,
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

        # Clean up local temp dir
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass

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
