"""Tests for the three dispatch incident fixes.

Fix 1 — Logging configuration: verify configure_logging installs a handler
         and that src.* log records reach it (SPEC §10.1 observability note).

Fix 2 — SessionLimitHold / AllHarnessesExhausted at both _await_run call sites
         in the issues dispatch path (SPEC §10.1 Step A + Step C, §14.5, §14.8).

Fix 3 — Race-proof PR discovery: label-lag fallback (full open-PR scan) and
         bounded retry absorbs GitHub label-index lag (SPEC §10.1 Step B).
"""

from __future__ import annotations

import logging

import pytest

from src.domain.types import (
    LABEL_AGENT_WORK,
    LABEL_IMPLEMENTING,
    IssueRef,
    PRRef,
    RepoRef,
    RunHandle,
)
from src.engine.dispatch import Engine, _find_implementing_pr, _is_implementing_pr_for_issue
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort
from src.ports.harness_registry import AllHarnessesExhausted

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_REPO = RepoRef(owner="acme", name="svc")
_ISSUE_REF = IssueRef(repo=_REPO, number=77)
_PR_REF = PRRef(repo=_REPO, number=1)
_PR_BODY = "Closes #77"


def _engine(
    forge: FakeForgePort,
    harness: FakeHarnessPort,
    session: FakeSessionPort | None = None,
) -> Engine:
    return Engine(forge=forge, harness=harness, session=session or FakeSessionPort())


class _HarnessWithPRSideEffect(FakeHarnessPort):
    """FakeHarnessPort variant that seeds an implementing PR after the first dispatch.

    Simulates the orchestrator opening a draft PR so the sub-machine can find it.
    """

    def __init__(
        self,
        forge: FakeForgePort,
        pr_ref: PRRef,
        pr_body: str,
        head_branch: str = "agent/77-fix-something",
        label: str = LABEL_IMPLEMENTING,
    ) -> None:
        super().__init__()
        self._side_forge = forge
        self._pr_ref = pr_ref
        self._pr_body = pr_body
        self._head_branch = head_branch
        self._label = label

    async def dispatch(self, context):  # type: ignore[override]
        handle = await super().dispatch(context)
        # After the first dispatch (orchestrator), seed the implementing PR.
        if len(self.dispatch_calls) == 1:
            self._side_forge.seed_pr(
                self._pr_ref,
                draft=True,
                labels=[self._label],
                body=self._pr_body,
                head_branch=self._head_branch,
            )
        return handle


# ===========================================================================
# Fix 1 — Logging configuration
# ===========================================================================


def test_configure_logging_installs_handler() -> None:
    """configure_logging() adds a StreamHandler to the 'src' logger.

    Tests that the handler is actually present after the call — even if the
    module is imported before any log record is emitted.
    """
    from src.logging_setup import configure_logging

    # Ensure a clean slate: remove any handler our sentinel attr marks.
    src_logger = logging.getLogger("src")
    src_logger.handlers = [
        h for h in src_logger.handlers if not getattr(h, "_orchestrator_installed", False)
    ]

    configure_logging()

    installed = [h for h in src_logger.handlers if getattr(h, "_orchestrator_installed", False)]
    assert len(installed) == 1, "Expected exactly one orchestrator StreamHandler installed"
    assert isinstance(installed[0], logging.StreamHandler)


def test_configure_logging_idempotent() -> None:
    """Calling configure_logging() twice does NOT install a second handler."""
    from src.logging_setup import configure_logging

    src_logger = logging.getLogger("src")
    src_logger.handlers = [
        h for h in src_logger.handlers if not getattr(h, "_orchestrator_installed", False)
    ]

    configure_logging()
    configure_logging()

    installed = [h for h in src_logger.handlers if getattr(h, "_orchestrator_installed", False)]
    assert len(installed) == 1, (
        "Expected exactly one handler even after two configure_logging() calls"
    )


def test_configure_logging_src_logger_emits_records(caplog: pytest.LogCaptureFixture) -> None:
    """After configure_logging(), a src.* logger's INFO record is captured.

    Uses caplog (pytest's built-in log capture) to confirm a record at INFO level
    from a src.* logger is propagated and captured.  This verifies that the
    logging setup does not silently swallow application log records.
    """
    from src.logging_setup import configure_logging

    configure_logging()

    test_logger = logging.getLogger("src.test.dispatch_incident")
    with caplog.at_level(logging.INFO, logger="src"):
        test_logger.info("test-message-visible-%s", "dispatch-fix")

    assert any(
        "test-message-visible-dispatch-fix" in record.message
        for record in caplog.records
    ), "Expected src.* INFO log record to be visible in caplog after configure_logging()"


# ===========================================================================
# Fix 2 — SessionLimitHold / AllHarnessesExhausted at both _await_run calls
# ===========================================================================


@pytest.mark.covers("§10.1", "dispatch-sub-machine-orch-await-hold-no-exception-escape")
@pytest.mark.covers("§14.8", "session-limit-dispatch-orchestrator-await-hold")
async def test_dispatch_orchestrator_await_session_limit_hold_is_caught() -> None:
    """SessionLimitHold raised by _await_run(orch_handle) does NOT escape the sub-machine.

    SPEC §14.8: _await_run raises SessionLimitHold on awaiting_quota.
    The orchestrator await must catch it and HOLD (return orch_handle, no escalation).
    Entity stays QUEUED; no label mutations.
    """
    forge = FakeForgePort()
    forge.seed_issue(_ISSUE_REF, labels=[LABEL_AGENT_WORK])

    harness = FakeHarnessPort()
    # First dispatch (orchestrator) → awaiting_quota → _await_run raises SessionLimitHold
    harness.script_next_dispatch_quota(after_n_dispatches=0)

    engine = _engine(forge, harness)

    # Must NOT raise; HOLD means return the orch_handle
    handle = await engine.dispatch("issues", issue_ref=_ISSUE_REF)

    assert handle is not None, "HOLD must return the orchestrator handle (not None)"
    # Only the orchestrator was dispatched; implementer skipped
    assert len(harness.dispatch_calls) == 1, (
        "Only orchestrator dispatch; implementer must not be dispatched on HOLD"
    )
    # No label mutations on the issue
    assert forge.add_label_calls == [], (
        "No label mutations when HOLD — entity stays QUEUED"
    )


@pytest.mark.covers("§10.1", "dispatch-sub-machine-impl-await-hold-no-exception-escape")
@pytest.mark.covers("§14.8", "session-limit-dispatch-implementer-await-hold")
async def test_dispatch_implementer_await_session_limit_hold_is_caught() -> None:
    """SessionLimitHold raised by _await_run(impl_handle) does NOT escape the sub-machine.

    SPEC §14.8: _await_run raises SessionLimitHold on awaiting_quota.
    The implementer await must catch it and HOLD (return impl_handle, no escalation).
    PR draft stays BUILDING; RC-1 re-arms.  No label mutations.
    """
    forge = FakeForgePort()
    forge.seed_issue(_ISSUE_REF, labels=[LABEL_AGENT_WORK])

    harness = _HarnessWithPRSideEffect(
        forge=forge,
        pr_ref=_PR_REF,
        pr_body=_PR_BODY,
    )
    # Second dispatch (implementer) → awaiting_quota → _await_run raises SessionLimitHold
    harness.script_next_dispatch_quota(after_n_dispatches=1)

    engine = _engine(forge, harness)

    handle = await engine.dispatch("issues", issue_ref=_ISSUE_REF)

    assert handle is not None, "HOLD must return the implementer handle"
    # Both orchestrator and implementer were dispatched
    assert len(harness.dispatch_calls) == 2, (
        "Both orchestrator and implementer dispatched before the HOLD"
    )
    # No label mutations — PR draft stays BUILDING, entity stays QUEUED
    assert forge.add_label_calls == [], (
        "No label mutations on implementer SessionLimitHold — PR stays BUILDING"
    )


@pytest.mark.covers("§10.1", "dispatch-sub-machine-orch-await-hold-no-exception-escape")
@pytest.mark.covers("§14.5", "dispatch-orch-await-all-harnesses-exhausted-hold")
async def test_dispatch_orchestrator_await_general_hold_no_escalation() -> None:
    """AllHarnessesExhausted raised at orch _await_run does NOT escalate or mutate labels.

    Uses a subclassed Engine where _await_run raises AllHarnessesExhausted directly
    (simulating the generic HOLD, not just the session-limit variant).
    """
    forge = FakeForgePort()
    forge.seed_issue(_ISSUE_REF, labels=[LABEL_AGENT_WORK])

    harness = FakeHarnessPort()

    class _AwaitRaisesOnFirst(Engine):
        _await_count: int = 0

        async def _await_run(self, handle: RunHandle) -> bool:
            self._await_count += 1
            if self._await_count == 1:
                raise AllHarnessesExhausted("test: all exhausted at orchestrator await")
            return True

    engine = _AwaitRaisesOnFirst(forge=forge, harness=harness, session=FakeSessionPort())
    handle = await engine.dispatch("issues", issue_ref=_ISSUE_REF)

    assert handle is not None
    assert len(harness.dispatch_calls) == 1
    assert forge.add_label_calls == []


@pytest.mark.covers("§10.1", "dispatch-sub-machine-impl-await-hold-no-exception-escape")
@pytest.mark.covers("§14.5", "dispatch-impl-await-all-harnesses-exhausted-hold")
async def test_dispatch_implementer_await_general_hold_no_escalation() -> None:
    """AllHarnessesExhausted raised at impl _await_run does NOT escalate or mutate labels."""
    forge = FakeForgePort()
    forge.seed_issue(_ISSUE_REF, labels=[LABEL_AGENT_WORK])

    harness = _HarnessWithPRSideEffect(
        forge=forge,
        pr_ref=_PR_REF,
        pr_body=_PR_BODY,
    )

    class _AwaitRaisesOnSecond(Engine):
        _await_count: int = 0

        async def _await_run(self, handle: RunHandle) -> bool:
            self._await_count += 1
            if self._await_count == 2:
                raise AllHarnessesExhausted("test: all exhausted at implementer await")
            return True

    engine = _AwaitRaisesOnSecond(forge=forge, harness=harness, session=FakeSessionPort())
    handle = await engine.dispatch("issues", issue_ref=_ISSUE_REF)

    assert handle is not None
    assert len(harness.dispatch_calls) == 2
    assert forge.add_label_calls == []


# ===========================================================================
# Fix 3 — Race-proof PR discovery (label-lag resilience)
# ===========================================================================


@pytest.mark.covers("§10.1", "dispatch-sub-machine-pr-discovery-label-lag-fallback")
async def test_pr_discovery_finds_pr_via_full_scan_when_label_not_indexed() -> None:
    """Implementing PR found via full open-PR scan when label-index lags.

    Simulates the case where the PR exists and ``Closes #N`` is in the body,
    but ``list_prs(..., labels=[LABEL_IMPLEMENTING])`` returns empty (GitHub
    label-index lag) — the full scan path must still find it.
    """
    forge = FakeForgePort()

    # Seed the PR WITHOUT the label first (simulates label-index lag).
    # The PR is open and has the correct Closes #N body.
    forge.seed_pr(
        _PR_REF,
        draft=True,
        labels=[],  # no LABEL_IMPLEMENTING yet in the index
        body="Closes #77",
        head_branch="agent/77-fix-something",
    )

    found = await _find_implementing_pr(forge, _REPO, 77)

    assert found == _PR_REF, (
        "Full-scan fallback must find the PR by Closes #N body even when label is absent"
    )


@pytest.mark.covers("§10.1", "dispatch-sub-machine-pr-discovery-closes-n-authoritative")
async def test_pr_discovery_requires_closes_n_even_when_branch_matches() -> None:
    """Correctness: a PR with agent/* branch but no Closes #N is NOT dispatched.

    Security invariant: we never dispatch the implementer based on branch name alone.
    The ``Closes #N`` body token is the authoritative correctness check.
    """
    forge = FakeForgePort()

    # PR on agent/* branch but body does NOT contain Closes #77.
    pr_ref = PRRef(repo=_REPO, number=2)
    forge.seed_pr(
        pr_ref,
        draft=True,
        labels=[LABEL_IMPLEMENTING],
        body="Implements a feature (no closes token)",  # no Closes #N
        head_branch="agent/77-fix-something",
    )

    found = await _find_implementing_pr(forge, _REPO, 77)

    assert found is None, (
        "Branch prefix alone must NOT match — Closes #N body token is required"
    )


@pytest.mark.covers("§10.1", "dispatch-sub-machine-pr-discovery-no-pr-clean-skip")
async def test_pr_discovery_returns_none_when_no_pr_exists() -> None:
    """When genuinely no implementing PR exists, _find_implementing_pr returns None.

    Mirrors the existing skip behavior (orchestrator crashed / protected-path abort).
    """
    forge = FakeForgePort()
    # No PRs seeded at all.

    found = await _find_implementing_pr(forge, _REPO, 77)

    assert found is None, "No PR → must return None (clean skip)"


@pytest.mark.covers("§10.1", "dispatch-sub-machine-pr-discovery-label-lag-integration")
async def test_dispatch_implementer_dispatched_when_pr_only_found_via_full_scan() -> None:
    """Implementer IS dispatched when the PR is visible only via full-scan (label lag).

    End-to-end dispatch test: orchestrator run completes, PR is in the forge but
    the labeled list_prs returns empty for the first attempt (label-index lag).
    The fallback full-scan finds it and the implementer is dispatched.

    We use a _HarnessWithPRSideEffect that seeds the PR without the label,
    then a custom FakeForgePort subclass that always returns empty for the labeled
    query but returns the PR for the unlabeled query.
    """
    class _LagForge(FakeForgePort):
        """list_prs with labels=[...] always returns empty (simulates label lag)."""

        async def list_prs(self, repo, state="open", labels=None):  # type: ignore[override]
            if labels:
                return []  # label index always lags
            return await super().list_prs(repo, state=state, labels=None)

    forge = _LagForge()
    forge.seed_issue(_ISSUE_REF, labels=[LABEL_AGENT_WORK])

    harness = _HarnessWithPRSideEffect(
        forge=forge,
        pr_ref=_PR_REF,
        pr_body=_PR_BODY,
        head_branch="agent/77-fix-something",
        label=LABEL_IMPLEMENTING,
    )

    engine = _engine(forge, harness)
    handle = await engine.dispatch("issues", issue_ref=_ISSUE_REF)

    # Both runs dispatched: orchestrator + implementer
    assert len(harness.dispatch_calls) == 2, (
        "Implementer must be dispatched even when label-indexed list_prs returns empty"
    )
    assert handle is not None


@pytest.mark.covers("§10.1", "dispatch-sub-machine-pr-discovery-wrong-issue-no-match")
async def test_pr_discovery_does_not_match_pr_for_different_issue() -> None:
    """A PR closing a different issue (#88) is NOT returned for issue #77."""
    forge = FakeForgePort()

    pr_ref = PRRef(repo=_REPO, number=3)
    forge.seed_pr(
        pr_ref,
        draft=True,
        labels=[LABEL_IMPLEMENTING],
        body="Closes #88",  # closes different issue
        head_branch="agent/88-other-thing",
    )

    found = await _find_implementing_pr(forge, _REPO, 77)

    assert found is None, "PR closing #88 must not be matched for issue #77"


def test_is_implementing_pr_for_issue_basic_match() -> None:
    """_is_implementing_pr_for_issue returns True for Closes #N in body."""
    from src.domain.types import PR as PRModel

    pr = PRModel(
        ref=_PR_REF,
        title="[Agent] fix thing",
        body="This PR closes #77 and implements the fix.",
        head_branch="agent/77-fix-thing",
        draft=True,
        merged=False,
        labels=[LABEL_IMPLEMENTING],
        changed_files=3,
        state="open",
    )
    assert _is_implementing_pr_for_issue(pr, 77) is True
    assert _is_implementing_pr_for_issue(pr, 88) is False
