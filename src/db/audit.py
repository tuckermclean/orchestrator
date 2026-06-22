"""SQLite audit log for intake decisions and operator actions (SPEC §10.4, §11.3)."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite

from src.domain.types import IssueRef, PRRef, RepoRef

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS audit_events (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT    NOT NULL,
    repo_owner              TEXT    NOT NULL,
    repo_name               TEXT    NOT NULL,
    entity_type             TEXT    NOT NULL,
    entity_number           INTEGER NOT NULL,
    action                  TEXT    NOT NULL,
    operator                TEXT,
    escalation_cause        TEXT,
    pr_labels               TEXT
)
"""

# Migration: add columns to existing tables created before the schema extension.
_ALTER_ADD_ESCALATION_CAUSE = (
    "ALTER TABLE audit_events ADD COLUMN escalation_cause TEXT"
)
_ALTER_ADD_PR_LABELS = (
    "ALTER TABLE audit_events ADD COLUMN pr_labels TEXT"
)


class AuditLog:
    """SQLite-backed audit log."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the database connection and create the table if absent."""
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(_CREATE_TABLE)
        # Idempotent column additions for existing schemas (no-op on fresh tables).
        for alter in (_ALTER_ADD_ESCALATION_CAUSE, _ALTER_ADD_PR_LABELS):
            try:
                await self._db.execute(alter)
            except Exception:
                pass  # column already exists
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
        escalation_cause: str | None = None,
        pr_labels: list[str] | None = None,
    ) -> None:
        """Append one audit record (I6, §11.3).

        Optional structured fields:
          - ``escalation_cause``: §6 E-code or None (for deescalate_pr records).
          - ``pr_labels``: label snapshot at the time of the audit event (stored as
            comma-separated string for forensic inspection).

        Callers other than ``deescalate_pr`` (intake, promote, decline) pass neither
        optional field; the schema extension is backward-compatible.
        """
        ts = datetime.now(tz=UTC).isoformat()
        if isinstance(entity_ref, IssueRef):
            entity_type = "issue"
            entity_number = entity_ref.number
        else:
            entity_type = "pr"
            entity_number = entity_ref.number

        pr_labels_str = ",".join(pr_labels) if pr_labels is not None else None

        await self._conn.execute(
            """
            INSERT INTO audit_events
                (ts, repo_owner, repo_name, entity_type, entity_number,
                 action, operator, escalation_cause, pr_labels)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                repo.owner,
                repo.name,
                entity_type,
                entity_number,
                action,
                operator,
                escalation_cause,
                pr_labels_str,
            ),
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
