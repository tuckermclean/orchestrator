"""Advisory lock implementations (SPEC §11.3).

Single-process default: ``AsyncioLockProvider`` — one ``asyncio.Lock`` per entity key.

Multi-replica seam: replace with a DB-backed provider that wraps Postgres
``pg_advisory_xact_lock(hash_key)`` inside an async context manager.  The
interface is identical; only the constructor changes.  All replicas may accept
requests; the DB lock serializes per-entity work (SPEC §11.3 step 4).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from src.domain.types import IssueRef, PRRef


def _entity_key(entity_ref: IssueRef | PRRef) -> str:
    """Stable string key for an entity reference.

    Issue:  ``issue:owner/name#42``
    PR:     ``pr:owner/name!42``
    """
    repo = entity_ref.repo
    base = f"{repo.owner}/{repo.name}"
    if isinstance(entity_ref, IssueRef):
        return f"issue:{base}#{entity_ref.number}"
    return f"pr:{base}!{entity_ref.number}"


class AsyncioLockProvider:
    """Single-process advisory lock backed by one ``asyncio.Lock`` per entity key.

    Suitable for single-instance deployments.  For horizontal scaling (multiple
    replicas), swap this out for a Postgres-backed provider that uses
    ``pg_advisory_xact_lock`` — the ``lock()`` seam is the same.

    Thread safety: ``asyncio.Lock`` objects are not thread-safe.  This provider
    assumes a single event loop (standard asyncio service model).
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    def lock(self, entity_ref: IssueRef | PRRef) -> AbstractAsyncContextManager[None]:
        return self._acquire(entity_ref)

    @asynccontextmanager
    async def _acquire(self, entity_ref: IssueRef | PRRef) -> AsyncGenerator[None, None]:
        key = _entity_key(entity_ref)
        async with self._meta_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            entity_lock = self._locks[key]
        async with entity_lock:
            yield
