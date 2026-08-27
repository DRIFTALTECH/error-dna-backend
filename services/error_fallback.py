"""Diagnose fallback — the ZHC answer used only when the RAG returns nothing.

The knowledge base always wins: this runs exclusively when zero solution notes
cleared the retrieval floor. Its output is returned to the caller and is never
written back to the knowledge base — an unverified answer must not become a
note that a later diagnose retrieves as if it were sourced.
"""

from __future__ import annotations

import logging

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from config import (
    ERROR_FALLBACK_MAX_TOKENS,
    ERROR_FALLBACK_TEMPERATURE,
    ERROR_FALLBACK_TIMEOUT,
)
from prompts import ERROR_FALLBACK_SYSPROMPT
from services.llm import chat_model

logger = logging.getLogger(__name__)

_USER_TURN = (
    "Analyze the error per your instructions. "
    "Output ONLY the user-facing response (OUTPUT FORMAT section)."
)

_NOT_PROVIDED = "Not provided"
_UNKNOWN_FIELDS = (
    "adapter_type", "sap_version", "runtime", "region",
    "tenant_type", "mpl_trace", "steps_tried", "sap_notes_checked",
)

_chain = None


def _get_chain():
    """Built on first use — a missing LLM_API_KEY must not break boot."""
    global _chain
    if _chain is None:
        _chain = (
            ChatPromptTemplate.from_messages([("system", "{sysprompt}"), ("human", _USER_TURN)])
            | chat_model(
                temperature=ERROR_FALLBACK_TEMPERATURE,
                max_tokens=ERROR_FALLBACK_MAX_TOKENS,
                timeout=ERROR_FALLBACK_TIMEOUT,
            )
            | StrOutputParser()
        )
    return _chain


def _fill(template: str, values: dict[str, str]) -> str:
    out = template
    for key, val in values.items():
        out = out.replace("{" + key + "}", val or _NOT_PROVIDED)
    return out


def _context_block(*, expanded: str, title: str, family_name: str, source: str | None) -> str:
    parts = []
    if title:
        parts.append(f"Cluster title: {title}")
    if expanded and expanded != title:
        parts.append(f"Expanded error: {expanded}")
    if family_name:
        parts.append(f"Error family: {family_name}")
    if source:
        parts.append(f"Caller source tag: {source}")
    return "\n".join(parts) if parts else _NOT_PROVIDED


def _build_sysprompt(
    raw_error: str, expanded: str, title: str, family_name: str, family_code: str, source: str | None
) -> str:
    component = "SAP Integration Suite / Cloud Integration"
    if family_code:
        component = f"{component} ({family_code})"
    values = {
        "error_message": raw_error,
        "context": _context_block(
            expanded=expanded, title=title, family_name=family_name, source=source
        ),
        "component": component,
    }
    values.update({k: _NOT_PROVIDED for k in _UNKNOWN_FIELDS})
    return _fill(ERROR_FALLBACK_SYSPROMPT, values)


async def generate_fallback_solution(
    raw_error: str,
    *,
    expanded: str = "",
    title: str = "",
    family_name: str = "",
    family_code: str = "",
    source: str | None = None,
) -> str | None:
    """ZHC answer as markdown, or None when the LLM is unavailable or fails."""
    sysprompt = _build_sysprompt(raw_error, expanded, title, family_name, family_code, source)
    try:
        return (await _get_chain().ainvoke({"sysprompt": sysprompt})).strip()
    except Exception as e:
        logger.warning("error_fallback unavailable: %s", e)
        return None


if __name__ == "__main__":
    # ponytail self-check: an unfilled {placeholder} would reach the model verbatim.
    p = _build_sysprompt("boom", "expanded boom", "Boom", "HTTP failed", "HTTP_REQUEST_FAILED", "cpi")
    assert "boom" in p and "HTTP_REQUEST_FAILED" in p
    for token in ("{error_message}", "{context}", "{component}", "{mpl_trace}"):
        assert token not in p, token
    assert _context_block(expanded="", title="", family_name="", source=None) == _NOT_PROVIDED
    print("error_fallback: ok")
