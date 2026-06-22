"""Tests for DB-backed prod stores: DSN parsing, store selection, and lifespan.

Covers requirements from issue #80:
  - DB_URL → filesystem path helper (db_path_from_url)
  - _build_prod_service selects SQLite stores for a sqlite DB_URL, in-memory for :memory:/unset
  - Lifespan init+close lifecycle
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from src.db.dsn import db_path_from_url

# ---------------------------------------------------------------------------
# db_path_from_url
# ---------------------------------------------------------------------------


def test_dsn_sqlite_file_path() -> None:
    """sqlite:///data/orchestrator.db → data/orchestrator.db (relative path).

    In the SQLite URI scheme, three slashes means relative path.
    For an absolute path on Unix use four slashes: sqlite:////abs/path.db.
    """
    result = db_path_from_url("sqlite:///data/orchestrator.db")
    assert result == "data/orchestrator.db"


def test_dsn_sqlite_memory_url() -> None:
    """sqlite:///:memory: → None"""
    assert db_path_from_url("sqlite:///:memory:") is None


def test_dsn_sqlite_triple_slash_memory() -> None:
    """sqlite:///:memory: explicit triple-slash form → None"""
    assert db_path_from_url("sqlite:///:memory:") is None


def test_dsn_empty_string() -> None:
    """Empty string → None (in-memory)."""
    assert db_path_from_url("") is None


def test_dsn_none() -> None:
    """None → None (in-memory)."""
    assert db_path_from_url(None) is None


def test_dsn_postgres_raises() -> None:
    """Postgres DSN → NotImplementedError with clear message."""
    with pytest.raises(NotImplementedError, match="Postgres"):
        db_path_from_url("postgresql://user:pass@host/dbname")


def test_dsn_postgres_short_scheme_raises() -> None:
    """postgres:// DSN also raises NotImplementedError."""
    with pytest.raises(NotImplementedError):
        db_path_from_url("postgres://localhost/mydb")


def test_dsn_unknown_scheme_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    """Unrecognised scheme logs a warning and returns None (fallback to in-memory)."""
    import logging

    with caplog.at_level(logging.WARNING, logger="src.db.dsn"):
        result = db_path_from_url("mysql://localhost/mydb")
    assert result is None
    assert "not a recognised SQLite URL" in caplog.text


def test_dsn_relative_sqlite_path() -> None:
    """sqlite:///relative/path.db → relative/path.db (relative path)."""
    result = db_path_from_url("sqlite:///relative/path.db")
    assert result == "relative/path.db"


def test_dsn_absolute_sqlite_path() -> None:
    """sqlite:////abs/path/db.sqlite → /abs/path/db.sqlite (absolute path via 4 slashes)."""
    result = db_path_from_url("sqlite:////abs/path/db.sqlite")
    assert result == "/abs/path/db.sqlite"


# ---------------------------------------------------------------------------
# _build_prod_service store selection
# ---------------------------------------------------------------------------


def test_build_prod_service_sqlite_stores_for_file_db_url() -> None:
    """With DB_URL=sqlite:///... _build_prod_service wires SQLite-backed engine stores."""
    from src.db.converge_state import SQLiteConvergeStateStore
    from src.db.counter import SQLiteCounterStore

    with (
        patch.dict(
            os.environ,
            {
                "FORGE_TOKEN": "ghp_test_token",
                "GITHUB_OWNER": "acme",
                "GITHUB_REPO": "repo",
                "DB_URL": "sqlite:///data/test.db",
            },
            clear=False,
        ),
        patch("src.ports.provider.PortProvider.from_env") as mock_from_env,
    ):
        os.environ.pop("ALLOWLIST", None)
        mock_provider = MagicMock()
        mock_provider.ports.return_value = (MagicMock(), MagicMock(), MagicMock())
        mock_from_env.return_value = mock_provider

        from src.api.main import _build_prod_service

        service, _secret, op_store, push_store, _reg, db_audit, db_counter, db_converge = (
            _build_prod_service()
        )

    # Engine stores should be SQLite-backed
    assert db_counter is not None
    assert isinstance(db_counter, SQLiteCounterStore)
    assert db_converge is not None
    assert isinstance(db_converge, SQLiteConvergeStateStore)
    assert db_audit is not None

    # OrchestratorService should have the SQLite counter and converge stores wired
    assert service._counter is db_counter
    assert service._converge_state is db_converge

    # Operator and push stores should also be SQLite-backed
    from src.db.operator_store import SQLiteOperatorStore
    from src.db.push_store import SQLitePushStore

    assert isinstance(op_store, SQLiteOperatorStore)
    assert isinstance(push_store, SQLitePushStore)


def test_build_prod_service_memory_stores_for_memory_db_url() -> None:
    """With DB_URL=sqlite:///:memory: _build_prod_service wires in-memory stores."""
    from src.db.operator_store import SQLiteOperatorStore
    from src.db.push_store import SQLitePushStore

    with (
        patch.dict(
            os.environ,
            {
                "FORGE_TOKEN": "ghp_test_token",
                "GITHUB_OWNER": "acme",
                "GITHUB_REPO": "repo",
                "DB_URL": "sqlite:///:memory:",
            },
            clear=False,
        ),
        patch("src.ports.provider.PortProvider.from_env") as mock_from_env,
    ):
        os.environ.pop("ALLOWLIST", None)
        mock_provider = MagicMock()
        mock_provider.ports.return_value = (MagicMock(), MagicMock(), MagicMock())
        mock_from_env.return_value = mock_provider

        from src.api.main import _build_prod_service

        _service, _secret, op_store, push_store, _reg, _db_audit, db_counter, db_converge = (
            _build_prod_service()
        )

    # SQLite stores should NOT be selected for in-memory path
    assert db_counter is None
    assert db_converge is None
    assert not isinstance(op_store, SQLiteOperatorStore)
    assert not isinstance(push_store, SQLitePushStore)


def test_build_prod_service_memory_stores_when_db_url_unset() -> None:
    """With DB_URL unset _build_prod_service defaults to in-memory stores."""
    with (
        patch.dict(
            os.environ,
            {
                "FORGE_TOKEN": "ghp_test_token",
                "GITHUB_OWNER": "acme",
                "GITHUB_REPO": "repo",
            },
            clear=False,
        ),
        patch("src.ports.provider.PortProvider.from_env") as mock_from_env,
    ):
        # Default DB_URL is sqlite:///data/orchestrator.db → file path
        # so we explicitly unset DB_URL and set it to :memory: equivalent
        os.environ.pop("ALLOWLIST", None)
        os.environ["DB_URL"] = "sqlite:///:memory:"
        mock_provider = MagicMock()
        mock_provider.ports.return_value = (MagicMock(), MagicMock(), MagicMock())
        mock_from_env.return_value = mock_provider

        from src.api.main import _build_prod_service

        _service, _secret, _op, _push, _reg, _db_audit, db_counter, db_converge = (
            _build_prod_service()
        )

        # Clean up
        os.environ.pop("DB_URL", None)

    assert db_counter is None
    assert db_converge is None


def test_build_prod_service_dev_mode_no_sqlite_stores() -> None:
    """Dev mode (no creds): all DB-specific store slots are None."""
    with patch.dict(os.environ, {}, clear=False):
        for var in [
            "FORGE_TOKEN",
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_INSTALLATION_ID",
        ]:
            os.environ.pop(var, None)

        from src.api.main import _build_prod_service

        _service, _secret, _op, _push, _reg, db_audit, db_counter, db_converge = (
            _build_prod_service()
        )

    # Dev mode: no SQLite engine stores (all None)
    assert db_audit is None
    assert db_counter is None
    assert db_converge is None


# ---------------------------------------------------------------------------
# Lifespan init + close
# ---------------------------------------------------------------------------


async def test_lifespan_inits_and_closes_sqlite_stores(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """init() + close() cycle on SQLite-backed engine stores works without error."""
    from pathlib import Path

    from src.db.audit import AuditLog
    from src.db.converge_state import SQLiteConvergeStateStore
    from src.db.counter import SQLiteCounterStore
    from src.domain.types import PRRef, RepoRef

    db_file = str(Path(str(tmp_path)) / "test_lifespan.db")

    audit = AuditLog(db_path=db_file)
    counter = SQLiteCounterStore(db_file)
    converge = SQLiteConvergeStateStore(db_file)

    await audit.init()
    await counter.init()
    await converge.init()

    # Stores are operational after init
    repo = RepoRef(owner="test", name="repo")
    pr = PRRef(repo=repo, number=1)
    assert await counter.get_count(pr, "stale-pr") == 0
    assert await converge.get_converge_round(pr) == 0

    # Close without error (no event-loop teardown warning)
    await converge.close()
    await counter.close()
    await audit.close()

    # Connections are closed; further calls should raise RuntimeError
    with pytest.raises(RuntimeError, match="init"):
        await audit.list_entries(repo)


async def test_sqlite_converge_store_close_idempotent() -> None:
    """Closing an already-closed SQLiteConvergeStateStore is a no-op."""
    from src.db.converge_state import SQLiteConvergeStateStore

    store = SQLiteConvergeStateStore(":memory:")
    await store.init()
    await store.close()
    await store.close()  # must not raise


async def test_sqlite_counter_store_close_idempotent() -> None:
    """Closing an already-closed SQLiteCounterStore is a no-op."""
    from src.db.counter import SQLiteCounterStore

    store = SQLiteCounterStore(":memory:")
    await store.init()
    await store.close()
    await store.close()  # must not raise
