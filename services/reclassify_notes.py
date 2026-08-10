"""LLM reclassification for notes with missing, stale, or unclassified families.

Processes one note at a time in the background (same pattern as embed_backfill).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

from config import LLM_API_KEY, LLM_API_URL, LLM_MODEL
from db import read, write
from services.error_families import catalog_for_llm, classify_text, valid_codes

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

INTER_ITEM_SLEEP_SEC = 0.35
_RETRIES = 6
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_UNCLASSIFIED = "UNCLASSIFIED_ERROR"

_SYSTEM = """You classify SAP integration knowledge-base articles into exactly ONE error family code.

The note is currently unclassified or has a stale label — assign the best-matching SPECIFIC family.
UNCLASSIFIED_ERROR is NOT in the catalog and must NOT be returned.

Pick family_code from this catalog (exact code string):
{family_catalog}

Rules:
- You MUST pick a specific family from the catalog above.
- Choose the closest match even if confidence is moderate; only pick when content is completely unrelated.
- family_code must be an exact catalog code string.

Output ONLY valid JSON:
{{
  "family_code": "CODE_FROM_CATALOG",
  "confidence": 0-100,
  "reason": "one sentence why this code fits"
}}

No markdown, no code fences."""

_running = False
# batch_* keys only — never "pending"/"total" (those come from counts() and must not be overwritten).
_state: dict[str, Any] = {
    "batch_total": 0,
    "batch_done": 0,
    "updated": 0,
    "unchanged": 0,
    "skipped": 0,
    "failed": 0,
    "current": None,
    "last_error": None,
    "recent": [],
}


def is_running() -> bool:
    return _running


def status() -> dict[str, Any]:
    return {"running": _running, **_state}


def needs_reclassify(family: str | None, codes: set[str]) -> bool:
    """True when a note should be sent through LLM family assignment."""
    label = (family or "").strip()
    if not label:
        return True
    if label == _UNCLASSIFIED:
        return True
    return label not in codes


def _note_blob(row: dict) -> str:
    raw = (row.get("family") or "").strip()
    if not raw or raw == _UNCLASSIFIED:
        current = "(none — pick a specific family from the catalog)"
    else:
        current = raw
    parts = [
        f"CURRENT_FAMILY: {current}",
        f"TITLE: {row.get('title') or ''}",
        f"ISSUE: {row.get('issue') or ''}",
        f"SUMMARY: {row.get('summary') or ''}",
    ]
    return "\n\n".join(parts)


def _resolve_code(
    llm_code: str,
    assignable: set[str],
    regex_code: str | None,
) -> tuple[str, str]:
    """Pick a specific family code; never return UNCLASSIFIED from reclassify."""
    code = (llm_code or "").strip()
    if code in assignable:
        return code, ""
    if regex_code and regex_code in assignable:
        return regex_code, "pattern match"
    return _UNCLASSIFIED, ""


def _parse_llm_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    return json.loads(text)


async def _classify_note(client: httpx.AsyncClient, system: str, row: dict) -> dict:
    last_err: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            resp = await client.post(
                LLM_API_URL,
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={
                    "model": LLM_MODEL,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": _note_blob(row)},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 512,
                    "response_format": {"type": "json_object"},
                },
            )
            if resp.status_code in _RETRYABLE_STATUS:
                wait = min(30, 2 ** attempt)
                logger.warning("reclassify retry %s HTTP %s, wait %ss", attempt + 1, resp.status_code, wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            payload = resp.json()
            content = payload.get("choices", [{}])[0].get("message", {}).get("content")
            if not (content or "").strip():
                raise ValueError("empty LLM response")
            return _parse_llm_json(content)
        except (json.JSONDecodeError, ValueError, KeyError, httpx.HTTPError) as e:
            last_err = e
            wait = min(20, 1.5 * (attempt + 1))
            if attempt < _RETRIES - 1:
                await asyncio.sleep(wait)
    raise last_err or RuntimeError("classify failed")


def _push_recent(entry: dict) -> None:
    recent = _state.get("recent") or []
    sid = entry.get("id")
    recent = [r for r in recent if r.get("id") != sid]
    recent.insert(0, entry)
    _state["recent"] = recent[:25]


async def counts() -> dict[str, int]:
    codes = await valid_codes()
    rows = await read(
        """SELECT id, family FROM summaries WHERE is_latest = 1 ORDER BY id""",
    )
    pending = sum(1 for r in rows if needs_reclassify(r.get("family"), codes))
    return {
        "total_notes": len(rows),
        "classified": len(rows) - pending,
        "pending": pending,
    }


async def _pending_rows() -> list[dict]:
    codes = await valid_codes()
    rows = await read(
        """SELECT id, source_id, title, issue, summary, family
           FROM summaries WHERE is_latest = 1 ORDER BY id""",
    )
    return [r for r in rows if needs_reclassify(r.get("family"), codes)]


async def _apply_one(
    client: httpx.AsyncClient,
    system: str,
    assignable: set[str],
    row: dict,
    now: str,
) -> dict:
    old = (row.get("family") or "").strip()
    blob = f"{row.get('issue') or ''} {row.get('summary') or ''}"
    regex_code = await classify_text(blob)

    data = await _classify_note(client, system, row)
    code, fallback_note = _resolve_code(
        data.get("family_code") or "",
        assignable,
        regex_code if regex_code != _UNCLASSIFIED else None,
    )
    conf = float(data.get("confidence") or 0)
    reason = data.get("reason") or ""
    if fallback_note:
        reason = f"{reason} ({fallback_note})".strip()

    if code == _UNCLASSIFIED or code not in assignable:
        return {
            "id": row["id"],
            "source_id": row.get("source_id"),
            "old_family": old or None,
            "new_family": old or _UNCLASSIFIED,
            "confidence": conf,
            "reason": reason,
            "changed": False,
            "skipped": True,
        }

    changed = code != old
    if changed:
        await write(
            "UPDATE summaries SET family = ?, area = ?, updated_at = ? WHERE id = ?",
            (code, code, now, row["id"]),
        )
        await write(
            """INSERT INTO family_migration_log
               (summary_id, source_id, old_family, new_family, confidence, reason)
               VALUES (?,?,?,?,?,?)""",
            (row["id"], row.get("source_id"), old or None, code, conf, reason),
        )

    return {
        "id": row["id"],
        "source_id": row.get("source_id"),
        "old_family": old or None,
        "new_family": code,
        "confidence": conf,
        "reason": reason,
        "changed": changed,
        "skipped": False,
    }


async def _run() -> None:
    global _running
    if not LLM_API_KEY:
        _state["last_error"] = "LLM_API_KEY is not set"
        _running = False
        return

    try:
        codes = await valid_codes()
        assignable = codes - {_UNCLASSIFIED}
        catalog = await catalog_for_llm(exclude={_UNCLASSIFIED})
        system = _SYSTEM.format(family_catalog=catalog)
        rows = await _pending_rows()

        _state["batch_total"] = len(rows)
        _state["batch_done"] = 0
        _state["updated"] = 0
        _state["unchanged"] = 0
        _state["skipped"] = 0
        _state["failed"] = 0
        _state["current"] = None
        _state["last_error"] = None
        _state["recent"] = []

        now = datetime.now(IST).isoformat()

        async with httpx.AsyncClient(timeout=120.0) as client:
            for row in rows:
                _state["current"] = row.get("source_id") or str(row["id"])
                try:
                    result = await _apply_one(client, system, assignable, row, now)
                    if result.get("skipped"):
                        _state["skipped"] += 1
                    elif result["changed"]:
                        _state["updated"] += 1
                        _push_recent({**result, "status": "ok"})
                        logger.info(
                            "reclassify #%s %s: %r → %s",
                            row["id"],
                            row.get("source_id"),
                            result["old_family"],
                            result["new_family"],
                        )
                    else:
                        _state["unchanged"] += 1
                except Exception as e:
                    _state["failed"] += 1
                    _state["last_error"] = str(e)[:300]
                    _push_recent({
                        "id": row["id"],
                        "source_id": row.get("source_id"),
                        "old_family": row.get("family"),
                        "status": "error",
                        "error": str(e)[:200],
                    })
                    logger.warning("reclassify #%s failed: %s", row["id"], e)
                _state["batch_done"] += 1
                await asyncio.sleep(INTER_ITEM_SLEEP_SEC)
    finally:
        _state["current"] = None
        _running = False


def start() -> tuple[bool, str | None]:
    """Start background reclassification. Returns (started, error_message)."""
    global _running
    if _running:
        return False, None
    if not LLM_API_KEY:
        return False, "LLM_API_KEY is not set"
    _running = True
    asyncio.create_task(_run())
    return True, None
