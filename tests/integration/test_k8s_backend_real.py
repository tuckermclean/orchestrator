"""Real Kubernetes cluster end-to-end tests for K8sJobBackend.

These tests are SKIPPED unless:
  1. KUBECONFIG is set and points to a reachable cluster, OR
  2. The test is running inside a K8s pod (in-cluster config available).
  3. HARNESS_K8S_TEST_NAMESPACE env var is set (safety gate).

All tests are marked @pytest.mark.integration_real and are excluded from the
default CI run (pytest -q, no integration_real marks collected by default).

To run against a real cluster:
  KUBECONFIG=/path/to/kubeconfig \
  HARNESS_K8S_TEST_NAMESPACE=orchestrator-test \
  pytest tests/integration/test_k8s_backend_real.py -v -m integration_real

The tests require:
  - The kubernetes Python package installed (pip install kubernetes).
  - RBAC granting the test ServiceAccount create/get/list/watch/delete on
    jobs and secrets in HARNESS_K8S_TEST_NAMESPACE.
  - The agent-runner image available in the cluster's registry.
"""

from __future__ import annotations

import os

import pytest

# Skip the entire module if no cluster is configured.
_K8S_NAMESPACE = os.environ.get("HARNESS_K8S_TEST_NAMESPACE", "")
_IN_CLUSTER = os.path.exists("/var/run/secrets/kubernetes.io")
_HAS_KUBECONFIG = bool(os.environ.get("KUBECONFIG") or _IN_CLUSTER)
_SKIP_REASON = (
    "Real K8s cluster not available — set KUBECONFIG and HARNESS_K8S_TEST_NAMESPACE "
    "to run these tests."
)

pytestmark = pytest.mark.integration_real


@pytest.mark.skipif(
    not (_HAS_KUBECONFIG and _K8S_NAMESPACE),
    reason=_SKIP_REASON,
)
async def test_k8s_job_backend_real_dispatch_success() -> None:
    """Real cluster: K8sJobBackend dispatches a Job and records success.

    Uses a minimal Job that runs 'true' (exits 0) so we get a clean success
    path without needing the full agent-runner image.
    """
    try:
        from src.ports.execution_backend import K8sJobBackend, _make_real_kube_client
    except ImportError:
        pytest.skip("kubernetes package not installed")

    kube_client = _make_real_kube_client()
    backend = K8sJobBackend(
        image="busybox:latest",
        namespace=_K8S_NAMESPACE,
        kube_client=kube_client,
        poll_interval_s=2.0,
        job_timeout_s=120.0,
    )

    from src.ports.harness import RunEventStore

    store = RunEventStore()
    import uuid

    run_id = str(uuid.uuid4())
    store.register(run_id)

    # 'true' exits 0 — Job should succeed
    await backend.dispatch(
        run_id=run_id,
        claude_args=["sh", "-c", "exit 0"],
        repo_dir="/tmp",
        work_dir="/tmp",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "fake-for-test", "GH_TOKEN": "fake-for-test"},
        event_store=store,
    )

    import asyncio

    # Wait up to 60 seconds for the Job to complete
    for _ in range(60):
        await asyncio.sleep(1.0)
        status = store.get_status(run_id)
        if status.state == "completed":
            break

    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "success", f"Expected success, got {status.conclusion}"


@pytest.mark.skipif(
    not (_HAS_KUBECONFIG and _K8S_NAMESPACE),
    reason=_SKIP_REASON,
)
async def test_k8s_job_backend_real_dispatch_failure() -> None:
    """Real cluster: K8sJobBackend records failure when Job exits non-zero."""
    try:
        from src.ports.execution_backend import K8sJobBackend, _make_real_kube_client
    except ImportError:
        pytest.skip("kubernetes package not installed")

    kube_client = _make_real_kube_client()
    backend = K8sJobBackend(
        image="busybox:latest",
        namespace=_K8S_NAMESPACE,
        kube_client=kube_client,
        poll_interval_s=2.0,
        job_timeout_s=120.0,
    )

    from src.ports.harness import RunEventStore

    store = RunEventStore()
    import uuid

    run_id = str(uuid.uuid4())
    store.register(run_id)

    # 'exit 1' — Job should fail
    await backend.dispatch(
        run_id=run_id,
        claude_args=["sh", "-c", "exit 1"],
        repo_dir="/tmp",
        work_dir="/tmp",
        child_env={"CLAUDE_CODE_OAUTH_TOKEN": "fake-for-test", "GH_TOKEN": "fake-for-test"},
        event_store=store,
    )

    import asyncio

    for _ in range(60):
        await asyncio.sleep(1.0)
        status = store.get_status(run_id)
        if status.state == "completed":
            break

    status = store.get_status(run_id)
    assert status.state == "completed"
    assert status.conclusion == "failure", f"Expected failure, got {status.conclusion}"
