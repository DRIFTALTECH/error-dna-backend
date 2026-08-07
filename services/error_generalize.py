"""Generalize raw integration errors via LLM — strip volatile ids, produce fingerprint text."""

import json
import logging
import re

import httpx

from config import (
    ERROR_GENERALIZE_MAX_TOKENS,
    ERROR_GENERALIZE_TEMPERATURE,
    ERROR_GENERALIZE_TIMEOUT,
    LLM_API_KEY,
    LLM_API_URL,
    LLM_MODEL,
)
from services.error_families import catalog_for_llm, classify_text, valid_codes

logger = logging.getLogger(__name__)

_SYSTEM_BASE = """You normalize integration error messages for a knowledge base.
Remove user ids, tenant ids, timestamps, correlation ids, GUIDs, IP addresses, and host-specific paths.
Keep the technical failure meaning intact.

Output ONLY valid JSON:
{{
  "title": "descriptive label for the error",
  "generalized_text": "the full error in stable generic form — preserve all technical detail",
  "summary": "full root-cause and context explanation for search",
  "family_code": "CODE_FROM_CATALOG"
}}

Pick family_code from this catalog (exact code):
{family_catalog}

No markdown, no code fences."""


async def generalize_error(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("error_text is empty")

    pattern_code = await classify_text(text)

    if not LLM_API_KEY:
        cleaned = re.sub(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            "<id>",
            text,
            flags=re.I,
        )
        cleaned = re.sub(r"\bS\d{7,10}\b", "<user>", cleaned)
        return {
            "title": cleaned,
            "generalized_text": cleaned,
            "summary": cleaned,
            "family_code": pattern_code,
        }

    catalog = await catalog_for_llm()
    system = _SYSTEM_BASE.format(family_catalog=catalog)

    async with httpx.AsyncClient(timeout=ERROR_GENERALIZE_TIMEOUT) as client:
        resp = await client.post(
            LLM_API_URL,
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"Normalize this error:\n\n{text}"},
                ],
                "temperature": ERROR_GENERALIZE_TEMPERATURE,
                "max_tokens": ERROR_GENERALIZE_MAX_TOKENS,
                "response_format": {"type": "json_object"},
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    data = json.loads(content)
    codes = await valid_codes()
    llm_code = (data.get("family_code") or "").strip()
    if pattern_code != "UNCLASSIFIED_ERROR":
        family_code = pattern_code
    elif llm_code in codes:
        family_code = llm_code
    else:
        family_code = "UNCLASSIFIED_ERROR"

    generalized = data.get("generalized_text") or text
    summary = data.get("summary") or generalized
    return {
        "title": data.get("title") or "Integration error",
        "generalized_text": generalized,
        "summary": summary,
        "family_code": family_code,
    }
