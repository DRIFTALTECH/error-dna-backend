"""SAP Community ingest — drain pending community_urls one by one.

No auth, no scheduler, no delays (unlike the SAP-notes scraper): just take the
next pending row, scrape it via the browser, summarize, save, repeat until empty.
One drain runs at a time; kicking it again while it runs is a no-op.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta

from db import read, write
from services.scraper import scrape_community
from services.summarizer import summarize

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

# ponytail: single global flag — one drain at a time is exactly what we want
# (the browser can only be one place at once). Add a queue only if that changes.
_running = False
_current = None  # source_id currently being processed, for the status endpoint


def is_running() -> bool:
    return _running


def current() -> str | None:
    return _current


def _s(v):
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


async def _process_one(url: dict) -> None:
    global _current
    _current = url["source_id"]
    t0 = time.time()
    trace = [{"at": datetime.now(IST).strftime("%H:%M:%S"), "phase": "queued",
              "status": "info", "message": "Picked up by community ingest"}]

    await write("UPDATE community_urls SET status='scraping', updated_at=datetime('now','localtime') WHERE id=?",
                (url["id"],))

    result = await asyncio.to_thread(scrape_community, url["source_url"])
    trace.extend(result.get("trace") or [])

    def log_row(status, action, err=None):
        return write(
            """INSERT INTO community_scrape_log(url_id, source_id, status, action, duration_ms, error_message, trace)
               VALUES(?,?,?,?,?,?,?)""",
            (url["id"], url["source_id"], status, action,
             int((time.time() - t0) * 1000), err, json.dumps(trace)),
        )

    if not result.get("success"):
        err = result.get("error", "scrape_failed")
        await write("UPDATE community_urls SET status='failed', error_message=? WHERE id=?", (err, url["id"]))
        await log_row("failed", "scrape", err)
        logger.warning(f"community #{url['source_id']} scrape failed: {err}")
        return

    # Persist scraped images (S3 or local), keep briefs for the summarizer +
    # a manifest {ref: {key, alt}} for the frontend to render.
    from services.image_store import save as _img_save
    briefs, manifest = [], {}
    for im in (result.get("images") or []):
        try:
            key = await asyncio.to_thread(_img_save, __import__("base64").b64decode(im["data_b64"]), im.get("ext", "png"))
        except Exception as e:
            logger.warning(f"community image save failed: {e}")
            continue
        briefs.append({"ref": im["ref"], "context": im.get("context", ""), "alt": im.get("alt", "")})
        manifest[im["ref"]] = {"key": key, "alt": im.get("alt", "")}
    if manifest:
        trace.append({"at": datetime.now(IST).strftime("%H:%M:%S"), "phase": "images",
                      "status": "ok", "message": f"Saved {len(manifest)} image(s)", "detail": ", ".join(manifest)})

    try:
        summary = await summarize(result.get("clean_text") or result.get("raw_text") or "", images=briefs)
    except Exception as e:
        trace.append({"at": datetime.now(IST).strftime("%H:%M:%S"), "phase": "summarize",
                      "status": "error", "message": "LLM summarization failed", "detail": str(e)})
        await write("UPDATE community_urls SET status='failed', error_message=? WHERE id=?", (f"LLM:{e}", url["id"]))
        await log_row("failed", "summarize", f"LLM:{e}")
        logger.warning(f"community #{url['source_id']} LLM failed: {e}")
        return

    # Keep only images the model actually placed; delete the rest (no orphan storage).
    blob = _s(summary.get("summary")) + " " + _s(summary.get("steps"))
    from services.image_store import delete as _img_del
    used = {}
    for ref, meta in manifest.items():
        if "{" + ref + "}" in blob:
            used[ref] = meta
        else:
            _img_del(meta["key"])
    manifest = used

    now = datetime.now(IST).isoformat()
    await write(
        """INSERT INTO community_summaries(source_id, url_id, title, family, area, type, issue, summary, steps,
           gotchas, tags, source_version, source_date, source_url, component, environment, images, is_latest,
           verification_status, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'current',?,?)""",
        (url["source_id"], url["id"], _s(summary.get("title")) or url.get("title") or "Untitled",
         _s(summary.get("family")), _s(summary.get("area")), _s(summary.get("type")),
         _s(summary.get("issue")), _s(summary.get("summary")), _s(summary.get("steps")),
         _s(summary.get("gotchas")), _s(summary.get("tags")), 1, url.get("released_on"),
         url["source_url"], url.get("component"), _s(summary.get("environment", "[]")),
         _s(manifest), now, now),
    )
    await write("UPDATE community_urls SET status='completed', scraped_at=? WHERE id=?", (now, url["id"]))
    trace.append({"at": datetime.now(IST).strftime("%H:%M:%S"), "phase": "done",
                  "status": "ok", "message": "Stored in community knowledge base"})
    await log_row("success", "create")
    logger.info(f"community #{url['source_id']} saved: {summary.get('title','')[:60]}")


async def _drain() -> None:
    global _running, _current
    try:
        while True:
            rows = await read("SELECT * FROM community_urls WHERE status='pending' ORDER BY id ASC LIMIT 1")
            if not rows:
                break
            await _process_one(rows[0])
    except Exception:
        logger.exception("community drain crashed")
    finally:
        _running = False
        _current = None
        logger.info("community drain finished")


def start_drain() -> bool:
    """Kick a background drain. Returns False if one is already running."""
    global _running
    if _running:
        return False
    _running = True
    asyncio.create_task(_drain())
    return True
