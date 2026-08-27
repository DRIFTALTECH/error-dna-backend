"""Community vision settings — Gemini key pool + SAP AI Core. Secrets never leave encrypted."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from services import vision as vis

router = APIRouter(prefix="/api/vision", tags=["vision"])


class SettingsIn(BaseModel):
    provider: str = Field(..., min_length=1, max_length=20)
    gemini_model: str | None = Field(None, max_length=80)


class GeminiKeyIn(BaseModel):
    api_key: str = Field(..., min_length=8, max_length=200)
    label: str = Field("", max_length=80)


class GeminiKeyPatch(BaseModel):
    api_key: str | None = Field(None, min_length=8, max_length=200)
    label: str | None = Field(None, max_length=80)
    is_enabled: bool | None = None


class AiCoreIn(BaseModel):
    client_id: str = Field("", max_length=200)
    client_secret: str = Field("", max_length=400)
    token_url: str = Field("", max_length=500)
    infer_url: str = Field(..., min_length=1, max_length=500)
    label: str = Field("", max_length=80)


def _settings_out(provider: str, model: str, keys: list, aicore: list) -> dict:
    return {
        "provider": provider,
        "gemini_model": model,
        "gemini_key_count": len(keys),
        "aicore_configured": bool(aicore and (aicore[0].get("secret_enc") or vis._extra(aicore[0]).get("infer_url"))),
    }


@router.get("/settings")
async def get_settings():
    provider = await vis.active_provider()
    model = await vis.gemini_model()
    keys = await vis.list_rows("gemini")
    aicore = await vis.list_rows("aicore")
    return _settings_out(provider, model, keys, aicore)


@router.put("/settings")
async def put_settings(body: SettingsIn):
    try:
        await vis.set_provider(body.provider, body.gemini_model)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return await get_settings()


@router.get("/keys")
async def list_keys():
    return [vis.public_row(r) for r in await vis.list_rows("gemini")]


@router.post("/keys")
async def add_key(body: GeminiKeyIn):
    try:
        row = await vis.add_gemini_key(body.api_key, body.label)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return vis.public_row(row)


@router.patch("/keys/{row_id}")
async def patch_key(row_id: int, body: GeminiKeyPatch):
    row = await vis.get_row(row_id)
    if not row or row.get("provider") != "gemini":
        raise HTTPException(404, "key not found")
    updated = await vis.patch_row(
        row_id, label=body.label, api_key=body.api_key, is_enabled=body.is_enabled,
    )
    return vis.public_row(updated)


@router.delete("/keys/{row_id}")
async def delete_key(row_id: int):
    row = await vis.get_row(row_id)
    if not row or row.get("provider") != "gemini":
        raise HTTPException(404, "key not found")
    await vis.delete_row(row_id)
    return {"ok": True}


@router.get("/aicore")
async def get_aicore():
    rows = await vis.list_rows("aicore")
    if not rows:
        return {
            "configured": False,
            "id": None,
            "label": "",
            "client_id": "",
            "token_url": "",
            "infer_url": "",
            "secret_configured": False,
            "secret_masked": "",
            "last_error": "",
            "last_used_at": "",
        }
    out = vis.public_row(rows[0])
    out["configured"] = True
    return out


@router.put("/aicore")
async def put_aicore(body: AiCoreIn):
    if not (body.infer_url or "").strip():
        raise HTTPException(400, "infer_url is required")
    row = await vis.upsert_aicore(
        client_id=body.client_id,
        token_url=body.token_url,
        infer_url=body.infer_url,
        client_secret=body.client_secret,
        label=body.label,
    )
    out = vis.public_row(row)
    out["configured"] = True
    return out


@router.post("/test")
async def test_vision():
    """Hit the active provider with a 1×1 PNG. Proves auth + wiring, not image quality."""
    provider = await vis.active_provider()
    if provider == "off":
        raise HTTPException(400, "Provider is Off — pick Gemini or SAP AI Core first")
    out = await vis.describe(vis.TEST_PNG, "image/png")
    ok = bool(out.get("caption") or out.get("ocr_text"))
    # Surface the last error if describe returned empty.
    err = ""
    if not ok:
        rows = await vis.list_rows(provider)
        err = (rows[0].get("last_error") if rows else "") or "No caption returned"
    return {
        "ok": ok,
        "provider": provider,
        "caption": out.get("caption") or "",
        "ocr_text": out.get("ocr_text") or "",
        "kind": out.get("kind") or "",
        "error": "" if ok else err,
    }
