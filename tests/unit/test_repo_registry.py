"""Unit tests for the repo registry (issue #49 — multi-repo support).

Covers:
- RepoConfig defaults and custom values
- FakeRepoRegistry CRUD and lookup
- EnvRepoRegistry from_env: single-repo, REPOS_JSON multi-repo, backward compat
- _parse_repos_json edge cases
- SQLiteRepoRegistry CRUD and lookup
"""

from __future__ import annotations

import json

import pytest

from src.domain.types import RepoRef
from src.service.registry import (
    EnvRepoRegistry,
    FakeRepoRegistry,
    RepoConfig,
    SQLiteRepoRegistry,
    _parse_repos_json,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ACME_API = RepoRef(owner="acme", name="api")
_ACME_UI = RepoRef(owner="acme", name="ui")


def _cfg(repo: RepoRef, **kwargs: object) -> RepoConfig:
    return RepoConfig(repo=repo, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# RepoConfig defaults
# ---------------------------------------------------------------------------


def test_repo_config_defaults() -> None:
    """RepoConfig defaults: enabled=True, intake_enabled=True, empty allowlist."""
    cfg = RepoConfig(repo=_ACME_API)
    assert cfg.enabled is True
    assert cfg.intake_enabled is True
    assert cfg.allowlist == []


def test_repo_config_custom_values() -> None:
    """RepoConfig accepts custom values."""
    cfg = RepoConfig(
        repo=_ACME_API,
        enabled=False,
        intake_enabled=False,
        allowlist=["alice", "bob"],
    )
    assert cfg.enabled is False
    assert cfg.intake_enabled is False
    assert cfg.allowlist == ["alice", "bob"]


def test_repo_config_equality() -> None:
    """Two RepoConfig objects with identical values are equal."""
    a = RepoConfig(repo=_ACME_API, allowlist=["alice"])
    b = RepoConfig(repo=_ACME_API, allowlist=["alice"])
    assert a == b


def test_repo_config_inequality_different_allowlist() -> None:
    """RepoConfigs with different allowlists are not equal."""
    a = RepoConfig(repo=_ACME_API, allowlist=["alice"])
    b = RepoConfig(repo=_ACME_API, allowlist=["bob"])
    assert a != b


# ---------------------------------------------------------------------------
# FakeRepoRegistry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fake_registry_empty_list() -> None:
    """Empty FakeRepoRegistry returns empty list."""
    reg = FakeRepoRegistry()
    assert await reg.list_repos() == []
    assert await reg.enabled_repos() == []


@pytest.mark.asyncio
async def test_fake_registry_seed_and_lookup() -> None:
    """FakeRepoRegistry pre-seeded configs are returned by get_repo."""
    cfg = _cfg(_ACME_API, allowlist=["alice"])
    reg = FakeRepoRegistry([cfg])
    result = await reg.get_repo(_ACME_API)
    assert result is not None
    assert result.allowlist == ["alice"]


@pytest.mark.asyncio
async def test_fake_registry_get_unknown_returns_none() -> None:
    """get_repo returns None for an unregistered repo."""
    reg = FakeRepoRegistry()
    assert await reg.get_repo(_ACME_API) is None


@pytest.mark.asyncio
async def test_fake_registry_upsert_insert() -> None:
    """upsert_repo inserts a new config."""
    reg = FakeRepoRegistry()
    cfg = _cfg(_ACME_API)
    await reg.upsert_repo(cfg)
    assert await reg.get_repo(_ACME_API) is not None


@pytest.mark.asyncio
async def test_fake_registry_upsert_update() -> None:
    """upsert_repo replaces an existing config."""
    reg = FakeRepoRegistry([_cfg(_ACME_API, allowlist=["alice"])])
    updated = _cfg(_ACME_API, allowlist=["bob"])
    await reg.upsert_repo(updated)
    result = await reg.get_repo(_ACME_API)
    assert result is not None
    assert result.allowlist == ["bob"]


@pytest.mark.asyncio
async def test_fake_registry_enabled_repos_filter() -> None:
    """enabled_repos returns only repos with enabled=True."""
    reg = FakeRepoRegistry([
        _cfg(_ACME_API, enabled=True),
        _cfg(_ACME_UI, enabled=False),
    ])
    enabled = await reg.enabled_repos()
    assert len(enabled) == 1
    assert enabled[0].repo == _ACME_API


@pytest.mark.asyncio
async def test_fake_registry_preserves_insertion_order() -> None:
    """list_repos preserves insertion order."""
    reg = FakeRepoRegistry()
    await reg.upsert_repo(_cfg(_ACME_API))
    await reg.upsert_repo(_cfg(_ACME_UI))
    repos = await reg.list_repos()
    assert repos[0].repo == _ACME_API
    assert repos[1].repo == _ACME_UI


# ---------------------------------------------------------------------------
# EnvRepoRegistry.from_env — single-repo backward compat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_registry_single_repo_backward_compat() -> None:
    """EnvRepoRegistry builds a one-entry registry from GITHUB_OWNER/REPO/ALLOWLIST."""
    reg = EnvRepoRegistry.from_env(
        github_owner="acme",
        github_repo="api",
        allowlist_raw="alice,bob",
    )
    repos = await reg.list_repos()
    assert len(repos) == 1
    assert repos[0].repo == _ACME_API
    assert repos[0].allowlist == ["alice", "bob"]
    assert repos[0].enabled is True


@pytest.mark.asyncio
async def test_env_registry_single_repo_empty_allowlist() -> None:
    """Single-repo env registry with no ALLOWLIST: empty allowlist → owner-only default-deny."""
    reg = EnvRepoRegistry.from_env(
        github_owner="acme",
        github_repo="api",
        allowlist_raw="",
    )
    repos = await reg.list_repos()
    assert repos[0].allowlist == []


@pytest.mark.asyncio
async def test_env_registry_single_repo_strips_whitespace() -> None:
    """ALLOWLIST whitespace is stripped in single-repo env mode."""
    reg = EnvRepoRegistry.from_env(
        github_owner="acme",
        github_repo="api",
        allowlist_raw=" alice , bob , ",
    )
    repos = await reg.list_repos()
    assert sorted(repos[0].allowlist) == ["alice", "bob"]


@pytest.mark.asyncio
async def test_env_registry_no_owner_no_repo_empty() -> None:
    """No GITHUB_OWNER or GITHUB_REPO → empty registry (dev mode)."""
    reg = EnvRepoRegistry.from_env()
    assert await reg.list_repos() == []
    assert await reg.enabled_repos() == []


# ---------------------------------------------------------------------------
# EnvRepoRegistry.from_env — REPOS_JSON multi-repo mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_registry_repos_json_multi_repo() -> None:
    """REPOS_JSON builds a registry with multiple repos."""
    repos_json = json.dumps([
        {"owner": "acme", "name": "api", "allowlist": ["alice"]},
        {"owner": "acme", "name": "ui",  "enabled": False},
    ])
    reg = EnvRepoRegistry.from_env(repos_json=repos_json)
    repos = await reg.list_repos()
    assert len(repos) == 2
    assert repos[0].repo == _ACME_API
    assert repos[0].allowlist == ["alice"]
    assert repos[1].repo == _ACME_UI
    assert repos[1].enabled is False


@pytest.mark.asyncio
async def test_env_registry_repos_json_takes_precedence() -> None:
    """REPOS_JSON is used even when GITHUB_OWNER/REPO are also set."""
    repos_json = json.dumps([{"owner": "acme", "name": "api"}])
    reg = EnvRepoRegistry.from_env(
        repos_json=repos_json,
        github_owner="other",
        github_repo="other",
    )
    repos = await reg.list_repos()
    assert len(repos) == 1
    assert repos[0].repo.owner == "acme"


@pytest.mark.asyncio
async def test_env_registry_repos_json_enabled_filter() -> None:
    """enabled_repos returns only repos with enabled=True in REPOS_JSON mode."""
    repos_json = json.dumps([
        {"owner": "acme", "name": "api", "enabled": True},
        {"owner": "acme", "name": "ui",  "enabled": False},
    ])
    reg = EnvRepoRegistry.from_env(repos_json=repos_json)
    enabled = await reg.enabled_repos()
    assert len(enabled) == 1
    assert enabled[0].repo.name == "api"


@pytest.mark.asyncio
async def test_env_registry_repos_json_ignores_required_checks() -> None:
    """REPOS_JSON with required_checks key is silently accepted (forward-compat).

    The required_checks concept was removed; the key is now silently ignored.
    The CI gate trusts the repo's actual check runs instead.
    """
    repos_json = json.dumps([
        {"owner": "acme", "name": "api", "required_checks": ["Type Check", "Lint"]},
    ])
    reg = EnvRepoRegistry.from_env(repos_json=repos_json)
    repos = await reg.list_repos()
    # required_checks field no longer exists; the repo parses without error.
    assert repos[0].repo.name == "api"
    assert not hasattr(repos[0], "required_checks")


# ---------------------------------------------------------------------------
# _parse_repos_json edge cases
# ---------------------------------------------------------------------------


def test_parse_repos_json_invalid_not_array() -> None:
    """_parse_repos_json raises ValueError when input is not a JSON array."""
    with pytest.raises(ValueError, match="JSON array"):
        _parse_repos_json('{"owner": "acme", "name": "api"}')


def test_parse_repos_json_entry_not_object() -> None:
    """_parse_repos_json raises ValueError when an entry is not a JSON object."""
    with pytest.raises(ValueError, match="JSON object"):
        _parse_repos_json('["acme/api"]')


# ---------------------------------------------------------------------------
# SQLiteRepoRegistry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_registry_empty() -> None:
    """Fresh SQLiteRepoRegistry has no repos."""
    reg = SQLiteRepoRegistry(db_path=":memory:")
    await reg.init()
    assert await reg.list_repos() == []
    await reg.close()


@pytest.mark.asyncio
async def test_sqlite_registry_upsert_insert() -> None:
    """SQLiteRepoRegistry upsert inserts a new row."""
    reg = SQLiteRepoRegistry(db_path=":memory:")
    await reg.init()
    cfg = RepoConfig(repo=_ACME_API, allowlist=["alice"])
    await reg.upsert_repo(cfg)
    result = await reg.get_repo(_ACME_API)
    assert result is not None
    assert result.allowlist == ["alice"]
    await reg.close()


@pytest.mark.asyncio
async def test_sqlite_registry_upsert_update() -> None:
    """SQLiteRepoRegistry upsert replaces an existing row."""
    reg = SQLiteRepoRegistry(db_path=":memory:")
    await reg.init()
    await reg.upsert_repo(RepoConfig(repo=_ACME_API, allowlist=["alice"]))
    await reg.upsert_repo(RepoConfig(repo=_ACME_API, allowlist=["bob"]))
    result = await reg.get_repo(_ACME_API)
    assert result is not None
    assert result.allowlist == ["bob"]
    await reg.close()


@pytest.mark.asyncio
async def test_sqlite_registry_get_unknown_returns_none() -> None:
    """SQLiteRepoRegistry returns None for an unregistered repo."""
    reg = SQLiteRepoRegistry(db_path=":memory:")
    await reg.init()
    assert await reg.get_repo(_ACME_API) is None
    await reg.close()


@pytest.mark.asyncio
async def test_sqlite_registry_enabled_filter() -> None:
    """enabled_repos returns only enabled repos."""
    reg = SQLiteRepoRegistry(db_path=":memory:")
    await reg.init()
    await reg.upsert_repo(RepoConfig(repo=_ACME_API, enabled=True))
    await reg.upsert_repo(RepoConfig(repo=_ACME_UI, enabled=False))
    enabled = await reg.enabled_repos()
    assert len(enabled) == 1
    assert enabled[0].repo == _ACME_API
    await reg.close()


@pytest.mark.asyncio
async def test_sqlite_registry_preserves_insertion_order() -> None:
    """SQLiteRepoRegistry returns repos in insertion order."""
    reg = SQLiteRepoRegistry(db_path=":memory:")
    await reg.init()
    await reg.upsert_repo(RepoConfig(repo=_ACME_API))
    await reg.upsert_repo(RepoConfig(repo=_ACME_UI))
    repos = await reg.list_repos()
    assert repos[0].repo == _ACME_API
    assert repos[1].repo == _ACME_UI
    await reg.close()


@pytest.mark.asyncio
async def test_sqlite_registry_roundtrip_no_required_checks() -> None:
    """SQLiteRepoRegistry round-trips a config without required_checks (field removed)."""
    reg = SQLiteRepoRegistry(db_path=":memory:")
    await reg.init()
    await reg.upsert_repo(RepoConfig(repo=_ACME_API, allowlist=["alice"]))
    result = await reg.get_repo(_ACME_API)
    assert result is not None
    assert result.allowlist == ["alice"]
    assert not hasattr(result, "required_checks")
    await reg.close()


@pytest.mark.asyncio
async def test_sqlite_registry_seed() -> None:
    """seed() bulk-inserts configs."""
    reg = SQLiteRepoRegistry(db_path=":memory:")
    await reg.init()
    await reg.seed([
        RepoConfig(repo=_ACME_API),
        RepoConfig(repo=_ACME_UI),
    ])
    repos = await reg.list_repos()
    assert len(repos) == 2
    await reg.close()


@pytest.mark.asyncio
async def test_sqlite_registry_not_init_raises() -> None:
    """Accessing SQLiteRepoRegistry before init() raises RuntimeError."""
    reg = SQLiteRepoRegistry(db_path=":memory:")
    with pytest.raises(RuntimeError, match="init()"):
        await reg.list_repos()
