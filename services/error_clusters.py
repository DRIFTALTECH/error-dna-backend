"""Error cluster listing + Neo4j-shaped graph for the UI."""

from __future__ import annotations

from db import read
from services.error_families import family_display_name


async def list_clusters() -> dict:
    rows = await read(
        """SELECT de.id, de.title, de.generalized_text, de.summary, de.family_code,
                  de.occurrence_count, de.first_seen_at, de.last_seen_at,
                  (SELECT COUNT(*) FROM error_events ee WHERE ee.distinct_error_id = de.id) AS event_count,
                  (SELECT COUNT(*) FROM error_cluster_solutions ecs WHERE ecs.distinct_error_id = de.id) AS solution_count
           FROM distinct_errors de
           ORDER BY de.last_seen_at DESC, de.id DESC""",
    )
    items = []
    for r in rows:
        fname = await family_display_name(r.get("family_code"))
        items.append({
            "id": r["id"],
            "title": r.get("title") or "",
            "generalized_error": r.get("generalized_text") or "",
            "summary": r.get("summary") or "",
            "family_code": r.get("family_code") or "",
            "family_name": fname or "",
            "occurrence_count": r.get("occurrence_count") or 0,
            "event_count": r.get("event_count") or 0,
            "solution_count": r.get("solution_count") or 0,
            "first_seen_at": r.get("first_seen_at"),
            "last_seen_at": r.get("last_seen_at"),
            "informational": (r.get("family_code") or "") == "RUN_DIAGNOSTIC_EVENT",
        })
    return {"items": items, "total": len(items)}


async def get_cluster(cluster_id: int) -> dict | None:
    rows = await read("SELECT * FROM distinct_errors WHERE id = ?", (cluster_id,))
    if not rows:
        return None
    de = rows[0]
    family_name = await family_display_name(de.get("family_code"))
    events = await read(
        """SELECT id, raw_text, generalized_text, error_match_percent, created_new,
                  caller, source, created_at
           FROM error_events WHERE distinct_error_id = ?
           ORDER BY created_at DESC LIMIT 50""",
        (cluster_id,),
    )
    solutions = await read(
        """SELECT ecs.summary_id, ecs.match_percent, ecs.hit_count, ecs.last_seen_at,
                  s.title, s.source_id AS note_number
           FROM error_cluster_solutions ecs
           JOIN summaries s ON s.id = ecs.summary_id
           WHERE ecs.distinct_error_id = ?
           ORDER BY ecs.match_percent DESC, ecs.hit_count DESC""",
        (cluster_id,),
    )
    return {
        "id": de["id"],
        "title": de.get("title") or "",
        "generalized_error": de.get("generalized_text") or "",
        "summary": de.get("summary") or "",
        "family_code": de.get("family_code") or "",
        "family_name": family_name or "",
        "occurrence_count": de.get("occurrence_count") or 0,
        "informational": (de.get("family_code") or "") == "RUN_DIAGNOSTIC_EVENT",
        "first_seen_at": de.get("first_seen_at"),
        "last_seen_at": de.get("last_seen_at"),
        "events": [dict(e) for e in events],
        "solutions": [
            {
                "summary_id": s["summary_id"],
                "note_number": s.get("note_number"),
                "title": s.get("title") or "",
                "match_percent": s.get("match_percent"),
                "hit_count": s.get("hit_count"),
                "last_seen_at": s.get("last_seen_at"),
            }
            for s in solutions
        ],
    }


async def build_graph() -> dict:
    """Nodes + links shaped like a property graph (Neo4j-style)."""
    clusters = await read(
        """SELECT de.id, de.title, de.family_code, de.occurrence_count
           FROM distinct_errors de ORDER BY de.family_code, de.id""",
    )
    families_seen: dict[str, dict] = {}
    nodes: list[dict] = []
    links: list[dict] = []

    for c in clusters:
        fcode = c.get("family_code") or "UNCLASSIFIED_ERROR"
        if fcode not in families_seen:
            fname = await family_display_name(fcode)
            nid = f"family:{fcode}"
            families_seen[fcode] = {"id": nid, "code": fcode}
            nodes.append({
                "id": nid,
                "label": fname or fcode,
                "type": "Family",
                "family_code": fcode,
            })
        cid = f"cluster:{c['id']}"
        nodes.append({
            "id": cid,
            "label": c.get("title") or f"Cluster {c['id']}",
            "type": "Cluster",
            "cluster_id": c["id"],
            "occurrence_count": c.get("occurrence_count") or 0,
            "family_code": fcode,
        })
        links.append({
            "source": f"family:{fcode}",
            "target": cid,
            "type": "HAS_CLUSTER",
        })

    events = await read(
        """SELECT id, distinct_error_id, raw_text, error_match_percent, created_at
           FROM error_events ORDER BY created_at DESC LIMIT 200""",
    )
    for e in events:
        eid = f"event:{e['id']}"
        cid = f"cluster:{e['distinct_error_id']}"
        if not any(n["id"] == cid for n in nodes):
            continue
        preview = (e.get("raw_text") or "")[:120]
        nodes.append({
            "id": eid,
            "label": preview or f"Event {e['id']}",
            "type": "Event",
            "event_id": e["id"],
            "cluster_id": e["distinct_error_id"],
        })
        links.append({
            "source": eid,
            "target": cid,
            "type": "MATCHED",
            "match_percent": e.get("error_match_percent"),
        })

    sols = await read(
        """SELECT ecs.distinct_error_id, ecs.summary_id, ecs.match_percent,
                  s.title, s.source_id
           FROM error_cluster_solutions ecs
           JOIN summaries s ON s.id = ecs.summary_id
           ORDER BY ecs.match_percent DESC""",
    )
    for s in sols:
        sid = f"note:{s['summary_id']}"
        cid = f"cluster:{s['distinct_error_id']}"
        if not any(n["id"] == cid for n in nodes):
            continue
        if not any(n["id"] == sid for n in nodes):
            nodes.append({
                "id": sid,
                "label": s.get("title") or f"Note {s.get('source_id')}",
                "type": "Solution",
                "summary_id": s["summary_id"],
                "note_number": s.get("source_id"),
            })
        links.append({
            "source": cid,
            "target": sid,
            "type": "SUGGESTS",
            "match_percent": s.get("match_percent"),
        })

    return {"nodes": nodes, "links": links}
