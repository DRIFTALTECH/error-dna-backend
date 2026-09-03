# Error DNA Backend

Error DNA is a knowledge base for **SAP Integration Suite / Cloud Integration** errors. It collects SAP Notes and Community posts, turns them into structured fixes, and when you paste a new error it finds the closest known problem and returns step-by-step solutions.

Production API: `https://16.113.9.182.sslip.io`

---

## What this repo does


There are two main jobs:

1. **Build the knowledge base** — scrape SAP Notes & Community threads, summarize them with an LLM, store them in PostgreSQL with vector embeddings.
2. **Diagnose new errors** — when someone sends an error message, match it to a known error cluster, search for relevant SAP Note fixes, and if nothing matches, generate a safe fallback answer.

The web UI lives in the `error-knowledge-base/` submodule (React). This repo is the **Python FastAPI backend** + **MCP server**.

---

## The diagnose flow (simple walkthrough)

When you call `POST /api/errors/diagnose` with an error message:

```
You send error text
        │
        ▼
┌───────────────────────────────────────┐
│ L0. Have we seen this exact string?   │  Hash lookup against every raw error
│                                       │  ever received. A repeat needs no AI
│                                       │  call at all — it answers in ~80ms.
└───────────────────────────────────────┘
        │
        ├── Seen it ──► jump straight to L3 with the known error cluster
        │
        ▼ New string
┌───────────────────────────────────────┐
│ L1. LLM expands the error             │  Produces a retrieval query that keeps
│                                       │  every code, adapter and path verbatim,
│                                       │  plus a short signature, a family, and
│                                       │  a plain-language "what broke".
└───────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────┐
│ L2. Search known error clusters       │  Vector similarity on the expanded text.
└───────────────────────────────────────┘
        │
        ├── Match ≥ 75%? ──► use that cluster (its stored wording wins)
        │
        ▼ No match
┌───────────────────────────────────────┐
│     Create a new cluster              │  New row + embedding.
└───────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────┐
│ L3. Search the knowledge base         │  Vector similarity over every stored
│                                       │  note. On a cluster match the cluster's
│                                       │  own vector searches too. Floor 50%,
│                                       │  up to 10 notes.
└───────────────────────────────────────┘
        │
        ├── Found notes? ──► return them. The knowledge base always wins.
        │
        ▼ Zero notes
┌───────────────────────────────────────┐
│     LLM fallback (ZHC prompt)         │  Zero-Hallucination assistant writes
│                                       │  ROOT CAUSE + STEPS TO FIX. This answer
│                                       │  is returned but never saved.
└───────────────────────────────────────┘
        │
        ▼
   JSON response back to you
```

### Key ideas

- **Repeats are free.** The same error string twice costs one hash lookup and one
  vector query — no LLM call, no embedding call.
- **Cluster ≠ Family.** A cluster is one distinct error pattern. A family is a broad
  label (e.g. `HTTP_REQUEST_FAILED`). Many clusters share a family.
- **A cluster's identity is stable.** Once an error merges into a cluster, the
  response uses that cluster's stored title, wording and family — the same error
  never comes back under a different name.
- **Solutions are semantic.** Note search is **not** filtered by family. Pure
  vector similarity; there is no keyword or pattern matching anywhere.
- **The knowledge base always wins.** The LLM fallback runs only when zero notes
  clear the 50% floor, and its answer never enters the knowledge base.
- **One at a time.** A second diagnose that needs the LLM gets `409` rather than
  being queued. Retry it.

---

## API response fields

```json
{
  "distinct_error": {
    "title": "", "generalized_error": "", "problem": "",
    "family_code": "", "family_name": "",
    "cluster_confidence": 0.0, "informational": false
  },
  "solutions": [],
  "fallback_solution": "..."
}
```

| Field | Meaning |
|---|---|
| `distinct_error.title` | The distinct error — a short stable label for this failure |
| `distinct_error.generalized_error` | The expanded retrieval text for this cluster |
| `distinct_error.problem` | Plain-language what broke and where |
| `distinct_error.family_code` / `family_name` | Broad error category |
| `distinct_error.cluster_confidence` | `100.0` exact repeat · similarity % on a cluster match · `0.0` for a brand-new cluster |
| `distinct_error.informational` | Always `false` |
| `solutions[]` | Up to 10 knowledge base fixes: `title`, `problem`, `whats_wrong`, `solution[]`, `cautions[]`, `match_percent`, `images` (`{image_N: {url, alt}}` for community; `{}` for notes — swap `{image_N}` tokens in the text) |
| `fallback_solution` | LLM markdown answer. **Present only when `solutions` is empty** |

| Status | When |
|---|---|
| `400` | `error_text` was whitespace only |
| `401` | Missing, malformed or expired Bearer token |
| `409` | Another diagnose is running — trigger again |
| `422` | `error_text` missing or empty |

---

## External API usage

### Step 1 — Get a token

```bash
curl -s -X POST "https://16.113.9.182.sslip.io/api/oauth/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -u "YOUR_CLIENT_ID:YOUR_CLIENT_SECRET" \
  -d "grant_type=client_credentials"
```

Create OAuth clients in the UI under **Developer → OAuth Clients**. Token lasts 1 hour.

### Step 2 — Diagnose an error

```bash
curl -s -X POST "https://16.113.9.182.sslip.io/api/errors/diagnose" \
  -H "Authorization: Bearer YOUR_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "error_text": "HTTP request failed with status code 500",
    "source": "my-integration-system"
  }'
```

| Header | Value |
|---|---|
| `Authorization` | `Bearer <access_token>` |
| `Content-Type` | `application/json` |

| Body field | Required | Description |
|---|---|---|
| `error_text` | Yes | Full error message — do not truncate |
| `source` | No | Label for your system (stored in activity log) |

---

## Knowledge base flow — the ingest chain

One chain, eight steps, in `services/ingest_chain.py`. Both SAP Notes and SAP
Community run it; `SOURCES[source]["reader"]` picks how steps 2–4 get the content.
Nothing else ingests.

**Notes** go through the signed-in headless browser. **Community** goes through the
Khoros public API (`services/community_api.py`) — community.sap.com is Cloudflare-
fronted, and a headless browser on a datacenter IP gets a managed challenge it can
never clear. The API answers anonymously and returns more than the page did: every
message in the thread, `is_solution` per reply, `conversation.solved` per thread, and
image URLs that fetch with a plain GET.

```
cron (services/scheduler.py) — decides WHEN, nothing else
        │
        ▼
┌─ 1 fetch_url ─────────────────────────────────────────────┐
│  Atomically claim the next pending URL. Already summarized │
│  → skip.                                                   │
└────────────────────────────────────────────────────────────┘
        ▼
┌─ 2 login ─────────────────────────────────────────────────┐
│  Sign in with the active SAP credential. Notes sign in on  │
│  the note URL; community signs in on the Khoros login URL, │
│  which hands off to the same IdP. Live session → reused.   │
└────────────────────────────────────────────────────────────┘
        ▼
┌─ 3 open_page ─────────────────────────────────────────────┐
│  Verify the page is really on screen.                      │
│    3.1 thin / rendering / bot check → wait, re-probe, renav│
│    3.2 bounced to login             → re-run step 2 once   │
│    3.3 still not there              → fail, URL requeued   │
└────────────────────────────────────────────────────────────┘
        ▼
┌─ 4 extract ───────────────────────────────────────────────┐
│  Article text + attachments (notes) or images (community). │
│  Blobs go to S3/local now; the LLM never carries bytes.    │
└────────────────────────────────────────────────────────────┘
        ▼
┌─ 4b describe_images ──────────────────────────────────────┐
│  Vision caption + OCR per image, so error text inside a    │
│  screenshot becomes searchable. Non-fatal, notes skip it.  │
└────────────────────────────────────────────────────────────┘
        ▼
┌─ 5 summarize ─────────────────────────────────────────────┐
│  LangChain: PromptTemplate | ChatOpenAI | JsonOutputParser │
│  → a validated NoteSummary. Community blogs self-skip.     │
└────────────────────────────────────────────────────────────┘
        ▼
┌─ 6 embed ─────────────────────────────────────────────────┐
│  build_blob() → Titan V2 vector.                           │
└────────────────────────────────────────────────────────────┘
        ▼
┌─ 7 persist ───────────────────────────────────────────────┐
│  Summary row + embedding row + URL status + run log.       │
└────────────────────────────────────────────────────────────┘
```

Every step appends to one trace, and every exit — success, skip, failure, crash —
lands in the same `_finish()`: URL status, run log, orphan blob cleanup. There is
no second exit path.

`CHAIN_STEP_DELAY_SEC` pauses between steps so Chrome settles and the trace stays
readable.

### Retrieval fields

Step 5 also produces two fields that exist purely so a note can be found again:

| Field | What it is |
|---|---|
| `error_signatures` | The literal error strings, codes and log lines, copied verbatim — never paraphrased |
| `search_text` | One dense paragraph phrased the way an engineer describes the failure |

Diagnose queries are raw error text, so without these a note only matches on its
prose. Both feed `build_blob()`. **Changing `build_blob()` invalidates every stored
content hash** — run the embedding backfill (Scheduler → Embeddings) afterwards; it
walks every latest summary and re-embeds the ones whose hash moved.

You manage the queues from the UI: **URLs**, **Scheduler**, **Community Ingest**.

---

## MCP server

A separate MCP (Model Context Protocol) server exposes the same knowledge base to AI tools like Cursor:

```bash
python3 -m mcp_server    # listens on port 3333
```

Tools: `hybrid_search` (semantic; the name predates the removal of its keyword leg), `search_errors`, `get_error`, `list_families`.

Bearer token is configured in **Developer → MCP Server** (not in `.env`).

---

## Project structure

```
error-dna-backend/
├── main.py                 # FastAPI app entrypoint
├── config.py               # Reads all settings from .env
├── prompts.py              # LLM system prompts (ZHC fallback)
├── db.py                   # PostgreSQL + migrations
├── routes/                 # API endpoints
│   ├── errors.py           # POST /api/errors/diagnose
│   ├── oauth.py            # OAuth client_credentials
│   ├── summaries.py        # SAP Note summaries
│   ├── community.py        # SAP Community summaries
│   └── scheduler.py        # Scrape + embed jobs
├── services/
│   ├── ingest_chain.py     # THE ingest chain — 7 steps, both sources
│   ├── scheduler.py        # Cron only — decides when the chain runs
│   ├── scraper.py          # Browser primitives the chain drives
│   ├── llm.py              # Shared ChatOpenAI factory + article chat
│   ├── error_diagnose.py   # The diagnose chain (L0-L4)
│   ├── error_expand.py     # LLM: retrieval query + signature + family
│   ├── error_fallback.py   # LLM fallback when no notes match
│   ├── error_families.py   # Family catalog (CSV seed + LLM catalog)
│   └── embeddings.py       # Titan vector embeddings
├── mcp_server/             # MCP tools for external AI clients
├── data/
│   └── error_families.csv  # 24 error family definitions (seeded on startup)
├── error-knowledge-base/   # Frontend UI (git submodule)
└── deploy.sh               # EC2 one-shot deploy script
```

---

## Local setup

### Requirements

- Python 3.11+
- PostgreSQL (Aurora RDS in production)
- AWS credentials (for RDS IAM auth + Bedrock embeddings)
- DeepSeek API key (LLM)

### Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create .env with DB, LLM, and auth keys (see Configuration section below).

python3 main.py         # API on http://localhost:3000
python3 -m mcp_server   # MCP on http://localhost:3333
```

### Frontend

```bash
cd error-knowledge-base
npm install
npm run dev
```

---

## Configuration

All tunables live in `.env` with inline comments explaining each key. Highlights:

| Variable | Default | What it controls |
|---|---|---|
| `ERROR_CLUSTER_THRESHOLD` | `0.75` | Min similarity to merge into an existing cluster |
| `ERROR_SOLUTION_THRESHOLD` | `0.50` | Min similarity for a note to be returned |
| `ERROR_SOLUTION_LIMIT` | `10` | Knowledge base fixes returned per diagnose |
| `ERROR_VECTOR_SEARCH_LIMIT` | `5` | Cluster candidates pulled in L2 |
| `CHAIN_STEP_DELAY_SEC` | `3` | Pause between ingest chain steps |
| `COMMUNITY_MAX_IMAGES` | `8` | Images pulled per community thread |
| `COMMUNITY_REQUIRE_ANSWER` | `1` | Skip threads with no replies before the LLM call |
| `CHAIN_PAGE_RETRIES` | `3` | Step 3 re-verify attempts before failing |
| `COMMUNITY_LOGIN_URL` | Khoros login page | Where community signs in (hands off to SAP ID) |
| `COMMUNITY_PAGE_RETRIES` | `10` | Step 3 attempts for community — Cloudflare needs longer |
| `COMMUNITY_MIN_CHARS` | `600` | Below this a thread is a shell, not the article |
| `LLM_BASE_URL` | derived | ChatOpenAI base URL (defaults from `LLM_API_URL`) |

See `.env` for the full list (generalize LLM, fallback LLM, hybrid keyword scores, etc.).

---

## Deploy to EC2

```bash
sudo bash deploy.sh          # full deploy
sudo bash deploy.sh status   # check services
sudo bash deploy.sh test     # smoke tests
```

This installs dependencies, sets up systemd services for API + MCP, and configures Caddy with HTTPS.

After code changes on the server:

```bash
git pull && sudo systemctl restart error-dna-api
```

---

## Auth summary

| Who | How |
|---|---|
| UI users | Login → JWT Bearer token |
| External systems | OAuth `client_credentials` → access token |
| MCP clients | Bearer token from Developer Settings |

All protected routes require `Authorization: Bearer <token>`.
