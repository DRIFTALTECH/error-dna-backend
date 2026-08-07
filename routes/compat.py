"""Compatibility routes — adapt backend responses to match frontend expectations."""

from fastapi import APIRouter
from db import read
from services.error_families import FAMILY_JOIN

router = APIRouter(prefix="/api", tags=["compat"])


@router.get("/families")
async def families():
    rows = await read(
        f"""SELECT f.code, f.family_name as name, f.description, f.color, f.severity,
                  COUNT(s.id) as fix_count,
                  COALESCE(MAX(s.updated_at), datetime('now')) as updated_at
           FROM error_families f
           LEFT JOIN summaries s ON s.is_latest = 1 AND {FAMILY_JOIN}
           GROUP BY f.code, f.family_name, f.description, f.color, f.severity, f.match_priority
           ORDER BY f.match_priority ASC, fix_count DESC""",
    )
    out = []
    for r in rows:
        code = r.get("code") or r["name"]
        err_rows = await read(
            """SELECT id, title, updated_at FROM summaries
               WHERE is_latest=1 AND (family=? OR family=?) ORDER BY updated_at DESC""",
            (code, r["name"]),
        )
        errors = [{
            "id": str(e["id"]),
            "title": e["title"] or "",
            "updated_at": e["updated_at"] or "",
            "fixes": 1,
        } for e in err_rows]
        out.append({
            "id": code, "name": r["name"], "code": code,
            "description": r["description"] or "",
            "color": r["color"] or "#58a6ff",
            "severity": r.get("severity") or "medium",
            "fix_count": r["fix_count"] or 0,
            "updated_at": r["updated_at"] or "",
            "errors": errors,
        })
    return {"families": out}
