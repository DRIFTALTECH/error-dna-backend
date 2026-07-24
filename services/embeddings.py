"""Embed summary chunks via Amazon Titan V2 and store in summary_embeddings.

Blob = title + family + issue + summary + tags + gotchas (no images, no raw).
Best-effort: callers should catch failures so a scrape still succeeds.
"""

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta

import boto3

from config import EMBED_DIMENSIONS, EMBED_MODEL_ID, EMBED_REGION
from db import read, write

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))

_bedrock = None


def _client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=EMBED_REGION)
    return _bedrock


def build_blob(row: dict) -> str:
    """Canonical text chunk for one summary row."""
    def field(key: str) -> str:
        v = row.get(key)
        if v is None:
            return ""
        if isinstance(v, (list, dict)):
            return json.dumps(v, ensure_ascii=False)
        return str(v).strip()

    parts = [
        f"TITLE: {field('title')}",
        f"FAMILY: {field('family')}",
        f"ISSUE: {field('issue')}",
        f"SUMMARY: {field('summary')}",
        f"TAGS: {field('tags')}",
        f"GOTCHAS: {field('gotchas')}",
    ]
    return "\n\n".join(parts)


def content_hash(blob: str) -> str:
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def embed_text(text: str) -> list[float]:
    """Call Titan Text Embeddings V2 → list of EMBED_DIMENSIONS floats."""
    body = json.dumps({
        "inputText": text[:50000],  # Titan hard cap ~50k chars
        "dimensions": EMBED_DIMENSIONS,
        "normalize": True,
    })
    resp = _client().invoke_model(
        modelId=EMBED_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=body,
    )
    payload = json.loads(resp["body"].read())
    emb = payload.get("embedding")
    if not emb or len(emb) != EMBED_DIMENSIONS:
        raise ValueError(f"unexpected embedding size: {len(emb) if emb else 0}")
    return [float(x) for x in emb]


def _vec_literal(emb: list[float]) -> str:
    """pgvector text form — asyncpg has no built-in vector codec."""
    return "[" + ",".join(repr(x) for x in emb) + "]"


async def upsert_embedding(
    source: str,
    summary_id: int,
    source_id: str,
    row: dict,
) -> str:
    """Embed + upsert. Returns 'created' | 'updated' | 'skipped'."""
    if source not in ("notes", "community"):
        raise ValueError(f"invalid source: {source}")

    blob = build_blob(row)
    digest = content_hash(blob)
    existing = await read(
        "SELECT content_hash FROM summary_embeddings WHERE source=? AND summary_id=?",
        (source, summary_id),
    )
    if existing and existing[0]["content_hash"] == digest:
        return "skipped"

    emb = await asyncio.to_thread(embed_text, blob)
    now = datetime.now(IST).isoformat()
    vec = _vec_literal(emb)

    await write(
        """INSERT INTO summary_embeddings
           (source, summary_id, source_id, content_hash, embedding, model, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?::vector, ?, ?, ?)
           ON CONFLICT (source, summary_id) DO UPDATE SET
             source_id = EXCLUDED.source_id,
             content_hash = EXCLUDED.content_hash,
             embedding = EXCLUDED.embedding,
             model = EXCLUDED.model,
             updated_at = EXCLUDED.updated_at""",
        (source, summary_id, source_id, digest, vec, EMBED_MODEL_ID, now, now),
    )
    return "updated" if existing else "created"


async def embed_summary_safe(source: str, summary_id: int, source_id: str, row: dict) -> None:
    """Best-effort wrapper — never raises to the scrape pipeline."""
    try:
        action = await upsert_embedding(source, summary_id, source_id, row)
        logger.info(f"embed {source}#{source_id} (id={summary_id}): {action}")
    except Exception as e:
        logger.warning(f"embed {source}#{source_id} failed: {e}")
