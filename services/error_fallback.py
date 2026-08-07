"""LLM fallback when hybrid_search returns no SAP note matches — ZHC prompt."""

from __future__ import annotations

import logging

import httpx

from config import (
    ERROR_FALLBACK_MAX_TOKENS,
    ERROR_FALLBACK_TEMPERATURE,
    ERROR_FALLBACK_TIMEOUT,
    LLM_API_KEY,
    LLM_API_URL,
    LLM_MODEL,
)
from prompts import ERROR_FALLBACK_SYSPROMPT

logger = logging.getLogger(__name__)


def _fill(template: str, values: dict[str, str]) -> str:
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", val or "Not provided")
    return out


def _context_block(
    *,
    generalized: str,
    title: str,
    family_name: str,
    source: str | None,
) -> str:
    parts = []
    if title:
        parts.append(f"Cluster title: {title}")
    if generalized and generalized != title:
        parts.append(f"Generalized error: {generalized}")
    if family_name:
        parts.append(f"Error family: {family_name}")
    if source:
        parts.append(f"Caller source tag: {source}")
    return "\n".join(parts) if parts else "Not provided"


async def generate_fallback_solution(
    raw_error: str,
    *,
    generalized: str = "",
    title: str = "",
    family_name: str = "",
    family_code: str = "",
    source: str | None = None,
) -> str | None:
    """Run ZHC sysprompt LLM when KB has zero solution notes. Returns markdown or None."""
    if not LLM_API_KEY or LLM_API_KEY == "your-deepseek-api-key-here":
        logger.warning("error_fallback skipped — LLM_API_KEY not configured")
        return None

    component = "SAP Integration Suite / Cloud Integration"
    if family_code:
        component = f"{component} ({family_code})"

    filled = _fill(
        ERROR_FALLBACK_SYSPROMPT,
        {
            "error_message": raw_error,
            "context": _context_block(
                generalized=generalized,
                title=title,
                family_name=family_name or "",
                source=source,
            ),
            "component": component,
            "adapter_type": "Not provided",
            "sap_version": "Not provided",
            "runtime": "Not provided",
            "region": "Not provided",
            "tenant_type": "Not provided",
            "mpl_trace": "Not provided",
            "steps_tried": "Not provided",
            "sap_notes_checked": "Not provided",
        },
    )

    async with httpx.AsyncClient(timeout=ERROR_FALLBACK_TIMEOUT) as client:
        resp = await client.post(
            LLM_API_URL,
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [
                    {"role": "system", "content": filled},
                    {
                        "role": "user",
                        "content": (
                            "Analyze the error per your instructions. "
                            "Output ONLY the user-facing response (OUTPUT FORMAT section)."
                        ),
                    },
                ],
                "temperature": ERROR_FALLBACK_TEMPERATURE,
                "max_tokens": ERROR_FALLBACK_MAX_TOKENS,
            },
        )
        if resp.status_code != 200:
            logger.error("error_fallback LLM %s — %s", resp.status_code, resp.text[:300])
            return None
        return resp.json()["choices"][0]["message"]["content"].strip()
