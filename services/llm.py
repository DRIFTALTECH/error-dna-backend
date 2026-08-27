"""Single LLM entry point — LangChain ChatOpenAI against the DeepSeek-compatible API.

`chat_model()` is the shared factory; every chain builds on it. `chat()` answers a
free-form question grounded in one article (the two /chat endpoints).
"""

from __future__ import annotations

import logging

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL

logger = logging.getLogger(__name__)

_PLACEHOLDER_KEY = "your-deepseek-api-key-here"


def require_key() -> str:
    if not LLM_API_KEY or LLM_API_KEY == _PLACEHOLDER_KEY:
        raise ValueError("LLM_API_KEY is not set in .env")
    return LLM_API_KEY


def chat_model(
    *,
    temperature: float = 0.3,
    max_tokens: int = 4000,
    timeout: float = 120,
    json_mode: bool = False,
) -> ChatOpenAI:
    """Shared ChatOpenAI. json_mode binds response_format so the parser gets clean JSON."""
    model = ChatOpenAI(
        model=LLM_MODEL,
        base_url=LLM_BASE_URL,
        api_key=require_key(),
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        max_retries=2,
    )
    if json_mode:
        model = model.bind(response_format={"type": "json_object"})
    return model


CHAT_SYSTEM = """You are a knowledge-base assistant. Answer the user's question about the
technical article below. Use ONLY the article as your source; if it doesn't cover the
question, say so plainly. Be concise, vendor-neutral, and practical. Plain text, no JSON."""

_chat_chain = (
    ChatPromptTemplate.from_messages([
        ("system", CHAT_SYSTEM),
        ("human", "ARTICLE:\n{context}\n\nQUESTION: {question}"),
    ])
    | chat_model(temperature=0.3, max_tokens=1200, timeout=60)
    | StrOutputParser()
) if (LLM_API_KEY and LLM_API_KEY != _PLACEHOLDER_KEY) else None


async def chat(question: str, context: str) -> str:
    """Answer a question grounded in one article's text. Returns plain text."""
    require_key()
    return (await _chat_chain.ainvoke({
        "context": context[:15000],
        "question": question,
    })).strip()
