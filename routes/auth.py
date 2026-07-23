"""Auth routes — login only (no signup; users are inserted into the DB manually)."""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from db import read, write
from services.auth import verify_password, hash_password, make_token, require_auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class NewAccountBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)


@router.post("/login")
async def login(body: LoginBody):
    rows = await read("SELECT username, password_hash FROM app_users WHERE username = ?", (body.username,))
    if not rows or not verify_password(body.password, rows[0]["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    return {"token": make_token(rows[0]["username"]), "username": rows[0]["username"]}


@router.get("/me")
async def me(user: str = Depends(require_auth)):
    return {"username": user}


@router.post("/change-password")
async def change_password(body: ChangePasswordBody, user: str = Depends(require_auth)):
    rows = await read("SELECT password_hash FROM app_users WHERE username = ?", (user,))
    if not rows or not verify_password(body.current_password, rows[0]["password_hash"]):
        raise HTTPException(400, "Current password is incorrect")
    await write("UPDATE app_users SET password_hash = ? WHERE username = ?", (hash_password(body.new_password), user))
    return {"ok": True}


@router.post("/users")
async def create_account(body: NewAccountBody, _: str = Depends(require_auth)):
    if await read("SELECT id FROM app_users WHERE username = ?", (body.username,)):
        raise HTTPException(409, "Username already exists")
    await write("INSERT INTO app_users(username, password_hash) VALUES (?,?)", (body.username, hash_password(body.password)))
    return {"ok": True, "username": body.username}
