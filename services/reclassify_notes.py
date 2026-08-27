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
from services.error_families import catalog_for_llm, valid_codes

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

INTER_ITEM_SLEEP_SEC = 3.0  # pause between notes — strictly one LLM call at a time
_LLM_TIMEOUT_SEC = 180.0
_RETRIES = 8
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


def _json_list(raw) -> list:
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else [str(v)]
    except (json.JSONDecodeError, TypeError):
        return [s.strip() for s in str(raw).split(",") if s.strip()]


def _format_steps(raw) -> str:
    lines: list[str] = []
    for step in _json_list(raw):
        if isinstance(step, dict):
            title = (step.get("title") or "").strip()
            details = [str(d).strip() for d in step.get("details", []) if str(d).strip()]
            block = f"{title}: {' '.join(details)}" if details else title
            if block:
                lines.append(block)
        elif str(step).strip():
            lines.append(str(step).strip())
    return "\n".join(lines)


def _note_blob(row: dict) -> str:
    raw = (row.get("family") or "").strip()
    if not raw or raw == _UNCLASSIFIED:
        current = "(none — pick a specific family from the catalog)"
    else:
        current = raw
    steps = _format_steps(row.get("steps"))
    parts = [
        f"CURRENT_FAMILY: {current}",
        f"TITLE: {row.get('title') or ''}",
        f"ISSUE: {row.get('issue') or ''}",
        f"SUMMARY: {row.get('summary') or ''}",
    ]
    if steps:
        parts.append(f"STEPS (how to fix):\n{steps}")
    return "\n\n".join(parts)


def _resolve_code(llm_code: str, assignable: set[str]) -> tuple[str, str]:
    """Pick a specific family code. The LLM is the only classifier."""
    code = (llm_code or "").strip()
    if code in assignable:
        return code, ""
    return _UNCLASSIFIED, "not in catalog"


def _parse_llm_json(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("LLM response is not a JSON object")
    if not (data.get("family_code") or "").strip():
        for key in ("familyCode", "code", "family"):
            if (data.get(key) or "").strip():
                data["family_code"] = data[key]
                break
    return data


def _parse_confidence(raw) -> float:
    try:
        if isinstance(raw, str):
            raw = raw.strip().rstrip("%")
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


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
                    "max_tokens": 1024,
                    "response_format": {"type": "json_object"},
                },
            )
            if resp.status_code in _RETRYABLE_STATUS:
                wait = min(45, 3 * (attempt + 1))
                logger.warning(
                    "reclassify retry %s/%s HTTP %s, wait %ss",
                    attempt + 1, _RETRIES, resp.status_code, wait,
                )
                await asyncio.sleep(wait)
                continue
            if resp.status_code >= 400:
                body = (resp.text or "")[:400]
                raise ValueError(f"HTTP {resp.status_code}: {body}")
            payload = resp.json()
            content = payload.get("choices", [{}])[0].get("message", {}).get("content")
            if not (content or "").strip():
                raise ValueError("empty LLM response")
            return _parse_llm_json(content)
        except (json.JSONDecodeError, ValueError, KeyError, httpx.HTTPError) as e:
            last_err = e
            wait = min(30, 2 * (attempt + 1))
            if attempt < _RETRIES - 1:
                logger.warning("reclassify attempt %s/%s failed: %s", attempt + 1, _RETRIES, e)
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
        """SELECT id, source_id, title, issue, summary, steps, family
           FROM summaries WHERE is_latest = 1 ORDER BY id""",
    )
    return [r for r in rows if needs_reclassify(r.get("family"), codes)]


async def _llm_classify(
    client: httpx.AsyncClient,
    system: str,
    row: dict,
    assignable: set[str],
) -> tuple[str, float, str, str]:
    """Returns (code, confidence, reason, source). source is always 'llm'."""
    data = await _classify_note(client, system, row)

    code, fallback_note = _resolve_code(data.get("family_code") or "", assignable)
    conf = _parse_confidence(data.get("confidence"))
    reason = (data.get("reason") or "").strip()
    if fallback_note:
        reason = f"{reason} ({fallback_note})".strip() if reason else fallback_note
    return code, conf, reason, "llm"


async def _apply_one(
    client: httpx.AsyncClient,
    system: str,
    assignable: set[str],
    row: dict,
    now: str,
) -> dict:
    old = (row.get("family") or "").strip()
    code, conf, reason, source = await _llm_classify(client, system, row, assignable)

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
            (row["id"], row.get("source_id"), old or None, code, conf, f"{reason} [{source}]"),
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


def _record_result(
    result: dict,
    row: dict,
    exc: Exception | None = None,
    *,
    retry: bool = False,
) -> None:
    if exc is not None:
        if not retry:
            _state["failed"] += 1
        msg = str(exc)[:300]
        _state["last_error"] = msg
        _push_recent({
            "id": row["id"],
            "source_id": row.get("source_id"),
            "old_family": row.get("family"),
            "status": "error",
            "error": msg[:200],
        })
        logger.warning("reclassify #%s failed: %s", row["id"], exc)
        return

    if result.get("skipped"):
        _state["skipped"] += 1
    elif result["changed"]:
        if retry:
            _state["failed"] = max(0, _state["failed"] - 1)
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


async def _process_rows(
    client: httpx.AsyncClient,
    system: str,
    assignable: set[str],
    rows: list[dict],
    now: str,
    *,
    count_progress: bool = True,
    retry: bool = False,
) -> list[dict]:
    """Process notes strictly one at a time; return rows that failed."""
    failed: list[dict] = []
    for row in rows:
        _state["current"] = row.get("source_id") or str(row["id"])
        try:
            result = await _apply_one(client, system, assignable, row, now)
            _record_result(result, row, retry=retry)
        except Exception as e:
            _record_result({}, row, exc=e, retry=retry)
            if not retry:
                failed.append(row)
        if count_progress:
            _state["batch_done"] += 1
        await asyncio.sleep(INTER_ITEM_SLEEP_SEC)
    return failed


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

        async with httpx.AsyncClient(timeout=_LLM_TIMEOUT_SEC) as client:
            failed = await _process_rows(client, system, assignable, rows, now)

            if failed:
                logger.info("reclassify second pass for %s failed note(s)", len(failed))
                await asyncio.sleep(10)
                await _process_rows(
                    client, system, assignable, failed, now,
                    count_progress=False, retry=True,
                )
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
