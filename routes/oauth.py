"""OAuth 2.0 client_credentials — token endpoint + client management."""

import base64
import secrets

from fastapi import APIRouter, Depends, Form, Header, HTTPException
from pydantic import BaseModel, Field

from config import OAUTH_TOKEN_TTL
from db import read, write
from services.auth import hash_password, make_token, require_auth, verify_password

router = APIRouter(prefix="/api/oauth", tags=["oauth"])


def _parse_client_auth(
    authorization: str | None,
    client_id: str | None,
    client_secret: str | None,
) -> tuple[str, str]:
    cid, secret = client_id, client_secret
    if authorization and authorization.lower().startswith("basic "):
        try:
            raw = base64.b64decode(authorization.split(" ", 1)[1].strip()).decode()
            cid, secret = raw.split(":", 1)
        except Exception as exc:
            raise HTTPException(401, "invalid_client") from exc
    if not cid or not secret:
        raise HTTPException(401, "invalid_client")
    return cid.strip(), secret


async def _authenticate_client(client_id: str, client_secret: str) -> None:
    rows = await read(
        "SELECT secret_hash, is_active FROM oauth_clients WHERE client_id = ?",
        (client_id,),
    )
    if not rows or not rows[0]["is_active"]:
        raise HTTPException(401, "invalid_client")
    if not verify_password(client_secret, rows[0]["secret_hash"]):
        raise HTTPException(401, "invalid_client")


@router.post("/token")
async def token(
    grant_type: str = Form(...),
    client_id: str | None = Form(None),
    client_secret: str | None = Form(None),
    authorization: str | None = Header(None),
):
    """OAuth 2.0 client_credentials — RFC 6749."""
    if grant_type != "client_credentials":
        raise HTTPException(400, "unsupported_grant_type")
    cid, secret = _parse_client_auth(authorization, client_id, client_secret)
    await _authenticate_client(cid, secret)
    access_token = make_token(cid, ttl=OAUTH_TOKEN_TTL, kind="client")
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": OAUTH_TOKEN_TTL,
    }


class CreateClientBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


@router.get("/clients")
async def list_clients(_: str = Depends(require_auth)):
    rows = await read(
        """SELECT id, client_id, name, is_active, created_at
           FROM oauth_clients ORDER BY created_at DESC""",
    )
    return {
        "items": [
            {
                "id": r["id"],
                "client_id": r["client_id"],
                "name": r["name"],
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ],
        "token_url": "/api/oauth/token",
    }


@router.post("/clients")
async def create_client(body: CreateClientBody, _: str = Depends(require_auth)):
    client_id = f"edna_{secrets.token_urlsafe(12)}"
    client_secret = secrets.token_urlsafe(32)
    rows = await write(
        "INSERT INTO oauth_clients(client_id, secret_hash, name) VALUES (?,?,?) RETURNING id, created_at",
        (client_id, hash_password(client_secret), body.name.strip()),
    )
    row = rows[0]
    return {
        "id": row["id"],
        "client_id": client_id,
        "client_secret": client_secret,
        "name": body.name.strip(),
        "created_at": row["created_at"],
        "token_url": "/api/oauth/token",
        "note": "Copy client_secret now — it cannot be shown again.",
    }


@router.delete("/clients/{client_id}")
async def deactivate_client(client_id: str, _: str = Depends(require_auth)):
    rows = await write(
        "UPDATE oauth_clients SET is_active = 0 WHERE client_id = ? AND is_active = 1 RETURNING client_id",
        (client_id,),
    )
    if not rows:
        raise HTTPException(404, "Client not found or already inactive")
    return {"ok": True, "client_id": client_id}
