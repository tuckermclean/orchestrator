"""FastAPI router for operator account management (WEBUI.md §5.6).

Routes (all require JWT auth):
  GET    /api/operators           — list operators (no password hashes)
  POST   /api/operators           — add operator account
  DELETE /api/operators/:id       — remove operator (rejected if last)
  POST   /api/operators/:id/password — change password (self only; requires current)
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from src.api.auth import hash_password, require_auth, verify_password
from src.db.operator_store import OperatorStorePort

AuthPayload = dict[str, object]


class CreateOperatorRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


def _make_operator_router(operator_store: OperatorStorePort) -> APIRouter:
    r = APIRouter()

    @r.get("/api/operators")
    async def list_operators(
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> list[dict[str, object]]:
        """Return all operator accounts (no password hashes)."""
        return await operator_store.list_operators()

    @r.post("/api/operators", status_code=status.HTTP_201_CREATED)
    async def create_operator(
        body: CreateOperatorRequest,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> dict[str, object]:
        """Add a new operator account."""
        ph = hash_password(body.password)
        try:
            await operator_store.create_operator(body.username, ph)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            )
        return {"status": "created", "id": body.username}

    @r.delete("/api/operators/{operator_id}", status_code=status.HTTP_200_OK)
    async def delete_operator(
        operator_id: str,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> dict[str, object]:
        """Remove an operator account (rejected if it is the last account)."""
        try:
            await operator_store.delete_operator(operator_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            )
        return {"status": "deleted", "id": operator_id}

    @r.post("/api/operators/{operator_id}/password", status_code=status.HTTP_200_OK)
    async def change_password(
        operator_id: str,
        body: ChangePasswordRequest,
        auth: Annotated[AuthPayload, Depends(require_auth)],
    ) -> dict[str, object]:
        """Change password for an operator (self only; requires current password)."""
        # Self-only: the requesting operator must match the target
        requesting = str(auth.get("sub", ""))
        if requesting != operator_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only change your own password",
            )
        record = await operator_store.get_operator(operator_id)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Operator not found",
            )
        stored_hash = str(record.get("password_hash", ""))
        if not verify_password(body.current_password, stored_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Current password is incorrect",
            )
        new_hash = hash_password(body.new_password)
        await operator_store.update_password(operator_id, new_hash)
        return {"status": "updated"}

    return r
