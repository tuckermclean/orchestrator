"""SQLiteRunStore and FakeRunStore — run metadata store.

Records run metadata at dispatch time (run_id, repo, type, model, status,
started_at) and reflects status updates from the harness RunEventStore.

Design:
  - Single source of truth: run metadata is written here at dispatch; status
    is updated here as the run progresses.
  - FakeRunStore (below) is the in-memory counterpart used in tests and dev.
  - Both implement the same duck-typed interface so OrchestratorService
    can swap between them without branching logic.
  - SQLiteRunStore is selected in _build_prod_service when DB_URL names a
    file path; FakeRunStore is used otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime  # noqa: TC003

import aiosqlite

from src.db import configure_sqlite_connection, serialized_write
from src.domain.types import RepoRef, RunDetail, RunEvent, RunSummary

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT    PRIMARY KEY,
    repo_owner   TEXT    NOT NULL,
    repo_name    TEXT    NOT NULL,
    type         TEXT    NOT NULL DEFAULT 'dispatch',
    status       TEXT    NOT NULL DEFAULT 'queued',
    model        TEXT    NOT NULL DEFAULT '',
    started_at   TEXT    NOT NULL,
    completed_at TEXT
)
"""

_CREATE_EVENTS = """
CREATE TABLE IF NOT EXISTS run_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT    NOT NULL REFERENCES runs(run_id),
    event_type   TEXT    NOT NULL,
    event_data   TEXT    NOT NULL DEFAULT '{}',
    ts           TEXT    NOT NULL
)
"""


# ---------------------------------------------------------------------------
# FakeRunStore — in-memory; used in tests and dev/CI
# ---------------------------------------------------------------------------


class FakeRunStore:
    """In-memory run metadata store.

    Satisfies the same duck-typed interface as SQLiteRunStore so
    OrchestratorService can use either without branching.
    Tests and the dev service use this.
    """

    def __init__(self) -> None:
        self._summaries: dict[str, RunSummary] = {}
        self._events: dict[str, list[RunEvent]] = {}

    def record(
        self,
        run_id: str,
        repo: RepoRef,
        *,
        type: str,
        model: str,
        started_at: datetime,
    ) -> None:
        """Record a newly dispatched run (called at dispatch time, sync)."""
        self._summaries[run_id] = RunSummary(
            run_id=run_id,
            repo=repo,
            type=type,
            status="queued",
            started_at=started_at,
        )
        self._events[run_id] = []

    def set_status(self, run_id: str, status: str, completed_at: datetime | None = None) -> None:
        """Update run status (called by harness event-store integration, sync)."""
        existing = self._summaries.get(run_id)
        if existing is None:
            return
        self._summaries[run_id] = existing.model_copy(
            update={"status": status, "completed_at": completed_at}
        )

    def append_event(self, run_id: str, event: RunEvent) -> None:
        """Append a run event (sync)."""
        if run_id not in self._events:
            self._events[run_id] = []
        self._events[run_id].append(event)

    async def list_runs(
        self,
        repo: RepoRef,
        since: datetime | None = None,
        status: str | None = None,
        type: str | None = None,
    ) -> list[RunSummary]:
        result = []
        for summary in self._summaries.values():
            if summary.repo.owner != repo.owner or summary.repo.name != repo.name:
                continue
            if since is not None and summary.started_at < since:
                continue
            if status is not None and summary.status != status:
                continue
            if type is not None and summary.type != type:
                continue
            result.append(summary)
        return result

    async def get_run(self, run_id: str) -> RunDetail | None:
        summary = self._summaries.get(run_id)
        if summary is None:
            return None
        events = list(self._events.get(run_id, []))
        return RunDetail(
            run_id=summary.run_id,
            repo=summary.repo,
            type=summary.type,
            status=summary.status,
            started_at=summary.started_at,
            completed_at=summary.completed_at,
            events=events,
        )

    async def init(self) -> None:
        """No-op — in-memory store needs no initialisation."""

    async def close(self) -> None:
        """No-op — in-memory store has no connection to close."""


# ---------------------------------------------------------------------------
# SQLiteRunStore — file-backed; used in prod when DB_URL names a file path
# ---------------------------------------------------------------------------


class SQLiteRunStore:
    """SQLite-backed run metadata store.

    Schema: two tables — runs (one row per run) and run_events (one row per
    event).  Status updates are applied via UPDATE.  Events are appended.

    The record() / set_status() / append_event() methods are synchronous
    (matching FakeRunStore's interface) and schedule their async writes via
    _spawn(), which keeps a strong reference to each task (so it can't be GC'd
    mid-write) and logs failures — safe to call from fire-and-forget dispatch.

    Callers must call init() before use and close() on shutdown.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        # Keep strong refs to fire-and-forget write tasks: an unreferenced
        # asyncio task can be garbage-collected mid-flight, silently dropping
        # the DB write (run record / status update). Discard on completion and
        # log any exception rather than swallowing it.
        self._tasks: set[asyncio.Task[None]] = set()

    def _spawn(self, coro: Coroutine[object, object, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and (exc := t.exception()) is not None:
                _log.error("SQLiteRunStore async write failed: %r", exc)

        task.add_done_callback(_done)

    async def init(self) -> None:
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self._db_path)
        await configure_sqlite_connection(self._db)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(_CREATE_RUNS)
        await self._db.execute(_CREATE_EVENTS)
        await self._db.commit()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SQLiteRunStore.init() must be called before use")
        return self._db

    def record(
        self,
        run_id: str,
        repo: RepoRef,
        *,
        type: str,
        model: str,
        started_at: datetime,
    ) -> None:
        """Schedule async INSERT for the new run."""
        self._spawn(
            self._record_async(
                run_id=run_id,
                repo=repo,
                type=type,
                model=model,
                started_at=started_at,
            )
        )

    @serialized_write
    async def _record_async(
        self,
        run_id: str,
        repo: RepoRef,
        *,
        type: str,
        model: str,
        started_at: datetime,
    ) -> None:
        await self._conn.execute(
            """
            INSERT OR IGNORE INTO runs
                (run_id, repo_owner, repo_name, type, status, model, started_at)
            VALUES (?, ?, ?, ?, 'queued', ?, ?)
            """,
            (run_id, repo.owner, repo.name, type, model, started_at.isoformat()),
        )
        await self._conn.commit()

    def set_status(self, run_id: str, status: str, completed_at: datetime | None = None) -> None:
        """Schedule async UPDATE for the run status."""
        self._spawn(self._set_status_async(run_id, status, completed_at))

    @serialized_write
    async def _set_status_async(
        self,
        run_id: str,
        status: str,
        completed_at: datetime | None,
    ) -> None:
        completed_str = completed_at.isoformat() if completed_at is not None else None
        await self._conn.execute(
            "UPDATE runs SET status = ?, completed_at = ? WHERE run_id = ?",
            (status, completed_str, run_id),
        )
        await self._conn.commit()

    def append_event(self, run_id: str, event: RunEvent) -> None:
        """Schedule async INSERT for the event."""
        self._spawn(
            self._append_event_async(
                run_id=run_id,
                event_type=event.event_type,
                event_data=json.dumps(event.data),
                ts=event.timestamp.isoformat(),
            )
        )

    @serialized_write
    async def _append_event_async(
        self,
        run_id: str,
        event_type: str,
        event_data: str,
        ts: str,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO run_events (run_id, event_type, event_data, ts) VALUES (?, ?, ?, ?)",
            (run_id, event_type, event_data, ts),
        )
        await self._conn.commit()

    async def list_runs(
        self,
        repo: RepoRef,
        since: datetime | None = None,
        status: str | None = None,
        type: str | None = None,
    ) -> list[RunSummary]:
        clauses = ["repo_owner = ? AND repo_name = ?"]
        params: list[object] = [repo.owner, repo.name]
        if since is not None:
            clauses.append("started_at >= ?")
            params.append(since.isoformat())
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if type is not None:
            clauses.append("type = ?")
            params.append(type)
        where = " AND ".join(clauses)
        async with self._conn.execute(
            f"SELECT * FROM runs WHERE {where} ORDER BY started_at DESC",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            completed_at = (
                datetime.fromisoformat(str(row["completed_at"])).replace(tzinfo=UTC)
                if row["completed_at"]
                else None
            )
            result.append(
                RunSummary(
                    run_id=str(row["run_id"]),
                    repo=RepoRef(owner=str(row["repo_owner"]), name=str(row["repo_name"])),
                    type=str(row["type"]),
                    status=str(row["status"]),
                    started_at=datetime.fromisoformat(str(row["started_at"])).replace(tzinfo=UTC),
                    completed_at=completed_at,
                )
            )
        return result

    async def get_run(self, run_id: str) -> RunDetail | None:
        async with self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        completed_at = (
            datetime.fromisoformat(str(row["completed_at"])).replace(tzinfo=UTC)
            if row["completed_at"]
            else None
        )
        async with self._conn.execute(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
        ) as cursor:
            event_rows = await cursor.fetchall()
        events = [
            RunEvent(
                event_type=str(er["event_type"]),
                data=json.loads(str(er["event_data"])),
                timestamp=datetime.fromisoformat(str(er["ts"])).replace(tzinfo=UTC),
            )
            for er in event_rows
        ]
        return RunDetail(
            run_id=str(row["run_id"]),
            repo=RepoRef(owner=str(row["repo_owner"]), name=str(row["repo_name"])),
            type=str(row["type"]),
            status=str(row["status"]),
            started_at=datetime.fromisoformat(str(row["started_at"])).replace(tzinfo=UTC),
            completed_at=completed_at,
            events=events,
        )

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def iter_events(self, run_id: str) -> AsyncIterator[RunEvent]:
        """Async iterator over stored events (for SSE streaming from DB)."""
        async with self._conn.execute(
            "SELECT * FROM run_events WHERE run_id = ? ORDER BY id", (run_id,)
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            yield RunEvent(
                event_type=str(row["event_type"]),
                data=json.loads(str(row["event_data"])),
                timestamp=datetime.fromisoformat(str(row["ts"])).replace(tzinfo=UTC),
            )
