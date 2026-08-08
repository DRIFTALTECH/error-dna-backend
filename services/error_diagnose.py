"""Error diagnose chain — RAG-first distinct errors, then solution notes."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone, timedelta

from config import (
    EMBED_MODEL_ID,
    ERROR_MATCH_THRESHOLD,
    ERROR_SOLUTION_LIMIT,
    ERROR_VECTOR_SEARCH_LIMIT,
    INFORMATIONAL_FAMILY_CODE,
)
from db import read, write
from mcp_server.tools.hybrid_search.handler import handle as hybrid_search
from services.embeddings import content_hash, embed_text, _vec_literal
from services.error_fallback import generate_fallback_solution
from services.error_generalize import generalize_error

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _distinct_blob(title: str, generalized: str, summary: str | None) -> str:
    parts = [f"TITLE: {title}", f"GENERALIZED: {generalized}"]
    if summary:
        parts.append(f"SUMMARY: {summary}")
    return "\n\n".join(parts)


async def _vector_search(text: str, limit: int | None = None) -> list[dict]:
    if limit is None:
        limit = ERROR_VECTOR_SEARCH_LIMIT
    emb = await asyncio.to_thread(embed_text, text)
    vec = _vec_literal(emb)
    rows = await read(
        """SELECT de.id, de.title, de.generalized_text, de.summary, de.family_code,
                  de.occurrence_count,
                  (1 - (dee.embedding <=> ?::vector)) AS similarity
           FROM distinct_error_embeddings dee
           JOIN distinct_errors de ON de.id = dee.distinct_error_id
           ORDER BY dee.embedding <=> ?::vector
           LIMIT ?""",
        (vec, vec, limit),
    )
    out = []
    for r in rows:
        sim = max(0.0, min(1.0, float(r["similarity"] or 0)))
        out.append({**dict(r), "similarity": sim})
    return out


def _best_match(hits: list[dict]) -> dict | None:
    if not hits:
        return None
    best = hits[0]
    if best["similarity"] >= ERROR_MATCH_THRESHOLD:
        return best
    return None


async def _touch_distinct(distinct_id: int) -> dict:
    now = datetime.now(IST).isoformat()
    rows = await write(
        """UPDATE distinct_errors
           SET occurrence_count = occurrence_count + 1, last_seen_at = ?
           WHERE id = ? RETURNING *""",
        (now, distinct_id),
    )
    return rows[0]


async def _create_distinct(
    title: str,
    generalized: str,
    family_code: str,
    summary: str | None = None,
) -> tuple[dict, bool]:
    """Insert distinct + embedding. Returns (row, created_new)."""
    fp = _fingerprint(generalized)
    existing = await read("SELECT * FROM distinct_errors WHERE fingerprint_hash = ?", (fp,))
    if existing:
        row = await _touch_distinct(existing[0]["id"])
        return row, False

    now = datetime.now(IST).isoformat()
    rows = await write(
        """INSERT INTO distinct_errors
           (fingerprint_hash, title, generalized_text, summary, family_code,
            occurrence_count, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,1,?,?) RETURNING *""",
        (fp, title, generalized, summary, family_code, now, now),
    )
    row = rows[0]
    blob = _distinct_blob(title, generalized, summary)
    digest = content_hash(blob)
    emb = await asyncio.to_thread(embed_text, blob)
    vec = _vec_literal(emb)
    await write(
        """INSERT INTO distinct_error_embeddings
           (distinct_error_id, content_hash, embedding, model, created_at, updated_at)
           VALUES (?,?,?::vector,?,?,?)""",
        (row["id"], digest, vec, EMBED_MODEL_ID, now, now),
    )
    return row, True


async def _family_name(code: str | None) -> str | None:
    from services.error_families import family_display_name
    return await family_display_name(code)


def _slim_cautions(gotchas) -> list[str]:
    out: list[str] = []
    for g in gotchas or []:
        if isinstance(g, dict):
            desc = (g.get("description") or "").strip()
            name = (g.get("name") or "").strip()
            if not desc:
                continue
            if name and name.lower() != "heads up":
                out.append(f"{name}: {desc}")
            else:
                out.append(desc)
        else:
            text = str(g).strip()
            if text:
                out.append(text)
    return out


def _slim_solution(hit: dict) -> dict:
    """Diagnose API — problem / fix / cautions only; no note ids or SAP metadata."""
    fix = hit.get("how_to_fix")
    if not isinstance(fix, list):
        fix = [str(fix)] if fix else []
    return {
        "title": hit.get("title") or "",
        "problem": hit.get("the_problem") or "",
        "whats_wrong": hit.get("whats_going_on") or "",
        "solution": [str(s).strip() for s in fix if str(s).strip()],
        "cautions": _slim_cautions(hit.get("gotchas")),
        "match_percent": hit.get("match_percent"),
    }


def _diagnose_response(
    *,
    distinct_row: dict,
    generalized: str,
    family_code: str,
    family_name: str | None,
    match_percent: float,
    informational: bool,
    solutions: list[dict],
    fallback_solution: str | None,
) -> dict:
    body: dict = {
        "distinct_error": {
            "title": distinct_row.get("title") or "",
            "generalized_error": generalized,
            "problem": distinct_row.get("summary") or generalized,
            "family_code": family_code,
            "family_name": family_name or "",
            "cluster_confidence": match_percent,
            "informational": informational,
        },
        "solutions": [_slim_solution(s) for s in solutions],
    }
    if fallback_solution:
        body["fallback_solution"] = fallback_solution
    return body


async def _persist_cluster_solutions(distinct_id: int, solutions: list[dict]) -> None:
    """Upsert semantic note links for graph + future diagnoses."""
    if not solutions:
        return
    now = datetime.now(IST).isoformat()
    for sol in solutions:
        sid = sol.get("id")
        if not sid:
            continue
        pct = float(sol.get("match_percent") or 0)
        await write(
            """INSERT INTO error_cluster_solutions
               (distinct_error_id, summary_id, match_percent, last_seen_at)
               VALUES (?,?,?,?)
               ON CONFLICT (distinct_error_id, summary_id) DO UPDATE SET
                 match_percent = EXCLUDED.match_percent,
                 hit_count = error_cluster_solutions.hit_count + 1,
                 last_seen_at = EXCLUDED.last_seen_at""",
            (distinct_id, sid, pct, now),
        )


async def diagnose(raw_error: str, caller: str | None = None, source: str | None = None) -> dict:
    raw = (raw_error or "").strip()
    if not raw:
        raise ValueError("error_text is required")

    created_new = False
    match_percent = 0.0
    distinct_row: dict | None = None
    generalized = raw
    gen: dict | None = None

    hit = _best_match(await _vector_search(raw))

    if not hit:
        gen = await generalize_error(raw)
        generalized = gen["generalized_text"]
        hit = _best_match(await _vector_search(generalized))

    if hit:
        distinct_row = await _touch_distinct(hit["id"])
        generalized = distinct_row["generalized_text"]
        match_percent = round(hit["similarity"] * 100, 1)
    else:
        if gen is None:
            gen = await generalize_error(raw)
        generalized = gen["generalized_text"]
        distinct_row, created_new = await _create_distinct(
            gen["title"],
            generalized,
            gen.get("family_code") or "UNCLASSIFIED_ERROR",
            gen.get("summary"),
        )

    family_code = distinct_row.get("family_code") or "UNCLASSIFIED_ERROR"
    family_name = await _family_name(family_code)
    informational = family_code == INFORMATIONAL_FAMILY_CODE

    solutions: list[dict] = []
    fallback_solution: str | None = None

    if not informational:
        query = generalized or distinct_row.get("title") or raw
        solutions = await hybrid_search(query=query, limit=ERROR_SOLUTION_LIMIT)
        if solutions:
            await _persist_cluster_solutions(distinct_row["id"], solutions)
        else:
            fallback_solution = await generate_fallback_solution(
                raw,
                generalized=generalized,
                title=distinct_row.get("title") or "",
                family_name=family_name or "",
                family_code=family_code,
                source=source,
            )

    await write(
        """INSERT INTO error_events
           (raw_text, generalized_text, distinct_error_id, family_code,
            error_match_percent, created_new, caller, source)
           VALUES (?,?,?,?,?,?,?,?)""",
        (
            raw,
            generalized,
            distinct_row["id"],
            family_code,
            match_percent,
            1 if created_new else 0,
            caller,
            source,
        ),
    )

    return _diagnose_response(
        distinct_row=distinct_row,
        generalized=generalized,
        family_code=family_code,
        family_name=family_name,
        match_percent=match_percent,
        informational=informational,
        solutions=solutions,
        fallback_solution=fallback_solution,
    )
