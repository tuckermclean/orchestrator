"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from src.db import reset_write_lock
from src.ports.fakes import FakeForgePort, FakeHarnessPort, FakeSessionPort


@pytest.fixture(autouse=True)
def _reset_db_write_lock() -> None:
    """Reset the process-wide SQLite write lock after each test.

    ``_db_write_lock`` in ``src.db`` is created lazily and bound to the running
    asyncio event loop.  pytest-asyncio uses a fresh loop per test, so the lock
    must be discarded between tests — otherwise the next test's coroutines
    attempt to acquire a lock bound to a stale (closed) loop and raise
    ``RuntimeError: … is bound to a different event loop``.
    """
    yield
    reset_write_lock()


@pytest.fixture
def forge_port() -> FakeForgePort:
    return FakeForgePort()


@pytest.fixture
def harness_port() -> FakeHarnessPort:
    return FakeHarnessPort()


@pytest.fixture
def session_port() -> FakeSessionPort:
    return FakeSessionPort()
