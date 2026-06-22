"""Integration tests — per-repo required_checks in the converge approve gate (closes #71).

Covers SPEC §7 BLOCKING_CI_CHECKS gate wiring:
- A repo whose required_checks is a narrowed subset blocks converge on ONLY those
  checks; other checks being red/absent are irrelevant.
- A repo with the default required_checks == BLOCKING_CI_CHECKS behaves exactly as today.
- No-registry path: Engine.converge falls back to BLOCKING_CI_CHECKS unchanged.
- OrchestratorService.converge_pr resolves required_checks from the registry and passes
  it down; the registry lookup itself is backward-compatible (None → global constant).
"""

from __future__ import annotations

import pytest

from src.domain.types import (
    BLOCKING_CI_CHECKS,
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    LABEL_READY,
    PRRef,
    RepoRef,
    Verdict,
)
from src.engine import converge as converge_mod
from src.engine.dispatch import Engine
from src.ports.fakes import (
    FakeConvergeStateStore,
    FakeCounterStore,
    FakeForgePort,
    FakeHarnessPort,
    FakeSessionPort,
)
from src.service.orchestrator import OrchestratorService
from src.service.registry import FakeRepoRegistry, RepoConfig

_REPO = RepoRef(owner="acme", name="service")
_PR = PRRef(repo=_REPO, number=99)
_CHANGED_FILES = ["src/foo.py"]

# A minimal subset of BLOCKING_CI_CHECKS to use as the per-repo override.
_NARROW_CHECKS: tuple[str, ...] = (BLOCKING_CI_CHECKS[0], BLOCKING_CI_CHECKS[1])


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _seed_pr(forge: FakeForgePort, *, changed_files: list[str] | None = None) -> None:
    """Seed a non-draft converge PR with no check runs (CI red by default)."""
    files = changed_files if changed_files is not None else _CHANGED_FILES
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=len(files))
    forge._changed_files[forge._pr_key(_PR)] = files


def _seed_checks(
    forge: FakeForgePort,
    names: tuple[str, ...],
    *,
    conclusion: str = "success",
) -> None:
    """Seed completed check runs for the given names on _PR."""
    for name in names:
        forge.seed_check_run(_PR, name, "completed", conclusion)


def _engine(
    forge: FakeForgePort,
    harness: FakeHarnessPort,
) -> Engine:
    return Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
    )


def _zero_verdict() -> Verdict:
    return Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])


def _blocker_verdict(sigs: list[str]) -> Verdict:
    return Verdict(blockers=1, suggestions=0, nits=[], blocker_signatures=sigs)


# ---------------------------------------------------------------------------
# §7-converge-gate / narrowed-subset-approves
# ---------------------------------------------------------------------------


@pytest.mark.covers("§7-converge-gate", "narrowed-subset-approves")
async def test_converge_narrowed_subset_approves_when_subset_green() -> None:
    """A repo with required_checks narrowed to 2 checks approves when those 2 are green.

    The remaining 4 BLOCKING_CI_CHECKS are NOT seeded (absent/red); under the global
    constant they would cause CI to be red.  Under the per-repo narrowed subset the
    gate should approve.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _seed_pr(forge)
    # Only seed the 2 narrowed checks as green; the other 4 are absent.
    _seed_checks(forge, _NARROW_CHECKS)
    harness.script_reviewer_verdicts(_zero_verdict())

    state = await _engine(forge, harness).converge(_PR, required_checks=_NARROW_CHECKS)

    assert state == "APPROVED"
    assert (_PR, LABEL_READY) in forge.add_label_calls


@pytest.mark.covers("§7-converge-gate", "narrowed-subset-full-still-required")
async def test_converge_narrowed_subset_escalates_when_subset_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo with a narrowed required_checks escalates when ANY required check is red.

    Uses the R1-fix → R2-fix → R3-ci-red path so the ci-red gate fires with the
    narrowed subset.  Only the first check of _NARROW_CHECKS is seeded green; the
    second is absent.  trigger_ci finds no recovery → ESCALATED (E4).
    """
    monkeypatch.setattr(converge_mod, "CI_WAIT_S", 0)

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _seed_pr(forge)
    # Seed only the first narrowed check; the second is absent (ci_green=False).
    _seed_checks(forge, (_NARROW_CHECKS[0],))
    # R1 fix, R2 fix, R3 zero blockers (→ ci-red because second narrow check is absent).
    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),
        _blocker_verdict(["sig-b"]),
        _zero_verdict(),
    )
    # No CI recovery scripted → trigger_ci leaves second check absent → E4.

    state = await _engine(forge, harness).converge(_PR, required_checks=_NARROW_CHECKS)

    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls


# ---------------------------------------------------------------------------
# §7-converge-gate / default-behaviour-unchanged
# ---------------------------------------------------------------------------


@pytest.mark.covers("§7-converge-gate", "default-behaviour-unchanged")
async def test_converge_default_checks_all_six_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When required_checks is the default (BLOCKING_CI_CHECKS), ALL 6 checks must be green.

    Seeds only 5 of 6 checks; under the default gate this means CI is not green at R3.
    Uses R1-fix → R2-fix → R3-ci-red path; no CI recovery scripted → ESCALATED (E4).
    """
    monkeypatch.setattr(converge_mod, "CI_WAIT_S", 0)

    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _seed_pr(forge)
    # Seed 5 of 6 green; the 6th is absent.
    _seed_checks(forge, BLOCKING_CI_CHECKS[:5])
    harness.script_reviewer_verdicts(
        _blocker_verdict(["sig-a"]),
        _blocker_verdict(["sig-b"]),
        _zero_verdict(),
    )
    # No CI recovery scripted → trigger_ci finds 6th check still absent → E4.
    state = await _engine(forge, harness).converge(
        _PR, required_checks=BLOCKING_CI_CHECKS
    )

    assert state == "ESCALATED"
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls


# ---------------------------------------------------------------------------
# §7-converge-gate / no-registry-fallback
# ---------------------------------------------------------------------------


@pytest.mark.covers("§7-converge-gate", "no-registry-fallback")
async def test_engine_converge_no_required_checks_uses_blocking_ci_checks() -> None:
    """Engine.converge without required_checks defaults to BLOCKING_CI_CHECKS.

    This is the backward-compat path: no registry, no override → existing behaviour.
    Seeds all 6 BLOCKING_CI_CHECKS as green; omits required_checks → APPROVED.
    """
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    _seed_pr(forge)
    _seed_checks(forge, BLOCKING_CI_CHECKS)
    harness.script_reviewer_verdicts(_zero_verdict())

    state = await _engine(forge, harness).converge(_PR)  # no required_checks kwarg

    assert state == "APPROVED"
    assert (_PR, LABEL_READY) in forge.add_label_calls


# ---------------------------------------------------------------------------
# §7-converge-gate / service-layer-resolves-registry
# ---------------------------------------------------------------------------


@pytest.mark.covers("§7-converge-gate", "service-layer-resolves-registry")
async def test_service_converge_pr_resolves_per_repo_required_checks() -> None:
    """OrchestratorService.converge_pr uses the registry's required_checks for the repo.

    The registry has a config with a 2-check narrowed subset.  Only those 2 checks
    are seeded green; all other BLOCKING_CI_CHECKS are absent.  converge_pr should
    APPROVE because the narrowed subset is satisfied.
    """
    forge = FakeForgePort()
    session = FakeSessionPort()
    harness = FakeHarnessPort(forge=forge)
    _seed_pr(forge)
    _seed_checks(forge, _NARROW_CHECKS)

    registry = FakeRepoRegistry(
        [
            RepoConfig(
                repo=_REPO,
                required_checks=_NARROW_CHECKS,
            )
        ]
    )
    harness.script_reviewer_verdicts(_zero_verdict())

    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
        registry=registry,
    )

    state = await service.converge_pr(_PR)

    assert state == "APPROVED"
    assert (_PR, LABEL_READY) in forge.add_label_calls


@pytest.mark.covers("§7-converge-gate", "service-layer-resolves-registry")
async def test_service_converge_pr_no_registry_uses_global_constant() -> None:
    """OrchestratorService.converge_pr without registry falls back to BLOCKING_CI_CHECKS.

    No registry is wired; all 6 BLOCKING_CI_CHECKS are seeded green → APPROVED.
    This verifies the no-registry backward-compat path through the service layer.
    """
    forge = FakeForgePort()
    session = FakeSessionPort()
    harness = FakeHarnessPort(forge=forge)
    _seed_pr(forge)
    _seed_checks(forge, BLOCKING_CI_CHECKS)

    harness.script_reviewer_verdicts(_zero_verdict())

    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
        registry=None,  # explicit no-registry
    )

    state = await service.converge_pr(_PR)

    assert state == "APPROVED"
    assert (_PR, LABEL_READY) in forge.add_label_calls


@pytest.mark.covers("§7-converge-gate", "service-layer-resolves-registry")
async def test_service_converge_pr_unregistered_repo_uses_global_constant() -> None:
    """converge_pr with a registry that doesn't know the PR's repo falls back to BLOCKING_CI_CHECKS.

    The registry is set but the PR's repo is not in it.  All 6 BLOCKING_CI_CHECKS are
    seeded green → APPROVED (global-constant fallback).
    """
    forge = FakeForgePort()
    session = FakeSessionPort()
    harness = FakeHarnessPort(forge=forge)
    _seed_pr(forge)
    _seed_checks(forge, BLOCKING_CI_CHECKS)

    # Registry knows a different repo, not _REPO.
    other_repo = RepoRef(owner="acme", name="other")
    registry = FakeRepoRegistry(
        [RepoConfig(repo=other_repo, required_checks=_NARROW_CHECKS)]
    )
    harness.script_reviewer_verdicts(_zero_verdict())

    service = OrchestratorService(
        forge=forge,
        harness=harness,
        session=session,
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
        registry=registry,
    )

    state = await service.converge_pr(_PR)

    assert state == "APPROVED"
    assert (_PR, LABEL_READY) in forge.add_label_calls
