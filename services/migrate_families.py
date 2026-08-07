"""One-shot LLM migration: old summary family labels → new CSV family codes.

Run once:
    python3 -m services.migrate_families

Skips rows whose family is already a valid catalog code (safe to re-run, but intended once).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone, timedelta

import httpx

from config import LLM_API_KEY, LLM_API_URL, LLM_MODEL
from db import init_db, read, write
from services.error_families import catalog_for_llm, valid_codes

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

_SYSTEM = """You classify integration knowledge-base articles into exactly ONE error family code.

The old family label (e.g. "Authentication", "Connection") is often too broad — ignore it when a
more specific code fits the actual failure or problem the article addresses.

Pick family_code from this catalog (exact code string):
{family_catalog}

Output ONLY valid JSON:
{{
  "family_code": "CODE_FROM_CATALOG",
  "confidence": 0-100,
  "reason": "one sentence why this code fits"
}}

No markdown, no code fences."""

_CONCURRENCY = 2
_DELAY_SEC = 0.5
_RETRIES = 4


def _note_blob(row: dict) -> str:
    parts = [
        f"OLD_FAMILY: {row.get('family') or ''}",
        f"TITLE: {row.get('title') or ''}",
        f"ISSUE: {row.get('issue') or ''}",
        f"SUMMARY: {row.get('summary') or ''}",
    ]
    return "\n\n".join(parts)


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
            if resp.status_code == 429:
                await asyncio.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            if not (content or "").strip():
                raise ValueError("empty LLM response")
            return _parse_llm_json(content)
        except Exception as e:
            last_err = e
            await asyncio.sleep(1.5 * (attempt + 1))
    raise last_err or RuntimeError("classify failed")


async def migrate_all() -> dict:
    """Classify every latest summary still on a legacy family label; update DB."""
    if not LLM_API_KEY:
        raise RuntimeError("LLM_API_KEY is not set — cannot classify notes")

    codes = await valid_codes()
    catalog = await catalog_for_llm()
    system = _SYSTEM.format(family_catalog=catalog)

    rows = await read(
        """SELECT id, source_id, title, issue, summary, family
           FROM summaries WHERE is_latest = 1 ORDER BY id""",
    )
    todo = [r for r in rows if (r.get("family") or "").strip() not in codes]
    skipped = len(rows) - len(todo)

    print(f"📋 {len(rows)} latest summaries — {skipped} already on new codes, {len(todo)} to migrate")

    if not todo:
        return {"total": len(rows), "skipped": skipped, "updated": 0, "failed": 0}

    updated = 0
    failed = 0
    sem = asyncio.Semaphore(_CONCURRENCY)
    now = datetime.now(IST).isoformat()

    async with httpx.AsyncClient(timeout=90.0) as client:

        async def one(row: dict, idx: int) -> None:
            nonlocal updated, failed
            async with sem:
                old = row.get("family") or ""
                try:
                    data = await _classify_note(client, system, row)
                    code = (data.get("family_code") or "").strip()
                    if code not in codes:
                        code = "UNCLASSIFIED_ERROR"
                    conf = float(data.get("confidence") or 0)
                    reason = data.get("reason") or ""

                    await write(
                        "UPDATE summaries SET family = ?, area = ?, updated_at = ? WHERE id = ?",
                        (code, code, now, row["id"]),
                    )
                    await write(
                        """INSERT INTO family_migration_log
                           (summary_id, source_id, old_family, new_family, confidence, reason)
                           VALUES (?,?,?,?,?,?)""",
                        (row["id"], row.get("source_id"), old, code, conf, reason),
                    )
                    updated += 1
                    print(
                        f"  [{idx}/{len(todo)}] #{row['id']} {row.get('source_id')} "
                        f"{old!r} → {code} ({conf:.0f}%)"
                    )
                except Exception as e:
                    failed += 1
                    logger.exception("migrate summary %s failed", row["id"])
                    print(f"  [{idx}/{len(todo)}] #{row['id']} FAILED: {e}")
                await asyncio.sleep(_DELAY_SEC)

        await asyncio.gather(*[one(r, i + 1) for i, r in enumerate(todo)])

    print(f"\n✅ Done — updated {updated}, skipped {skipped}, failed {failed}")
    return {"total": len(rows), "skipped": skipped, "updated": updated, "failed": failed}


async def _main() -> None:
    await init_db()
    stats = await migrate_all()
    if stats["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_main())
