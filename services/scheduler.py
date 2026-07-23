"""Scheduler — picks URL, scrapes (auto-login), summarizes, saves."""

import asyncio, random, time, json as _json, logging
from datetime import datetime, timezone, timedelta

from config import ACCOUNT_ROTATE_HOURS
from db import read, write
from services.scraper import scrape_note
from services.summarizer import summarize

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def log(msg: str):
    print(f"  {msg}", flush=True)


async def stamp_account_activated():
    """Mark 'now' as when the current active credential became active."""
    await write(
        "UPDATE scheduler_config SET account_activated_at=? WHERE id=1",
        (datetime.now(IST).isoformat(),),
    )


async def rotate_account():
    """Activate the next credential after the current active one (wraps)."""
    rows = await read("SELECT id, is_active, label FROM credentials ORDER BY id")
    if not rows:
        return None
    if len(rows) < 2:
        await stamp_account_activated()
        return rows[0].get("label")
    ids = [r["id"] for r in rows]
    active = next((r["id"] for r in rows if r["is_active"]), ids[0])
    nxt = ids[(ids.index(active) + 1) % len(ids)]
    nxt_label = next(r["label"] for r in rows if r["id"] == nxt)
    await write("UPDATE credentials SET is_active=0")
    await write("UPDATE credentials SET is_active=1 WHERE id=?", (nxt,))
    await stamp_account_activated()
    log(f"🔄 Rotated account → {nxt_label}")
    return nxt_label


async def maybe_auto_rotate() -> bool:
    """If active account older than ACCOUNT_ROTATE_HOURS and ≥2 creds, rotate once.

    Returns True when a rotation happened. ACCOUNT_ROTATE_HOURS≤0 disables.
    """
    if ACCOUNT_ROTATE_HOURS <= 0:
        return False
    n = (await read("SELECT COUNT(*) AS c FROM credentials"))[0]["c"]
    if n < 2:
        return False

    cfg = (await read("SELECT account_activated_at FROM scheduler_config WHERE id=1"))[0]
    raw = cfg.get("account_activated_at")
    now = datetime.now(IST)
    if not raw:
        # First observation — start the clock; don't rotate immediately on deploy.
        await stamp_account_activated()
        return False
    try:
        started = datetime.fromisoformat(raw)
        if started.tzinfo is None:
            started = started.replace(tzinfo=IST)
    except Exception:
        await stamp_account_activated()
        return False

    if now - started < timedelta(hours=ACCOUNT_ROTATE_HOURS):
        return False
    await rotate_account()
    return True


def seconds_until_account_rotate(activated_at: str | None) -> int | None:
    """Seconds until next auto-rotate, or None if disabled / unknown."""
    if ACCOUNT_ROTATE_HOURS <= 0 or not activated_at:
        return None
    try:
        started = datetime.fromisoformat(activated_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=IST)
    except Exception:
        return None
    due = started + timedelta(hours=ACCOUNT_ROTATE_HOURS)
    return max(0, int((due - datetime.now(IST)).total_seconds()))


def _now_hms():
    return datetime.now(IST).strftime("%H:%M:%S")


async def one_scrape():
    t0 = time.time()
    print(f"\n{'='*60}", flush=True)

    run_trace = [{"at": _now_hms(), "phase": "queued", "status": "info",
                  "message": "Scrape job accepted by scheduler"}]

    def rt(phase, status, message, detail=None):
        run_trace.append({"at": _now_hms(), "phase": phase, "status": status,
                          "message": message, "detail": detail})

    try:
        creds = await read("SELECT * FROM credentials WHERE is_active=1 LIMIT 1")
        if not creds:
            creds = await read("SELECT * FROM credentials LIMIT 1")
        cred = creds[0] if creds else None
        rt("account", "ok" if cred else "warn",
           f"Assigned {cred['label']}" if cred else "No credential configured")

        urls = await read("SELECT * FROM urls WHERE status='pending' ORDER BY id LIMIT 1")
        if not urls:
            print("✅ Queue empty", flush=True)
            return
        url = urls[0]
        log(f"URL #{url['source_id']}: {url.get('title','')[:60]}")
        await asyncio.sleep(5)

        ex = await read(
            "SELECT id,source_version FROM summaries WHERE source_id=? AND is_latest=1",
            (url["source_id"],),
        )
        if ex and (ex[0]["source_version"] or 0) >= 1:
            await write("UPDATE urls SET status='skipped' WHERE id=?", (url["id"],))
            log(f"⏭ SKIP: already have v{ex[0]['source_version']}")
            return
        await asyncio.sleep(5)

        await write("UPDATE urls SET status='scraping' WHERE id=?", (url["id"],))
        await asyncio.sleep(5)

        log(f"Scraping... {url['source_url'][:60]}")
        user = cred["username"] if cred else None
        pw = None
        if cred:
            from services.crypto import decrypt
            pw = decrypt(cred["password"])
        result = await asyncio.to_thread(scrape_note, url["source_url"], user, pw)
        run_trace.extend(result.get("trace") or [])

        if not result["success"]:
            log(f"❌ FAILED: {result['error']}")
            await write("UPDATE urls SET status='failed', error_message=? WHERE id=?", (result["error"], url["id"]))
            await write(
                "INSERT INTO scrape_log(url_id,source_id,status,duration_ms,error_message,trace) VALUES(?,?,?,?,?,?)",
                (url["id"], url["source_id"], "failed", int((time.time() - t0) * 1000),
                 result["error"], _json.dumps(run_trace)),
            )
            return

        log(f"✅ {len(result.get('clean_text',''))} chars cleaned")
        await asyncio.sleep(5)

        log("LLM summarizing...")
        rt("summarize", "info", "Sending article to LLM for summarization")
        try:
            summary = await summarize(result["clean_text"] or result["raw_text"])
            log(f"  → {summary.get('title','')[:60]}")
            log(f"  → {summary.get('family','')} / {summary.get('type','')}")
            rt("summarize", "ok", f"LLM produced: {summary.get('title','')[:60]}",
               f"{summary.get('family','')} / {summary.get('type','')}")
        except Exception as e:
            log(f"❌ LLM failed: {e}")
            rt("summarize", "error", "LLM summarization failed", str(e))
            await write("UPDATE urls SET status='failed', error_message=? WHERE id=?", (f"LLM:{e}", url["id"]))
            await write(
                "INSERT INTO scrape_log(url_id,source_id,status,duration_ms,error_message,trace) VALUES(?,?,?,?,?,?)",
                (url["id"], url["source_id"], "failed", int((time.time() - t0) * 1000),
                 f"LLM:{e}", _json.dumps(run_trace)),
            )
            return
        await asyncio.sleep(5)

        def s(v):
            if v is None:
                return ""
            if isinstance(v, (list, dict)):
                return _json.dumps(v)
            return str(v)

        now = datetime.now(IST).isoformat()
        await write(
            """INSERT INTO summaries(source_id,url_id,title,family,area,type,issue,summary,steps,gotchas,tags,
               source_version,source_date,source_url,component,environment,is_latest,verification_status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'current',?,?)""",
            (url["source_id"], url["id"], s(summary.get("title")), s(summary.get("family")),
             s(summary.get("area")), s(summary.get("type")), s(summary.get("issue")),
             s(summary.get("summary")), s(summary.get("steps")), s(summary.get("gotchas")),
             s(summary.get("tags")), 1, url.get("released_on"), url["source_url"],
             url.get("component"), s(summary.get("environment", "[]")), now, now),
        )
        await write("UPDATE urls SET status='completed', scraped_at=? WHERE id=?", (now, url["id"]))
        rt("store", "ok", "Summary stored in knowledge base", "action: create")
        rt("done", "ok", "Run completed successfully")
        await write(
            "INSERT INTO scrape_log(url_id,source_id,status,action,duration_ms,trace) VALUES(?,?,?,?,?,?)",
            (url["id"], url["source_id"], "success", "create", int((time.time() - t0) * 1000),
             _json.dumps(run_trace)),
        )

        dur = int((time.time() - t0) * 1000)
        print(f"✅ SAVED #{url['source_id']} [{dur}ms]", flush=True)
        print(f"   {summary.get('title','')[:80]}", flush=True)
        print(f"   {summary.get('family','')}", flush=True)

    except Exception as e:
        logger.exception(f"Fatal: {e}")


async def loop():
    while True:
        try:
            # Hard-reset: swap to the next SAP account every ACCOUNT_ROTATE_HOURS.
            await maybe_auto_rotate()

            cfg = (await read("SELECT is_paused, min_delay_min, max_delay_min, next_scrape_at FROM scheduler_config WHERE id=1"))[0]
            paused, min_d, max_d, next_at = cfg["is_paused"], cfg["min_delay_min"], cfg["max_delay_min"], cfg["next_scrape_at"]

            if not paused:
                should = True
                if next_at:
                    try:
                        if datetime.now(IST) < datetime.fromisoformat(next_at):
                            should = False
                    except Exception:
                        pass
                if should:
                    await one_scrape()
                    delay = random.randint(min_d, max_d) * 60
                    next_t = (datetime.now(IST) + timedelta(seconds=delay)).isoformat()
                    await write("UPDATE scheduler_config SET next_scrape_at=? WHERE id=1", (next_t,))
                    log(f"⏱ Next in {delay//60}min")

            await asyncio.sleep(30)
        except Exception as e:
            logger.exception(f"Loop: {e}")
            await asyncio.sleep(60)


async def start():
    # Self-heal: a scrape that crashed mid-run leaves its URL stuck in 'scraping'.
    # Nothing is scraping at boot (one runs at a time), so reset those to pending.
    healed = await write("UPDATE urls SET status='pending' WHERE status='scraping' RETURNING id")
    if healed:
        logger.info(f"↺ reset {len(healed)} stuck 'scraping' URL(s) → pending")
    # Start the account-rotate clock if never stamped (won't rotate until N hours later).
    cfg = await read("SELECT account_activated_at FROM scheduler_config WHERE id=1")
    if cfg and not cfg[0].get("account_activated_at"):
        await stamp_account_activated()
        logger.info(f"⏱ account rotate clock started ({ACCOUNT_ROTATE_HOURS}h)")
    asyncio.create_task(loop())
    logger.info("🚀 Scheduler started")
