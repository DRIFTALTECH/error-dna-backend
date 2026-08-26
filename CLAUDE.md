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
python3 -m services.auth            # password hash + token round-trip
python3 -m services.crypto
python3 -m services.attachments
python3 -m services.image_store
python3 -m services.scraper
python3 -m services.auth hash 'pw'  # generate a password_hash for an app_users INSERT
```

Keep this convention: new non-trivial logic gets one `assert`-based self-check in its own `__main__`, not a new test suite.

## Architecture

### DB layer — read this before writing any SQL

`db.py` exposes exactly three things: `read(sql, params)`, `write(sql, params)`, `init_db()`. Import nothing else from it.

- **Placeholders are `?`, not `$1`.** `_translate()` rewrites `?` → `$N` positionally, plus `datetime('now','localtime')` → an IST `to_char(...)` and `date(x)` → `substr(x,1,10)`. This is SQLite-shaped SQL running on Postgres; keep writing `?`.
- **Timestamps are `TEXT` in IST** (`Asia/Kolkata`), not `timestamptz`. Python side uses `datetime.now(IST).isoformat()`.
- **No connection pool** — one `asyncpg.connect()` per query, closed in `finally`. Password comes from `DB_PASSWORD` or, if empty, an RDS IAM auth token generated per connect.
- **pgvector has no asyncpg codec** — embeddings are passed as a text literal (`services.embeddings._vec_literal`) and cast in SQL with `?::vector`.
- **Schema lives in `db.py:SCHEMA`** and is applied idempotently on startup, followed by `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` migration lines. New columns go there as another `ADD COLUMN IF NOT EXISTS`, not into `SCHEMA` alone (existing tables won't pick it up). `db_schema.sql` is a reference dump, not the source of truth.
- Table is `app_users`, **not** `users` — another app owns `users` in the same database.

### Diagnose chain (`services/error_diagnose.py`)

`POST /api/errors/diagnose` → vector-search `distinct_errors` on raw text → if < `ERROR_MATCH_THRESHOLD`, LLM-generalize (`error_generalize.py`) and search again → still no match, create a new cluster + embedding → then `hybrid_search` for solution notes → zero notes, LLM fallback (`error_fallback.py`, ZHC prompt in `prompts.py`). Every call is audited into `error_events`.

Two invariants that are easy to break:
- **Solution search is not filtered by family.** It's purely semantic + keyword. Families are labels for grouping/display.
- **Informational lines short-circuit.** `family_code == INFORMATIONAL_FAMILY_CODE` (`RUN_DIAGNOSTIC_EVENT`) skips both note search and fallback.

A **cluster** (`distinct_errors` row) is one distinct error pattern; a **family** (`error_families` row) is a broad category. Many clusters per family.

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

### Scraping

`services/scraper.py` drives **OpenClaw** (headless-Chrome agent) to log into `me.sap.com` with a credential from the `credentials` table (password Fernet-encrypted via `services/crypto.py`), scrape a note, then `services/summarizer.py` LLM-summarizes it into the structured shape. `services/scheduler.py` runs the background loop, started in `main.py`'s lifespan — it fails soft if the browser isn't running. Community ingest (`services/community_ingest.py`) is a separate, unscheduled path because the public pages sit behind Cloudflare.

Blobs (community images, note attachments) go through `services/image_store.py`: S3 when `S3_BUCKET` is set (presigned URLs), else local `data/images/` served via `/api/files/<key>`.

### Config

Everything tunable is an env var read once in `config.py` — thresholds, hybrid-search weights, LLM temperatures/timeouts, ports. Add new tunables there with a comment, never inline `os.getenv` in a service. `.env` is gitignored; `config.py` is the de-facto documentation of what it must contain.

Error families are seeded from `data/error_families.csv` on every startup (upsert by `code`). Edit the CSV to change the catalog; the in-process catalog cache means a restart is required.

## Conventions

- `# ponytail:` comments mark deliberate simplifications and name the upgrade path. Respect them — they're decisions, not TODOs. Use the same marker when you take a shortcut on purpose.
- `routes/` is thin (parse, call service, shape response); logic lives in `services/`. `routes/compat.py` exists purely to serve frontend-shaped payloads (`/api/families`) — put adapter shims there rather than distorting a service.
- LLM calls are raw `httpx` to a DeepSeek-compatible chat-completions endpoint (`LLM_API_URL`); prompts live in `prompts.py`.
- Frontend (`error-knowledge-base/`) is a TanStack Start + React 19 + Tailwind v4 submodule with its own git history. It reads `VITE_API_URL`, falling back to same-origin `/api`.
