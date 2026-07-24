"""Key/value app settings (Developer → MCP server URL + bearer)."""

from __future__ import annotations

import secrets
import time

from db import read, write
from services.crypto import decrypt, encrypt

KEY_MCP_URL = "mcp_server_url"
KEY_MCP_BEARER = "mcp_bearer_token"

# Short cache so MCP middleware doesn't hit Aurora on every SSE chunk.
_cache: dict[str, tuple[float, str | None]] = {}
_CACHE_TTL = 5.0
_table_ready = False


async def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    await write(
        """CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
        )"""
    )
    _table_ready = True


def _cache_get(key: str) -> str | None | object:
    hit = _cache.get(key)
    if not hit:
        return _MISS
    ts, val = hit
    if time.monotonic() - ts > _CACHE_TTL:
        return _MISS
    return val


_MISS = object()


def invalidate_cache(*keys: str) -> None:
    if not keys:
        _cache.clear()
        return
    for k in keys:
        _cache.pop(k, None)


async def get_setting(key: str) -> str | None:
    cached = _cache_get(key)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]
    await _ensure_table()
    rows = await read("SELECT value FROM app_settings WHERE key = ?", (key,))
    val = rows[0]["value"] if rows else None
    _cache[key] = (time.monotonic(), val)
    return val


async def set_setting(key: str, value: str) -> None:
    await _ensure_table()
    await write(
        """INSERT INTO app_settings(key, value, updated_at)
           VALUES(?, ?, datetime('now','localtime'))
           ON CONFLICT (key) DO UPDATE
           SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at""",
        (key, value),
    )
    invalidate_cache(key)


async def get_mcp_bearer() -> str | None:
    """Plaintext bearer expected by the MCP server. None → MCP must reject all calls."""
    raw = await get_setting(KEY_MCP_BEARER)
    if not raw:
        return None
    try:
        return decrypt(raw).strip() or None
    except Exception:
        return raw.strip() or None


async def get_mcp_server_url() -> str | None:
    return await get_setting(KEY_MCP_URL)


async def set_mcp_server_url(url: str) -> None:
    await set_setting(KEY_MCP_URL, (url or "").strip())


async def set_mcp_bearer(token: str) -> None:
    token = (token or "").strip()
    if not token:
        raise ValueError("Bearer token cannot be empty")
    try:
        stored = encrypt(token)
    except Exception:
        stored = token  # ENCRYPTION_KEY missing — store plaintext
    await set_setting(KEY_MCP_BEARER, stored)


def new_bearer_token() -> str:
    return secrets.token_urlsafe(32)
