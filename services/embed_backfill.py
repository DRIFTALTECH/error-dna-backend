"""Backfill Titan embeddings for notes that have summaries but no vector row.

One job at a time. Processes missing notes one-by-one so Bedrock/IAM blips
don't take down the whole batch. Kick again while running is a no-op.
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
    """How many latest notes have / lack embeddings."""
    total = (await read(
        "SELECT COUNT(*) AS c FROM summaries WHERE is_latest = 1",
    ))[0]["c"]
    embedded = (await read(
        """SELECT COUNT(*) AS c FROM summaries s
           WHERE s.is_latest = 1
             AND EXISTS (
               SELECT 1 FROM summary_embeddings e
               WHERE e.source = 'notes' AND e.summary_id = s.id
             )""",
    ))[0]["c"]
    return {
        "total": int(total),
        "embedded": int(embedded),
        "missing": int(total) - int(embedded),
    }


async def _missing_rows() -> list[dict]:
    return await read(
        """SELECT s.id, s.source_id, s.title, s.family, s.issue, s.summary, s.tags, s.gotchas
           FROM summaries s
           WHERE s.is_latest = 1
             AND NOT EXISTS (
               SELECT 1 FROM summary_embeddings e
               WHERE e.source = 'notes' AND e.summary_id = s.id
             )
           ORDER BY s.id""",
    )


async def _run() -> None:
    global _running
    try:
        rows = await _missing_rows()
        _state["total"] = len(rows)
        _state["done"] = 0
        _state["created"] = 0
        _state["failed"] = 0
        _state["skipped"] = 0
        _state["current"] = None
        _state["last_error"] = None

        for r in rows:
            _state["current"] = r.get("source_id") or str(r["id"])
            try:
                action = await upsert_embedding("notes", r["id"], r["source_id"], r)
                _state[action] = _state.get(action, 0) + 1
                logger.info(f"backfill notes#{r['source_id']} id={r['id']}: {action}")
            except Exception as e:
                _state["failed"] += 1
                _state["last_error"] = str(e)[:300]
                logger.warning(f"backfill notes#{r['source_id']} failed: {e}")
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
