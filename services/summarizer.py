"""Summarizer — calls LLM API to generate clean knowledge base entries."""

import json
import logging
import httpx
from config import LLM_API_KEY, LLM_API_URL, LLM_MODEL

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior technical writer for a knowledge base. Your job is to take raw extraction from a technical support article and rewrite it into a clean, vendor-neutral knowledge base entry.

CRITICAL RULES:
1. IGNORE page navigation, breadcrumbs, UI labels, and chrome. Only read the BODY content.
2. The actual article title is in the text — find it and use it (strip leading note numbers like "3780883 - ").
3. The article body starts near "Symptom" or "Description" — everything before that is page chrome, NOT the article.
4. NEVER use the word "SAP", "SAP Note", "KBA", "SAP Knowledge Base Article", or any SAP product branding in the output.
5. NEVER mention version numbers, release dates, or source identifiers.
6. Use general technical language — rewrite SAP-specific terms generically (e.g., "Cloud Integration tenant" → "integration platform").
7. Write like a senior engineer explaining to a peer.
8. Keep the title concise, under 80 characters.
9. Extracting is MORE IMPORTANT than sanitizing — get the content right before stripping branding.

CLASSIFY into ONE of these error families:
"HTTP & Status Codes", "Authentication", "Certificate & TLS", "Connection",
"Groovy & Script", "Mapping & Transformation", "Messaging", "Database",
"Security", "Configuration"

OUTPUT only valid JSON with these keys:
- title: Clean title from the article (strip note numbers and SAP, keep the meaning)
- family: ONE error family from the list above
- area: Same as family
- type: "Problem" / "How To" / "FAQ" / "Configuration" (based on what the article actually is)
- issue: String describing 2-3 sentences of what goes wrong or what this addresses (NOT an array, one string with line breaks)
- summary: Root cause + context explanation in paragraph form. Be thorough — capture the actual technical details.
- steps: JSON array of { "title": "Step name", "details": ["detail 1", "detail 2"] }
- gotchas: JSON array of warning strings like ["Warning 1", "Warning 2"] — real technical gotchas from the article
- tags: JSON array of 5-10 search keywords
- environment: JSON array like ["Cloud Integration", "BTP"]

Output ONLY the JSON. No markdown, no explanation, no code fences."""


def build_user_prompt(raw_text: str) -> str:
    """Build the user prompt with the raw SAP note text."""
    # Truncate if too long (DeepSeek has 128K context, but keep it reasonable)
    max_chars = 15000
    truncated = raw_text[:max_chars]
    if len(raw_text) > max_chars:
        truncated += "\n\n[Text truncated — original was longer]"

    return f"Summarize this technical article:\n\n{truncated}"


def _image_directive(images: list) -> tuple[str, str]:
    """Extra system rule + user block so the (text-only) LLM places image tokens by
    context. Returns (system_extra, user_extra). Empty when there are no images."""
    if not images:
        return "", ""
    sys_extra = (
        "\n\nIMAGES: The article has attached images, listed at the end with their context. "
        "In the `summary` and/or `steps` output, insert the token {image_N} (e.g. {image_1}) "
        "on its OWN line at the single most relevant point for each image, judging from its "
        "context text. Use each token exactly once, never invent tokens, and keep the exact "
        "{image_N} spelling so the app can swap in the real image."
    )
    lines = ["\n\nATTACHED IMAGES (place each token where it best fits):"]
    for im in images:
        ctx = (im.get("context") or im.get("alt") or "").strip()[:300]
        lines.append(f"- {{{im['ref']}}} — context: {ctx or '(no caption)'}")
    return sys_extra, "\n".join(lines)


async def summarize(raw_text: str, images: list = None) -> dict:
    """
    Call the LLM API to generate a clean summary.
    If `images` (list of {ref, context, alt}) is given, the model interleaves
    {image_N} placeholder tokens into summary/steps by textual context.
    Returns structured dict or raises on failure.
    """
    if not LLM_API_KEY or LLM_API_KEY == "your-deepseek-api-key-here":
        raise ValueError("LLM_API_KEY is not set in .env")

    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }

    sys_extra, user_extra = _image_directive(images or [])
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + sys_extra},
            {"role": "user", "content": build_user_prompt(raw_text) + user_extra},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
        "response_format": {"type": "json_object"},
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(LLM_API_URL, json=payload, headers=headers)

        if response.status_code != 200:
            logger.error(f"LLM API error: {response.status_code} — {response.text[:200]}")
            raise ValueError(f"LLM API returned {response.status_code}")

        result = response.json()
        content = result["choices"][0]["message"]["content"]

        # Parse the JSON response
        try:
            summary = json.loads(content)
        except json.JSONDecodeError:
            # Try to extract JSON from markdown fences
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
                summary = json.loads(content)
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
                summary = json.loads(content)
            else:
                raise ValueError(f"Could not parse LLM response as JSON: {content[:200]}")

        # Ensure all required fields exist as strings for DB storage
        steps = summary.get("steps", [])
        gotchas = summary.get("gotchas", [])
        tags = summary.get("tags", [])
        environment = summary.get("environment", [])

        return {
            "title": summary.get("title", ""),
            "family": summary.get("family", ""),
            "area": summary.get("area", summary.get("family", "")),
            "type": summary.get("type", "Problem"),
            "issue": summary.get("issue", ""),
            "summary": summary.get("summary", ""),
            "steps": json.dumps(steps, ensure_ascii=False) if isinstance(steps, list) else str(steps),
            "gotchas": json.dumps(gotchas, ensure_ascii=False) if isinstance(gotchas, list) else str(gotchas),
            "tags": json.dumps(tags, ensure_ascii=False) if isinstance(tags, list) else str(tags),
            "environment": json.dumps(environment, ensure_ascii=False) if isinstance(environment, list) else str(environment),
        }


CHAT_SYSTEM = """You are a knowledge-base assistant. Answer the user's question about the
technical article below. Use ONLY the article as your source; if it doesn't cover the
question, say so plainly. Be concise, vendor-neutral, and practical. Plain text, no JSON."""


async def chat(question: str, context: str) -> str:
    """Answer a free-form question grounded in one article's text. Returns plain text."""
    if not LLM_API_KEY or LLM_API_KEY == "your-deepseek-api-key-here":
        raise ValueError("LLM_API_KEY is not set in .env")

    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": CHAT_SYSTEM},
            {"role": "user", "content": f"ARTICLE:\n{context[:15000]}\n\nQUESTION: {question}"},
        ],
        "temperature": 0.3,
        "max_tokens": 1200,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(LLM_API_URL, json=payload, headers=headers)
        if response.status_code != 200:
            logger.error(f"LLM chat error: {response.status_code} — {response.text[:200]}")
            raise ValueError(f"LLM API returned {response.status_code}")
        return response.json()["choices"][0]["message"]["content"].strip()
