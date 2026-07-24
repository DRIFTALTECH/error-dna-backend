"""SAP Community ingest — drain pending community_urls one by one.

No auth, no scheduler. Take next pending row → scrape → summarize → save →
1 min pause → next, until the queue is empty. One drain at a time; kicking
again while running is a no-op.

Unlike SAP Notes, community pages are heavy SPAs (Cloudflare + many images).
The inter-URL delay lets Chrome/openclaw reclaim RAM on small EC2 boxes.
"""

import asyncio
import gc
import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

from db import read, write
from services.scraper import scrape_community
from services.summarizer import summarize

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

# Pause between URLs so Chrome/openclaw can reclaim RAM (notes scraper has delays;
# community previously had none → sustained memory climb → OOM kill).
INTER_ITEM_SLEEP_SEC = float(os.getenv("COMMUNITY_INTER_ITEM_SLEEP_SEC", "60"))

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
        result.clear()
        return

    # Persist scraped images (S3 or local), keep briefs for the summarizer +
    # a manifest {ref: {key, alt}} for the frontend to render.
    # Drop data_b64 immediately after save — holding 3× base64 blobs across LLM
    # round-trips was a major EC2 RAM spike during continuous drain.
    from services.image_store import save as _img_save
    briefs, manifest = [], {}
    for im in (result.get("images") or []):
        b64 = im.pop("data_b64", None) or ""
        if not b64:
            continue
        try:
            key = await asyncio.to_thread(_img_save, __import__("base64").b64decode(b64), im.get("ext", "png"))
        except Exception as e:
            logger.warning(f"community image save failed: {e}")
            continue
        finally:
            del b64
        briefs.append({"ref": im["ref"], "context": im.get("context", ""), "alt": im.get("alt", "")})
        manifest[im["ref"]] = {"key": key, "alt": im.get("alt", "")}
    result["images"] = []  # free any leftover image dicts
    if manifest:
        trace.append({"at": datetime.now(IST).strftime("%H:%M:%S"), "phase": "images",
                      "status": "ok", "message": f"Saved {len(manifest)} image(s)", "detail": ", ".join(manifest)})

    page_text = result.get("clean_text") or result.get("raw_text") or ""
    result.pop("clean_text", None)
    result.pop("raw_text", None)

    try:
        summary = await summarize(page_text, images=briefs, allow_skip=True)
    except Exception as e:
        trace.append({"at": datetime.now(IST).strftime("%H:%M:%S"), "phase": "summarize",
                      "status": "error", "message": "LLM summarization failed", "detail": str(e)})
        await write("UPDATE community_urls SET status='failed', error_message=? WHERE id=?", (f"LLM:{e}", url["id"]))
        await log_row("failed", "summarize", f"LLM:{e}")
        logger.warning(f"community #{url['source_id']} LLM failed: {e}")
        page_text = ""
        return

    page_text = ""  # free before DB work

    # Blog / non-solution → skip: don't store as knowledge, record the reason, drop images.
    if summary.get("is_solution") is False:
        reason = summary.get("skip_reason") or "Not a problem/solution (e.g. a blog post)"
        from services.image_store import delete as _img_del
        for meta in manifest.values():
            _img_del(meta.get("key", ""))
        trace.append({"at": datetime.now(IST).strftime("%H:%M:%S"), "phase": "skip",
                      "status": "info", "message": "Skipped — not a solution", "detail": reason})
        await write("UPDATE community_urls SET status='skipped', error_message=? WHERE id=?", (reason, url["id"]))
        await log_row("skipped", "skip", reason)
        logger.info(f"community #{url['source_id']} skipped: {reason}")
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
    title = _s(summary.get("title")) or url.get("title") or "Untitled"
    family = _s(summary.get("family"))
    issue = _s(summary.get("issue"))
    body = _s(summary.get("summary"))
    tags = _s(summary.get("tags"))
    gotchas = _s(summary.get("gotchas"))
    inserted = await write(
        """INSERT INTO community_summaries(source_id, url_id, title, family, area, type, issue, summary, steps,
           gotchas, tags, source_version, source_date, source_url, component, environment, images, is_latest,
           verification_status, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'current',?,?) RETURNING id""",
        (url["source_id"], url["id"], title, family, _s(summary.get("area")), _s(summary.get("type")),
         issue, body, _s(summary.get("steps")), gotchas, tags, 1, url.get("released_on"),
         url["source_url"], url.get("component"), _s(summary.get("environment", "[]")),
         _s(manifest), now, now),
    )
    summary_id = inserted[0]["id"]
    await write("UPDATE community_urls SET status='completed', scraped_at=? WHERE id=?", (now, url["id"]))
    trace.append({"at": datetime.now(IST).strftime("%H:%M:%S"), "phase": "store",
                  "status": "ok", "message": "Stored in community knowledge base"})
    # Vector chunk — best-effort; recorded in the same community_scrape_log trace.
    from services.embeddings import embed_summary_safe
    emb = await embed_summary_safe("community", summary_id, url["source_id"], {
        "title": title, "family": family, "issue": issue,
        "summary": body, "tags": tags, "gotchas": gotchas,
    })
    trace.append({"at": datetime.now(IST).strftime("%H:%M:%S"), "phase": "embed",
                  "status": "ok" if emb["ok"] else "error",
                  "message": emb["message"], "detail": emb.get("detail")})
    trace.append({"at": datetime.now(IST).strftime("%H:%M:%S"), "phase": "done",
                  "status": "ok", "message": "Run completed successfully"})
    await log_row("success", "create")
    logger.info(f"community #{url['source_id']} saved: {summary.get('title','')[:60]}")


async def _next_pending() -> dict | None:
    """Fetch exactly one pending row. Never prefetch a queue into memory."""
    rows = await read(
        "SELECT id, source_id, source_url, title, released_on, component "
        "FROM community_urls WHERE status='pending' ORDER BY id ASC LIMIT 1"
    )
    return rows[0] if rows else None


async def _drain() -> None:
    """Process pending URLs strictly one-at-a-time:

    load 1 → scrape/summarize/save → free it → sleep → load next 1 → …
    Never loads the full pending list into memory.
    """
    global _running, _current
    processed = 0
    try:
        while True:
            url = await _next_pending()
            if not url:
                break
            logger.info(
                f"community drain: loading 1 URL #{url['source_id']} "
                f"(processed so far: {processed})"
            )
            await _process_one(url)
            url.clear()
            del url
            processed += 1
            _current = None
            gc.collect()

            # Count peek only — never pull the next row until after the sleep.
            more = await read(
                "SELECT 1 AS ok FROM community_urls WHERE status='pending' LIMIT 1"
            )
            if not more:
                break
            if INTER_ITEM_SLEEP_SEC > 0:
                logger.info(
                    f"community drain: {processed} done — sleeping "
                    f"{INTER_ITEM_SLEEP_SEC:.0f}s before loading next URL"
                )
                await asyncio.sleep(INTER_ITEM_SLEEP_SEC)
    except Exception:
        logger.exception("community drain crashed")
    finally:
        _running = False
        _current = None
        logger.info(f"community drain finished ({processed} processed)")


def start_drain() -> bool:
    """Kick a background drain. Returns False if one is already running."""
    global _running
    if _running:
        return False
    _running = True
    asyncio.create_task(_drain())
    return True
