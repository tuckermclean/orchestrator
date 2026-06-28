"""Doctrine Principle 3 — Bound everything, and order the bounds (DOCTRINE.md).

The run-lifecycle timeouts must hold a strict ordering, or the supervisor reaps work that
is still legitimately in flight (SPEC §7, timeout-ordering invariant):

    POLL_INTERVAL_S  ≪  CI_WAIT_S  <  _K8S_JOB_TIMEOUT_S  <  STALE_DRAFT_THRESHOLD_S

This test is the enforcing gate. An invariant that lives only in someone's head is one a
single edit can break: raising CI_WAIT_S without moving the coupled constants arms a
time-bomb. If any of these drifts out of order, this fails loudly before merge.
"""

from src.domain.types import (
    CI_WAIT_S,
    POLL_INTERVAL_S,
    STALE_DRAFT_THRESHOLD_S,
)
from src.ports.execution_backend import _K8S_JOB_TIMEOUT_S


def test_poll_interval_far_below_run_deadline() -> None:
    # POLL_INTERVAL_S must be a small fraction of the run deadline — many polls per run.
    assert POLL_INTERVAL_S * 10 <= CI_WAIT_S, (
        f"POLL_INTERVAL_S ({POLL_INTERVAL_S}s) is not << CI_WAIT_S ({CI_WAIT_S}s): "
        "the await loop must poll many times within one run deadline."
    )


def test_run_deadline_below_backend_safety_net() -> None:
    # The control-plane _await_run deadline must be the authoritative one; the K8s-Job
    # backend net must sit strictly above it so it never preempts the control plane.
    assert CI_WAIT_S < _K8S_JOB_TIMEOUT_S, (
        f"CI_WAIT_S ({CI_WAIT_S}s) must be < _K8S_JOB_TIMEOUT_S ({_K8S_JOB_TIMEOUT_S}s) "
        "so the backend safety net never preempts the control-plane run deadline."
    )


def test_backend_net_below_stale_draft_threshold() -> None:
    # RC-1 must not treat a run that is still legitimately in flight as a stale draft.
    assert _K8S_JOB_TIMEOUT_S < STALE_DRAFT_THRESHOLD_S, (
        f"_K8S_JOB_TIMEOUT_S ({_K8S_JOB_TIMEOUT_S}s) must be < STALE_DRAFT_THRESHOLD_S "
        f"({STALE_DRAFT_THRESHOLD_S}s) so the reconciler never re-dispatches a live run."
    )


def test_full_ordering_invariant_holds() -> None:
    chain = [
        ("POLL_INTERVAL_S", POLL_INTERVAL_S),
        ("CI_WAIT_S", CI_WAIT_S),
        ("_K8S_JOB_TIMEOUT_S", _K8S_JOB_TIMEOUT_S),
        ("STALE_DRAFT_THRESHOLD_S", STALE_DRAFT_THRESHOLD_S),
    ]
    values = [v for _, v in chain]
    assert values == sorted(values) and len(set(values)) == len(values), (
        "timeout-ordering invariant violated; required strict order "
        f"{' < '.join(name for name, _ in chain)} but got {chain}"
    )
