"""FastAPI router for authentication endpoints.

Routes:
  POST /api/auth         — login (issue JWT)
  POST /api/auth/refresh — refresh a valid/near-expiry JWT

These routes are excluded from JWT middleware (see auth.py _AUTH_BYPASS_PREFIXES).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.api.auth import decode_token, issue_token, verify_password
from src.db.operator_store import OperatorStorePort


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


def _make_auth_router(operator_store: OperatorStorePort) -> APIRouter:
    r = APIRouter()

    @r.post("/api/auth", response_model=TokenResponse)
    async def login(body: LoginRequest) -> TokenResponse:
        """Authenticate operator credentials; return a signed JWT."""
        record = await operator_store.get_operator(body.username)
        if record is None:
            # Constant-time: always check password even on miss to prevent timing attacks
            _dummy = "pbkdf2:sha256:260000$00000000000000000000000000000000$00"
            verify_password("__placeholder__", _dummy)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        stored_hash = str(record.get("password_hash", ""))
        if not verify_password(body.password, stored_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
            )
        await operator_store.record_login(body.username)
        token = issue_token(body.username, remember_me=body.remember_me)
        return TokenResponse(access_token=token)

    @r.post("/api/auth/refresh", response_model=TokenResponse)
    async def refresh(body: dict[str, str]) -> TokenResponse:
        """Accept a valid JWT; return a fresh JWT with a renewed expiry.

        The service worker calls this silently before expiry.  A refresh
        failure (expired or invalid token) returns 401 and the client
        redirects to /login.
        """
        token = body.get("token", "")
        if not token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="token field is required",
            )
        # decode_token raises 401 on invalid/expired
        payload = decode_token(token)
        operator_id = str(payload.get("sub", ""))
        new_token = issue_token(operator_id)
        return TokenResponse(access_token=new_token)

    return r
