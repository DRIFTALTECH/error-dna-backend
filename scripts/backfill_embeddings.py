"""One-shot: embed all latest summaries missing (or stale) vectors.

Usage:  python3 scripts/backfill_embeddings.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import init_db, read
from services.embeddings import upsert_embedding


async def all_latest(table: str):
    return await read(
        f"""SELECT id, source_id, title, family, issue, summary, tags, gotchas
            FROM {table} WHERE is_latest = 1 ORDER BY id""",
    )


async def main():
    await init_db()
    totals = {"created": 0, "updated": 0, "skipped": 0, "failed": 0}

    for source, table in (("notes", "summaries"), ("community", "community_summaries")):
        rows = await all_latest(table)
        print(f"\n{source}: {len(rows)} latest summary(ies)")
        for r in rows:
            try:
                action = await upsert_embedding(source, r["id"], r["source_id"], r)
                totals[action] = totals.get(action, 0) + 1
                print(f"  [{action}] {source}#{r['source_id']} id={r['id']} {(r.get('title') or '')[:50]}")
            except Exception as e:
                totals["failed"] += 1
                print(f"  [failed] {source}#{r['source_id']}: {e}")

    print("\nDone:", totals)


if __name__ == "__main__":
    asyncio.run(main())
