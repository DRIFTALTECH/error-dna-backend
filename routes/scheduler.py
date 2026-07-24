"""Scheduler control routes — status, config, pause/resume, scrape-now, rotate, logs."""

import json
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Query
from config import ACCOUNT_ROTATE_HOURS
from db import read, write
from models import SchedulerUpdate
from services.scheduler import seconds_until_account_rotate

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])
IST = timezone(timedelta(hours=5, minutes=30))


async def _active_account_label():
    rows = await read("SELECT label FROM credentials WHERE is_active = 1 LIMIT 1")
    return rows[0]["label"] if rows else None


@router.get("")
async def scheduler_status():
    cfg_rows = await read("SELECT * FROM scheduler_config WHERE id = 1")
    config = cfg_rows[0] if cfg_rows else {}

    today = datetime.now(IST).strftime("%Y-%m-%d")
    today_stats = (await read(
        """SELECT
            SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) as scraped,
            SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
           FROM scrape_log WHERE date(created_at) = ?""",
        (today,),
    ))[0]

    total_pending = (await read("SELECT COUNT(*) as c FROM urls WHERE status='pending'"))[0]["c"]
    n_creds = (await read("SELECT COUNT(*) as c FROM credentials"))[0]["c"]
    active_label = await _active_account_label()

    remaining = 0
    if config.get("next_scrape_at"):
        try:
            next_time = datetime.fromisoformat(config["next_scrape_at"])
            remaining = max(0, int((next_time - datetime.now(IST)).total_seconds()))
        except Exception:
            pass

    rotate_secs = seconds_until_account_rotate(config.get("account_activated_at"))
    # Auto-rotate only applies when there are ≥2 accounts.
    if n_creds < 2:
        rotate_secs = None

    is_paused = bool(config.get("is_paused", 1))
    return {
        "active_account": active_label or "—",
        "running": not is_paused,
        "next_scrape_seconds": remaining,
        "account_rotate_hours": ACCOUNT_ROTATE_HOURS,
        "next_account_rotate_seconds": rotate_secs,
        "today": {
            "scraped": today_stats.get("scraped") or 0,
            "failed": today_stats.get("failed") or 0,
            "remaining": total_pending,
        },
        "min_delay_min": config.get("min_delay_min", 5),
        "max_delay_min": config.get("max_delay_min", 60),
        "alert": None if active_label else "No active account — add or activate a credential to start scraping",
    }


@router.put("")
async def update_scheduler(body: SchedulerUpdate):
    sets, params = [], []
    if body.min_delay_min is not None:
        sets.append("min_delay_min = ?"); params.append(body.min_delay_min)
    if body.max_delay_min is not None:
        sets.append("max_delay_min = ?"); params.append(body.max_delay_min)
    if sets:
        sets.append("updated_at = datetime('now', 'localtime')")
        await write(f"UPDATE scheduler_config SET {', '.join(sets)} WHERE id = 1", params)
    return {"ok": True}


@router.post("/pause")
async def pause_scheduler():
    await write("UPDATE scheduler_config SET is_paused = 1 WHERE id = 1")
    return {"ok": True, "paused": True}


@router.post("/resume")
async def resume_scheduler():
    await write("UPDATE scheduler_config SET is_paused = 0 WHERE id = 1")
    return {"ok": True, "paused": False}


@router.post("/scrape-now")
async def scrape_now():
    await write("UPDATE scheduler_config SET next_scrape_at = NULL, is_paused = 0 WHERE id = 1")
    return {"ok": True, "message": "Scrape triggered. Processing next pending URL."}


@router.post("/rotate")
async def rotate_account():
    try:
        from services.scheduler import rotate_account as _rotate
        await _rotate()
    except Exception:
        pass
    active = await _active_account_label()
    return {"active_account": active or "—", "ok": True}


def _account_from_trace(trace) -> str | None:
    """Pull 'Assigned <label>' from the account phase of a stored trace."""
    if not isinstance(trace, list):
        return None
    for step in trace:
        if not isinstance(step, dict):
            continue
        if (step.get("phase") or "") != "account":
            continue
        msg = step.get("message") or ""
        if msg.startswith("Assigned "):
            label = msg[len("Assigned "):].strip()
            return label or None
    return None


def _log_to_ui(row: dict) -> dict:
    raw = (row.get("status") or "").lower()
    status = "completed" if raw in ("success", "completed") else ("failed" if raw == "failed" else raw or "completed")
    note = row.get("source_id") or ""
    ts = row.get("created_at") or ""
    err = row.get("error_message")

    # Prefer the REAL per-step trace captured during the run; synthesize only for
    # legacy rows written before the trace column existed.
    trace = None
    stored = row.get("trace")
    if stored:
        try:
            trace = json.loads(stored) if isinstance(stored, str) else stored
        except (json.JSONDecodeError, TypeError):
            trace = None

    # Per-run account: column first, then trace "Assigned …", never the live active account.
    account = (row.get("account_label") or "").strip() or _account_from_trace(trace) or "—"

    if not trace:
        trace = [
            {"at": ts, "phase": "queued", "status": "info", "message": "Scrape job accepted by scheduler"},
            {"at": ts, "phase": "account", "status": "ok", "message": f"Assigned {account}"},
        ]
        if status == "failed":
            trace.append({"at": ts, "phase": "fetch", "status": "error",
                          "message": f"Fetch note {note} failed", "detail": err or "Unknown error"})
            trace.append({"at": ts, "phase": "done", "status": "error", "message": "Run finished with failure"})
        else:
            action = row.get("action") or "stored"
            trace.append({"at": ts, "phase": "fetch", "status": "ok", "message": f"Fetched note {note}"})
            trace.append({"at": ts, "phase": "store", "status": "ok",
                          "message": "Summary stored in knowledge base", "detail": f"action: {action}"})
            trace.append({"at": ts, "phase": "done", "status": "ok", "message": "Run completed successfully"})

    return {
        "id": row["id"],
        "run_id": f"run_{row['id']}",
        "note_number": note,
        "account": account,
        "status": status,
        "retry_count": 0,
        "duration_ms": row.get("duration_ms"),
        "timestamp": ts,
        "error": err,
        "trace": trace,
    }


@router.get("/logs")
async def scheduler_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    status: str = Query(None),
    search: str = Query(None),
):
    where, params = [], []
    if status and status != "all":
        where.append("status = ?")
        params.append("success" if status == "completed" else status)
    if search:
        where.append("(source_id LIKE ? OR error_message LIKE ? OR account_label LIKE ? OR trace LIKE ?)")
        params.extend([f"%{search}%"] * 4)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    offset = (page - 1) * page_size

    total = (await read(f"SELECT COUNT(*) as c FROM scrape_log {where_sql}", params))[0]["c"]
    completed = (await read("SELECT COUNT(*) as c FROM scrape_log WHERE status='success'"))[0]["c"]
    failed = (await read("SELECT COUNT(*) as c FROM scrape_log WHERE status='failed'"))[0]["c"]
    rows = await read(
        f"SELECT * FROM scrape_log {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    return {
        "items": [_log_to_ui(r) for r in rows],
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "completed": completed,
        "failed": failed,
    }
