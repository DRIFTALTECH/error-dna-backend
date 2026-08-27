"""The diagnose chain — one raw error to a cluster identity plus its fixes.

  L0  exact repeat   sha256 of the raw text against error_messages.
                     A string we have seen before needs no LLM and no Bedrock
                     call, so this runs before the lock and never blocks.
  L1  expand         LLM: retrieval query, signature, family, problem.
  L2  error VDB      cosine over distinct_error_embeddings: merge into a
                     cluster above ERROR_CLUSTER_THRESHOLD, else create one.
  L3  notes RAG      cosine over summary_embeddings. On a cluster hit the
                     cluster's stored vector searches alongside the fresh one,
                     which costs a SELECT rather than a second embedding.
  L4  persist        cluster, raw message, embedding, audit event.

The knowledge base always wins: services/error_fallback.py runs only when zero
notes clear ERROR_SOLUTION_THRESHOLD, and its answer is never stored.

One diagnose runs at a time. A second caller that needs the LLM is refused
outright rather than queued (DiagnoseBusy -> 409); L0 repeats are unaffected.
No pattern matching anywhere in this file — the LLM is the only classifier.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from config import (
    EMBED_MODEL_ID,
    ERROR_CLUSTER_THRESHOLD,
    ERROR_SOLUTION_LIMIT,
    ERROR_SOLUTION_THRESHOLD,
    ERROR_VECTOR_SEARCH_LIMIT,
)
from db import read, write
from mcp_server.tools.hybrid_search.handler import hydrate, search_vector
from services.embeddings import _vec_literal, content_hash, embed_text
from services.error_expand import UNCLASSIFIED, expand_error
from services.error_fallback import generate_fallback_solution

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

# ponytail: one asyncio.Lock, correct because the API runs a single uvicorn
# worker. Add a Postgres advisory lock before running more than one process.
_LOCK = asyncio.Lock()


class DiagnoseBusy(Exception):
    """Another diagnose holds the lock. The route turns this into a 409."""


def _now() -> str:
    return datetime.now(IST).isoformat()


def _fingerprint(text: str) -> str:
    """Whitespace-normalized hash. Same error, different indentation, same row."""
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# L0 — the raw message log
# ---------------------------------------------------------------------------

async def _lookup_message(fp: str) -> int | None:
    rows = await read(
        "SELECT distinct_error_id FROM error_messages WHERE raw_hash = ?", (fp,)
    )
    return rows[0]["distinct_error_id"] if rows else None


async def _record_message(cluster_id: int, fp: str, raw: str) -> None:
    """Log this raw string against its cluster; a repeat only bumps the counter."""
    await write(
        """INSERT INTO error_messages (distinct_error_id, raw_hash, raw_text, last_seen_at)
           VALUES (?,?,?,?)
           ON CONFLICT (raw_hash) DO UPDATE SET
             seen_count = error_messages.seen_count + 1,
             last_seen_at = EXCLUDED.last_seen_at""",
        (cluster_id, fp, raw, _now()),
    )


# ---------------------------------------------------------------------------
# L2 — the error cluster store
# ---------------------------------------------------------------------------

async def _cluster_by_id(cluster_id: int) -> dict | None:
    rows = await read("SELECT * FROM distinct_errors WHERE id = ?", (cluster_id,))
    return rows[0] if rows else None


async def _touch_cluster(cluster_id: int) -> dict:
    rows = await write(
        """UPDATE distinct_errors
           SET occurrence_count = occurrence_count + 1, last_seen_at = ?
           WHERE id = ? RETURNING *""",
        (_now(), cluster_id),
    )
    return rows[0]


async def _search_clusters(vec: str) -> dict | None:
    """Nearest cluster to `vec`, or None if none clears the merge threshold."""
    rows = await read(
        """SELECT de.id, (1 - (dee.embedding <=> ?::vector)) AS similarity
           FROM distinct_error_embeddings dee
           JOIN distinct_errors de ON de.id = dee.distinct_error_id
           ORDER BY dee.embedding <=> ?::vector
           LIMIT ?""",
        (vec, vec, ERROR_VECTOR_SEARCH_LIMIT),
    )
    if not rows:
        return None
    best = rows[0]
    sim = max(0.0, min(1.0, float(best["similarity"] or 0)))
    return {"id": best["id"], "similarity": sim} if sim >= ERROR_CLUSTER_THRESHOLD else None


async def _create_cluster(fp: str, gen: dict, vec: str) -> dict:
    """Insert the cluster plus the embedding of its expanded_error."""
    now = _now()
    rows = await write(
        """INSERT INTO distinct_errors
           (fingerprint_hash, title, generalized_text, summary, family_code,
            occurrence_count, first_seen_at, last_seen_at)
           VALUES (?,?,?,?,?,1,?,?)
           ON CONFLICT (fingerprint_hash) DO UPDATE SET
             occurrence_count = distinct_errors.occurrence_count + 1,
             last_seen_at = EXCLUDED.last_seen_at
           RETURNING *""",
        (
            fp,
            gen["error_signature"],
            gen["expanded_error"],
            gen["problem"],
            gen["family_code"],
            now,
            now,
        ),
    )
    row = rows[0]
    await write(
        """INSERT INTO distinct_error_embeddings
           (distinct_error_id, content_hash, embedding, model, created_at, updated_at)
           VALUES (?,?,?::vector,?,?,?)
           ON CONFLICT (distinct_error_id) DO UPDATE SET
             content_hash = EXCLUDED.content_hash,
             embedding = EXCLUDED.embedding,
             updated_at = EXCLUDED.updated_at""",
        (row["id"], content_hash(gen["expanded_error"]), vec, EMBED_MODEL_ID, now, now),
    )
    return row


async def _cluster_vector(cluster_id: int) -> str | None:
    """The cluster's stored embedding as a pgvector literal — no Bedrock call.

    Cast to text in SQL: asyncpg has no codec for the vector type.
    """
    rows = await read(
        "SELECT embedding::text AS vec FROM distinct_error_embeddings WHERE distinct_error_id = ?",
        (cluster_id,),
    )
    return rows[0]["vec"] if rows else None


# ---------------------------------------------------------------------------
# L3 — the notes RAG
# ---------------------------------------------------------------------------

async def _find_solutions(vectors: list[str]) -> list[dict]:
    """Search each vector, keep the best score per note, cut at the floor."""
    best: dict[tuple, dict] = {}
    for vec in vectors:
        try:
            hits = await search_vector(vec, ERROR_SOLUTION_LIMIT)
        except Exception as e:
            logger.warning("notes vector search failed: %s", e)
            continue
        for hit in hits:
            key = (hit["source"], hit["summary_id"])
            if key not in best or hit["score"] > best[key]["score"]:
                best[key] = hit

    ranked = sorted(
        (h for h in best.values() if h["score"] >= ERROR_SOLUTION_THRESHOLD),
        key=lambda h: h["score"],
        reverse=True,
    )[:ERROR_SOLUTION_LIMIT]
    return await hydrate(ranked, with_blobs=False)


# ---------------------------------------------------------------------------
# Response shaping — the envelope is fixed, nothing may be added to it
# ---------------------------------------------------------------------------

def _slim_cautions(gotchas) -> list[str]:
    out: list[str] = []
    for g in gotchas or []:
        if isinstance(g, dict):
            desc = (g.get("description") or "").strip()
            name = (g.get("name") or "").strip()
            if not desc:
                continue
            out.append(f"{name}: {desc}" if name and name.lower() != "heads up" else desc)
        elif str(g).strip():
            out.append(str(g).strip())
    return out


def _slim_solution(hit: dict) -> dict:
    """problem / fix / cautions only — no note ids, no SAP metadata."""
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


async def _family_name(code: str | None) -> str | None:
    from services.error_families import family_display_name
    return await family_display_name(code)


async def _envelope(
    cluster: dict, confidence: float, solutions: list[dict], fallback: str | None
) -> dict:
    family_code = cluster.get("family_code") or UNCLASSIFIED
    expanded = cluster.get("generalized_text") or ""
    body: dict = {
        "distinct_error": {
            "title": cluster.get("title") or "",
            "generalized_error": expanded,
            "problem": cluster.get("summary") or expanded,
            "family_code": family_code,
            "family_name": await _family_name(family_code) or "",
            "cluster_confidence": confidence,
            "informational": False,
        },
        "solutions": [_slim_solution(s) for s in solutions],
    }
    if fallback:
        body["fallback_solution"] = fallback
    return body


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------

async def _solve(cluster: dict, vectors: list[str], raw: str, source: str | None) -> tuple:
    """L3 plus the fallback. Returns (solutions, fallback_solution)."""
    solutions = await _find_solutions([v for v in vectors if v])
    if solutions:
        return solutions, None
    fallback = await generate_fallback_solution(
        raw,
        expanded=cluster.get("generalized_text") or "",
        title=cluster.get("title") or "",
        family_name=await _family_name(cluster.get("family_code")) or "",
        family_code=cluster.get("family_code") or UNCLASSIFIED,
        source=source,
    )
    return [], fallback


def _solution_source(body: dict | None) -> str:
    if not body:
        return "none"
    if body.get("solutions"):
        return "knowledge_base"
    return "llm_fallback" if body.get("fallback_solution") else "none"


async def _audit(
    *,
    raw: str,
    caller: str | None,
    source: str | None,
    started: float,
    cluster: dict | None = None,
    confidence: float = 0.0,
    created_new: bool = False,
    body: dict | None = None,
    error: BaseException | None = None,
) -> None:
    """The single place an error_events row is written — success and failure both.

    ponytail: `response` stores the whole envelope verbatim, so a busy audit log
    grows by roughly the size of what we returned. Trim to the solution titles if
    the table gets heavy.
    """
    cluster = cluster or {}
    await write(
        """INSERT INTO error_events
           (raw_text, generalized_text, distinct_error_id, family_code,
            error_match_percent, created_new, caller, source,
            status, error_message, solution_source, solution_count, response, duration_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            raw,
            cluster.get("generalized_text"),
            cluster.get("id"),
            cluster.get("family_code"),
            confidence,
            1 if created_new else 0,
            caller,
            source,
            "error" if error else "ok",
            f"{type(error).__name__}: {error}" if error else None,
            "none" if error else _solution_source(body),
            len((body or {}).get("solutions") or []),
            json.dumps(body, ensure_ascii=False) if body else None,
            int((time.monotonic() - started) * 1000),
        ),
    )


async def _run(raw: str, source: str | None) -> tuple[dict, dict, float, bool]:
    """The chain. Returns (body, cluster, confidence, created_new)."""

    fp = _fingerprint(raw)

    # ---- L0: a string we have already seen. No LLM, no embedding, no lock.
    known_id = await _lookup_message(fp)
    if known_id is not None:
        cluster = await _cluster_by_id(known_id)
        if cluster:
            cluster = await _touch_cluster(known_id)
            await _record_message(known_id, fp, raw)
            vec = await _cluster_vector(known_id)
            solutions, fallback = await _solve(cluster, [vec], raw, source)
            body = await _envelope(cluster, 100.0, solutions, fallback)
            return body, cluster, 100.0, False
        logger.warning("error_messages row %s points at a missing cluster", known_id)

    # Everything below needs the LLM. One at a time, and never a queue.
    if _LOCK.locked():
        raise DiagnoseBusy("another diagnose is already running")

    async with _LOCK:
        # ---- L1
        gen = await expand_error(raw)
        expanded = gen["expanded_error"]

        # ---- L2
        emb = await asyncio.to_thread(embed_text, expanded)
        fresh_vec = _vec_literal(emb)
        match = await _search_clusters(fresh_vec)

        if match:
            cluster = await _touch_cluster(match["id"])
            confidence = round(match["similarity"] * 100, 1)
            created_new = False
            # The stored vector searches alongside the fresh one: a cluster's
            # canonical wording and this caller's wording rarely match exactly.
            vectors = [fresh_vec, await _cluster_vector(match["id"])]
        else:
            # ponytail: fingerprint of LLM output, so two runs of the same error
            # can land on different hashes. The vector search above is the real
            # dedup; this only catches byte-identical expansions. Upgrade path is
            # dropping fingerprint_hash once the column has no other readers.
            cluster = await _create_cluster(_fingerprint(expanded), gen, fresh_vec)
            confidence = 0.0
            created_new = True
            vectors = [fresh_vec]

        await _record_message(cluster["id"], fp, raw)

        # ---- L3 + fallback
        solutions, fallback = await _solve(cluster, vectors, raw, source)

        body = await _envelope(cluster, confidence, solutions, fallback)
        return body, cluster, confidence, created_new


async def diagnose(raw_error: str, caller: str | None = None, source: str | None = None) -> dict:
    """Public entry point. Every call lands in error_events, however it ends."""
    started = time.monotonic()
    raw = (raw_error or "").strip()

    try:
        # Inside the try on purpose: a rejected payload is a failed call the
        # audit page has to show, same as a dead LLM or a 409.
        if not raw:
            raise ValueError("error_text is required")
        body, cluster, confidence, created_new = await _run(raw, source)
    except Exception as e:
        # L4 for the failure path: a 409, a dead LLM and a bad payload are all
        # things the audit page has to show, so none of them may skip the log.
        try:
            await _audit(raw=raw or (raw_error or ""), caller=caller,
                         source=source, started=started, error=e)
        except Exception as audit_err:
            logger.error("audit write failed for a failed diagnose: %s", audit_err)
        raise

    await _audit(
        raw=raw, caller=caller, source=source, started=started,
        cluster=cluster, confidence=confidence, created_new=created_new, body=body,
    )
    return body


if __name__ == "__main__":
    # ponytail self-check: hashing, the floor, and the envelope shape.
    assert _fingerprint("a  b\n c") == _fingerprint("a b c")
    assert _fingerprint("HTTP 500") != _fingerprint("HTTP 501")

    assert _slim_cautions([{"name": "Heads up", "description": "x"}]) == ["x"]
    assert _slim_cautions([{"name": "TLS", "description": "x"}]) == ["TLS: x"]
    assert _slim_cautions([{"name": "TLS", "description": ""}]) == []
    assert _slim_cautions(["plain"]) == ["plain"]

    s = _slim_solution({"how_to_fix": "one step", "match_percent": 71})
    assert s["solution"] == ["one step"] and s["match_percent"] == 71

    async def _check_envelope():
        globals()["_family_name"] = lambda code: _done("HTTP failed")
        cluster = {"id": 1, "title": "T", "generalized_text": "E", "summary": "P",
                   "family_code": "HTTP_REQUEST_FAILED"}
        body = await _envelope(cluster, 84.2, [], None)
        assert set(body) == {"distinct_error", "solutions"}, body
        assert set(body["distinct_error"]) == {
            "title", "generalized_error", "problem", "family_code",
            "family_name", "cluster_confidence", "informational",
        }, body["distinct_error"]
        assert body["distinct_error"]["informational"] is False
        body = await _envelope(cluster, 0.0, [], "## ROOT CAUSE")
        assert set(body) == {"distinct_error", "solutions", "fallback_solution"}
        # summary is the problem text; an empty one falls back to the expanded error.
        body = await _envelope({**cluster, "summary": None}, 0.0, [], None)
        assert body["distinct_error"]["problem"] == "E"

    async def _done(v):
        return v

    # The audit row must classify every outcome, including the failures.
    assert _solution_source({"solutions": [{}], "fallback_solution": "x"}) == "knowledge_base"
    assert _solution_source({"solutions": [], "fallback_solution": "x"}) == "llm_fallback"
    assert _solution_source({"solutions": []}) == "none"
    assert _solution_source(None) == "none"

    asyncio.run(_check_envelope())
    print("error_diagnose: ok")
