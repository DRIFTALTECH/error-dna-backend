"""Diagnose L1 — one raw error into a retrieval query plus its cluster identity.

`expanded_error` is the only text that gets embedded, and both vector searches
run on it, so it is written for retrieval rather than for reading: the verbatim
codes and adapter names are the discriminative tokens. `error_signature` is the
stable label the API returns as distinct_error.title.

The LLM is the only classifier — there is no pattern matching anywhere in this
path. When it is unreachable the chain degrades to the raw text instead of
failing the request.
"""

from __future__ import annotations

import logging

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel, Field

from config import (
    ERROR_EXPAND_MAX_TOKENS,
    ERROR_EXPAND_TEMPERATURE,
    ERROR_EXPAND_TIMEOUT,
)
from prompts import ERROR_EXPAND_TEMPLATE
from services.error_families import catalog_for_llm, valid_codes
from services.llm import chat_model

logger = logging.getLogger(__name__)

UNCLASSIFIED = "UNCLASSIFIED_ERROR"
SIGNATURE_MAX_CHARS = 80
EXPAND_MAX_INPUT_CHARS = 12000


class ErrorExpand(BaseModel):
    expanded_error: str = Field(
        default="", description="2-4 sentence retrieval query, verbatim codes and names kept"
    )
    error_signature: str = Field(
        default="", description="Short stable label for this distinct error, under 80 chars"
    )
    family_code: str = Field(
        default=UNCLASSIFIED, description="One error family CODE from the catalog"
    )
    problem: str = Field(
        default="", description="One or two plain sentences on what broke and where"
    )


_parser = JsonOutputParser(pydantic_object=ErrorExpand)
_chain = None


def _get_chain():
    """Built on first use, not at import — a missing LLM_API_KEY must not break boot."""
    global _chain
    if _chain is None:
        _chain = (
            PromptTemplate(
                template=ERROR_EXPAND_TEMPLATE,
                input_variables=["error_text", "family_catalog"],
                partial_variables={"format_instructions": _parser.get_format_instructions()},
            )
            | chat_model(
                temperature=ERROR_EXPAND_TEMPERATURE,
                max_tokens=ERROR_EXPAND_MAX_TOKENS,
                timeout=ERROR_EXPAND_TIMEOUT,
                json_mode=True,
            )
            | _parser
        )
    return _chain


def _first_sentence(text: str, limit: int) -> str:
    """Leading line, trimmed to `limit` on a word boundary. No regex."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), text.strip())
    if len(line) <= limit:
        return line
    return line[:limit].rsplit(" ", 1)[0]


def _degraded(text: str) -> dict:
    """No LLM available — the raw error is still a usable retrieval query."""
    return {
        "expanded_error": text,
        "error_signature": _first_sentence(text, SIGNATURE_MAX_CHARS),
        "family_code": UNCLASSIFIED,
        "problem": _first_sentence(text, 200),
    }


async def _normalize(data: dict, text: str) -> dict:
    """Fill blanks from the raw error and reject a family the catalog doesn't have."""
    expanded = (data.get("expanded_error") or "").strip() or text
    signature = (data.get("error_signature") or "").strip() or _first_sentence(
        expanded, SIGNATURE_MAX_CHARS
    )
    code = (data.get("family_code") or "").strip()
    if code not in await valid_codes():
        if code:
            logger.warning("expand: family_code %r not in catalog, using %s", code, UNCLASSIFIED)
        code = UNCLASSIFIED
    return {
        "expanded_error": expanded,
        "error_signature": signature[:SIGNATURE_MAX_CHARS],
        "family_code": code,
        "problem": (data.get("problem") or "").strip() or signature,
    }


async def expand_error(raw_text: str) -> dict:
    """Raw error → {expanded_error, error_signature, family_code, problem}."""
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("error_text is empty")
    text = text[:EXPAND_MAX_INPUT_CHARS]

    try:
        data = await _get_chain().ainvoke({
            "error_text": text,
            "family_catalog": await catalog_for_llm(),
        })
    except Exception as e:
        logger.warning("expand LLM failed, using raw error as the query: %s", e)
        return _degraded(text)

    return await _normalize(data, text)


if __name__ == "__main__":
    import asyncio

    # ponytail self-check: the trimming and the degraded path are the real logic.
    assert _first_sentence("HTTP 500 from receiver", 80) == "HTTP 500 from receiver"
    assert _first_sentence("\n\n  first line  \nsecond", 80) == "first line"
    assert len(_first_sentence("word " * 60, 80)) <= 80
    assert " " not in _first_sentence("a" * 200, 80)  # no boundary → hard cut

    d = _degraded("java.net.SocketTimeoutException: connect timed out")
    assert d["expanded_error"].startswith("java.net")
    assert d["family_code"] == UNCLASSIFIED

    async def _fake_codes():
        return {"HTTP_REQUEST_FAILED", UNCLASSIFIED}

    globals()["valid_codes"] = _fake_codes  # keep the check off the live DB

    async def _check_normalize():
        # An off-catalog code must not reach the response.
        out = await _normalize({"family_code": "MADE_UP_CODE"}, "boom")
        assert out["family_code"] == UNCLASSIFIED, out
        out = await _normalize({"family_code": "HTTP_REQUEST_FAILED"}, "boom")
        assert out["family_code"] == "HTTP_REQUEST_FAILED", out
        # Blank fields fall back to the raw error, never to empty strings.
        out = await _normalize({}, "boom")
        assert out["expanded_error"] == "boom" and out["error_signature"] == "boom"
        assert out["problem"] == "boom"

    asyncio.run(_check_normalize())
    print("error_expand: ok")
