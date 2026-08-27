"""hybrid_search — vector search over summary_embeddings (notes + community).

Returns top-N full summaries with match_percent and images. No source field.

ponytail: the tool keeps its name for the MCP clients that call it, but the
keyword leg is gone — scoring is pure cosine similarity, nothing else. There is
no pattern matching anywhere in this path.
"""

import asyncio
import logging

from config import (
    HYBRID_SEARCH_DEFAULT_LIMIT,
    HYBRID_SEARCH_MAX_LIMIT,
)
from db import read
from routes.community import _resolve_images
from routes.summaries import _summary_to_ui
from services.embeddings import _vec_literal, embed_text

logger = logging.getLogger(__name__)


async def search_vector(vec: str, limit: int) -> list[dict]:
    """Nearest `limit` summaries to an existing pgvector literal.

    Titan vectors are normalized, so cosine similarity is 1 - distance.
    Callers that already hold a stored embedding use this and skip Bedrock.
    """
    rows = await read(
        """SELECT source, summary_id,
                  (1 - (embedding <=> ?::vector)) AS similarity
           FROM summary_embeddings
           ORDER BY embedding <=> ?::vector
           LIMIT ?""",
        (vec, vec, limit),
    )
    return [
        {
            "source": r["source"],
            "summary_id": r["summary_id"],
            "score": max(0.0, min(1.0, float(r["similarity"] or 0))),
        }
        for r in rows
    ]


async def search_text(query: str, limit: int) -> list[dict]:
    """Embed `query` then search. One Bedrock call."""
    emb = await asyncio.to_thread(embed_text, query)
    return await search_vector(_vec_literal(emb), limit)


async def _load_summaries(hits: list[dict], with_blobs: bool = True) -> dict[tuple, dict]:
    """Hydrate every hit in two queries, not one per hit.

    with_blobs=False skips image/attachment URL resolution (S3 presigning) for
    callers that throw those fields away — the diagnose chain does.
    """
    from routes.summaries import _resolve_attachments

    wanted: dict[str, list[int]] = {"notes": [], "community": []}
    for h in hits:
        if h["source"] in wanted:
            wanted[h["source"]].append(h["summary_id"])

    out: dict[tuple, dict] = {}
    for source, table in (("notes", "summaries"), ("community", "community_summaries")):
        ids = wanted[source]
        if not ids:
            continue
        rows = await read(f"SELECT * FROM {table} WHERE id = ANY(?)", (ids,))
        for row in rows:
            ui = _summary_to_ui(row)
            if not with_blobs:
                ui["images"], ui["attachments"] = {}, []
            elif source == "notes":
                ui["images"] = {}
                ui["attachments"] = _resolve_attachments(row.get("attachments"))
            else:
                ui["images"] = _resolve_images(row.get("images"))
                ui["attachments"] = []
            out[(source, row["id"])] = ui
    return out


async def hydrate(hits: list[dict], with_blobs: bool = True) -> list[dict]:
    """Ranked hits → full summaries with match_percent, order preserved."""
    loaded = await _load_summaries(hits, with_blobs=with_blobs)
    results = []
    for hit in hits:
        ui = loaded.get((hit["source"], hit["summary_id"]))
        if not ui:
            continue
        results.append({**ui, "match_percent": int(round(hit["score"] * 100))})
    return results


async def handle(query: str, limit: int | None = None) -> list[dict]:
    """Top `limit` hits: full summary + images + match_percent. No source field."""
    q = (query or "").strip()
    if not q:
        return []
    if limit is None:
        limit = HYBRID_SEARCH_DEFAULT_LIMIT
    limit = max(1, min(int(limit), HYBRID_SEARCH_MAX_LIMIT))

    try:
        hits = await search_text(q, limit)
    except Exception as e:
        logger.warning("vector search failed: %s", e)
        return []

    return await hydrate(hits)
