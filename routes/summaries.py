"""Summaries and dashboard routes."""

import json
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel, Field
from db import read
from models import DashboardResponse, PaginatedResponse

router = APIRouter(prefix="/api", tags=["summaries"])


def _json_list(raw):
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else [str(v)]
    except (json.JSONDecodeError, TypeError):
        return [s.strip() for s in str(raw).split(",") if s.strip()]


def _steps_to_fixes(raw):
    out = []
    for step in _json_list(raw):
        if isinstance(step, dict):
            title = (step.get("title") or "").strip()
            details = [str(d).strip() for d in step.get("details", []) if str(d).strip()]
            out.append(f"{title}: {' '.join(details)}" if details else title)
        elif str(step).strip():
            out.append(str(step).strip())
    return [s for s in out if s]


def _gotchas_to_objs(raw):
    out = []
    for g in _json_list(raw):
        if isinstance(g, dict) and "description" in g:
            out.append({"name": g.get("name") or "Heads up", "description": g["description"]})
            continue
        text = str(g).strip()
        if not text:
            continue
        if ":" in text and len(text.split(":", 1)[0]) <= 40:
            name, desc = text.split(":", 1)
            out.append({"name": name.strip(), "description": desc.strip()})
        else:
            out.append({"name": "Heads up", "description": text})
    return out


def _summary_to_ui(row: dict) -> dict:
    return {
        "id": row["id"],
        "title": row.get("title") or "",
        "type": row.get("type"),
        "area": row.get("area") or row.get("family"),
        "tags": _json_list(row.get("tags")),
        "the_problem": row.get("issue"),
        "whats_going_on": row.get("summary"),
        "how_to_fix": _steps_to_fixes(row.get("steps")),
        "gotchas": _gotchas_to_objs(row.get("gotchas")),
        "environment": _json_list(row.get("environment")),
        "note_number": row.get("source_id"),
        "version": str(row["source_version"]) if row.get("source_version") is not None else "1",
        "source_date": row.get("source_date"),
        "stored_at": row.get("created_at"),
    }


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard():
    total = (await read("SELECT COUNT(*) as c FROM urls"))[0]["c"]
    completed = (await read("SELECT COUNT(*) as c FROM urls WHERE status='completed'"))[0]["c"]
    pending = (await read("SELECT COUNT(*) as c FROM urls WHERE status='pending'"))[0]["c"]
    failed = (await read("SELECT COUNT(*) as c FROM urls WHERE status='failed'"))[0]["c"]
    skipped = (await read("SELECT COUNT(*) as c FROM urls WHERE status='skipped'"))[0]["c"]
    s_count = (await read("SELECT COUNT(*) as c FROM summaries WHERE is_latest=1"))[0]["c"]

    recent = await read(
        """SELECT id, source_id, title, family, area, type, tags, is_latest, verification_status, created_at
           FROM summaries WHERE is_latest=1 ORDER BY created_at DESC LIMIT 12"""
    )
    families = await read(
        """SELECT f.family_name, f.color, COUNT(s.id) as count
           FROM error_families f
           LEFT JOIN summaries s ON s.family = f.family_name AND s.is_latest = 1
           GROUP BY f.family_name, f.color ORDER BY count DESC"""
    )
    return DashboardResponse(
        total_urls=total, completed=completed, pending=pending,
        failed=failed, skipped=skipped, summaries_count=s_count,
        recent_summaries=recent, families=families,
    )


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
    total = (await read(f"SELECT COUNT(*) as c FROM summaries {where_sql}", params))[0]["c"]
    rows = await read(
        f"SELECT * FROM summaries {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [page_size, offset],
    )
    return PaginatedResponse(
        data=rows, total=total, page=page,
        page_size=page_size,
        total_pages=max(1, (total + page_size - 1) // page_size),
    )


@router.get("/summaries/stats")
async def summary_stats():
    family_stats = await read(
        """SELECT f.family_name, f.color, f.icon, COUNT(s.id) as count
           FROM error_families f
           LEFT JOIN summaries s ON s.family = f.family_name AND s.is_latest = 1
           GROUP BY f.family_name, f.color, f.icon ORDER BY count DESC"""
    )
    type_stats = await read(
        """SELECT type, COUNT(*) as count FROM summaries
           WHERE is_latest=1 AND type IS NOT NULL
           GROUP BY type ORDER BY count DESC"""
    )
    return {"families": family_stats, "types": type_stats}


@router.get("/summaries/{summary_id}")
async def get_summary(summary_id: int):
    rows = await read("SELECT * FROM summaries WHERE id = ?", (summary_id,))
    if not rows:
        raise HTTPException(404, "Summary not found")
    return _summary_to_ui(rows[0])


class ChatBody(BaseModel):
    question: str = Field(..., min_length=1)


@router.post("/summaries/{summary_id}/chat")
async def chat_summary(summary_id: int, body: ChatBody):
    from services.summarizer import chat
    rows = await read("SELECT * FROM summaries WHERE id = ?", (summary_id,))
    if not rows:
        raise HTTPException(404, "Summary not found")
    r = rows[0]
    context = "\n\n".join(filter(None, [
        f"TITLE: {r.get('title')}",
        f"FAMILY: {r.get('family')}",
        f"PROBLEM: {r.get('issue')}",
        f"SUMMARY: {r.get('summary')}",
        f"STEPS: {r.get('steps')}",
        f"GOTCHAS: {r.get('gotchas')}",
    ]))
    try:
        answer = await chat(body.question, context)
    except ValueError as e:
        raise HTTPException(503, str(e))
    return {"answer": answer}
