"""Credential routes — plaintext storage; real login tests via openclaw browser."""

import asyncio
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db import read, write

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


class CredentialAdd(BaseModel):
    login_url: str = "https://me.sap.com"
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class CredentialUpdate(BaseModel):
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


class CredentialTestBody(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    login_url: str = "https://me.sap.com"


def _auto_label(username: str) -> str:
    u = (username or "").strip()
    if not u:
        return "Account"
    if "@" in u:
        return u.split("@", 1)[0] or u
    return u


def _mask(username: str) -> str:
    u = username or ""
    if len(u) <= 6:
        return (u[:1] + "***") if u else "***"
    return f"{u[:3]}***{u[-8:]}" if len(u) > 11 else f"{u[:3]}***{u[-3:]}"


def _cred_to_account(row: dict) -> dict:
    active = bool(row.get("is_active"))
    return {
        "id": row["id"],
        "label": row.get("label") or "",
        "login_url": row.get("login_url") or "",
        "username_masked": _mask(row.get("username")),
        "status": "healthy" if active else "standby",
        "usage_today": 0,
        "last_used_at": None,
        "active": active,
        "status_text": "✅ Active" if active else "⏸ Standby",
    }


async def _run_login_test(login_url: str, username: str, password: str) -> dict:
    from services.scraper import test_login
    result = await asyncio.to_thread(test_login, login_url, username, password)
    if not result.get("ok"):
        raise HTTPException(401, result.get("message") or "Login failed")
    return {
        "ok": True,
        "message": result.get("message") or "Login succeeded",
        "state": result.get("state"),
    }


@router.get("")
async def list_credentials():
    rows = await read("SELECT * FROM credentials ORDER BY is_active DESC, id ASC")
    return [_cred_to_account(r) for r in rows]


@router.post("")
async def add_credential(body: CredentialAdd):
    from services.crypto import encrypt
    rows = await write(
        "INSERT INTO credentials (label, login_url, username, password) VALUES (?,?,?,?) RETURNING id",
        (_auto_label(body.username), body.login_url, body.username, encrypt(body.password)),
    )
    return {"ok": True, "id": rows[0]["id"]}


@router.put("/{cred_id}")
async def update_credential(cred_id: int, body: CredentialUpdate):
    sets, params = [], []
    if body.login_url:
        sets.append("login_url=?"); params.append(body.login_url)
    if body.username:
        sets.append("username=?"); params.append(body.username)
        sets.append("label=?"); params.append(_auto_label(body.username))
    if body.password:
        from services.crypto import encrypt
        sets.append("password=?"); params.append(encrypt(body.password))
    if sets:
        params.append(cred_id)
        await write(f"UPDATE credentials SET {','.join(sets)} WHERE id=?", params)
    return {"ok": True}


@router.delete("/{cred_id}")
async def delete_credential(cred_id: int):
    await write("DELETE FROM credentials WHERE id=?", (cred_id,))
    return {"ok": True}


@router.patch("/{cred_id}/activate")
async def activate(cred_id: int):
    await write("UPDATE credentials SET is_active=0")
    await write("UPDATE credentials SET is_active=1 WHERE id=?", (cred_id,))
    # Reset the auto-rotate clock so this account gets a full ACCOUNT_ROTATE_HOURS window.
    from services.scheduler import stamp_account_activated
    await stamp_account_activated()
    return {"ok": True}


@router.post("/test")
async def test_new_credential(body: CredentialTestBody):
    return await _run_login_test(body.login_url, body.username, body.password)


@router.post("/{cred_id}/test")
async def test_credential(cred_id: int):
    rows = await read(
        "SELECT login_url, username, password FROM credentials WHERE id=?", (cred_id,)
    )
    if not rows:
        raise HTTPException(404, "Credential not found")
    row = rows[0]
    from services.crypto import decrypt
    return await _run_login_test(row["login_url"], row["username"], decrypt(row["password"]))
