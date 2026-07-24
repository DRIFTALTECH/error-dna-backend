"""SAP Community routes — mirrors the notes URL/summary endpoints, own tables.

Public source: no credentials, no scheduler. URLs are added/pasted, then drained
one-by-one through the browser scraper + LLM summarizer via services.community_ingest.
"""

import re
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field

from db import read, write
from models import PaginatedResponse
from routes.summaries import _summary_to_ui, _embedding_status
from services import community_ingest

router = APIRouter(prefix="/api/community", tags=["community"])


def _derive_source_id(url: str) -> str:
    """SAP Community URLs end in .../{slug}-p/<id> (qaq-p, ba-p, td-p, m-p, ...).
    Use that id as the stable dedup key; fall back to the full URL."""
    m = re.search(r"-p/(\d+)", url)
    return m.group(1) if m else url.strip()


# ---- URLs -----------------------------------------------------------------

class CommunityURLAdd(BaseModel):
    source_url: str = Field(..., min_length=8)
    title: str | None = None
    source_id: str | None = None
    component: str | None = None
    category: str | None = None


class CommunityBulkAdd(BaseModel):
    urls: list[str] = Field(..., min_length=1)


async def _insert_url(source_url: str, existing: set, title=None, source_id=None,
                      component=None, category=None) -> bool:
    """Insert one URL if not a duplicate. Returns True if inserted."""
    source_url = source_url.strip()
    if not source_url:
        return False
    sid = (source_id or "").strip() or _derive_source_id(source_url)
    if sid in existing:
        return False
    existing.add(sid)
    await write(
        """INSERT INTO community_urls (source_id, title, source_url, component, category)
           VALUES (?, ?, ?, ?, ?)""",
        (sid, title, source_url, component, category),
    )
    return True


@router.get("/urls")
async def list_urls(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: str = Query(None),
    search: str = Query(None),
):
    where, params = [], []
    if status:
        where.append("status = ?"); params.append(status)
    if search:
        where.append("(title LIKE ? OR source_id LIKE ? OR source_url LIKE ?)")
        params.extend([f"%{search}%"] * 3)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    offset = (page - 1) * page_size
    total = (await read(f"SELECT COUNT(*) as c FROM community_urls {where_sql}", params))[0]["c"]
    rows = await read(
        f"SELECT * FROM community_urls {where_sql} ORDER BY id ASC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    items = [{**r, "note_number": r.get("source_id")} for r in rows]
    return {
        "items": items, "total": total, "page": page, "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@router.post("/urls")
async def add_url(body: CommunityURLAdd):
    existing = {r["source_id"] for r in await read("SELECT source_id FROM community_urls")}
    inserted = await _insert_url(body.source_url, existing, body.title, body.source_id,
                                 body.component, body.category)
    return {"ok": True, "inserted": int(inserted), "duplicate": int(not inserted)}


@router.post("/urls/bulk")
async def add_urls_bulk(body: CommunityBulkAdd):
    # Accept newline- or comma-separated blobs too (one paste = one entry).
    raw = []
    for entry in body.urls:
        raw.extend(re.split(r"[\s,]+", entry.strip()))
    urls = [u for u in raw if u.startswith("http")]
    if not urls:
        raise HTTPException(400, "No valid http(s) URLs found")
    existing = {r["source_id"] for r in await read("SELECT source_id FROM community_urls")}
    imported = 0
    for u in urls:
        if await _insert_url(u, existing):
            imported += 1
    return {
        "imported": imported, "duplicates": len(urls) - imported, "total_rows": len(urls),
        "message": f"Added {imported} URLs. {len(urls) - imported} duplicates skipped.",
    }


@router.patch("/urls/{url_id}")
async def update_url(url_id: int, body: dict):
    sets, params = [], []
    if body.get("status") is not None:
        sets.append("status = ?"); params.append(body["status"])
    if body.get("title") is not None:
        sets.append("title = ?"); params.append(body["title"])
    if not sets:
        raise HTTPException(400, "Nothing to update")
    sets.append("updated_at = datetime('now', 'localtime')")
    params.append(url_id)
    await write(f"UPDATE community_urls SET {', '.join(sets)} WHERE id = ?", params)
    return {"ok": True}


@router.delete("/urls/{url_id}")
async def delete_url(url_id: int):
    await write("DELETE FROM community_urls WHERE id = ?", (url_id,))
    return {"ok": True}


# ---- Ingest ---------------------------------------------------------------

@router.post("/ingest")
async def ingest():
    """Kick a background drain of all pending community URLs (one by one)."""
    pending = (await read("SELECT COUNT(*) as c FROM community_urls WHERE status='pending'"))[0]["c"]
    started = community_ingest.start_drain()
    return {"started": started, "already_running": not started, "pending": pending}


@router.get("/ingest/status")
async def ingest_status():
    counts = {}
    for st in ("pending", "scraping", "completed", "failed"):
        counts[st] = (await read("SELECT COUNT(*) as c FROM community_urls WHERE status=?", (st,)))[0]["c"]
    return {
        "running": community_ingest.is_running(),
        "current": community_ingest.current(),
        **counts,
    }


@router.get("/logs")
async def logs(limit: int = Query(50, ge=1, le=200)):
    import json
    rows = await read("SELECT * FROM community_scrape_log ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        try:
            trace = json.loads(r.get("trace") or "[]")
        except (json.JSONDecodeError, TypeError):
            trace = []
        out.append({
            "id": r["id"], "note_number": r.get("source_id"), "status": r.get("status"),
            "action": r.get("action"), "duration_ms": r.get("duration_ms"),
            "error": r.get("error_message"), "timestamp": r.get("created_at"), "trace": trace,
        })
    return {"items": out}


# ---- Summaries ------------------------------------------------------------

@router.get("/dashboard")
async def dashboard():
    total = (await read("SELECT COUNT(*) as c FROM community_urls"))[0]["c"]
    completed = (await read("SELECT COUNT(*) as c FROM community_urls WHERE status='completed'"))[0]["c"]
    pending = (await read("SELECT COUNT(*) as c FROM community_urls WHERE status='pending'"))[0]["c"]
    failed = (await read("SELECT COUNT(*) as c FROM community_urls WHERE status='failed'"))[0]["c"]
    s_count = (await read("SELECT COUNT(*) as c FROM community_summaries WHERE is_latest=1"))[0]["c"]
    recent = await read(
        """SELECT id, source_id, title, family, area, type, tags, created_at
           FROM community_summaries WHERE is_latest=1 ORDER BY created_at DESC LIMIT 12"""
    )
    families = await read(
        """SELECT f.family_name, f.color, COUNT(s.id) as count
           FROM error_families f
           LEFT JOIN community_summaries s ON s.family = f.family_name AND s.is_latest = 1
           GROUP BY f.family_name, f.color ORDER BY count DESC"""
    )
    return {
        "total_urls": total, "completed": completed, "pending": pending, "failed": failed,
        "summaries_count": s_count, "recent_summaries": recent, "families": families,
    }


@router.get("/summaries")
async def list_summaries(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: str = Query(None),
    family: str = Query(None),
    type: str = Query(None),
):
    where, params = ["is_latest = 1"], []
    if search:
        where.append("(title LIKE ? OR issue LIKE ? OR summary LIKE ? OR tags LIKE ?)")
        params.extend([f"%{search}%"] * 4)
    if family:
        where.append("family = ?"); params.append(family)
    if type:
        where.append("type = ?"); params.append(type)
    where_sql = "WHERE " + " AND ".join(where)
    offset = (page - 1) * page_size
    total = (await read(f"SELECT COUNT(*) as c FROM community_summaries {where_sql}", params))[0]["c"]
    rows = await read(
        f"SELECT * FROM community_summaries {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    return PaginatedResponse(
        data=[_summary_to_ui(r) for r in rows], total=total, page=page,
        page_size=page_size, total_pages=max(1, (total + page_size - 1) // page_size),
    )


def _resolve_images(raw) -> dict:
    """Manifest {ref:{key,alt}} → {ref:{url,alt}} with a loadable URL per image."""
    import json as _json
    if not raw:
        return {}
    try:
        manifest = _json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    from services.image_store import url as _img_url
    return {ref: {"url": _img_url(meta.get("key", "")), "alt": meta.get("alt", "")}
            for ref, meta in manifest.items() if isinstance(meta, dict)}


@router.get("/summaries/{summary_id}")
async def get_summary(summary_id: int):
    rows = await read("SELECT * FROM community_summaries WHERE id = ?", (summary_id,))
    if not rows:
        raise HTTPException(404, "Summary not found")
    ui = _summary_to_ui(rows[0])
    ui["images"] = _resolve_images(rows[0].get("images"))
    ui["embedding_status"] = await _embedding_status("community", summary_id)
    return ui


class ChatBody(BaseModel):
    question: str = Field(..., min_length=1)


@router.post("/summaries/{summary_id}/chat")
async def chat_summary(summary_id: int, body: ChatBody):
    from services.summarizer import chat
    rows = await read("SELECT * FROM community_summaries WHERE id = ?", (summary_id,))
    if not rows:
        raise HTTPException(404, "Summary not found")
    r = rows[0]
    context = "\n\n".join(filter(None, [
        f"TITLE: {r.get('title')}", f"FAMILY: {r.get('family')}",
        f"PROBLEM: {r.get('issue')}", f"SUMMARY: {r.get('summary')}",
        f"STEPS: {r.get('steps')}", f"GOTCHAS: {r.get('gotchas')}",
    ]))
    try:
        answer = await chat(body.question, context)
    except ValueError as e:
        raise HTTPException(503, str(e))
    return {"answer": answer}
