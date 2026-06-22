"""Unit tests for GitHubAppForgePort GitHub App authentication.

Tests verify the JWT → installation token exchange and refresh-on-expiry behaviour
using httpx MockTransport.  No network calls are made.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.ports.github import GitHubAppForgePort

# ---------------------------------------------------------------------------
# Test RSA key (generated once per module for speed)
# ---------------------------------------------------------------------------

_PRIVATE_KEY_OBJ = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_TEST_PRIVATE_KEY_PEM = _PRIVATE_KEY_OBJ.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
).decode()
_TEST_PUBLIC_KEY = _PRIVATE_KEY_OBJ.public_key()

_APP_ID = "12345"
_INSTALLATION_ID = "67890"
_INSTALLATION_TOKEN = "ghs_test_installation_token_abc123"

# 55 minutes from now (well within the 1h expiry)
_EXPIRES_AT_FUTURE = time.strftime(
    "%Y-%m-%dT%H:%M:%SZ",
    time.gmtime(time.time() + 3300),
)


def _make_token_response(
    token: str = _INSTALLATION_TOKEN,
    expires_at: str = _EXPIRES_AT_FUTURE,
) -> dict[str, Any]:
    return {"token": token, "expires_at": expires_at}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_port(
    responses: list[httpx.Response],
) -> GitHubAppForgePort:
    """Build a GitHubAppForgePort backed by a scripted MockTransport."""
    idx: dict[str, int] = {"i": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if idx["i"] >= len(responses):
            raise RuntimeError(f"Unexpected request: {request.method} {request.url}")
        resp = responses[idx["i"]]
        idx["i"] += 1
        return resp

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.github.com")
    return GitHubAppForgePort(
        app_id=_APP_ID,
        private_key_pem=_TEST_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        client=client,
    )


# ---------------------------------------------------------------------------
# JWT minting
# ---------------------------------------------------------------------------


def test_app_jwt_is_valid_rs256() -> None:
    """_mint_app_jwt returns a valid RS256-signed JWT with correct claims."""
    port = GitHubAppForgePort(
        app_id=_APP_ID,
        private_key_pem=_TEST_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
    )
    token = port._mint_app_jwt()
    # Verify the JWT with the corresponding public key
    claims = jwt.decode(
        token,
        _TEST_PUBLIC_KEY,
        algorithms=["RS256"],
        options={"verify_exp": True},
    )
    assert claims["iss"] == _APP_ID
    assert "exp" in claims
    assert "iat" in claims
    assert claims["exp"] > claims["iat"]


def test_app_jwt_iss_is_app_id() -> None:
    """JWT issuer claim matches the app ID."""
    port = GitHubAppForgePort(
        app_id="99999",
        private_key_pem=_TEST_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
    )
    token = port._mint_app_jwt()
    claims = jwt.decode(
        token, _TEST_PUBLIC_KEY, algorithms=["RS256"], options={"verify_exp": True}
    )
    assert claims["iss"] == "99999"


# ---------------------------------------------------------------------------
# Token exchange (JWT → installation token)
# ---------------------------------------------------------------------------


async def test_app_port_exchanges_jwt_for_token() -> None:
    """First request triggers token exchange; the installation token is cached."""
    port = _make_port(
        [
            # POST /app/installations/{id}/access_tokens → token exchange
            httpx.Response(201, json=_make_token_response()),
        ]
    )
    await port._get_token()
    assert port._cached_token == _INSTALLATION_TOKEN


async def test_app_port_caches_token_on_subsequent_calls() -> None:
    """Second call uses cached token without a new exchange request."""
    call_count: dict[str, int] = {"exchange": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if "access_tokens" in str(request.url):
            call_count["exchange"] += 1
            return httpx.Response(201, json=_make_token_response())
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.github.com")
    port = GitHubAppForgePort(
        app_id=_APP_ID,
        private_key_pem=_TEST_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        client=client,
    )

    # Two calls to _get_token — should only exchange once
    token1 = await port._get_token()
    token2 = await port._get_token()

    assert token1 == _INSTALLATION_TOKEN
    assert token2 == _INSTALLATION_TOKEN
    assert call_count["exchange"] == 1


async def test_app_port_refreshes_token_when_expired() -> None:
    """Token is refreshed when _token_expires_at is in the past."""
    call_count: dict[str, int] = {"exchange": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        if "access_tokens" in str(request.url):
            call_count["exchange"] += 1
            new_token = f"ghs_refresh_{call_count['exchange']}"
            return httpx.Response(201, json=_make_token_response(token=new_token))
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(_handler)
    client = httpx.AsyncClient(transport=transport, base_url="https://api.github.com")
    port = GitHubAppForgePort(
        app_id=_APP_ID,
        private_key_pem=_TEST_PRIVATE_KEY_PEM,
        installation_id=_INSTALLATION_ID,
        client=client,
    )

    # First acquisition
    token1 = await port._get_token()
    assert call_count["exchange"] == 1
    assert token1 == "ghs_refresh_1"

    # Artificially expire the cached token
    port._token_expires_at = time.time() - 1.0

    # Second call should trigger a refresh
    token2 = await port._get_token()
    assert call_count["exchange"] == 2
    assert token2 == "ghs_refresh_2"
    assert token2 != token1


async def test_app_port_token_exchange_raises_on_api_error() -> None:
    """Token exchange propagates HTTP errors (does not swallow 401/500)."""
    port = _make_port(
        [httpx.Response(401, json={"message": "Unauthorized"})]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await port._get_token()


# ---------------------------------------------------------------------------
# PortProvider.from_env App mode branching
# ---------------------------------------------------------------------------


def test_port_provider_from_env_app_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env selects GitHubAppForgePort when all App env vars are set."""
    monkeypatch.setenv("FORGE_TOKEN", "ghp_fallback_pat")
    monkeypatch.setenv("GITHUB_APP_ID", "11111")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", _TEST_PRIVATE_KEY_PEM)
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "22222")

    from src.ports.provider import PortProvider

    provider = PortProvider.from_env()
    assert provider._github_app_id == "11111"
    assert provider._github_app_installation_id == "22222"

    from src.domain.types import RepoRef

    repo = RepoRef(owner="acme", name="testrepo")
    forge, _harness, _session = provider.ports(repo)
    assert isinstance(forge, GitHubAppForgePort)


def test_port_provider_from_env_pat_mode_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env selects GitHubForgePort (PAT mode) when App vars are absent."""
    monkeypatch.setenv("FORGE_TOKEN", "ghp_pat_token")
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)

    from src.ports.github import GitHubForgePort
    from src.ports.provider import PortProvider

    provider = PortProvider.from_env()
    from src.domain.types import RepoRef

    repo = RepoRef(owner="acme", name="testrepo")
    forge, _harness, _session = provider.ports(repo)
    assert isinstance(forge, GitHubForgePort)
    assert not isinstance(forge, GitHubAppForgePort)


def test_port_provider_from_env_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """from_env raises RuntimeError when neither PAT nor App vars are set."""
    monkeypatch.delenv("FORGE_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)

    from src.ports.provider import PortProvider

    with pytest.raises(RuntimeError, match="required"):
        PortProvider.from_env()
