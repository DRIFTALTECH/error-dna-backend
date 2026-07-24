"""Pure ASGI middleware — MCP HTTP requires a configured Bearer token.

Uses raw ASGI (not Starlette BaseHTTPMiddleware) so streamable-http / SSE
responses are not buffered.

If no bearer is saved in Developer Settings, every request is 401.
If a bearer is saved, Authorization: Bearer <token> must match (constant-time).
"""

from __future__ import annotations

import json
import secrets
from typing import Callable


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            return v.decode("latin-1")
    return ""


async def _send_json(send, status: int, body: dict) -> None:
    raw = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
            (b"www-authenticate", b'Bearer realm="Error DNA MCP"'),
        ],
    })
    await send({"type": "http.response.body", "body": raw})


class McpBearerMiddleware:
    """ASGI wrapper: (app) → gated app."""

    def __init__(self, app: Callable):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        from services.app_settings import get_mcp_bearer

        try:
            expected = await get_mcp_bearer()
        except Exception as e:
            await _send_json(send, 503, {
                "error": "mcp_auth_unavailable",
                "detail": str(e)[:200],
            })
            return

        if not expected:
            await _send_json(send, 401, {
                "error": "mcp_bearer_not_configured",
                "detail": "Set a bearer token in Developer Settings before using MCP.",
            })
            return

        auth = _header(scope, b"authorization")
        if not auth.lower().startswith("bearer "):
            await _send_json(send, 401, {
                "error": "missing_bearer",
                "detail": "Authorization: Bearer <token> required",
            })
            return

        presented = auth.split(" ", 1)[1].strip()
        if not presented or not secrets.compare_digest(presented, expected):
            await _send_json(send, 401, {
                "error": "invalid_bearer",
                "detail": "Bearer token rejected",
            })
            return

        await self.app(scope, receive, send)
