"""Cron — decides WHEN the ingest chain runs. It never touches a browser or an LLM.

Everything the run actually does lives in services/ingest_chain.py.
"""

import asyncio, random, logging
from datetime import datetime, timezone, timedelta

from config import ACCOUNT_ROTATE_HOURS
from db import read, write
from services import ingest_chain

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


async def loop():
    while True:
        try:
            # Hard-reset: swap to the next SAP account every ACCOUNT_ROTATE_HOURS.
            await maybe_auto_rotate()

            cfg = (await read("SELECT is_paused, min_delay_min, max_delay_min, next_scrape_at FROM scheduler_config WHERE id=1"))[0]
            paused, min_d, max_d, next_at = cfg["is_paused"], cfg["min_delay_min"], cfg["max_delay_min"], cfg["next_scrape_at"]

            # ponytail: one Chrome on a 2 GB box. _BROWSER_LOCK serializes browser
            # commands but not memory pressure — notes waking every 30s ate the
            # community drain's 60s inter-URL sleep, so Chrome never idled and the
            # OOM killer took the API down. Wait the drain out. Drop on a bigger box.
            if not paused and not ingest_chain.is_draining():
                should = True
                if next_at:
                    try:
                        if datetime.now(IST) < datetime.fromisoformat(next_at):
                            should = False
                    except Exception:
                        pass
                if should:
                    await ingest_chain.run("notes")
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
    for table in ("urls", "community_urls"):
        healed = await write(f"UPDATE {table} SET status='pending' WHERE status='scraping' RETURNING id")
        if healed:
            logger.info(f"↺ reset {len(healed)} stuck 'scraping' {table} row(s) → pending")
    # Start the account-rotate clock if never stamped (won't rotate until N hours later).
    cfg = await read("SELECT account_activated_at FROM scheduler_config WHERE id=1")
    if cfg and not cfg[0].get("account_activated_at"):
        await stamp_account_activated()
        logger.info(f"⏱ account rotate clock started ({ACCOUNT_ROTATE_HOURS}h)")
    asyncio.create_task(loop())
    logger.info("🚀 Scheduler started")
