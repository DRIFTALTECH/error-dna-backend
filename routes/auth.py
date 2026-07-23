"""Auth routes — login only (no signup; users are inserted into the DB manually)."""

from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field
from db import read
from services.auth import verify_password, make_token, require_auth

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


@router.post("/login")
async def login(body: LoginBody):
    rows = await read("SELECT username, password_hash FROM app_users WHERE username = ?", (body.username,))
    if not rows or not verify_password(body.password, rows[0]["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    return {"token": make_token(rows[0]["username"]), "username": rows[0]["username"]}


@router.get("/me")
async def me(user: str = Depends(require_auth)):
    return {"username": user}
