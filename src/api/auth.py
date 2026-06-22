"""JWT authentication for the orchestrator API.

Issues short-lived JWTs (8h session / 30d "remember me") signed with
OPERATOR_SECRET_KEY (HS256).  A Bearer middleware enforces auth on all
/api/* routes except:
  - POST /api/webhook  — authenticated by HMAC-SHA256 signature
  - GET  /healthz      — liveness probe (no auth needed)
  - GET  /readyz       — readiness probe (no auth needed)
  - POST /api/auth     — login endpoint itself
  - POST /api/auth/refresh — refresh endpoint

The middleware is a FastAPI dependency injected via Depends(); it is NOT
a starlette middleware so it does not intercept static asset serving.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# ---------------------------------------------------------------------------
# Token configuration constants
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS: int = 8 * 3600        # 8 hours (default)
REMEMBER_ME_TTL_SECONDS: int = 30 * 86400  # 30 days
JWT_ALGORITHM: str = "HS256"

# Routes that bypass JWT auth
_AUTH_BYPASS_PREFIXES: tuple[str, ...] = (
    "/api/auth",      # login + refresh
    "/api/webhook",   # HMAC-authenticated
)
_AUTH_BYPASS_EXACT: tuple[str, ...] = (
    "/healthz",
    "/readyz",
)

_logger = logging.getLogger(__name__)

# Module flag so the weak-key warning is emitted at most once (not per token op).
_short_key_warned_flag = False


def _short_key_warned() -> bool:
    """Return whether the short-key warning has already fired; mark it fired (warn-once)."""
    global _short_key_warned_flag
    already = _short_key_warned_flag
    _short_key_warned_flag = True
    return already


def _secret_key() -> str:
    """Return OPERATOR_SECRET_KEY from the environment.

    Raises RuntimeError if not set — callers must set the env var before
    using any auth functions.  This is intentionally deferred so tests that
    use monkeypatch can inject it before the first call.
    """
    key = os.environ.get("OPERATOR_SECRET_KEY", "")
    if not key:
        raise RuntimeError(
            "OPERATOR_SECRET_KEY environment variable is not set. "
            "JWT auth cannot function without it."
        )
    if len(key.encode()) < 32 and not _short_key_warned():
        _logger.warning(
            "OPERATOR_SECRET_KEY is %d bytes; RFC 7518 §3.2 recommends >= 32 bytes for "
            "HS256 JWT signing. Use a longer secret in production.",
            len(key.encode()),
        )
    return key


# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------


def issue_token(operator_id: str, *, remember_me: bool = False) -> str:
    """Issue a signed JWT for the given operator.

    Args:
        operator_id: The operator's username (sub claim).
        remember_me: When True, use the extended 30-day TTL.

    Returns:
        Encoded JWT string.
    """
    ttl = REMEMBER_ME_TTL_SECONDS if remember_me else SESSION_TTL_SECONDS
    now = int(time.time())
    payload = {
        "sub": operator_id,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, _secret_key(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, object]:
    """Decode and validate a JWT; raises HTTPException on failure.

    Raises:
        HTTPException 401: token is invalid, expired, or signature fails.
    """
    try:
        data: dict[str, object] = jwt.decode(
            token,
            _secret_key(),
            algorithms=[JWT_ALGORITHM],
        )
        return data
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Bearer dependency (injected on each protected route)
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer(auto_error=False)


async def require_auth(
    request: Request,
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ] = None,
) -> dict[str, object]:
    """FastAPI dependency that validates the Bearer JWT.

    Returns:
        Decoded JWT payload dict with at least {"sub": <operator_id>}.

    Raises:
        HTTPException 401 when no token is provided or the token is invalid.
    """
    # Pass-through for routes that bypass auth
    path = request.url.path
    if path in _AUTH_BYPASS_EXACT:
        return {}
    for prefix in _AUTH_BYPASS_PREFIXES:
        if path.startswith(prefix):
            return {}

    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return decode_token(credentials.credentials)


# ---------------------------------------------------------------------------
# Password helpers (bcrypt-free — PBKDF2-SHA256 via hashlib)
# ---------------------------------------------------------------------------

_PBKDF2_ITERS: int = 260_000  # NIST recommendation for PBKDF2-SHA256 (2024)


def hash_password(password: str) -> str:
    """Return a PBKDF2-SHA256 hash in the format ``pbkdf2:sha256:<iters>$<salt>$<hash>``."""
    import secrets

    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), _PBKDF2_ITERS
    ).hex()
    return f"pbkdf2:sha256:{_PBKDF2_ITERS}${salt}${dk}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify *password* against a stored PBKDF2 hash string.

    Returns True only when the password matches; constant-time comparison
    is used to prevent timing attacks.
    """
    try:
        # Re-parse: format is "pbkdf2:sha256:<iters>$<salt>$<hash>"
        algo_part, rest2 = stored_hash.split("$", 1)
        salt, expected_dk = rest2.split("$", 1)
        # Extract iterations from algo_part: "pbkdf2:sha256:260000"
        iters = int(algo_part.split(":")[-1])
        actual_dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), iters
        ).hex()
        return hmac_compare(actual_dk, expected_dk)
    except (ValueError, AttributeError):
        return False


def hmac_compare(a: str, b: str) -> bool:
    """Constant-time string comparison."""
    import hmac as _hmac

    return _hmac.compare_digest(a.encode(), b.encode())
