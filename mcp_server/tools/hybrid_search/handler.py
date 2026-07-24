"""hybrid_search — vector + keyword blend over summary_embeddings.

Returns top-N full summaries with match_percent and images. No source field.
"""

import asyncio
import logging
import re

from db import read
from routes.community import _resolve_images
from routes.summaries import _summary_to_ui
from services.embeddings import _vec_literal, embed_text

logger = logging.getLogger(__name__)

VECTOR_WEIGHT = 0.7
KEYWORD_WEIGHT = 0.3
CANDIDATE_LIMIT = 20  # per leg, before blend


def _keyword_score(query: str, row: dict) -> float:
    """0–1 score from substring hits (title counts most)."""
    q = (query or "").strip().lower()
    if not q:
        return 0.0
    title = (row.get("title") or "").lower()
    issue = (row.get("issue") or "").lower()
    tags = (row.get("tags") or "").lower()
    summary = (row.get("summary") or "").lower()
    score = 0.0
    if q in title:
        score = max(score, 1.0)
    if q in issue:
        score = max(score, 0.85)
    if q in tags:
        score = max(score, 0.75)
    if q in summary:
        score = max(score, 0.55)
    # Also score on individual tokens (≥3 chars) for multi-word queries.
    tokens = [t for t in re.split(r"\W+", q) if len(t) >= 3]
    if tokens and score < 1.0:
        blob = f"{title} {issue} {tags} {summary}"
        hits = sum(1 for t in tokens if t in blob)
        score = max(score, 0.4 * (hits / len(tokens)))
    return min(1.0, score)


async def _vector_hits(query: str, limit: int) -> list[dict]:
    emb = await asyncio.to_thread(embed_text, query)
    vec = _vec_literal(emb)
    # Cosine distance <=> ; Titan vectors are normalized → similarity ≈ 1 - distance.
    rows = await read(
        """SELECT source, summary_id,
                  (1 - (embedding <=> ?::vector)) AS similarity
           FROM summary_embeddings
           ORDER BY embedding <=> ?::vector
           LIMIT ?""",
        (vec, vec, limit),
    )
    out = []
    for r in rows:
        sim = float(r["similarity"] or 0)
        sim = max(0.0, min(1.0, sim))
        out.append({
            "source": r["source"],
            "summary_id": r["summary_id"],
            "vector_score": sim,
        })
    return out


async def _keyword_hits(query: str, limit: int) -> list[dict]:
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    # Pull candidates from both tables; score in Python.
    notes = await read(
        """SELECT id, title, issue, summary, tags FROM summaries
           WHERE is_latest = 1
             AND (title ILIKE ? OR issue ILIKE ? OR summary ILIKE ? OR tags ILIKE ?)
           LIMIT ?""",
        (like, like, like, like, limit),
    )
    community = await read(
        """SELECT id, title, issue, summary, tags FROM community_summaries
           WHERE is_latest = 1
             AND (title ILIKE ? OR issue ILIKE ? OR summary ILIKE ? OR tags ILIKE ?)
           LIMIT ?""",
        (like, like, like, like, limit),
    )
    scored = []
    for r in notes:
        scored.append({
            "source": "notes",
            "summary_id": r["id"],
            "keyword_score": _keyword_score(q, r),
        })
    for r in community:
        scored.append({
            "source": "community",
            "summary_id": r["id"],
            "keyword_score": _keyword_score(q, r),
        })
    scored.sort(key=lambda x: x["keyword_score"], reverse=True)
    return scored[:limit]


async def _load_summary(source: str, summary_id: int) -> dict | None:
    if source == "notes":
        rows = await read("SELECT * FROM summaries WHERE id = ?", (summary_id,))
        if not rows:
            return None
        from routes.summaries import _resolve_attachments
        ui = _summary_to_ui(rows[0])
        ui["images"] = {}
        ui["attachments"] = _resolve_attachments(rows[0].get("attachments"))
        return ui
    rows = await read("SELECT * FROM community_summaries WHERE id = ?", (summary_id,))
    if not rows:
        return None
    ui = _summary_to_ui(rows[0])
    ui["images"] = _resolve_images(rows[0].get("images"))
    ui["attachments"] = []
    return ui


async def handle(query: str, limit: int = 5) -> list[dict]:
    """Top `limit` hits: full summary + images + match_percent. No source field."""
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, min(int(limit), 20))

    try:
        vector_hits = await _vector_hits(q, CANDIDATE_LIMIT)
    except Exception as e:
        logger.warning(f"vector search failed, keyword-only: {e}")
        vector_hits = []

    keyword_hits = await _keyword_hits(q, CANDIDATE_LIMIT)

    merged: dict[tuple, dict] = {}
    for h in vector_hits:
        key = (h["source"], h["summary_id"])
        merged[key] = {
            "source": h["source"],
            "summary_id": h["summary_id"],
            "vector_score": h["vector_score"],
            "keyword_score": 0.0,
        }
    for h in keyword_hits:
        key = (h["source"], h["summary_id"])
        if key in merged:
            merged[key]["keyword_score"] = h["keyword_score"]
        else:
            merged[key] = {
                "source": h["source"],
                "summary_id": h["summary_id"],
                "vector_score": 0.0,
                "keyword_score": h["keyword_score"],
            }

    ranked = []
    for m in merged.values():
        v, k = m["vector_score"], m["keyword_score"]
        if v > 0 and k > 0:
            score = VECTOR_WEIGHT * v + KEYWORD_WEIGHT * k
        elif v > 0:
            score = v
        else:
            score = k
        ranked.append({**m, "score": score})

    ranked.sort(key=lambda x: x["score"], reverse=True)

    results = []
    for hit in ranked[:limit]:
        ui = await _load_summary(hit["source"], hit["summary_id"])
        if not ui:
            continue
        match_percent = int(round(max(0.0, min(1.0, hit["score"])) * 100))
        results.append({
            **ui,
            "match_percent": match_percent,
        })
    return results
