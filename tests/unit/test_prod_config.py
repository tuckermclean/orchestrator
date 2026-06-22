"""Tests for production app configuration wiring (_build_prod_service).

Verifies that environment-variable parsing for ALLOWLIST and OPERATOR_SECRET_KEY
works correctly without making real network calls.  Uses monkeypatching to avoid
instantiating real ports.

Also covers multi-repo registry wiring (issue #49):
- REPOS_JSON → multi-repo EnvRepoRegistry
- Single-repo backward-compat (GITHUB_OWNER/REPO/ALLOWLIST → one-entry registry)
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch


def test_prod_config_parses_allowlist_from_env(monkeypatch: object) -> None:
    """_build_prod_service reads ALLOWLIST env var and passes it to the service."""
    # We need to monkeypatch env vars and PortProvider.from_env
    with (
        patch.dict(
            os.environ,
            {
                "FORGE_TOKEN": "ghp_test_token",
                "ALLOWLIST": "alice,bob,charlie",
                "GITHUB_OWNER": "acme",
                "GITHUB_REPO": "repo",
            },
            clear=False,
        ),
        patch("src.ports.provider.PortProvider.from_env") as mock_from_env,
    ):
        mock_provider = MagicMock()
        mock_forge = MagicMock()
        mock_harness = MagicMock()
        mock_session = MagicMock()
        mock_provider.ports.return_value = (mock_forge, mock_harness, mock_session)
        mock_from_env.return_value = mock_provider

        # Import here to pick up the patched env
        from src.api.main import _build_prod_service

        service, webhook_secret, _op_store, _push_store, _reg = _build_prod_service()

    assert sorted(service._allowlist) == ["alice", "bob", "charlie"]


def test_prod_config_empty_allowlist_when_env_unset(monkeypatch: object) -> None:
    """_build_prod_service passes empty allowlist when ALLOWLIST is not set."""
    with (
        patch.dict(
            os.environ,
            {"FORGE_TOKEN": "ghp_test_token", "GITHUB_OWNER": "acme", "GITHUB_REPO": "repo"},
            clear=False,
        ),
        patch("src.ports.provider.PortProvider.from_env") as mock_from_env,
    ):
        # Remove ALLOWLIST from env if present
        os.environ.pop("ALLOWLIST", None)

        mock_provider = MagicMock()
        mock_forge = MagicMock()
        mock_harness = MagicMock()
        mock_session = MagicMock()
        mock_provider.ports.return_value = (mock_forge, mock_harness, mock_session)
        mock_from_env.return_value = mock_provider

        from src.api.main import _build_prod_service

        service, webhook_secret, _op_store, _push_store, _reg = _build_prod_service()

    assert service._allowlist == []


def test_prod_config_webhook_secret_uses_operator_secret_key(monkeypatch: object) -> None:
    """_build_prod_service reads OPERATOR_SECRET_KEY (not WEBHOOK_SECRET) for HMAC."""
    with (
        patch.dict(
            os.environ,
            {
                "FORGE_TOKEN": "ghp_test_token",
                "OPERATOR_SECRET_KEY": "my-operator-secret",
                "GITHUB_OWNER": "acme",
                "GITHUB_REPO": "repo",
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

        _service, webhook_secret, _op_store, _push_store, _reg = _build_prod_service()

    assert webhook_secret == "my-operator-secret"


def test_prod_config_allowlist_strips_whitespace(monkeypatch: object) -> None:
    """ALLOWLIST values with surrounding whitespace are stripped."""
    with (
        patch.dict(
            os.environ,
            {
                "FORGE_TOKEN": "ghp_test_token",
                "ALLOWLIST": " alice , bob , ",
                "GITHUB_OWNER": "acme",
                "GITHUB_REPO": "repo",
            },
            clear=False,
        ),
        patch("src.ports.provider.PortProvider.from_env") as mock_from_env,
    ):
        mock_provider = MagicMock()
        mock_provider.ports.return_value = (MagicMock(), MagicMock(), MagicMock())
        mock_from_env.return_value = mock_provider

        from src.api.main import _build_prod_service

        service, _secret, _op_store, _push_store, _reg = _build_prod_service()

    assert sorted(service._allowlist) == ["alice", "bob"]


# ---------------------------------------------------------------------------
# Multi-repo registry wiring (issue #49)
# ---------------------------------------------------------------------------


def test_prod_config_single_repo_backward_compat_wires_registry(
    monkeypatch: object,
) -> None:
    """_build_prod_service wires a one-entry EnvRepoRegistry when REPOS_JSON is absent.

    Single-repo backward compat: the registry contains exactly one entry
    built from GITHUB_OWNER + GITHUB_REPO + ALLOWLIST.
    """
    with (
        patch.dict(
            os.environ,
            {
                "FORGE_TOKEN": "ghp_test_token",
                "GITHUB_OWNER": "acme",
                "GITHUB_REPO": "myrepo",
                "ALLOWLIST": "alice",
            },
            clear=False,
        ),
        patch("src.ports.provider.PortProvider.from_env") as mock_from_env,
    ):
        os.environ.pop("REPOS_JSON", None)
        mock_provider = MagicMock()
        mock_provider.ports.return_value = (MagicMock(), MagicMock(), MagicMock())
        mock_from_env.return_value = mock_provider

        from src.api.main import _build_prod_service

        service, _secret, _op_store, _push_store, _reg = _build_prod_service()

    assert service._registry is not None
    assert len(service._registry._configs) == 1  # type: ignore[union-attr]
    cfg = service._registry._configs[0]  # type: ignore[union-attr]
    assert cfg.repo.owner == "acme"
    assert cfg.repo.name == "myrepo"
    assert cfg.allowlist == ["alice"]


def test_prod_config_repos_json_wires_multi_repo_registry(
    monkeypatch: object,
) -> None:
    """_build_prod_service builds a multi-entry registry from REPOS_JSON."""
    repos_json = json.dumps([
        {"owner": "acme", "name": "api", "allowlist": ["alice"]},
        {"owner": "acme", "name": "ui"},
    ])
    with (
        patch.dict(
            os.environ,
            {
                "FORGE_TOKEN": "ghp_test_token",
                "REPOS_JSON": repos_json,
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

        service, _secret, _op_store, _push_store, _reg = _build_prod_service()

    assert service._registry is not None
    assert len(service._registry._configs) == 2  # type: ignore[union-attr]
    names = [c.repo.name for c in service._registry._configs]  # type: ignore[union-attr]
    assert "api" in names
    assert "ui" in names


def test_prod_config_no_creds_still_wires_registry(
    monkeypatch: object,
) -> None:
    """_build_prod_service in dev mode (no creds): service starts without crashing."""
    import pytest as _pytest

    _pytest.importorskip("src.api.main")

    with patch.dict(
        os.environ,
        {},
        clear=False,
    ):
        for var in ["FORGE_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY",
                    "GITHUB_APP_INSTALLATION_ID", "REPOS_JSON"]:
            os.environ.pop(var, None)

        from src.api.main import _build_prod_service
        from src.ports.fakes import FakeForgePort

        service, webhook_secret, _op_store, _push_store, _reg = _build_prod_service()

    # Dev mode: forge is Fake; no crash
    assert isinstance(service.forge, FakeForgePort)
    assert webhook_secret is None
