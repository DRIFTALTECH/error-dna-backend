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
    "job_total": 0,
    "done": 0,
    "created": 0,
    "updated": 0,
    "failed": 0,
    "skipped": 0,
    "current": None,
    "last_error": None,
    "source": None,
}


def is_running() -> bool:
    return _running


def status() -> dict[str, Any]:
    return {"running": _running, **_state}


# ponytail: two sources, named here. A third means adding a line, not a registry.
_TABLES = {"notes": "summaries", "community": "community_summaries"}


def _sources(source: str | None) -> list[str]:
    return [source] if source in _TABLES else list(_TABLES)


async def counts(source: str | None = None) -> dict[str, int]:
    """How many latest summaries have / lack a vector row. One source, or both."""
    total = embedded = 0
    for src in _sources(source):
        table = _TABLES[src]
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
            (src,),
        ))[0]["c"])
    return {"total": total, "embedded": embedded, "missing": total - embedded}


_ROW_COLS = ("id, source_id, title, family, issue, summary, tags, gotchas, "
             "error_signatures, search_text")


async def _rows_to_embed(source: str | None = None) -> list[tuple[str, dict]]:
    """Every latest summary for the given source(s). Stale hashes re-embed."""
    out: list[tuple[str, dict]] = []
    for src in _sources(source):
        rows = await read(
            f"SELECT {_ROW_COLS} FROM {_TABLES[src]} WHERE is_latest = 1 ORDER BY id")
        out.extend((src, r) for r in rows)
    return out


async def _run(source: str | None = None) -> None:
    global _running
    try:
        rows = await _rows_to_embed(source)
        _state["job_total"] = len(rows)
        _state["done"] = 0
        _state["created"] = 0
        _state["updated"] = 0
        _state["failed"] = 0
        _state["skipped"] = 0
        _state["current"] = None
        _state["last_error"] = None
        _state["source"] = source or "all"

        for src, r in rows:
            _state["current"] = r.get("source_id") or str(r["id"])
            try:
                action = await upsert_embedding(src, r["id"], r["source_id"], r)
                _state[action] = _state.get(action, 0) + 1
                logger.info(f"backfill {src}#{r['source_id']} id={r['id']}: {action}")
            except Exception as e:
                _state["failed"] += 1
                _state["last_error"] = str(e)[:300]
                logger.warning(f"backfill {src}#{r['source_id']} failed: {e}")
            _state["done"] += 1
            await asyncio.sleep(INTER_ITEM_SLEEP_SEC)
    finally:
        _state["current"] = None
        _running = False


def start(source: str | None = None) -> bool:
    """Start background backfill for one source (or both). False if already running."""
    global _running
    if _running:
        return False
    _running = True
    asyncio.create_task(_run(source))
    return True


if __name__ == "__main__":
    # ponytail: the only branching worth a check — source scoping.
    assert _sources("notes") == ["notes"]
    assert _sources("community") == ["community"]
    assert _sources(None) == ["notes", "community"]
    assert _sources("bogus") == ["notes", "community"]   # unknown = both, never empty
    print("✅ embed_backfill self-check passed")
