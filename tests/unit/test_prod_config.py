"""Tests for production app configuration wiring (_build_prod_service).

Verifies that environment-variable parsing for ALLOWLIST and OPERATOR_SECRET_KEY
works correctly without making real network calls.  Uses monkeypatching to avoid
instantiating real ports.
"""

from __future__ import annotations

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

        service, webhook_secret = _build_prod_service()

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

        service, webhook_secret = _build_prod_service()

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

        _service, webhook_secret = _build_prod_service()

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

        service, _ = _build_prod_service()

    assert sorted(service._allowlist) == ["alice", "bob"]
