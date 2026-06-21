"""SQLite audit log for intake decisions and operator actions (SPEC §10.4, §11.3)."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from src.domain.types import IssueRef, PRRef, RepoRef

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    repo_owner    TEXT    NOT NULL,
    repo_name     TEXT    NOT NULL,
    entity_type   TEXT    NOT NULL,
    entity_number INTEGER NOT NULL,
    action        TEXT    NOT NULL,
    operator      TEXT
)
"""


class AuditLog:
    """SQLite-backed audit log."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the database connection and create the table if absent."""
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(_CREATE_TABLE)
        await self._db.commit()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("AuditLog.init() must be called before use")
        return self._db

    async def record(
        self,
        repo: RepoRef,
        entity_ref: IssueRef | PRRef,
        action: str,
        operator: str | None = None,
    ) -> None:
        """Append one audit record (I6)."""
        ts = datetime.now(tz=UTC).isoformat()
        if isinstance(entity_ref, IssueRef):
            entity_type = "issue"
            entity_number = entity_ref.number
        else:
            entity_type = "pr"
            entity_number = entity_ref.number

        await self._conn.execute(
            """
            INSERT INTO audit_events
                (ts, repo_owner, repo_name, entity_type, entity_number, action, operator)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, repo.owner, repo.name, entity_type, entity_number, action, operator),
        )
        await self._conn.commit()

    async def list_entries(
        self,
        repo: RepoRef,
        entity_ref: IssueRef | PRRef | None = None,
    ) -> list[dict[str, object]]:
        """Return audit entries for a repo, optionally filtered by entity."""
        if entity_ref is not None:
            if isinstance(entity_ref, IssueRef):
                entity_type = "issue"
                entity_number = entity_ref.number
            else:
                entity_type = "pr"
                entity_number = entity_ref.number
            async with self._conn.execute(
                """
                SELECT * FROM audit_events
                WHERE repo_owner = ? AND repo_name = ?
                  AND entity_type = ? AND entity_number = ?
                ORDER BY id
                """,
                (repo.owner, repo.name, entity_type, entity_number),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self._conn.execute(
                """
                SELECT * FROM audit_events
                WHERE repo_owner = ? AND repo_name = ?
                ORDER BY id
                """,
                (repo.owner, repo.name),
            ) as cursor:
                rows = await cursor.fetchall()

        return [dict(row) for row in rows]

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
