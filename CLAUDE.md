# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Python FastAPI backend + MCP server for **Error DNA** — a knowledge base of SAP Integration Suite / Cloud Integration errors. Two jobs: (1) scrape & summarize SAP Notes and SAP Community threads into PostgreSQL with vector embeddings; (2) diagnose a pasted error by matching it to a known error cluster and returning SAP Note fixes.

`README.md` has the full diagnose flow diagram, API field table, and external-API curl examples — read it before touching `services/error_diagnose.py`.

## Commands

```bash
# Setup
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # needs .env — see config.py for every key

# Run
python3 main.py                          # API on :3000  (PORT/HOST from .env)
python3 -m mcp_server                    # MCP streamable-http on :3333/mcp

# Frontend (git submodule, separate repo)
cd error-knowledge-base && npm install && npm run dev   # also: build, lint, format

# Deploy (EC2, run on the box)
sudo bash deploy.sh          # full: system deps, openclaw, python, systemd, Caddy
sudo bash deploy.sh status | test | swap
git pull && sudo systemctl restart error-dna-api error-dna-mcp
journalctl -u error-dna-api -f
```

### Tests

No pytest, no test framework. Verification is `__main__` self-checks inside the modules — run the module to run its check:

```bash
python3 -m mcp_server selftest      # exercises all 4 MCP tool handlers against the live DB
python3 -m services.ingest_chain    # chain wiring: step order, prompt render, abort routing
python3 -m services.community_api   # URL -> topic id, HTML -> text, image URL filter
python3 -m services.auth            # password hash + token round-trip
python3 -m services.error_diagnose  # fingerprinting, solution floor, envelope shape
python3 -m services.error_expand    # family validation + no-LLM degraded path
python3 -m services.error_fallback  # ZHC prompt fills every placeholder
python3 db.py                       # ? -> $N translation
python3 -m services.crypto
python3 -m services.attachments
python3 -m services.image_store
python3 -m services.scraper         # page classifier
python3 -m services.auth hash 'pw'  # generate a password_hash for an app_users INSERT
```

Keep this convention: new non-trivial logic gets one `assert`-based self-check in its own `__main__`, not a new test suite.

## Architecture

### DB layer — read this before writing any SQL

`db.py` exposes exactly three things: `read(sql, params)`, `write(sql, params)`, `init_db()`. Import nothing else from it.

- **Placeholders are `?`, not `$1`.** `_translate()` rewrites `?` → `$N` positionally with `str.split`, plus `datetime('now','localtime')` → an IST `to_char(...)`. This is SQLite-shaped SQL running on Postgres; keep writing `?`. There is no `date(x)` rewrite — write `substr(col, 1, 10)` yourself.
- **Timestamps are `TEXT` in IST** (`Asia/Kolkata`), not `timestamptz`. Python side uses `datetime.now(IST).isoformat()`.
- **No connection pool** — one `asyncpg.connect()` per query, closed in `finally`. Password comes from `DB_PASSWORD` or, if empty, an RDS IAM auth token generated per connect.
- **pgvector has no asyncpg codec** — embeddings are passed as a text literal (`services.embeddings._vec_literal`) and cast in SQL with `?::vector`.
- **Schema lives in `db.py:SCHEMA`** and is applied idempotently on startup, followed by `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migration lines. New columns go there as another `ADD COLUMN IF NOT EXISTS`, not into `SCHEMA` alone (existing tables won't pick it up). `db_schema.sql` is a reference dump, not the source of truth.
- Table is `app_users`, **not** `users` — another app owns `users` in the same database.

### Diagnose chain (`services/error_diagnose.py`)

`POST /api/errors/diagnose` is one flow with four layers. Everything is in
`error_diagnose.py`; don't add a second entry point.

- **L0 exact repeat** — `sha256` of the whitespace-normalized raw text against
  `error_messages.raw_hash`. A string seen before resolves to its cluster with
  no LLM call and no Bedrock call, so **L0 runs before the lock and never blocks**.
- **L1 expand** — `error_expand.py`: LLM returns `{expanded_error,
  error_signature, family_code, problem}`. `expanded_error` is the only text
  that gets embedded, so it is written for retrieval (verbatim codes, adapter
  and interface names) rather than for reading.
- **L2 error VDB** — cosine over `distinct_error_embeddings`. At or above
  `ERROR_CLUSTER_THRESHOLD` the error merges into that cluster and the **stored**
  signature/expanded/family/problem win over the fresh LLM output, so a given
  cluster always answers with the same identity. Below it, a new cluster.
- **L3 notes RAG** — cosine over `summary_embeddings`, floor
  `ERROR_SOLUTION_THRESHOLD`, cap `ERROR_SOLUTION_LIMIT`. On a cluster hit the
  cluster's stored vector searches alongside the fresh one; it is read back with
  `embedding::text` so no second embedding call is needed.
- **L4 persist** — cluster, raw message, embedding, `error_events` audit.

Invariants that are easy to break:
- **The knowledge base always wins.** `error_fallback.py` runs only when L3
  returns zero notes, and its answer is **never written to the KB** — an
  unverified answer must not become a note a later diagnose retrieves.
- **The response envelope is fixed.** `distinct_error` (7 keys) + `solutions` +
  an optional `fallback_solution`. Nothing may be added to it; `_envelope()`
  is the only place it is built and the self-check asserts its shape.
- **Solution search is not filtered by family.** Purely semantic. Families are
  labels for grouping and display.
- **No pattern matching anywhere.** The LLM is the only classifier, validated
  against `valid_codes()`. There is no regex in this repo at all — `error_families`
  keeps a `match_patterns` column for reference, and nothing reads it.
- **One diagnose at a time, and never a queue.** `_LOCK` is a single
  `asyncio.Lock` (correct for one uvicorn worker); a second caller that needs
  the LLM raises `DiagnoseBusy` → 409.

A **cluster** (`distinct_errors` row) is one distinct error pattern; a **family**
(`error_families` row) is a broad category. Many clusters per family.
`error_messages` is the growing log of every raw string that resolved to a
cluster — it is what makes L0 possible.

### Embeddings

Amazon Titan V2 via Bedrock, 1024-dim, sync boto3 wrapped in `asyncio.to_thread`. Two vector tables: `summary_embeddings` (KB notes, keyed by `content_hash` so re-embedding a stale blob is skipped) and `distinct_error_embeddings` (error clusters). `build_blob()` defines the canonical text chunk — changing it invalidates every stored hash.

### MCP server

`mcp_server/` is a second process, not mounted in the FastAPI app. One directory per tool under `mcp_server/tools/<name>/` containing `tool.py` (registration), `handler.py` (logic), `reference.md`. Add a tool by creating that trio and wiring `register` into `mcp_server/tools/__init__.py:register_all`. Handlers are plain async functions — `services/error_diagnose.py` imports `hybrid_search`'s handler directly, so handler signatures are shared API.

Auth is `McpBearerMiddleware` reading the token from the `app_settings` table (set in the UI under Developer → MCP Server), **not** from `.env`.

### Auth — three separate bearer flavors, one verifier

`services/auth.py` is stdlib-only: pbkdf2 hashing + an HMAC-signed compact token (not real JWT). All three kinds mint via `make_token(sub, ttl, kind)` and validate via `require_auth`:
- UI login — `POST /api/auth/login`, 7d TTL. No signup route; add users by INSERT.
- External systems — `POST /api/oauth/token`, client_credentials, 1h TTL, clients in `oauth_clients` (secret stored as pbkdf2 hash only).
- MCP clients — bearer from Developer Settings, checked by MCP middleware.

`main.py` gates routers wholesale with `dependencies=[Depends(require_auth)]`. Open by design: health, auth, oauth token, `/api/community/images/*` and `/api/files/*` (an `<img>`/download can't send a header).

### Ingest — one chain, nothing else

`services/ingest_chain.py` is the **only** path from a queued URL to a stored note.
Seven steps, piped as an LCEL chain, each wrapped by `step()` (trace entry +
`CHAIN_STEP_DELAY_SEC` pause):

1. `fetch_url` — atomic `UPDATE ... status='scraping'` claim; already-summarized → skip
2. `login` — browser: `scraper.ensure_session` reuses a live session, drives the auth wall only if one appears. api: no-op, the endpoint is public
3. `open_page` — browser: `scraper.open_page` verifies the page is up (3.1 wait/re-probe/re-navigate · 3.2 bounced to IdP → one re-auth · 3.3 fail). api: `community_api.fetch_thread`, then skip unanswered threads before any LLM spend
4. `extract` — browser: `scraper.extract_note`. api: render the thread, download images by URL. Blobs persisted here so steps 5–7 never carry bytes
5. `summarize` — `PromptTemplate | ChatOpenAI | JsonOutputParser` against the `NoteSummary` pydantic schema
6. `embed` — `build_blob()` → Titan; a failure here is non-fatal (backfill catches it)
7. `persist` — summary row + embedding row + URL status + run log

Rules that keep it one flow:

- **Notes use the browser; community uses the Khoros public API.** `SOURCES[source]["reader"]` is the only branch. community.sap.com is Cloudflare-fronted and a headless browser on a datacenter IP gets a *managed* challenge it cannot clear — the API answers anonymously and gives more (every message, `is_solution`, `conversation.solved`, image URLs that fetch with a plain GET). Don't add a second ingest path — add a `SOURCES` entry.
- **Never reintroduce a browser login for community.** The login page is the most heavily protected endpoint on that host; navigating to it is what produced the permanent challenge loop.
- **One exit.** Steps raise `ChainAbort(error, status, action)`; `_finish()` is the sole place that writes URL status, the run log, and deletes orphan blobs. Never write those inline in a step.
- **`_requeue_status()` decides burn vs retry.** Environment failures (MFA, expired session, probe/navigate failure, `cloudflare_challenge`, `page_not_reached`) send the URL back to `pending`; content failures mark it `failed`.
- **`BROWSER_LOCK` is held across steps 2–4 only** — acquired in `login`, released at the end of `extract`, so the LLM step doesn't block a credential test-login.
- **The community drain stops after `MAX_CONSECUTIVE_STALLS` requeues.** A requeued URL stays `pending`, so without that guard a blocked browser spins on the same row forever.
- `services/scheduler.py` is **cron only**: when to run, account rotation, stuck-row self-heal. It must never touch a browser or an LLM.

### Bot verification — do not "solve" it

`scraper.is_challenge()` detects Cloudflare interstitials and both steps 2 and 3 **wait** for them (`CHALLENGE_WAITS` × `CHALLENGE_WAIT_SEC`), then fail with `cloudflare_challenge` so the URL requeues. We never click the widget, and nothing in this repo should try to.

Two variants exist and the original code knew only one. The no-JS page says `Just a moment...`; the JS-rendered Turnstile page says `Verify you are human`, carries a Ray ID, and sets its heading to the bare hostname over a ~200–300 char body. That second one reads as a fully rendered page, which is how a challenge used to pass as "Page rendered", reach the LLM as content, and get filed as *"This is a bot verification page"* → `skipped`. Length alone is not a detector — keep `is_challenge()` as the check.

Step 5 emits `error_signatures` (verbatim error strings) and `search_text` (a dense
retrieval paragraph) specifically because diagnose queries are raw error text. Both
feed `build_blob()`. Changing that blob invalidates every stored `content_hash` —
`embed_backfill` walks all latest summaries and re-embeds whatever moved.

### Scraping

`services/scraper.py` drives **OpenClaw** (headless Chrome) for SAP Notes only and
exposes primitives, not workflows: `ensure_session`, `open_page`, `extract_note`,
`is_challenge`, plus `test_login` for the credentials UI. The page state machine (`_probe` → `classify` →
`_act`) is unchanged; only the orchestration moved into the chain.

Community pages do **not** touch the browser — see `services/community_api.py`.

`_clear_session()` wipes every cookie, including Cloudflare's `cf_clearance`.
`ensure_session` claims `_last_account` immediately after clearing, not on success:
setting it on success let a failing account re-clear on every attempt, which made
recovery impossible by construction.

SAP credentials come from the `credentials` table (password Fernet-encrypted via
`services/crypto.py`). Blobs (community images, note attachments) go through
`services/image_store.py`: S3 when `S3_BUCKET` is set (presigned URLs), else local
`data/images/` served via `/api/files/<key>`.

### LLM calls

`services/llm.py` owns the shared `chat_model()` factory (LangChain `ChatOpenAI`
against the DeepSeek-compatible endpoint) and `chat()` for the article Q&A endpoints.
Prompts live in `prompts.py`. The diagnose-side calls (`error_generalize.py`,
`error_fallback.py`, `reclassify_notes.py`) are still raw `httpx` — convert them to
`chat_model()` when that chain gets the same treatment.

### Config

Everything tunable is an env var read once in `config.py` — thresholds, hybrid-search weights, LLM temperatures/timeouts, ports. Add new tunables there with a comment, never inline `os.getenv` in a service. `.env` is gitignored; `config.py` is the de-facto documentation of what it must contain.

Error families are seeded from `data/error_families.csv` on every startup (upsert by `code`). Edit the CSV to change the catalog; the in-process catalog cache means a restart is required.

## Conventions

- `# ponytail:` comments mark deliberate simplifications and name the upgrade path. Respect them — they're decisions, not TODOs. Use the same marker when you take a shortcut on purpose.
- One flow per job. If you find yourself copying a pipeline to handle a variant, parameterize the existing one instead — that is how `community_ingest.py` grew into a 242-line duplicate of the scraper loop before it was deleted.
- `routes/` is thin (parse, call service, shape response); logic lives in `services/`. `routes/compat.py` exists purely to serve frontend-shaped payloads (`/api/families`) — put adapter shims there rather than distorting a service.
- LLM calls go through `services/llm.py:chat_model()` (LangChain). Prompts live in `prompts.py`, never inline in a service.
- Frontend (`error-knowledge-base/`) is a TanStack Start + React 19 + Tailwind v4 submodule with its own git history. It reads `VITE_API_URL`, falling back to same-origin `/api`.
