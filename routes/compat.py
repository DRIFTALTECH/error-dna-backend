"""Compatibility routes — adapt backend responses to match frontend expectations."""

from fastapi import APIRouter
from db import read

router = APIRouter(prefix="/api", tags=["compat"])


@router.get("/families")
async def families():
    rows = await read(
        """SELECT f.family_name as name, f.description, f.color,
                  COUNT(s.id) as fix_count,
                  COALESCE(MAX(s.updated_at), datetime('now')) as updated_at
           FROM error_families f
           LEFT JOIN summaries s ON s.family = f.family_name AND s.is_latest = 1
           GROUP BY f.family_name, f.description, f.color ORDER BY fix_count DESC"""
    )
    out = []
    for r in rows:
        err_rows = await read(
            "SELECT id, title, updated_at FROM summaries WHERE family=? AND is_latest=1 ORDER BY updated_at DESC",
            (r["name"],),
        )
        errors = [{
            "id": str(e["id"]),
            "title": e["title"] or "",
            "updated_at": e["updated_at"] or "",
            "fixes": 1,
        } for e in err_rows]
        out.append({
            "id": r["name"], "name": r["name"],
            "description": r["description"] or "",
            "color": r["color"] or "#58a6ff",
            "fix_count": r["fix_count"] or 0,
            "updated_at": r["updated_at"] or "",
            "errors": errors,
        })
    return {"families": out}
