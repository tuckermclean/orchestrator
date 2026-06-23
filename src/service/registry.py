"""Repo registry — ordered set of enabled repos with per-repo config.

Design rationale
----------------
A *config-driven* env registry is used rather than a DB-backed one for the
initial implementation.  Reasons:

1. Existing production config is env-based (GITHUB_OWNER / GITHUB_REPO /
   ALLOWLIST); adding REPOS_JSON=... extends that pattern with zero new
   infrastructure.
2. ARCHITECTURE.md says the registry belongs in the service DB *eventually*;
   a DB backend can replace EnvRepoRegistry behind the same RepoRegistryPort
   seam without touching callers.
3. The SQLiteRepoRegistry class below provides the DB path for operators who
   need runtime mutability; the app factory selects the implementation based
   on the presence of the REPOS_JSON env var vs. live DB rows.

Single-repo backward compat
----------------------------
When REPOS_JSON is absent, the factory falls back to building a one-entry
registry from GITHUB_OWNER / GITHUB_REPO / ALLOWLIST — identical to the
pre-multi-repo behaviour.

I3 compliance
--------------
RepoConfig carries *configuration* only (owner, name, allowlist).
Credentials live exclusively in PortProvider.
No token is accepted by or stored in any registry type.
"""

from __future__ import annotations

import json
from typing import Protocol, runtime_checkable

import aiosqlite

from src.domain.types import RepoRef


class RepoConfig:
    """Per-repo configuration for the orchestrator.

    Attributes
    ----------
    repo:
        Identifies the repository (owner + name).
    enabled:
        When False, the repo is known but not actively managed (events ignored,
        reconciler skips it).  Defaults to True.
    intake_enabled:
        When False, new issues skip the intake gate (useful for repos where the
        operator handles promotion manually).  Defaults to True.
    allowlist:
        GitHub logins admitted through decide_intake without queuing.  Empty →
        owner-only default-deny (issue #48).  The repo owner (repo.owner) is
        always admitted regardless of this list (same semantics as single-repo).
    """

    __slots__ = ("repo", "enabled", "intake_enabled", "allowlist")

    def __init__(
        self,
        repo: RepoRef,
        *,
        enabled: bool = True,
        intake_enabled: bool = True,
        allowlist: list[str] | None = None,
    ) -> None:
        self.repo = repo
        self.enabled = enabled
        self.intake_enabled = intake_enabled
        self.allowlist: list[str] = allowlist if allowlist is not None else []

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RepoConfig):
            return NotImplemented
        return (
            self.repo == other.repo
            and self.enabled == other.enabled
            and self.intake_enabled == other.intake_enabled
            and self.allowlist == other.allowlist
        )

    def __repr__(self) -> str:
        return (
            f"RepoConfig(repo={self.repo!r}, enabled={self.enabled!r}, "
            f"intake_enabled={self.intake_enabled!r}, "
            f"allowlist={self.allowlist!r})"
        )


@runtime_checkable
class RepoRegistryPort(Protocol):
    """Port for the repo registry — ordered set of enabled repos.

    Implementations must be safe to call concurrently from an async context.
    """

    async def list_repos(self) -> list[RepoConfig]:
        """Return all registered repos (enabled and disabled), in insertion order."""
        ...

    async def get_repo(self, repo: RepoRef) -> RepoConfig | None:
        """Look up config for a specific repo; return None if not registered."""
        ...

    async def upsert_repo(self, config: RepoConfig) -> None:
        """Insert or update the config for a repo.

        If a repo with the same owner+name exists it is replaced; otherwise it
        is appended.  The order is preserved for repos that already exist.
        """
        ...

    async def enabled_repos(self) -> list[RepoConfig]:
        """Return only repos where enabled=True, in insertion order."""
        ...


# ---------------------------------------------------------------------------
# Fake — in-process, ordered dict, no I/O
# ---------------------------------------------------------------------------


class FakeRepoRegistry:
    """In-process registry for tests and dev mode.

    Preserves insertion order; supports upsert semantics identical to the
    production implementations.
    """

    def __init__(self, configs: list[RepoConfig] | None = None) -> None:
        # Ordered by insertion key (owner/name string), not an unordered dict.
        self._store: dict[str, RepoConfig] = {}
        for cfg in configs or []:
            key = f"{cfg.repo.owner}/{cfg.repo.name}"
            self._store[key] = cfg

    # -- RepoRegistryPort implementation ------------------------------------

    async def list_repos(self) -> list[RepoConfig]:
        return list(self._store.values())

    async def get_repo(self, repo: RepoRef) -> RepoConfig | None:
        return self._store.get(f"{repo.owner}/{repo.name}")

    async def upsert_repo(self, config: RepoConfig) -> None:
        key = f"{config.repo.owner}/{config.repo.name}"
        self._store[key] = config

    async def enabled_repos(self) -> list[RepoConfig]:
        return [c for c in self._store.values() if c.enabled]


# ---------------------------------------------------------------------------
# EnvRepoRegistry — built from environment variables, immutable at runtime
# ---------------------------------------------------------------------------


class EnvRepoRegistry:
    """Read-only registry populated once from environment variables.

    Two source modes (selected by the caller via from_env()):

    1. **REPOS_JSON** — JSON array of repo-config objects:
       ``[{"owner": "acme", "name": "api", "allowlist": ["alice"]}, ...]``
       Optional keys: ``enabled``, ``intake_enabled``, ``required_checks``.

    2. **Legacy single-repo** (REPOS_JSON absent):
       Reads GITHUB_OWNER + GITHUB_REPO + ALLOWLIST and builds a one-entry
       registry — backward-compatible with all existing single-repo deploys.

    Credentials are NOT read here (I3); they stay in PortProvider.
    """

    def __init__(self, configs: list[RepoConfig]) -> None:
        self._configs: list[RepoConfig] = list(configs)
        # Index for O(1) lookup
        self._index: dict[str, RepoConfig] = {
            f"{c.repo.owner}/{c.repo.name}": c for c in self._configs
        }

    @classmethod
    def from_env(
        cls,
        *,
        repos_json: str | None = None,
        github_owner: str | None = None,
        github_repo: str | None = None,
        allowlist_raw: str | None = None,
    ) -> EnvRepoRegistry:
        """Build the registry from the given env-var values (caller reads os.environ).

        Parameters are accepted explicitly so the factory is testable without
        monkeypatching os.environ.
        """
        if repos_json:
            configs = _parse_repos_json(repos_json)
            return cls(configs)

        # Single-repo fallback: GITHUB_OWNER + GITHUB_REPO + ALLOWLIST
        owner = (github_owner or "").strip()
        name = (github_repo or "").strip()
        if not owner or not name:
            # No repos configured — return an empty registry (dev mode)
            return cls([])

        allowlist = [
            u.strip() for u in (allowlist_raw or "").split(",") if u.strip()
        ]
        config = RepoConfig(
            repo=RepoRef(owner=owner, name=name),
            allowlist=allowlist,
        )
        return cls([config])

    # -- RepoRegistryPort implementation ------------------------------------

    async def list_repos(self) -> list[RepoConfig]:
        return list(self._configs)

    async def get_repo(self, repo: RepoRef) -> RepoConfig | None:
        return self._index.get(f"{repo.owner}/{repo.name}")

    async def upsert_repo(self, config: RepoConfig) -> None:  # pragma: no cover
        raise NotImplementedError(
            "EnvRepoRegistry is read-only; use SQLiteRepoRegistry for runtime mutation"
        )

    async def enabled_repos(self) -> list[RepoConfig]:
        return [c for c in self._configs if c.enabled]


# ---------------------------------------------------------------------------
# SQLiteRepoRegistry — DB-backed, runtime-mutable
# ---------------------------------------------------------------------------

_CREATE_REPOS_TABLE = """
CREATE TABLE IF NOT EXISTS repos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner           TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    intake_enabled  INTEGER NOT NULL DEFAULT 1,
    allowlist_json  TEXT    NOT NULL DEFAULT '[]',
    checks_json     TEXT    NOT NULL DEFAULT 'null',
    UNIQUE (owner, name)
)
"""


class SQLiteRepoRegistry:
    """SQLite-backed registry; supports runtime upsert for admin API use.

    ``init()`` must be awaited once before any other method.  Designed as an
    async context manager as well as a standalone object.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        """Open the DB and create the repos table if absent."""
        if self._db is not None:
            return
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(_CREATE_REPOS_TABLE)
        await self._db.commit()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("SQLiteRepoRegistry.init() must be called before use")
        return self._db

    async def close(self) -> None:
        """Close the DB connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- RepoRegistryPort implementation ------------------------------------

    async def list_repos(self) -> list[RepoConfig]:
        async with self._conn.execute(
            "SELECT owner, name, enabled, intake_enabled, allowlist_json, checks_json "
            "FROM repos ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_config(row) for row in rows]

    async def get_repo(self, repo: RepoRef) -> RepoConfig | None:
        async with self._conn.execute(
            "SELECT owner, name, enabled, intake_enabled, allowlist_json, checks_json "
            "FROM repos WHERE owner = ? AND name = ?",
            (repo.owner, repo.name),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_config(row) if row is not None else None

    async def upsert_repo(self, config: RepoConfig) -> None:
        await self._conn.execute(
            """
            INSERT INTO repos (owner, name, enabled, intake_enabled, allowlist_json, checks_json)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (owner, name) DO UPDATE SET
                enabled         = excluded.enabled,
                intake_enabled  = excluded.intake_enabled,
                allowlist_json  = excluded.allowlist_json,
                checks_json     = excluded.checks_json
            """,
            (
                config.repo.owner,
                config.repo.name,
                int(config.enabled),
                int(config.intake_enabled),
                json.dumps(config.allowlist),
                "null",
            ),
        )
        await self._conn.commit()

    async def enabled_repos(self) -> list[RepoConfig]:
        async with self._conn.execute(
            "SELECT owner, name, enabled, intake_enabled, allowlist_json, checks_json "
            "FROM repos WHERE enabled = 1 ORDER BY id ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_config(row) for row in rows]

    async def seed(self, configs: list[RepoConfig]) -> None:
        """Bulk-insert configs; convenience method for bootstrapping prod DB."""
        for cfg in configs:
            await self.upsert_repo(cfg)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_repos_json(raw: str) -> list[RepoConfig]:
    """Parse REPOS_JSON env var into a list of RepoConfig objects.

    Expected format::

        [
            {"owner": "acme", "name": "api", "allowlist": ["alice"]},
            {"owner": "acme", "name": "ui",  "enabled": false}
        ]

    Unknown keys are silently ignored to allow forward-compatible additions.
    The ``required_checks`` key is no longer supported; include it in REPOS_JSON
    for forward-compat and it will be silently ignored.
    """
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("REPOS_JSON must be a JSON array")

    configs: list[RepoConfig] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError(f"Each REPOS_JSON entry must be a JSON object, got {entry!r}")
        owner = str(entry["owner"])
        name = str(entry["name"])
        enabled = bool(entry.get("enabled", True))
        intake_enabled = bool(entry.get("intake_enabled", True))
        allowlist = [str(u) for u in entry.get("allowlist", [])]
        configs.append(
            RepoConfig(
                repo=RepoRef(owner=owner, name=name),
                enabled=enabled,
                intake_enabled=intake_enabled,
                allowlist=allowlist,
            )
        )
    return configs


def _row_to_config(row: aiosqlite.Row) -> RepoConfig:
    """Convert a SQLite row dict to a RepoConfig."""
    owner = str(row["owner"])
    name = str(row["name"])
    enabled = bool(row["enabled"])
    intake_enabled = bool(row["intake_enabled"])
    allowlist: list[str] = json.loads(str(row["allowlist_json"]))
    # checks_json is retained in the DB schema for forward-compat but is no longer
    # used by the converge gate; the CI green definition trusts all present checks.
    return RepoConfig(
        repo=RepoRef(owner=owner, name=name),
        enabled=enabled,
        intake_enabled=intake_enabled,
        allowlist=allowlist,
    )
