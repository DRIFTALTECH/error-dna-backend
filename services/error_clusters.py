"""Error cluster listing + embedding-similarity graph for the UI."""

from __future__ import annotations

import math
import random
import re

from config import (
    GRAPH_NOTE_SIMILARITY_THRESHOLD,
    GRAPH_SIMILAR_K,
    GRAPH_SIMILARITY_THRESHOLD,
)
from db import read
from services.error_families import family_display_name

_VEC_RE = re.compile(r"[\[\],\s]+")


def _parse_vector(raw) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    text = str(raw).strip()
    if not text:
        return []
    parts = [p for p in _VEC_RE.split(text.strip("[]")) if p]
    return [float(p) for p in parts]


def _random_project_2d(vectors: list[list[float]], scale: float = 1100.0) -> list[tuple[float, float]]:
    if not vectors:
        return []
    if len(vectors) == 1:
        return [(0.0, 0.0)]
    d = len(vectors[0])
    rng = random.Random(42)

    def unit() -> list[float]:
        v = [rng.gauss(0, 1) for _ in range(d)]
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    ax, ay = unit(), unit()
    out: list[tuple[float, float]] = []
    for vec in vectors:
        x = sum(a * b for a, b in zip(vec, ax)) * scale
        y = sum(a * b for a, b in zip(vec, ay)) * scale
        out.append((round(x, 1), round(y, 1)))
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


async def _cluster_similar_pairs(threshold: float, top_k: int) -> list[dict]:
    rows = await read(
        """WITH ranked AS (
               SELECT a.distinct_error_id AS source_id,
                      b.distinct_error_id AS target_id,
                      (1 - (a.embedding <=> b.embedding)) AS similarity,
                      ROW_NUMBER() OVER (
                          PARTITION BY a.distinct_error_id
                          ORDER BY a.embedding <=> b.embedding
                      ) AS rn
               FROM distinct_error_embeddings a
               JOIN distinct_error_embeddings b
                 ON a.distinct_error_id <> b.distinct_error_id
               WHERE (1 - (a.embedding <=> b.embedding)) >= ?
           )
           SELECT source_id, target_id, similarity
           FROM ranked
           WHERE rn <= ?
           ORDER BY similarity DESC""",
        (threshold, top_k),
    )
    return [dict(r) for r in rows]


async def _note_similar_pairs(note_ids: list[int], threshold: float) -> list[dict]:
    if len(note_ids) < 2:
        return []
    placeholders = ",".join("?" * len(note_ids))
    rows = await read(
        f"""SELECT a.summary_id AS source_id, b.summary_id AS target_id,
                   (1 - (a.embedding <=> b.embedding)) AS similarity
            FROM summary_embeddings a
            JOIN summary_embeddings b
              ON a.summary_id < b.summary_id
             AND a.source = 'notes' AND b.source = 'notes'
            WHERE a.summary_id IN ({placeholders})
              AND b.summary_id IN ({placeholders})
              AND (1 - (a.embedding <=> b.embedding)) >= ?
            ORDER BY similarity DESC""",
        (*note_ids, *note_ids, threshold),
    )
    return [dict(r) for r in rows]


async def build_graph(
    min_similarity: float | None = None,
    similar_k: int | None = None,
) -> dict:
    """Embedding-driven cluster graph — SIMILAR edges from distinct_error_embeddings."""
    threshold = min_similarity if min_similarity is not None else GRAPH_SIMILARITY_THRESHOLD
    top_k = similar_k if similar_k is not None else GRAPH_SIMILAR_K
    threshold = max(0.0, min(1.0, threshold))
    top_k = max(1, min(top_k, 20))

    cluster_rows = await read(
        """SELECT de.id, de.title, de.generalized_text, de.family_code, de.occurrence_count,
                  dee.embedding::text AS embedding
           FROM distinct_errors de
           JOIN distinct_error_embeddings dee ON dee.distinct_error_id = de.id
           ORDER BY de.id""",
    )

    nodes: list[dict] = []
    links: list[dict] = []
    node_ids: set[str] = set()
    cluster_xy: dict[int, tuple[float, float]] = {}

    valid_rows: list[dict] = []
    vectors: list[list[float]] = []
    for r in cluster_rows:
        vec = _parse_vector(r.get("embedding"))
        if not vec:
            continue
        valid_rows.append(dict(r))
        vectors.append(vec)

    coords = _random_project_2d(vectors)

    for r, (x, y) in zip(valid_rows, coords):
        cid = f"cluster:{r['id']}"
        node_ids.add(cid)
        cluster_xy[r["id"]] = (x, y)
        nodes.append({
            "id": cid,
            "label": r.get("title") or f"Cluster {r['id']}",
            "type": "Cluster",
            "labels": ["DistinctError"],
            "cluster_id": r["id"],
            "family_code": r.get("family_code") or "",
            "occurrence_count": r.get("occurrence_count") or 0,
            "x": x,
            "y": y,
        })

    seen_edges: set[tuple[int, int]] = set()
    for pair in await _cluster_similar_pairs(threshold, top_k):
        a, b = int(pair["source_id"]), int(pair["target_id"])
        key = (min(a, b), max(a, b))
        if key in seen_edges:
            continue
        seen_edges.add(key)
        sa, sb = f"cluster:{a}", f"cluster:{b}"
        if sa not in node_ids or sb not in node_ids:
            continue
        sim = max(0.0, min(1.0, float(pair["similarity"] or 0)))
        links.append({
            "source": sa,
            "target": sb,
            "type": "SIMILAR",
            "match_percent": round(sim * 100, 1),
        })

    sol_rows = await read(
        """SELECT ecs.distinct_error_id, ecs.summary_id, ecs.match_percent,
                  s.title, s.source_id AS note_number
           FROM error_cluster_solutions ecs
           JOIN summaries s ON s.id = ecs.summary_id
           ORDER BY ecs.match_percent DESC""",
    )

    note_ids: list[int] = []
    cluster_note_links: list[dict] = []
    for s in sol_rows:
        cid = f"cluster:{s['distinct_error_id']}"
        if cid not in node_ids:
            continue
        sid = f"note:{s['summary_id']}"
        cluster_note_links.append({**dict(s), "cid": cid, "sid": sid})
        note_ids.append(int(s["summary_id"]))

    note_ids_unique = sorted(set(note_ids))
    note_xy: dict[int, tuple[float, float]] = {}

    for nid in note_ids_unique:
        sid = f"note:{nid}"
        related = [l for l in cluster_note_links if l["summary_id"] == nid]
        xs, ys, wsum = 0.0, 0.0, 0.0
        for l in related:
            xy = cluster_xy.get(int(l["distinct_error_id"]))
            if not xy:
                continue
            w = max(float(l.get("match_percent") or 1), 1.0)
            xs += xy[0] * w
            ys += xy[1] * w
            wsum += w
        if wsum > 0:
            angle = (nid % 360) * (math.pi / 180)
            note_xy[nid] = (
                round(xs / wsum + math.cos(angle) * 180, 1),
                round(ys / wsum + math.sin(angle) * 180, 1),
            )

    for nid, (x, y) in note_xy.items():
        sid = f"note:{nid}"
        if sid in node_ids:
            continue
        row = next((l for l in cluster_note_links if l["summary_id"] == nid), None)
        if not row:
            continue
        node_ids.add(sid)
        nodes.append({
            "id": sid,
            "label": row.get("title") or f"Note {row.get('note_number')}",
            "type": "Solution",
            "labels": ["SolutionNote"],
            "summary_id": nid,
            "note_number": row.get("note_number"),
            "x": x,
            "y": y,
        })

    for l in cluster_note_links:
        if l["sid"] not in node_ids:
            continue
        links.append({
            "source": l["cid"],
            "target": l["sid"],
            "type": "SUGGESTS",
            "match_percent": l.get("match_percent"),
        })

    for pair in await _note_similar_pairs(note_ids_unique, GRAPH_NOTE_SIMILARITY_THRESHOLD):
        sa, sb = f"note:{pair['source_id']}", f"note:{pair['target_id']}"
        if sa not in node_ids or sb not in node_ids:
            continue
        sim = max(0.0, min(1.0, float(pair["similarity"] or 0)))
        links.append({
            "source": sa,
            "target": sb,
            "type": "NOTE_SIMILAR",
            "match_percent": round(sim * 100, 1),
        })

    relationships: list[dict] = []
    for i, lnk in enumerate(links):
        relationships.append({
            "elementId": f"rel:{i}",
            "type": lnk["type"],
            "startNode": lnk["source"],
            "endNode": lnk["target"],
            "properties": {
                k: v for k, v in lnk.items()
                if k not in ("source", "target", "type") and v is not None
            },
        })

    return {
        "nodes": nodes,
        "links": links,
        "relationships": relationships,
        "meta": {
            "format": "neo4j",
            "layout": "embedding-force",
            "similarity_threshold": threshold,
            "similar_k": top_k,
            "cluster_count": sum(1 for n in nodes if n["type"] == "Cluster"),
            "solution_count": sum(1 for n in nodes if n["type"] == "Solution"),
            "similar_edge_count": sum(1 for l in links if l["type"] == "SIMILAR"),
        },
    }
