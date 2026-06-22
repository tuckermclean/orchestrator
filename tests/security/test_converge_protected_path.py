"""Security tests — I2: PROTECTED_PATHS PR escalates to E1 before any specialist spawn.

SPEC §6 E1, SECURITY.md §3 I2, TESTING.md §5.
"""

from __future__ import annotations

import pytest

from src.domain.types import (
    LABEL_CONVERGE,
    LABEL_NEEDS_HUMAN,
    PRRef,
    RepoRef,
)
from src.engine.dispatch import Engine
from src.ports.fakes import (
    FakeConvergeStateStore,
    FakeCounterStore,
    FakeForgePort,
    FakeHarnessPort,
    FakeSessionPort,
)

_REPO = RepoRef(owner="acme", name="service")
_PR = PRRef(repo=_REPO, number=21)


def _engine(forge: FakeForgePort, harness: FakeHarnessPort) -> Engine:
    return Engine(
        forge=forge,
        harness=harness,
        session=FakeSessionPort(),
        counter=FakeCounterStore(),
        converge_state=FakeConvergeStateStore(),
    )


# One matching path per PROTECTED_PATHS entry (SPEC §7).
_MATCHING_PATHS = [
    ".github/workflows/deploy.yml",  # .github/workflows/**
    "ARCHITECTURE.md",  # bare filename at root
    "SECURITY.md",  # bare filename at root
    "COMPLIANCE.md",  # bare filename at root
    ".agents/engineering-security-engineer.md",  # .agents/**
    "agents/converge-reviewer.md",  # agents/**
]


@pytest.mark.parametrize("path", _MATCHING_PATHS)
async def test_converge_protected_path_escalates(path: str) -> None:
    """Each PROTECTED_PATHS entry → ESCALATED before any reviewer dispatch (E1)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=1)
    forge._changed_files[forge._pr_key(_PR)] = [path]
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    assert state == "ESCALATED"
    # No reviewer (or any specialist) was ever dispatched.
    assert harness.dispatch_calls == []
    # LABEL_NEEDS_HUMAN was added.
    assert (_PR, LABEL_NEEDS_HUMAN) in forge.add_label_calls


async def test_converge_protected_path_clears_converge_state() -> None:
    """E1 clears converge state so de-escalation restarts at R1 (SPEC §10.2 H3)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=1)
    forge._changed_files[forge._pr_key(_PR)] = ["agents/converge-reviewer.md"]
    engine = _engine(forge, harness)
    assert isinstance(engine.converge_state, FakeConvergeStateStore)

    await engine.converge(_PR)

    assert _PR in engine.converge_state.clear_calls


async def test_converge_protected_path_non_matching_proceeds() -> None:
    """A non-protected path is NOT escalated by the protected-path gate (B1 matrix)."""
    forge = FakeForgePort()
    harness = FakeHarnessPort(forge=forge)
    forge.seed_pr(_PR, draft=False, labels=[LABEL_CONVERGE], changed_files=1)
    # 'src/agents/foo.py' must NOT match 'agents/**' (root-anchored).
    forge._changed_files[forge._pr_key(_PR)] = ["src/agents_helper.py"]
    for name in (
        "Type Check",
        "Lint",
        "Integration Tests",
        "Docker Build & Scan",
        "Helm Lint",
        "Helm Kubeconform",
    ):
        forge.seed_check_run(_PR, name, "completed", "success")
    from src.domain.types import Verdict

    harness.script_reviewer_verdicts(
        Verdict(blockers=0, suggestions=0, nits=[], blocker_signatures=[])
    )
    engine = _engine(forge, harness)

    state = await engine.converge(_PR)

    # Proceeds past the gate and reaches the reviewer.
    assert state == "APPROVED"
    assert len(harness.dispatch_calls) == 1
