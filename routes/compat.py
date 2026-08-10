"""Compatibility routes — adapt backend responses to match frontend expectations."""

from fastapi import APIRouter
from db import read
from services.error_families import FAMILY_JOIN

router = APIRouter(prefix="/api", tags=["compat"])

_UNCLASSIFIED_CODE = "UNCLASSIFIED_ERROR"

# Notes whose family string does not match any catalog code or display name.
_ORPHAN_WHERE = """s.is_latest = 1 AND (
    s.family IS NULL OR TRIM(s.family) = ''
    OR NOT EXISTS (
        SELECT 1 FROM error_families f
        WHERE s.family = f.code OR s.family = f.family_name
    )
)"""


async def _orphan_errors() -> list[dict]:
    rows = await read(
        f"""SELECT s.id, s.title, s.updated_at FROM summaries s
            WHERE {_ORPHAN_WHERE}
            ORDER BY s.updated_at DESC""",
    )
    return [{
        "id": str(e["id"]),
        "title": e["title"] or "",
        "updated_at": e["updated_at"] or "",
        "fixes": 1,
    } for e in rows]


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
    orphan_errors = await _orphan_errors()

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

        # Unclassified bucket also shows orphan/stale-label notes so nothing is hidden.
        if code == _UNCLASSIFIED_CODE and orphan_errors:
            seen = {e["id"] for e in errors}
            for o in orphan_errors:
                if o["id"] not in seen:
                    errors.append(o)
                    seen.add(o["id"])

        fix_count = len(errors) if code == _UNCLASSIFIED_CODE else (r["fix_count"] or 0)
        out.append({
            "id": code, "name": r["name"], "code": code,
            "description": r["description"] or "",
            "color": r["color"] or "#58a6ff",
            "severity": r.get("severity") or "medium",
            "fix_count": fix_count,
            "updated_at": r["updated_at"] or "",
            "errors": errors,
        })

    # Pin Unclassified near the top (after the frontend "All" aggregate).
    unclassified = [f for f in out if f["id"] == _UNCLASSIFIED_CODE]
    rest = [f for f in out if f["id"] != _UNCLASSIFIED_CODE]
    return {"families": unclassified + rest}
