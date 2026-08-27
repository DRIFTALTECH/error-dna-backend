"""Backfill Titan embeddings for summaries whose vector row is missing or stale.

One job at a time, one row at a time, so a Bedrock/IAM blip doesn't take down the
batch. Kicking again while running is a no-op.

It walks every latest summary, not only the un-embedded ones: `upsert_embedding`
compares content hashes and returns "skipped" when nothing changed, so this is
also how a build_blob() change gets rolled out.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from db import read
from services.embeddings import upsert_embedding

logger = logging.getLogger(__name__)

# Small pause between Bedrock calls — avoid bursting InvokeModel.
INTER_ITEM_SLEEP_SEC = 0.5

_running = False
_state: dict[str, Any] = {
    "total": 0,
    "done": 0,
    "created": 0,
    "updated": 0,
    "failed": 0,
    "skipped": 0,
    "current": None,
    "last_error": None,
}


def is_running() -> bool:
    return _running


def status() -> dict[str, Any]:
    return {"running": _running, **_state}


async def counts() -> dict[str, int]:
    """How many latest summaries have / lack a vector row, across both sources."""
    total = embedded = 0
    for source, table in (("notes", "summaries"), ("community", "community_summaries")):
        total += int((await read(
            f"SELECT COUNT(*) AS c FROM {table} WHERE is_latest = 1",
        ))[0]["c"])
        embedded += int((await read(
            f"""SELECT COUNT(*) AS c FROM {table} s
                WHERE s.is_latest = 1
                  AND EXISTS (
                    SELECT 1 FROM summary_embeddings e
                    WHERE e.source = ? AND e.summary_id = s.id
                  )""",
            (source,),
        ))[0]["c"])
    return {"total": total, "embedded": embedded, "missing": total - embedded}


_ROW_COLS = ("id, source_id, title, family, issue, summary, tags, gotchas, "
             "error_signatures, search_text")


async def _rows_to_embed() -> list[tuple[str, dict]]:
    """Every latest summary, notes first. (source, row) — stale hashes re-embed."""
    out: list[tuple[str, dict]] = []
    for source, table in (("notes", "summaries"), ("community", "community_summaries")):
        rows = await read(f"SELECT {_ROW_COLS} FROM {table} WHERE is_latest = 1 ORDER BY id")
        out.extend((source, r) for r in rows)
    return out


async def _run() -> None:
    global _running
    try:
        rows = await _rows_to_embed()
        _state["total"] = len(rows)
        _state["done"] = 0
        _state["created"] = 0
        _state["updated"] = 0
        _state["failed"] = 0
        _state["skipped"] = 0
        _state["current"] = None
        _state["last_error"] = None

        for source, r in rows:
            _state["current"] = r.get("source_id") or str(r["id"])
            try:
                action = await upsert_embedding(source, r["id"], r["source_id"], r)
                _state[action] = _state.get(action, 0) + 1
                logger.info(f"backfill {source}#{r['source_id']} id={r['id']}: {action}")
            except Exception as e:
                _state["failed"] += 1
                _state["last_error"] = str(e)[:300]
                logger.warning(f"backfill {source}#{r['source_id']} failed: {e}")
            _state["done"] += 1
            await asyncio.sleep(INTER_ITEM_SLEEP_SEC)
    finally:
        _state["current"] = None
        _running = False


def start() -> bool:
    """Start background backfill. Returns False if already running."""
    global _running
    if _running:
        return False
    _running = True
    asyncio.create_task(_run())
    return True
