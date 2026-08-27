"""Audit routes — what each API client sent and what we answered with.

`error_events.caller` is the token subject: an OAuth client_id for external
systems, a username for UI logins. Both show up here; the client list joins
oauth_clients so a client_id renders under its human name.
"""

import json

from typing import Annotated

from fastapi import APIRouter, Query

from db import read

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _json_or_none(raw):
    if not raw:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _event_to_ui(row: dict) -> dict:
    """One audit row: the error that came in, the answer that went out."""
    body = _json_or_none(row.get("response")) or {}
    distinct = body.get("distinct_error") or {}
    return {
        "id": row["id"],
        "at": row.get("created_at") or "",
        "client": row.get("caller") or "",
        "client_name": row.get("client_name") or "",
        "source": row.get("source") or "",
        "status": row.get("status") or "ok",
        "error_message": row.get("error_message") or "",
        "duration_ms": row.get("duration_ms"),
        # what came in
        "error_text": row.get("raw_text") or "",
        # what we made of it
        "distinct_error": distinct.get("title") or row.get("generalized_text") or "",
        "problem": distinct.get("problem") or "",
        "family_code": row.get("family_code") or "",
        "family_name": distinct.get("family_name") or "",
        "cluster_confidence": row.get("error_match_percent") or 0,
        "is_new_cluster": bool(row.get("created_new")),
        # what went out
        "solution_source": row.get("solution_source") or "none",
        "solution_count": row.get("solution_count") or 0,
        "solutions": body.get("solutions") or [],
        "fallback_solution": body.get("fallback_solution") or "",
    }


@router.get("/clients")
async def clients():
    """Every caller that has ever hit diagnose, with its call and failure counts."""
    rows = await read(
        """SELECT e.caller,
                  MAX(c.name) AS client_name,
                  COUNT(*) AS total,
                  SUM(CASE WHEN e.status = 'error' THEN 1 ELSE 0 END) AS failed,
                  MAX(e.created_at) AS last_seen
           FROM error_events e
           LEFT JOIN oauth_clients c ON c.client_id = e.caller
           WHERE e.caller IS NOT NULL
           GROUP BY e.caller
           ORDER BY MAX(e.created_at) DESC"""
    )
    return [
        {
            "client": r["caller"],
            "client_name": r["client_name"] or "",
            "total": r["total"],
            "failed": r["failed"] or 0,
            "last_seen": r["last_seen"] or "",
        }
        for r in rows
    ]


@router.get("/events")
async def events(
    client: str | None = None,
    status: str | None = None,
    search: str | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    """Paginated audit trail, newest first. Filters are all optional."""
    where = ["1=1"]
    params: list = []
    if client and client != "all":
        where.append("e.caller = ?")
        params.append(client)
    if status and status != "all":
        where.append("e.status = ?")
        params.append(status)
    if search and search.strip():
        like = f"%{search.strip()}%"
        where.append("(e.raw_text ILIKE ? OR e.generalized_text ILIKE ? OR e.error_message ILIKE ?)")
        params.extend([like, like, like])
    clause = " AND ".join(where)

    totals = await read(
        f"""SELECT COUNT(*) AS total,
                   SUM(CASE WHEN e.status = 'error' THEN 1 ELSE 0 END) AS failed,
                   SUM(CASE WHEN e.solution_source = 'knowledge_base' THEN 1 ELSE 0 END) AS from_kb,
                   SUM(CASE WHEN e.solution_source = 'llm_fallback' THEN 1 ELSE 0 END) AS from_llm
            FROM error_events e WHERE {clause}""",
        tuple(params),
    )
    t = totals[0]
    total = t["total"] or 0

    rows = await read(
        f"""SELECT e.*, c.name AS client_name
            FROM error_events e
            LEFT JOIN oauth_clients c ON c.client_id = e.caller
            WHERE {clause}
            ORDER BY e.id DESC
            LIMIT ? OFFSET ?""",
        tuple(params) + (page_size, (page - 1) * page_size),
    )

    return {
        "data": [_event_to_ui(r) for r in rows],
        "total": total,
        "failed": t["failed"] or 0,
        "from_kb": t["from_kb"] or 0,
        "from_llm": t["from_llm"] or 0,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, -(-total // page_size)),
    }


if __name__ == "__main__":
    # ponytail self-check: the row shaper has to survive a missing/corrupt body.
    assert _json_or_none(None) is None
    assert _json_or_none("not json") is None
    assert _json_or_none('{"a":1}') == {"a": 1}

    ui = _event_to_ui({"id": 1, "raw_text": "boom", "status": "error",
                       "error_message": "DiagnoseBusy: busy", "response": None})
    assert ui["solutions"] == [] and ui["fallback_solution"] == ""
    assert ui["status"] == "error" and ui["error_message"].startswith("DiagnoseBusy")

    ui = _event_to_ui({
        "id": 2, "raw_text": "boom", "created_new": 1, "error_match_percent": 84.2,
        "solution_source": "knowledge_base", "solution_count": 2,
        "response": json.dumps({
            "distinct_error": {"title": "T", "problem": "P", "family_name": "HTTP failed"},
            "solutions": [{"title": "a"}, {"title": "b"}],
        }),
    })
    assert ui["distinct_error"] == "T" and len(ui["solutions"]) == 2
    assert ui["is_new_cluster"] is True and ui["cluster_confidence"] == 84.2
    print("audit: ok")
