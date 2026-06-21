"""Contract tests for SessionPort against FakeSessionPort."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.domain.types import RepoRef, RunEvent
from src.ports.fakes import FakeSessionPort


@pytest.fixture
def session_port() -> FakeSessionPort:
    return FakeSessionPort()


@pytest.fixture
def repo() -> RepoRef:
    return RepoRef(owner="acme", name="repo")


def _now() -> datetime:
    return datetime.now(tz=UTC)


async def test_session_list_runs_returns_all(
    session_port: FakeSessionPort,
    repo: RepoRef,
) -> None:
    session_port.seed_run_summary("run-1", repo, "issues", "completed", _now())
    session_port.seed_run_summary("run-2", repo, "issues", "in_progress", _now())
    runs = await session_port.list_runs(repo)
    assert len(runs) == 2


async def test_session_list_runs_filters_by_status(
    session_port: FakeSessionPort,
    repo: RepoRef,
) -> None:
    session_port.seed_run_summary("run-1", repo, "issues", "completed", _now())
    session_port.seed_run_summary("run-2", repo, "issues", "in_progress", _now())
    runs = await session_port.list_runs(repo, status="completed")
    assert len(runs) == 1
    assert runs[0].run_id == "run-1"


async def test_session_list_runs_filters_by_type(
    session_port: FakeSessionPort,
    repo: RepoRef,
) -> None:
    session_port.seed_run_summary("run-1", repo, "issues", "completed", _now())
    session_port.seed_run_summary("run-2", repo, "issue_comment", "completed", _now())
    runs = await session_port.list_runs(repo, type="issue_comment")
    assert len(runs) == 1
    assert runs[0].run_id == "run-2"


async def test_session_get_run_returns_detail(
    session_port: FakeSessionPort,
    repo: RepoRef,
) -> None:
    now = _now()
    events = [RunEvent(event_type="queued", data={}, timestamp=now)]
    session_port.seed_run_summary("run-1", repo, "issues", "completed", now, events=events)
    detail = await session_port.get_run("run-1")
    assert detail.run_id == "run-1"
    assert detail.type == "issues"
    assert len(detail.events) == 1


async def test_session_get_run_missing_raises(
    session_port: FakeSessionPort,
) -> None:
    with pytest.raises(KeyError):
        await session_port.get_run("nonexistent")


async def test_session_stream_events_yields_all(
    session_port: FakeSessionPort,
    repo: RepoRef,
) -> None:
    now = _now()
    events = [
        RunEvent(event_type="queued", data={}, timestamp=now),
        RunEvent(event_type="in_progress", data={}, timestamp=now),
        RunEvent(event_type="completed", data={}, timestamp=now),
    ]
    session_port.seed_run_summary("run-1", repo, "issues", "completed", now, events=events)
    collected = []
    async for event in session_port.stream_events("run-1"):
        collected.append(event)
    assert len(collected) == 3
    assert collected[0].event_type == "queued"
    assert collected[-1].event_type == "completed"


async def test_session_cancel_updates_status(
    session_port: FakeSessionPort,
    repo: RepoRef,
) -> None:
    session_port.seed_run_summary("run-1", repo, "issues", "in_progress", _now())
    await session_port.cancel("run-1")
    detail = await session_port.get_run("run-1")
    assert detail.status == "cancelled"


async def test_session_intervene_records_call(
    session_port: FakeSessionPort,
    repo: RepoRef,
) -> None:
    session_port.seed_run_summary("run-1", repo, "issues", "in_progress", _now())
    await session_port.intervene("run-1", "Please stop")
    assert ("run-1", "Please stop") in session_port.intervene_calls


async def test_session_list_runs_filters_by_since(
    session_port: FakeSessionPort,
    repo: RepoRef,
) -> None:

    old_ts = datetime(2020, 1, 1, tzinfo=UTC)
    new_ts = datetime(2024, 1, 1, tzinfo=UTC)
    cutoff = datetime(2023, 1, 1, tzinfo=UTC)

    session_port.seed_run_summary("run-old", repo, "issues", "completed", old_ts)
    session_port.seed_run_summary("run-new", repo, "issues", "completed", new_ts)

    runs = await session_port.list_runs(repo, since=cutoff)
    assert len(runs) == 1
    assert runs[0].run_id == "run-new"
