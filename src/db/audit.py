"""SQLite audit log for intake decisions and operator actions (SPEC §10.4, §11.3)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiosqlite

from src.db import SharedDB, _make_shared_db, serialized_write
from src.domain.types import IssueRef, PRRef, RepoRef

_log = logging.getLogger(__name__)

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
    """SQLite-backed audit log.

    Accepts either a ``SharedDB`` (production, shared connections) or a plain
    path string (tests / legacy callers).  When passed a string, creates an
    internal ``SharedDB`` and owns its lifecycle.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._shared: SharedDB = _make_shared_db(db_path)
        # Track whether we own the SharedDB (created from a string path) so
        # close() tears it down correctly.  When a SharedDB is passed in, the
        # caller owns its lifecycle.
        self._owns_shared: bool = not isinstance(db_path, SharedDB)
        self._initialized = False

    async def init(self) -> None:
        """Open the database connection and create the table if absent."""
        if self._initialized:
            return
        await self._shared.init()
        wc = self._shared.write
        wc.row_factory = aiosqlite.Row
        await wc.execute(_CREATE_TABLE)
        # Idempotent column additions for existing schemas (no-op on fresh tables).
        for alter in (_ALTER_ADD_ESCALATION_CAUSE, _ALTER_ADD_PR_LABELS):
            try:
                await wc.execute(alter)
            except Exception:
                pass  # column already exists
        await wc.commit()
        # Mirror row_factory on read connection (may differ from write for file DB).
        self._shared.read.row_factory = aiosqlite.Row
        self._initialized = True

    @property
    def _db(self) -> aiosqlite.Connection | None:
        """Legacy compat: expose the write connection as ``_db`` so existing
        WAL/busy_timeout tests (``store._db``) keep working."""
        return self._shared._write

    @property
    def _conn(self) -> aiosqlite.Connection:
        if not self._initialized:
            raise RuntimeError("AuditLog.init() must be called before use")
        return self._shared.write

    @property
    def _read_conn(self) -> aiosqlite.Connection:
        if not self._initialized:
            raise RuntimeError("AuditLog.init() must be called before use")
        return self._shared.read

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

        Resilient: the write goes through the single shared write connection
        (serialised by aiosqlite's background thread) plus belt-and-suspenders
        retry via ``@serialized_write``.  Persistent failures are logged rather
        than re-raised so a DB hiccup never propagates as a 500 to the webhook
        caller.
        """
        ts = datetime.now(tz=UTC).isoformat()
        if isinstance(entity_ref, IssueRef):
            entity_type = "issue"
            entity_number = entity_ref.number
        else:
            entity_type = "pr"
            entity_number = entity_ref.number

        pr_labels_str = ",".join(pr_labels) if pr_labels is not None else None
        params = (
            ts,
            repo.owner,
            repo.name,
            entity_type,
            entity_number,
            action,
            operator,
            escalation_cause,
            pr_labels_str,
        )

        try:
            await self._write_audit_row(params)
        except Exception:
            # A persistent DB failure must not 500 the webhook path (issue #109).
            # The write is already retried inside _write_audit_row via
            # @serialized_write; if all retries fail we log and continue.
            _log.exception(
                "audit.record failed (action=%r, repo=%s/%s) — audit row dropped",
                action,
                repo.owner,
                repo.name,
            )

    @serialized_write
    async def _write_audit_row(self, params: tuple[object, ...]) -> None:
        """Inner write helper wrapped with belt-and-suspenders retry."""
        await self._conn.execute(
            """
            INSERT INTO audit_events
                (ts, repo_owner, repo_name, entity_type, entity_number,
                 action, operator, escalation_cause, pr_labels)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
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
            async with self._read_conn.execute(
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
            async with self._read_conn.execute(
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
        if self._owns_shared:
            await self._shared.close()
        self._initialized = False
