"""Community-only vision: describe a screenshot so DeepSeek can place it and copy OCR.

Provider (app_settings.vision_provider): off | gemini | aicore.
Gemini keys rotate; SAP AI Core is one row whose infer_url returns a text summary.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from config import VISION_COOLDOWN_SEC, VISION_TIMEOUT
from db import read, write
from services.app_settings import get_setting, set_setting
from services.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

KEY_PROVIDER = "vision_provider"
KEY_GEMINI_MODEL = "vision_gemini_model"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
PROVIDERS = ("off", "gemini", "aicore")
KINDS = ("error_dialog", "screenshot", "diagram", "other")

EMPTY = {"caption": "", "ocr_text": "", "kind": ""}

_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_DESCRIBE_PROMPT = """You are reading a screenshot from an SAP Integration Suite / Cloud Integration forum post.
Return JSON only, no markdown:
{"caption": "one sentence of what the image shows",
 "ocr_text": "verbatim text visible in the image (error codes, HTTP status, adapter names, stack frames). Empty string if none.",
 "kind": "error_dialog" | "screenshot" | "diagram" | "other"}
"""


class ProviderDown(Exception):
    """5xx — the service is down, not this key. Back the whole provider off."""


class QuotaError(Exception):
    """429 / 401 / 403 — cool this Gemini key and try the next."""

    def __init__(self, status: int, detail: str = ""):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status


def _now() -> datetime:
    return datetime.now(IST)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def pick_gemini_row(rows: list[dict], now: datetime) -> dict | None:
    """Least-recently-used enabled key whose cooldown has expired."""
    ready = []
    for r in rows:
        if not int(r.get("is_enabled") or 0):
            continue
        until = _parse_iso(r.get("cooldown_until"))
        if until and until > now:
            continue
        ready.append(r)
    if not ready:
        return None
    ready.sort(key=lambda r: r.get("last_used_at") or "")
    return ready[0]


def _mask(secret: str) -> str:
    s = secret or ""
    if not s:
        return ""
    if s.startswith("AIza") and len(s) > 8:
        return f"AIza***{s[-4:]}"
    return f"****{s[-4:]}" if len(s) > 4 else "****"


def _extra(row: dict) -> dict:
    raw = row.get("extra") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _secret(row: dict) -> str:
    return decrypt(row.get("secret_enc") or "") or ""


async def active_provider() -> str:
    val = (await get_setting(KEY_PROVIDER) or "off").strip().lower()
    return val if val in PROVIDERS else "off"


async def gemini_model() -> str:
    return (await get_setting(KEY_GEMINI_MODEL) or "").strip() or DEFAULT_GEMINI_MODEL


async def set_provider(provider: str, model: str | None = None) -> None:
    p = (provider or "off").strip().lower()
    if p not in PROVIDERS:
        raise ValueError(f"provider must be one of {PROVIDERS}")
    await set_setting(KEY_PROVIDER, p)
    if model is not None:
        await set_setting(KEY_GEMINI_MODEL, (model or "").strip() or DEFAULT_GEMINI_MODEL)


def public_row(row: dict) -> dict:
    """API shape: never the plaintext secret."""
    secret = _secret(row)
    extra = _extra(row)
    return {
        "id": row["id"],
        "provider": row["provider"],
        "label": row.get("label") or "",
        "is_enabled": bool(int(row.get("is_enabled") or 0)),
        "secret_configured": bool(secret),
        "secret_masked": _mask(secret),
        "client_id": extra.get("client_id") or "",
        "token_url": extra.get("token_url") or "",
        "infer_url": extra.get("infer_url") or "",
        "last_used_at": row.get("last_used_at") or "",
        "last_error": row.get("last_error") or "",
        "cooldown_until": row.get("cooldown_until") or "",
        "created_at": row.get("created_at") or "",
    }


async def list_rows(provider: str) -> list[dict]:
    return await read(
        "SELECT * FROM vision_credentials WHERE provider=? ORDER BY id ASC", (provider,)
    )


async def get_row(row_id: int) -> dict | None:
    rows = await read("SELECT * FROM vision_credentials WHERE id=?", (row_id,))
    return rows[0] if rows else None


async def add_gemini_key(api_key: str, label: str = "") -> dict:
    key = (api_key or "").strip()
    if not key:
        raise ValueError("api_key is required")
    rows = await write(
        """INSERT INTO vision_credentials (provider, label, secret_enc, extra, is_enabled)
           VALUES ('gemini', ?, ?, '{}', 1) RETURNING *""",
        ((label or "").strip() or "Gemini key", encrypt(key)),
    )
    return rows[0]


async def patch_row(row_id: int, *, label=None, api_key=None, is_enabled=None, extra=None) -> dict:
    row = await get_row(row_id)
    if not row:
        raise KeyError(row_id)
    fields, params = [], []
    if label is not None:
        fields.append("label=?"); params.append(label.strip())
    if api_key:
        fields.append("secret_enc=?"); params.append(encrypt(api_key.strip()))
    if is_enabled is not None:
        fields.append("is_enabled=?"); params.append(1 if is_enabled else 0)
    if extra is not None:
        fields.append("extra=?"); params.append(json.dumps(extra, ensure_ascii=False))
    if not fields:
        return row
    params.append(row_id)
    await write(f"UPDATE vision_credentials SET {', '.join(fields)} WHERE id=? RETURNING id", tuple(params))
    return (await get_row(row_id)) or row


async def delete_row(row_id: int) -> None:
    await write("DELETE FROM vision_credentials WHERE id=? RETURNING id", (row_id,))


async def upsert_aicore(*, client_id: str, token_url: str, infer_url: str,
                        client_secret: str = "", label: str = "") -> dict:
    extra = json.dumps({
        "client_id": (client_id or "").strip(),
        "token_url": (token_url or "").strip(),
        "infer_url": (infer_url or "").strip(),
    }, ensure_ascii=False)
    existing = await list_rows("aicore")
    if existing:
        kwargs = dict(label=label or existing[0].get("label") or "SAP AI Core", extra=json.loads(extra))
        if (client_secret or "").strip():
            kwargs["api_key"] = client_secret.strip()
        return await patch_row(existing[0]["id"], **kwargs)
    secret = encrypt((client_secret or "").strip())
    rows = await write(
        """INSERT INTO vision_credentials (provider, label, secret_enc, extra, is_enabled)
           VALUES ('aicore', ?, ?, ?, 1) RETURNING *""",
        ((label or "").strip() or "SAP AI Core", secret, extra),
    )
    return rows[0]


async def _mark(row_id: int, *, error: str | None = None, cooldown: bool = False) -> None:
    until = (_now() + timedelta(seconds=VISION_COOLDOWN_SEC)).isoformat() if cooldown else ""
    await write(
        """UPDATE vision_credentials
           SET last_used_at=?, last_error=?, cooldown_until=?
           WHERE id=? RETURNING id""",
        (_now_iso(), (error or "")[:400], until, row_id),
    )


def _parse_describe(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"caption": raw[:800], "ocr_text": "", "kind": "screenshot"} if raw else dict(EMPTY)
    if not isinstance(data, dict):
        return dict(EMPTY)
    kind = (data.get("kind") or "screenshot").strip()
    if kind not in KINDS:
        kind = "screenshot"
    return {
        "caption": str(data.get("caption") or "").strip()[:800],
        "ocr_text": str(data.get("ocr_text") or "").strip()[:2000],
        "kind": kind,
    }


def _response_text(resp: httpx.Response) -> str:
    """AI Core infer_url returns the image summary as text (maybe wrapped in JSON)."""
    raw = (resp.text or "").strip()
    if not raw:
        return ""
    if raw[:1] in "{[":
        try:
            data = resp.json()
        except Exception:
            return raw
        if isinstance(data, str):
            return data.strip()
        if isinstance(data, dict):
            for k in ("summary", "text", "caption", "content", "result"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()
            try:
                return data["choices"][0]["message"]["content"].strip()
            except (KeyError, IndexError, TypeError, AttributeError):
                pass
        return raw
    return raw


async def _gemini(image_bytes: bytes, mime: str, api_key: str, model: str) -> dict:
    url = _GEMINI_URL.format(model=model)
    payload = {
        "contents": [{
            "parts": [
                {"text": _DESCRIBE_PROMPT},
                {"inline_data": {"mime_type": mime, "data": base64.b64encode(image_bytes).decode()}},
            ]
        }],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(timeout=VISION_TIMEOUT) as client:
        r = await client.post(url, headers={"x-goog-api-key": api_key}, json=payload)
    if r.status_code in (401, 403, 429):
        raise QuotaError(r.status_code, r.text[:200])
    if r.status_code >= 500:
        raise ProviderDown(f"HTTP {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"unexpected Gemini body: {e}") from e
    return _parse_describe(text)


async def _aicore_token(token_url: str, client_id: str, client_secret: str) -> str:
    async with httpx.AsyncClient(timeout=VISION_TIMEOUT) as client:
        r = await client.post(
            token_url,
            data={"grant_type": "client_credentials", "client_id": client_id,
                  "client_secret": client_secret},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    r.raise_for_status()
    token = (r.json() or {}).get("access_token") or ""
    if not token:
        raise RuntimeError("AI Core token_url returned no access_token")
    return token


async def _aicore(image_bytes: bytes, mime: str, row: dict) -> dict:
    """POST the image at infer_url; the endpoint returns a text summary of the image."""
    extra = _extra(row)
    infer_url = (extra.get("infer_url") or "").strip()
    if not infer_url:
        raise RuntimeError("SAP AI Core infer_url is empty")
    headers = {}
    token_url = (extra.get("token_url") or "").strip()
    client_id = (extra.get("client_id") or "").strip()
    secret = _secret(row)
    if token_url and client_id and secret:
        headers["Authorization"] = f"Bearer {await _aicore_token(token_url, client_id, secret)}"
    async with httpx.AsyncClient(timeout=VISION_TIMEOUT) as client:
        r = await client.post(
            infer_url,
            headers=headers,
            json={"image": base64.b64encode(image_bytes).decode(), "mime_type": mime},
        )
    r.raise_for_status()
    caption = _response_text(r)
    if not caption:
        return dict(EMPTY)
    return {"caption": caption[:800], "ocr_text": "", "kind": "screenshot"}


# When the provider itself is 5xx-ing, every further image would pay the full
# VISION_TIMEOUT before failing the same way. Back off once, process-wide, so a
# thread with N screenshots costs one timeout instead of N.
# ponytail: module global, resets on restart. Per-provider state if we ever run two.
_provider_down_until: datetime | None = None


def provider_backoff_left() -> int:
    """Seconds until the provider is retried, 0 when it is not backed off."""
    if _provider_down_until is None:
        return 0
    return max(0, int((_provider_down_until - _now()).total_seconds()))


async def describe(image_bytes: bytes, mime: str = "image/png") -> dict:
    """Look at one image. Empty result = caller keeps alt/context. Never raises for 'off'."""
    global _provider_down_until
    if not image_bytes:
        return dict(EMPTY)
    provider = await active_provider()
    if provider == "off":
        return dict(EMPTY)
    if provider_backoff_left():
        return dict(EMPTY)
    if provider == "aicore":
        rows = await list_rows("aicore")
        if not rows:
            return dict(EMPTY)
        row = rows[0]
        try:
            out = await _aicore(image_bytes, mime, row)
            await _mark(row["id"])
            return out
        except ProviderDown as e:
            _provider_down_until = _now() + timedelta(seconds=VISION_COOLDOWN_SEC)
            logger.warning(f"AI Core down — vision backed off {VISION_COOLDOWN_SEC}s: {e}")
            await _mark(row["id"], error=str(e)[:400])
            return dict(EMPTY)
        except Exception as e:
            logger.warning(f"AI Core vision failed: {e}")
            await _mark(row["id"], error=str(e)[:400])
            return dict(EMPTY)

    model = await gemini_model()
    keys = await list_rows("gemini")
    last_err = ""
    for _ in range(max(len(keys), 1)):
        row = pick_gemini_row(keys, _now())
        if not row:
            break
        try:
            out = await _gemini(image_bytes, mime, _secret(row), model)
            await _mark(row["id"])
            return out
        except ProviderDown as e:
            # Not this key's fault — every key would get the same 5xx.
            last_err = str(e)[:400]
            _provider_down_until = _now() + timedelta(seconds=VISION_COOLDOWN_SEC)
            logger.warning(f"Gemini down — vision backed off {VISION_COOLDOWN_SEC}s: {e}")
            await _mark(row["id"], error=last_err)
            break
        except QuotaError as e:
            last_err = str(e)
            await _mark(row["id"], error=last_err, cooldown=True)
            row["cooldown_until"] = (_now() + timedelta(seconds=VISION_COOLDOWN_SEC)).isoformat()
            row["is_enabled"] = row.get("is_enabled") or 1
        except Exception as e:
            last_err = str(e)[:400]
            logger.warning(f"Gemini vision failed (key {row['id']}): {e}")
            await _mark(row["id"], error=last_err)
            break
    if last_err:
        logger.warning(f"vision.describe gave up: {last_err}")
    return dict(EMPTY)


# 1×1 PNG for POST /api/vision/test — not a real screenshot, just proves auth + wiring.
TEST_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


if __name__ == "__main__":
    import inspect
    import sys

    now = datetime(2026, 8, 27, 12, 0, tzinfo=IST)
    rows = [
        {"id": 1, "is_enabled": 1, "last_used_at": "2026-08-27T11:00:00+05:30", "cooldown_until": ""},
        {"id": 2, "is_enabled": 1, "last_used_at": "", "cooldown_until": ""},
        {"id": 3, "is_enabled": 1, "last_used_at": "", "cooldown_until": "2026-08-27T12:05:00+05:30"},
        {"id": 4, "is_enabled": 0, "last_used_at": "", "cooldown_until": ""},
    ]
    pick = pick_gemini_row(rows, now)
    assert pick and pick["id"] == 2, pick  # unused, not cooling

    cooled = pick_gemini_row([rows[2]], now)
    assert cooled is None

    disabled = pick_gemini_row([rows[3]], now)
    assert disabled is None

    parsed = _parse_describe('{"caption":"CPI adapter","ocr_text":"HTTP 401","kind":"error_dialog"}')
    assert parsed["ocr_text"] == "HTTP 401" and parsed["kind"] == "error_dialog"
    fenced = _parse_describe('```json\n{"caption":"x","ocr_text":"","kind":"diagram"}\n```')
    assert fenced["caption"] == "x" and fenced["kind"] == "diagram"
    assert _parse_describe("plain caption")["caption"] == "plain caption"
    assert _mask("AIzaSyDummyKeyXXXX") == "AIza***XXXX"
    assert _mask("supersecret") == "****cret"

    class _R:
        def __init__(self, text, data=None):
            self.text = text
            self._data = data
        def json(self):
            return self._data
    assert _response_text(_R("the screenshot shows an OAuth error")) == "the screenshot shows an OAuth error"
    assert _response_text(_R('{"summary":"oauth failed"}', {"summary": "oauth failed"})) == "oauth failed"

    src = inspect.getsource(sys.modules[__name__]).split('if __name__')[0]
    for banned in ("ingest_chain", "extract_note", "error_diagnose"):
        assert banned not in src, banned

    print("✅ vision self-check passed (rotation, cooldown, parse, no notes imports)")
