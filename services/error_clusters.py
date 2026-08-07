"""Error cluster listing + Neo4j-shaped graph for the UI."""

from __future__ import annotations

import math

from db import read
from services.error_families import code_from_family_field, family_display_name

# Default hub colors when family row has none
_HUB_COLORS = [
    "#8b5cf6", "#f97316", "#06b6d4", "#22c55e", "#ec4899",
    "#eab308", "#6366f1", "#14b8a6", "#ef4444", "#a855f7",
]


async def _family_meta() -> dict[str, dict]:
    rows = await read(
        "SELECT code, family_name, color, severity FROM error_families WHERE code IS NOT NULL",
    )
    out: dict[str, dict] = {}
    for i, r in enumerate(rows):
        code = r["code"]
        out[code] = {
            "code": code,
            "label": r.get("family_name") or code,
            "color": r.get("color") or _HUB_COLORS[i % len(_HUB_COLORS)],
            "severity": r.get("severity") or "medium",
        }
    return out


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
                  s.title, s.source_id AS note_number, s.family AS note_family
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
                "note_family": s.get("note_family"),
            }
            for s in solutions
        ],
    }


async def build_graph() -> dict:
    """Family hubs + clusters + events + solutions — grouped for force-graph UI."""
    meta = await _family_meta()
    clusters = await read(
        """SELECT de.id, de.title, de.family_code, de.occurrence_count
           FROM distinct_errors de ORDER BY de.family_code, de.id""",
    )

    active_families: list[str] = []
    seen_fc: set[str] = set()
    for c in clusters:
        fc = c.get("family_code") or "UNCLASSIFIED_ERROR"
        if fc not in seen_fc:
            seen_fc.add(fc)
            active_families.append(fc)

    # Hub positions on a ring — each family gets its own territory
    groups: list[dict] = []
    hub_xy: dict[str, dict] = {}
    n = max(len(active_families), 1)
    for i, fcode in enumerate(active_families):
        angle = (2 * math.pi * i / n) - (math.pi / 2)
        radius = 420
        x = round(math.cos(angle) * radius, 1)
        y = round(math.sin(angle) * radius, 1)
        fm = meta.get(fcode, {})
        groups.append({
            "family_code": fcode,
            "label": fm.get("label") or fcode,
            "color": fm.get("color") or _HUB_COLORS[i % len(_HUB_COLORS)],
            "x": x,
            "y": y,
            "cluster_count": sum(1 for c in clusters if (c.get("family_code") or "UNCLASSIFIED_ERROR") == fcode),
        })
        hub_xy[fcode] = {"x": x, "y": y, "color": groups[-1]["color"]}

    nodes: list[dict] = []
    links: list[dict] = []
    node_ids: set[str] = set()

    for fcode in active_families:
        fm = meta.get(fcode, {})
        hub = hub_xy[fcode]
        nid = f"family:{fcode}"
        node_ids.add(nid)
        nodes.append({
            "id": nid,
            "label": fm.get("label") or fcode,
            "type": "Family",
            "family_code": fcode,
            "color": hub["color"],
            "fx": hub["x"],
            "fy": hub["y"],
        })

    for c in clusters:
        fcode = c.get("family_code") or "UNCLASSIFIED_ERROR"
        hub = hub_xy.get(fcode, {"x": 0, "y": 0, "color": "#64748b"})
        cid = f"cluster:{c['id']}"
        node_ids.add(cid)
        nodes.append({
            "id": cid,
            "label": c.get("title") or f"Cluster {c['id']}",
            "type": "Cluster",
            "cluster_id": c["id"],
            "family_code": fcode,
            "color": hub["color"],
            "occurrence_count": c.get("occurrence_count") or 0,
        })
        links.append({"source": f"family:{fcode}", "target": cid, "type": "HAS_CLUSTER"})

    events = await read(
        """SELECT id, distinct_error_id, raw_text, error_match_percent
           FROM error_events ORDER BY created_at DESC LIMIT 150""",
    )
    for e in events:
        cid = f"cluster:{e['distinct_error_id']}"
        if cid not in node_ids:
            continue
        cluster_node = next((n for n in nodes if n["id"] == cid), None)
        fcode = cluster_node.get("family_code") if cluster_node else "UNCLASSIFIED_ERROR"
        eid = f"event:{e['id']}"
        if eid in node_ids:
            continue
        node_ids.add(eid)
        raw = (e.get("raw_text") or "").replace("\n", " ").strip()
        nodes.append({
            "id": eid,
            "label": raw or f"Event {e['id']}",
            "type": "Event",
            "event_id": e["id"],
            "cluster_id": e["distinct_error_id"],
            "family_code": fcode,
            "color": hub_xy.get(fcode, {}).get("color", "#94a3b8"),
            "match_percent": e.get("error_match_percent"),
        })
        links.append({
            "source": eid,
            "target": cid,
            "type": "MATCHED",
            "match_percent": e.get("error_match_percent"),
        })

    sols = await read(
        """SELECT ecs.distinct_error_id, ecs.summary_id, ecs.match_percent,
                  s.title, s.source_id, s.family AS note_family,
                  de.family_code AS cluster_family
           FROM error_cluster_solutions ecs
           JOIN summaries s ON s.id = ecs.summary_id
           JOIN distinct_errors de ON de.id = ecs.distinct_error_id
           ORDER BY ecs.match_percent DESC""",
    )
    note_family_cache: dict[str, str] = {}
    for s in sols:
        cid = f"cluster:{s['distinct_error_id']}"
        if cid not in node_ids:
            continue
        cluster_family = s.get("cluster_family") or "UNCLASSIFIED_ERROR"
        note_fam_raw = (s.get("note_family") or "").strip()
        if note_fam_raw not in note_family_cache:
            resolved = await code_from_family_field(note_fam_raw)
            note_family_cache[note_fam_raw] = resolved or cluster_family
        note_family = note_family_cache[note_fam_raw]
        # Visual home: note family if known, else cluster family
        home_family = note_family if note_family in hub_xy else cluster_family

        sid = f"note:{s['summary_id']}"
        if sid not in node_ids:
            node_ids.add(sid)
            hub = hub_xy.get(home_family, hub_xy.get(cluster_family, {"color": "#34d399"}))
            nodes.append({
                "id": sid,
                "label": s.get("title") or f"Note {s.get('source_id')}",
                "type": "Solution",
                "summary_id": s["summary_id"],
                "note_number": s.get("source_id"),
                "family_code": home_family,
                "cluster_family": cluster_family,
                "note_family": note_fam_raw,
                "color": hub.get("color", "#34d399"),
                "match_percent": s.get("match_percent"),
            })
            # Solution belongs to its catalog family hub
            fam_nid = f"family:{home_family}"
            if fam_nid in node_ids:
                links.append({
                    "source": fam_nid,
                    "target": sid,
                    "type": "IN_FAMILY",
                })

        links.append({
            "source": cid,
            "target": sid,
            "type": "SUGGESTS",
            "match_percent": s.get("match_percent"),
        })

    return {"nodes": nodes, "links": links, "groups": groups}
