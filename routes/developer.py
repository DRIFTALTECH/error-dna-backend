"""Developer settings — MCP server URL + bearer token (required for MCP access)."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from config import MCP_PUBLIC_URL
from services import app_settings as settings

router = APIRouter(prefix="/api/developer", tags=["developer"])


class McpSettingsOut(BaseModel):
    mcp_server_url: str
    bearer_token: str | None
    bearer_configured: bool
    note: str


class McpSettingsIn(BaseModel):
    mcp_server_url: str | None = Field(None, max_length=500)
    bearer_token: str | None = Field(None, min_length=16, max_length=200)
    regenerate_bearer: bool = False


@router.get("/mcp", response_model=McpSettingsOut)
async def get_mcp_settings():
    url = (await settings.get_mcp_server_url()) or MCP_PUBLIC_URL
    bearer = await settings.get_mcp_bearer()
    return McpSettingsOut(
        mcp_server_url=url or "",
        bearer_token=bearer,
        bearer_configured=bool(bearer),
        note=(
            "MCP rejects every request until a bearer is saved. "
            "Clients must send Authorization: Bearer <token>."
            if not bearer
            else "MCP is protected. Paste the URL + bearer into your MCP client config."
        ),
    )


@router.put("/mcp", response_model=McpSettingsOut)
async def put_mcp_settings(body: McpSettingsIn):
    if body.mcp_server_url is not None:
        await settings.set_mcp_server_url(body.mcp_server_url)

    if body.regenerate_bearer:
        token = settings.new_bearer_token()
        await settings.set_mcp_bearer(token)
    elif body.bearer_token is not None:
        try:
            await settings.set_mcp_bearer(body.bearer_token)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    return await get_mcp_settings()
