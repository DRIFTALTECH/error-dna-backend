"""The ingest chain — the single path from a queued URL to a stored, embedded note.

One LCEL chain, seven steps, one failure path:

  1 fetch_url   claim the next pending URL (atomic)
  2 login       open it and clear the auth wall, or reuse the live session
  3 open_page   verify the page is really on screen  (3.1 retry / 3.2 re-auth / 3.3 fail)
  4 extract     pull the article text + attachments/images
  5 summarize   PromptTemplate | ChatOpenAI | JsonOutputParser  → NoteSummary
  6 embed       build the retrieval blob and vectorize it
  7 persist     write summary + embedding + url status + run log

Both sources (SAP notes, SAP Community) run this chain; `source` decides which
tables it reads and writes and whether step 2 needs credentials.

Cron lives outside, in services/scheduler.py. Nothing else drives ingest.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, Field

from config import (
    CHAIN_STEP_DELAY_SEC,
    COMMUNITY_MIN_CHARS,
    COMMUNITY_PAGE_RETRIES,
    COMMUNITY_LOGIN_URL,
    SUMMARIZE_MAX_INPUT_CHARS,
    SUMMARIZE_MAX_TOKENS,
    SUMMARIZE_TEMPERATURE,
    SUMMARIZE_TIMEOUT,
)
from db import read, write
from prompts import SUMMARIZE_IMAGE_RULE, SUMMARIZE_SKIP_RULE, SUMMARIZE_TEMPLATE
from services import scraper
from services.llm import chat_model

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Per-source configuration. The only branching in the chain.
# ---------------------------------------------------------------------------

SOURCES = {
    "notes": {
        "urls_table": "urls",
        "summaries_table": "summaries",
        "log_table": "scrape_log",
        # Sign in on the target URL itself: an already-signed-in run costs no extra load.
        "login_url": None,
        "extractor": "note",
        "blob_col": "attachments",
        "target": "note",        # the Symptom/Resolution classifier must fire
        "page_retries": None,    # CHAIN_PAGE_RETRIES
        "min_chars": 200,
        "allow_skip": False,
    },
    "community": {
        "urls_table": "community_urls",
        "summaries_table": "community_summaries",
        "log_table": "community_scrape_log",
        # Khoros hands off to accounts.sap.com — the same IdP, the same S-user.
        "login_url": COMMUNITY_LOGIN_URL,
        "extractor": "community",
        "blob_col": "images",
        "target": "content",     # a forum thread never matches the note classifier
        "page_retries": COMMUNITY_PAGE_RETRIES,
        "min_chars": COMMUNITY_MIN_CHARS,
        "allow_skip": True,      # community pages are often blogs, not solutions
    },
}


class ChainAbort(Exception):
    """Stop the chain. `status` is what the URL row becomes: failed | skipped | pending."""

    def __init__(self, error: str, status: str = "failed", action: str = "scrape"):
        super().__init__(error)
        self.error = error
        self.status = status
        self.action = action


# Errors that are the environment's fault, not the URL's — requeue instead of burning it.
_REQUEUE_ERRORS = ("mfa_required", "needs_login", "session_expired", "probe_failed",
                   "navigate_failed", "max_steps", "cloudflare_challenge",
                   "page_not_reached")


def _requeue_status(error: str) -> str:
    return "pending" if any(error.startswith(e) for e in _REQUEUE_ERRORS) else "failed"


# ---------------------------------------------------------------------------
# Step 5 output schema — what the LLM must return.
# ---------------------------------------------------------------------------

class Step(BaseModel):
    title: str = Field(description="Short step name")
    details: list[str] = Field(default_factory=list, description="What to actually do")


class NoteSummary(BaseModel):
    is_solution: bool = Field(default=True, description="False only when the page is a blog/announcement with no concrete problem+solution")
    skip_reason: str = Field(default="", description="One sentence, only when is_solution is false")
    title: str = Field(default="", description="Clean article title, under 80 chars, no vendor branding")
    family: str = Field(default="UNCLASSIFIED_ERROR", description="One error family CODE from the catalog")
    type: str = Field(default="Problem", description="Problem | How To | FAQ | Configuration")
    issue: str = Field(default="", description="2-3 sentences on what goes wrong, one string")
    summary: str = Field(default="", description="Root cause plus context, paragraph form")
    steps: list[Step] = Field(default_factory=list, description="Ordered fix steps")
    gotchas: list[str] = Field(default_factory=list, description="Real technical warnings from the article")
    tags: list[str] = Field(default_factory=list, description="5-10 search keywords")
    environment: list[str] = Field(default_factory=list, description='e.g. ["Cloud Integration", "BTP"]')
    error_signatures: list[str] = Field(default_factory=list, description="Verbatim error strings, codes and log lines from the article")
    search_text: str = Field(default="", description="One dense retrieval paragraph phrased the way an engineer describes the failure")


_parser = JsonOutputParser(pydantic_object=NoteSummary)

_summarize_chain = (
    PromptTemplate(
        template=SUMMARIZE_TEMPLATE,
        input_variables=["article", "family_catalog", "extra_rules", "extra_context"],
        partial_variables={"format_instructions": _parser.get_format_instructions()},
    )
    | chat_model(
        temperature=SUMMARIZE_TEMPERATURE,
        max_tokens=SUMMARIZE_MAX_TOKENS,
        timeout=SUMMARIZE_TIMEOUT,
        json_mode=True,
    )
    | _parser
)


# ---------------------------------------------------------------------------
# Step plumbing — trace, inter-step delay, one failure path.
# ---------------------------------------------------------------------------

def _now_hms() -> str:
    return datetime.now(IST).strftime("%H:%M:%S")


def _rec(state: dict, phase: str, status: str, message: str, detail=None) -> None:
    state["trace"].append({"at": _now_hms(), "phase": phase, "status": status,
                           "message": message, "detail": detail})


def _s(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, dict)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


async def _acquire_browser(state: dict) -> None:
    """Hold the single-Chrome lock across steps 2-4 (a test-login must not interleave)."""
    if not state.get("holding_browser"):
        await asyncio.to_thread(scraper.BROWSER_LOCK.acquire)
        state["holding_browser"] = True


def _release_browser(state: dict) -> None:
    if state.pop("holding_browser", False):
        scraper.BROWSER_LOCK.release()


def step(name: str, fn):
    """Wrap one step: announce it, run it, then pause before the next one."""
    async def run(state: dict) -> dict:
        logger.info(f"  ▸ step {name}")
        state = await fn(state)
        if CHAIN_STEP_DELAY_SEC > 0:
            await asyncio.sleep(CHAIN_STEP_DELAY_SEC)
        return state

    return RunnableLambda(run, name=name)


# ---------------------------------------------------------------------------
# Step 1 — find the URL
# ---------------------------------------------------------------------------

async def fetch_url(state: dict) -> dict:
    cfg = state["cfg"]
    # Atomic claim: two API processes must never grab the same row and fight over Chrome.
    claimed = await write(
        f"""UPDATE {cfg['urls_table']} SET status='scraping'
            WHERE id = (
              SELECT id FROM {cfg['urls_table']} WHERE status='pending' ORDER BY id ASC LIMIT 1
            )
            RETURNING *"""
    )
    if not claimed:
        raise ChainAbort("queue_empty", status="none", action="none")

    url = claimed[0]
    state["url"] = url
    _rec(state, "fetch_url", "ok", f"Claimed #{url['source_id']}",
         (url.get("title") or url["source_url"])[:120])

    # Already have a summary for this source id — don't pay for it twice.
    existing = await read(
        f"SELECT id, source_version FROM {cfg['summaries_table']} WHERE source_id=? AND is_latest=1",
        (url["source_id"],),
    )
    if existing and (existing[0]["source_version"] or 0) >= 1:
        raise ChainAbort(f"already have v{existing[0]['source_version']}",
                         status="skipped", action="skip")
    return state


# ---------------------------------------------------------------------------
# Step 2 — login (or reuse the existing session)
# ---------------------------------------------------------------------------

async def login(state: dict) -> dict:
    """Sign in (or reuse a live session) before the page is opened.

    Notes sign in on the target URL itself. Community signs in on the Khoros login
    URL, which redirects straight back when a session already exists — so step 3
    navigates to the thread afterwards.
    """
    cfg, url = state["cfg"], state["url"]
    await _acquire_browser(state)

    cred = state.get("cred")
    username = cred["username"] if cred else None
    password = state.get("password")

    result = await asyncio.to_thread(
        scraper.ensure_session, cfg["login_url"] or url["source_url"], username, password)
    state["trace"].extend(result.get("trace") or [])
    if not result["ok"]:
        raise ChainAbort(result["error"], status=_requeue_status(result["error"]), action="login")

    state["login_mode"] = result["mode"]
    return state


# ---------------------------------------------------------------------------
# Step 3 — navigate + verify (3.1 retry, 3.2 re-auth, 3.3 fail)
# ---------------------------------------------------------------------------

async def open_page(state: dict) -> dict:
    cfg, url = state["cfg"], state["url"]
    cred = state.get("cred")

    result = await asyncio.to_thread(
        scraper.open_page,
        url["source_url"],
        cred["username"] if cred else None,
        state.get("password"),
        cfg["page_retries"],
        cfg["target"],
        bool(cfg["login_url"]),   # login landed elsewhere → navigate here
        cfg["min_chars"],
    )
    state["trace"].extend(result.get("trace") or [])
    if not result["ok"]:
        raise ChainAbort(result["error"], status=_requeue_status(result["error"]), action="open")
    return state


# ---------------------------------------------------------------------------
# Step 4 — extract
# ---------------------------------------------------------------------------

async def extract(state: dict) -> dict:
    cfg = state["cfg"]
    extractor = getattr(scraper, f"extract_{cfg['extractor']}")
    result = await asyncio.to_thread(extractor)
    state["trace"].extend(result.get("trace") or [])
    if not result["ok"]:
        raise ChainAbort(result["error"], status="failed", action="extract")

    state["article"] = result.get("clean_text") or result.get("raw_text") or ""
    state["scraped_title"] = result.get("title") or ""

    # Persist blobs now so the LLM step never carries bytes. Orphans are cleaned on abort.
    from services.image_store import save as blob_save

    if cfg["blob_col"] == "attachments":
        manifest = []
        for a in result.get("attachments") or []:
            try:
                key = await asyncio.to_thread(blob_save, a["data"], a.get("ext", "bin"), "doc")
                manifest.append({"name": a["name"], "key": key, "ext": a.get("ext", "")})
            except Exception as e:
                logger.warning(f"  ⚠️ attachment save failed ({a.get('name')}): {e}")
        state["attachments"] = manifest
        if manifest:
            _rec(state, "attachments", "ok", f"Saved {len(manifest)} attachment(s)",
                 ", ".join(a["name"] for a in manifest))
    else:
        briefs, images = [], {}
        for im in result.get("images") or []:
            b64 = im.pop("data_b64", None) or ""
            if not b64:
                continue
            try:
                key = await asyncio.to_thread(blob_save, base64.b64decode(b64), im.get("ext", "png"))
            except Exception as e:
                logger.warning(f"  ⚠️ community image save failed: {e}")
                continue
            finally:
                del b64
            briefs.append({"ref": im["ref"], "context": im.get("context", ""), "alt": im.get("alt", "")})
            images[im["ref"]] = {"key": key, "alt": im.get("alt", "")}
        state["image_briefs"] = briefs
        state["images"] = images
        if images:
            _rec(state, "images", "ok", f"Saved {len(images)} image(s)", ", ".join(images))

    result.clear()
    _release_browser(state)   # browser work is done — steps 5-7 are LLM + DB
    return state


def _blob_keys(state: dict) -> list[str]:
    """Every blob key this run stored — deleted if the run never persists a summary."""
    keys = [a["key"] for a in state.get("attachments") or []]
    keys += [m["key"] for m in (state.get("images") or {}).values()]
    return keys


# ---------------------------------------------------------------------------
# Step 5 — LLM (LangChain: prompt | model | json parser)
# ---------------------------------------------------------------------------

def _image_context(briefs: list) -> str:
    if not briefs:
        return ""
    lines = ["", "ATTACHED IMAGES (place each token where it best fits):"]
    for im in briefs:
        ctx = (im.get("context") or im.get("alt") or "").strip()[:300]
        lines.append(f"- {{{im['ref']}}} — context: {ctx or '(no caption)'}")
    return "\n".join(lines)


async def summarize(state: dict) -> dict:
    from services.error_families import catalog_for_llm

    cfg = state["cfg"]
    briefs = state.get("image_briefs") or []

    extra_rules = ""
    if cfg["allow_skip"]:
        extra_rules += SUMMARIZE_SKIP_RULE
    if briefs:
        extra_rules += SUMMARIZE_IMAGE_RULE

    article = state["article"][:SUMMARIZE_MAX_INPUT_CHARS]
    if len(state["article"]) > SUMMARIZE_MAX_INPUT_CHARS:
        article += "\n\n[Text truncated — original was longer]"

    _rec(state, "summarize", "info", "Sending article to LLM", f"{len(article)} chars")
    try:
        data = await _summarize_chain.ainvoke({
            "article": article,
            "family_catalog": await catalog_for_llm(),
            "extra_rules": extra_rules,
            "extra_context": _image_context(briefs),
        })
    except Exception as e:
        _rec(state, "summarize", "error", "LLM summarization failed", str(e))
        raise ChainAbort(f"LLM:{e}", status="failed", action="summarize")
    finally:
        state["article"] = ""

    if data.get("is_solution") is False:
        reason = data.get("skip_reason") or "Not a problem/solution (e.g. a blog post)"
        _rec(state, "skip", "info", "Skipped — not a solution", reason)
        raise ChainAbort(reason, status="skipped", action="skip")

    data["family"] = await _resolve_family(data)
    state["summary"] = data
    _rec(state, "summarize", "ok", f"LLM produced: {(data.get('title') or '')[:60]}",
         f"{data['family']} / {data.get('type', '')}")
    return state


async def _resolve_family(data: dict) -> str:
    """LLM family → a real catalog code. The LLM is the only classifier.

    It may answer with a display name instead of a code, so try that mapping
    before giving up; an unmappable answer is left unclassified rather than
    guessed at.
    """
    from services.error_families import code_from_family_field, valid_codes

    fam = (data.get("family") or "").strip()
    if fam in await valid_codes():
        return fam
    return await code_from_family_field(fam) or "UNCLASSIFIED_ERROR"


# ---------------------------------------------------------------------------
# Step 6 — build the retrieval chunk and embed it
# ---------------------------------------------------------------------------

async def embed(state: dict) -> dict:
    from services.embeddings import build_blob, content_hash, embed_text

    summary = state["summary"]
    row = _embed_row(summary)
    blob = build_blob(row)
    try:
        state["embedding"] = await asyncio.to_thread(embed_text, blob)
        state["content_hash"] = content_hash(blob)
        _rec(state, "embed", "ok", "Embedding generated", f"{len(blob)} chars")
    except Exception as e:
        # Not fatal — the note is still worth storing; embed_backfill can pick it up.
        logger.warning(f"  ⚠️ embed failed: {e}")
        state["embedding"] = None
        _rec(state, "embed", "error", "Embedding not generated", str(e))
    return state


def _embed_row(summary: dict) -> dict:
    return {
        "title": _s(summary.get("title")),
        "family": _s(summary.get("family")),
        "issue": _s(summary.get("issue")),
        "summary": _s(summary.get("summary")),
        "tags": _s(summary.get("tags")),
        "gotchas": _s(summary.get("gotchas")),
        "error_signatures": _s(summary.get("error_signatures")),
        "search_text": _s(summary.get("search_text")),
    }


# ---------------------------------------------------------------------------
# Step 7 — persist (summary row, embedding row, url status, run log)
# ---------------------------------------------------------------------------

async def persist(state: dict) -> dict:
    from config import EMBED_MODEL_ID
    from services.embeddings import _vec_literal

    cfg, url, summary = state["cfg"], state["url"], state["summary"]
    now = datetime.now(IST).isoformat()
    title = _s(summary.get("title")) or state.get("scraped_title") or url.get("title") or "Untitled"
    family = _s(summary.get("family"))

    # Community: keep only the images the model actually placed, drop the rest.
    images = state.get("images") or {}
    if images:
        placed = _s(summary.get("summary")) + " " + _s(summary.get("steps"))
        from services.image_store import delete as blob_delete
        kept = {}
        for ref, meta in images.items():
            if "{" + ref + "}" in placed:
                kept[ref] = meta
            else:
                blob_delete(meta["key"])
        images = kept
        state["images"] = kept

    blob_col = cfg["blob_col"]
    blob_val = _s(state.get("attachments") or []) if blob_col == "attachments" else _s(images)

    inserted = await write(
        f"""INSERT INTO {cfg['summaries_table']}
            (source_id, url_id, title, family, area, type, issue, summary, steps, gotchas, tags,
             error_signatures, search_text, source_version, source_date, source_url, component,
             environment, {blob_col}, is_latest, verification_status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'current',?,?) RETURNING id""",
        (url["source_id"], url["id"], title, family, family, _s(summary.get("type")),
         _s(summary.get("issue")), _s(summary.get("summary")), _s(summary.get("steps")),
         _s(summary.get("gotchas")), _s(summary.get("tags")),
         _s(summary.get("error_signatures")), _s(summary.get("search_text")),
         1, url.get("released_on"), url["source_url"], url.get("component"),
         _s(summary.get("environment") or []), blob_val, now, now),
    )
    summary_id = inserted[0]["id"]
    state["summary_id"] = summary_id
    await write(f"UPDATE {cfg['urls_table']} SET status='completed', scraped_at=? WHERE id=?",
                (now, url["id"]))
    _rec(state, "store", "ok", "Stored in knowledge base", f"{cfg['summaries_table']}#{summary_id}")

    if state.get("embedding"):
        await write(
            """INSERT INTO summary_embeddings
               (source, summary_id, source_id, content_hash, embedding, model, created_at, updated_at)
               VALUES (?,?,?,?,?::vector,?,?,?)
               ON CONFLICT (source, summary_id) DO UPDATE SET
                 content_hash = EXCLUDED.content_hash,
                 embedding = EXCLUDED.embedding,
                 model = EXCLUDED.model,
                 updated_at = EXCLUDED.updated_at""",
            (state["source"], summary_id, url["source_id"], state["content_hash"],
             _vec_literal(state["embedding"]), EMBED_MODEL_ID, now, now),
        )
        _rec(state, "store_embedding", "ok", "Embedding saved to vector store",
             f"model={EMBED_MODEL_ID}")

    _rec(state, "done", "ok", "Run completed successfully")
    return state


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------

CHAIN = (
    step("1_fetch_url", fetch_url)
    | step("2_login", login)
    | step("3_open_page", open_page)
    | step("4_extract", extract)
    | step("5_summarize", summarize)
    | step("6_embed", embed)
    | step("7_persist", persist)
)


async def _finish(state: dict, status: str, action: str, error: str | None) -> None:
    """The single exit — url row, run log, orphan blobs. Every path lands here."""
    cfg, url = state["cfg"], state.get("url")
    if url and status != "none":
        await write(
            f"UPDATE {cfg['urls_table']} SET status=?, error_message=? WHERE id=?",
            (status, error, url["id"]),
        )
    if error and not state.get("summary_id"):
        from services.image_store import delete as blob_delete
        for key in _blob_keys(state):
            blob_delete(key)
    if not url:
        return

    log_status = {"completed": "success", "skipped": "skipped", "pending": "requeued"}.get(status, "failed")
    cols = "url_id, source_id, status, action, duration_ms, error_message, trace"
    params = [url["id"], url["source_id"], log_status, action,
              int((time.time() - state["t0"]) * 1000), error, json.dumps(state["trace"])]
    cols += ", account_label"
    params.append(state.get("account_label") or None)
    placeholders = ",".join("?" * len(params))
    await write(f"INSERT INTO {cfg['log_table']}({cols}) VALUES({placeholders})", tuple(params))


async def run(source: str = "notes") -> dict:
    """Run the chain once for the next pending URL. Returns {status, source_id, error}."""
    cfg = SOURCES[source]
    state = {"source": source, "cfg": cfg, "trace": [], "t0": time.time(),
             "attachments": [], "images": {}, "image_briefs": []}
    _rec(state, "queued", "info", "Picked up by the ingest chain")

    # Every source signs in with the same SAP credential — notes on the note URL,
    # community on the Khoros login URL that hands off to the same IdP.
    creds = await read("SELECT * FROM credentials WHERE is_active=1 LIMIT 1") \
        or await read("SELECT * FROM credentials LIMIT 1")
    cred = creds[0] if creds else None
    state["cred"] = cred
    state["account_label"] = (cred.get("label") if cred else None) or ""
    if cred:
        from services.crypto import decrypt
        state["password"] = decrypt(cred["password"])
    _rec(state, "account", "ok" if cred else "warn",
         f"Assigned {state['account_label']}" if cred else "No credential configured")

    try:
        state = await CHAIN.ainvoke(state)
    except ChainAbort as abort:
        if abort.status == "none":
            logger.info(f"{source}: queue empty")
            return {"status": "empty", "source_id": None, "error": None}
        await _finish(state, abort.status, abort.action, abort.error)
        logger.info(f"{source} #{state['url']['source_id']}: {abort.status} — {abort.error}")
        return {"status": abort.status, "source_id": state["url"]["source_id"], "error": abort.error}
    except Exception as e:
        logger.exception(f"{source}: chain crashed")
        _rec(state, "done", "error", "Chain crashed", str(e))
        await _finish(state, "failed", "chain", str(e))
        return {"status": "failed",
                "source_id": (state.get("url") or {}).get("source_id"), "error": str(e)}
    finally:
        _release_browser(state)

    await _finish(state, "completed", "create", None)
    logger.info(f"{source} #{state['url']['source_id']} saved: {state['summary'].get('title', '')[:60]}")
    return {"status": "completed", "source_id": state["url"]["source_id"], "error": None}


# ---------------------------------------------------------------------------
# Community drain — the same chain, one URL at a time.
# ---------------------------------------------------------------------------

_draining = False
_current: str | None = None


def is_draining() -> bool:
    return _draining


def current() -> str | None:
    return _current


# A blocked browser fails every URL the same way, so a drain that keeps requeueing
# is not making progress — stop and let the operator see why.
MAX_CONSECUTIVE_STALLS = 3


async def _drain() -> None:
    """Run the chain over pending community_urls until the queue is empty.

    Loads one row at a time and pauses between them so Chrome can reclaim RAM.
    Bails out after MAX_CONSECUTIVE_STALLS requeues in a row: a requeued URL stays
    pending, so without this a Cloudflare block would spin on the same row forever.
    """
    global _draining, _current
    from config import COMMUNITY_INTER_ITEM_SLEEP_SEC

    processed, stalls = 0, 0
    try:
        while True:
            result = await run("community")
            if result["status"] == "empty":
                break

            if result["status"] == "pending":
                stalls += 1
                logger.warning("community #%s requeued (%s) — stall %d/%d",
                               result["source_id"], result["error"], stalls,
                               MAX_CONSECUTIVE_STALLS)
                if stalls >= MAX_CONSECUTIVE_STALLS:
                    logger.error("community drain stopping: %d requeues in a row (%s). "
                                 "The browser is not reaching the site — check the "
                                 "SAP session and whether Cloudflare is challenging "
                                 "this host.", stalls, result["error"])
                    break
            else:
                stalls = 0

            processed += 1
            _current = result["source_id"]
            more = await read("SELECT 1 AS ok FROM community_urls WHERE status='pending' LIMIT 1")
            if not more:
                break
            if COMMUNITY_INTER_ITEM_SLEEP_SEC > 0:
                logger.info(f"community drain: {processed} done — sleeping "
                            f"{COMMUNITY_INTER_ITEM_SLEEP_SEC:.0f}s")
                await asyncio.sleep(COMMUNITY_INTER_ITEM_SLEEP_SEC)
    except Exception:
        logger.exception("community drain crashed")
    finally:
        _draining = False
        _current = None
        logger.info(f"community drain finished ({processed} processed)")


def start_drain() -> bool:
    """Kick a background drain. False if one is already running."""
    global _draining
    if _draining:
        return False
    _draining = True
    asyncio.create_task(_drain())
    return True


if __name__ == "__main__":
    # ponytail: check the pieces with real branching — no DB, no browser, no LLM.
    assert _requeue_status("mfa_required") == "pending"
    assert _requeue_status("session_expired") == "pending"
    assert _requeue_status("too_short") == "failed"
    assert _requeue_status("LLM:boom") == "failed"

    assert _s(None) == ""
    assert _s(["a", "b"]) == '["a", "b"]'
    assert _s(7) == "7"

    st = {"attachments": [{"key": "doc/a"}], "images": {"image_1": {"key": "img/b"}}}
    assert _blob_keys(st) == ["doc/a", "img/b"]

    assert _image_context([]) == ""
    ctx = _image_context([{"ref": "image_1", "context": "the error dialog"}])
    assert "{image_1}" in ctx and "the error dialog" in ctx

    # The prompt must render with every variable the chain supplies.
    rendered = PromptTemplate(
        template=SUMMARIZE_TEMPLATE,
        input_variables=["article", "family_catalog", "extra_rules", "extra_context"],
        partial_variables={"format_instructions": "<schema>"},
    ).format(article="ARTICLE_BODY", family_catalog="- X: Y", extra_rules=SUMMARIZE_SKIP_RULE,
             extra_context="")
    assert "ARTICLE_BODY" in rendered and "<schema>" in rendered and "NOT-A-SOLUTION" in rendered

    # Both sources must name real tables and a real extractor.
    for name, cfg in SOURCES.items():
        assert cfg["urls_table"] and cfg["summaries_table"] and cfg["log_table"]
        assert hasattr(scraper, f"extract_{cfg['extractor']}")
        assert cfg["blob_col"] in ("attachments", "images")
        assert cfg["target"] in ("note", "content")

    # The chain must be all seven steps, in order.
    assert [s.name for s in CHAIN.steps] == [
        "1_fetch_url", "2_login", "3_open_page", "4_extract",
        "5_summarize", "6_embed", "7_persist",
    ]

    # ---- full run with every IO boundary stubbed -------------------------
    # Catches the wiring bugs a live run would find the expensive way: step order,
    # state keys, SQL placeholder/param counts, and the single exit path.
    import services.embeddings as _emb
    import services.error_families as _fam

    sql_log: list[tuple[str, tuple]] = []

    async def fake_read(sql, params=None):
        if "source_version" in sql:
            return []                       # not summarized yet
        if "credentials" in sql:
            return [{"id": 1, "label": "S001", "username": "s001", "password": "enc"}]
        return []

    async def fake_write(sql, params=None):
        sql_log.append((sql, tuple(params or ())))
        placeholders = sql.count("?")
        assert placeholders == len(params or ()), (
            f"{placeholders} placeholders vs {len(params or ())} params in: {sql[:90]}")
        if "SET status='scraping'" in sql:
            return [{"id": 7, "source_id": "3780883", "source_url": "https://me.sap.com/notes/3780883",
                     "title": "t", "released_on": None, "component": None}]
        if "RETURNING id" in sql:
            return [{"id": 42}]
        return []

    SUMMARY = {"is_solution": True, "title": "T", "family": "HTTP_REQUEST_FAILED", "type": "Problem",
               "issue": "i", "summary": "s", "steps": [{"title": "a", "details": ["b"]}],
               "gotchas": ["g"], "tags": ["t"], "environment": ["Cloud Integration"],
               "error_signatures": ["HTTP 500"], "search_text": "dense text"}

    class FakeChain:
        async def ainvoke(self, _payload):
            return dict(SUMMARY)

    globals()["read"] = fake_read
    globals()["write"] = fake_write
    globals()["_summarize_chain"] = FakeChain()
    scraper.BROWSER_LOCK = __import__("threading").Lock()
    scraper.ensure_session = lambda u, n=None, p=None: {"ok": True, "state": "target", "mode": "reused_session", "error": "", "trace": []}
    scraper.open_page = lambda u, n=None, p=None, r=None, t="note", nav=False, mc=200: {
        "ok": True, "state": "target", "error": "", "trace": []}
    scraper.extract_note = lambda: {"ok": True, "raw_text": "raw", "clean_text": "clean",
                                    "title": "T", "attachments": [], "error": "", "trace": []}
    _emb.embed_text = lambda _blob: [0.1] * 1024
    _fam.valid_codes = lambda: asyncio.sleep(0, {"HTTP_REQUEST_FAILED", "UNCLASSIFIED_ERROR"})
    _fam.catalog_for_llm = lambda *a, **k: asyncio.sleep(0, "- HTTP_REQUEST_FAILED: x")
    import services.crypto as _crypto
    _crypto.decrypt = lambda _v: "pw"

    globals()["CHAIN_STEP_DELAY_SEC"] = 0
    result = asyncio.run(run("notes"))
    assert result == {"status": "completed", "source_id": "3780883", "error": None}, result

    written = " || ".join(sql for sql, _ in sql_log)
    assert "INSERT INTO summaries" in written
    assert "error_signatures" in written and "search_text" in written
    assert "INSERT INTO summary_embeddings" in written
    assert "INSERT INTO scrape_log" in written
    assert "status='completed'" in written
    # The chain must never leave the browser lock held.
    assert scraper.BROWSER_LOCK.acquire(blocking=False)
    scraper.BROWSER_LOCK.release()

    # Community: same login, different tables, images instead of attachments.
    scraper.extract_community = lambda: {"ok": True, "raw_text": "raw", "clean_text": "clean",
                                         "title": "C", "images": [], "error": "", "trace": []}
    scraper._navigate = lambda u, t=30: (True, "")
    sql_log.clear()
    result = asyncio.run(run("community"))
    assert result["status"] == "completed", result
    written = " || ".join(sql for sql, _ in sql_log)
    assert "INSERT INTO community_summaries" in written and "images" in written
    assert "attachments" not in written
    assert "INSERT INTO community_scrape_log" in written

    # A blog must be skipped, not stored.
    class SkipChain:
        async def ainvoke(self, _payload):
            return {"is_solution": False, "skip_reason": "This is a blog post"}

    globals()["_summarize_chain"] = SkipChain()
    sql_log.clear()
    result = asyncio.run(run("community"))
    assert result["status"] == "skipped" and result["error"] == "This is a blog post", result
    written = " || ".join(sql for sql, _ in sql_log)
    assert "INSERT INTO community_summaries" not in written
    assert "status=?" in written  # url row marked skipped through the one exit

    # A Cloudflare challenge must requeue the URL, never store or "skip" it.
    globals()["_summarize_chain"] = FakeChain()
    scraper.open_page = lambda u, n=None, p=None, r=None, t="note", nav=False, mc=200: {
        "ok": False, "state": "challenge", "error": "cloudflare_challenge", "trace": []}
    sql_log.clear()
    result = asyncio.run(run("community"))
    assert result["status"] == "pending", result          # requeued, not burned
    assert result["error"] == "cloudflare_challenge", result
    written = " || ".join(sql for sql, _ in sql_log)
    assert "INSERT INTO community_summaries" not in written
    assert "requeued" in json.dumps([p for _, p in sql_log])

    # Both sources must sign in — a run without credentials still reaches step 2.
    assert SOURCES["notes"]["login_url"] is None          # signs in on the note URL
    assert SOURCES["community"]["login_url"].startswith("https://community.sap.com")

    # A drain whose every run requeues must STOP, not spin on the same pending row.
    import config as _cfg
    _cfg.COMMUNITY_INTER_ITEM_SLEEP_SEC = 0
    calls = {"n": 0}

    async def always_stalls(_source="community"):
        calls["n"] += 1
        assert calls["n"] < 20, "drain never stopped — it would spin forever in prod"
        return {"status": "pending", "source_id": "999", "error": "cloudflare_challenge"}

    async def always_more(_sql, _params=None):
        return [{"ok": 1}]

    globals()["run"] = always_stalls
    globals()["read"] = always_more
    globals()["_draining"] = True
    asyncio.run(_drain())
    assert calls["n"] == MAX_CONSECUTIVE_STALLS, f"stopped after {calls['n']} runs"
    assert _draining is False

    print("✅ ingest_chain self-check passed "
          "(stubbed runs: notes, community, blog-skip, cloudflare requeue, stall guard)")
