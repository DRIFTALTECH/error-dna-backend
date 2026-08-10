"""One-shot LLM migration: old summary family labels → new CSV family codes.

Run once (skips rows already on a valid catalog code):
    python3 -m services.migrate_families
"""

from __future__ import annotations

import asyncio
import logging
import sys

from db import init_db
from services.reclassify_notes import _pending_rows, _run, counts

logger = logging.getLogger(__name__)


def _out(msg: str) -> None:
    print(msg, flush=True)


async def migrate_all() -> dict:
    pending_rows = await _pending_rows()
    c = await counts()
    skipped = c["total_notes"] - len(pending_rows)
    _out(f"📋 {len(pending_rows)} notes to reclassify ({skipped} already classified)")

    if not pending_rows:
        return {"total": c["total_notes"], "skipped": skipped, "updated": 0, "failed": 0}

    await _run()
    from services.reclassify_notes import status

    s = status()
    c = await counts()
    return {
        "total": c["total_notes"],
        "skipped": skipped,
        "updated": s["updated"],
        "failed": s["failed"],
    }


async def _main() -> None:
    await init_db()
    stats = await migrate_all()
    if stats["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_main())
