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

When you call `POST /api/errors/diagnose` with an error message, this is what happens:

```
You send error text
        │
        ▼
┌───────────────────────────────────────┐
│ 1. Search existing error clusters     │  Compare your error against errors
│    (vector similarity on raw text)    │  we've seen before using embeddings.
└───────────────────────────────────────┘
        │
        ├── Match ≥ 70%? ──► Use that cluster (bump occurrence count)
        │
        ▼ No match
┌───────────────────────────────────────┐
│ 2. LLM generalizes the error           │  Strip user IDs, timestamps, GUIDs.
│                                       │  Assign a title, family, and summary.
└───────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────┐
│ 3. Search clusters again               │  Same vector search on generalized text.
└───────────────────────────────────────┘
        │
        ├── Match ≥ 70%? ──► Use that cluster
        │
        ▼ Still no match
┌───────────────────────────────────────┐
│ 4. Create a new cluster                │  New row in distinct_errors + embedding.
└───────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────┐
│ 5. Search SAP Note fixes               │  Hybrid search: vector + keyword over
│    (hybrid_search)                     │  summarized notes. Returns up to 10 hits.
└───────────────────────────────────────┘
        │
        ├── Found notes? ──► Return solutions[] from knowledge base
        │
        ▼ Zero notes (and not an informational log line)
┌───────────────────────────────────────┐
│ 6. LLM fallback (ZHC prompt)           │  Zero-Hallucination troubleshooting
│                                       │  assistant writes ROOT CAUSE + STEPS TO FIX.
└───────────────────────────────────────┘
        │
        ▼
   JSON response back to you
```

### Key ideas

- **Cluster ≠ Family** — A cluster is one distinct error pattern (e.g. "HTTP 500 from receiver"). A family is a broad label (e.g. `HTTP_REQUEST_FAILED`). Many clusters can share a family.
- **Similarity drives matching** — If two errors mean the same thing, they merge into one cluster (default threshold: 70%).
- **Solutions are semantic** — SAP Note search is **not** filtered by family. It uses meaning (embeddings + keywords), not category labels.
- **Informational errors are skipped** — Lines like "start execution of job" are classified as `RUN_DIAGNOSTIC_EVENT`. They get no solutions and no fallback.

---

## API response fields

| Field | Meaning |
|---|---|
| `distinct_error_id` | Cluster ID this error was matched to or created |
| `is_new_distinct` | `true` if a brand-new cluster was created |
| `cluster_confidence` | How similar this error was to the cluster (0–100%) |
| `occurrence_count` | How many times this cluster has been hit |
| `family_code` / `family_name` | Broad error category |
| `solutions` | SAP Note fixes from the knowledge base (up to 10) |
| `fallback_solution` | LLM markdown answer when `solutions` is empty |
| `solution_source` | `"knowledge_base"`, `"llm_fallback"`, or `"none"` |

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

## Knowledge base flow (how notes get in)

```
SAP Note URL added
        │
        ▼
Background scheduler scrapes the page (OpenClaw browser)
        │
        ▼
LLM summarizes → title, issue, steps, gotchas, tags, family
        │
        ▼
Stored in PostgreSQL (summaries / community_summaries)
        │
        ▼
Embedding generated (Amazon Titan via Bedrock)
        │
        ▼
Available for hybrid_search + MCP tools
```

You manage this from the UI: **URLs**, **Scheduler**, **Community Ingest**.

---

## MCP server

A separate MCP (Model Context Protocol) server exposes the same knowledge base to AI tools like Cursor:

```bash
python3 -m mcp_server    # listens on port 3333
```

Tools: `hybrid_search`, `search_errors`, `get_error`, `list_families`.

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
│   ├── error_diagnose.py   # Main diagnose chain
│   ├── error_generalize.py # LLM error normalization
│   ├── error_fallback.py   # LLM fallback when no notes match
│   ├── error_families.py   # Family catalog + pattern classifier
│   ├── embeddings.py       # Titan vector embeddings
│   ├── summarizer.py       # Note summarization LLM
│   └── scheduler.py        # Background scrape loop
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
| `ERROR_MATCH_THRESHOLD` | `0.70` | Min similarity to merge into existing cluster |
| `ERROR_SOLUTION_LIMIT` | `10` | SAP Note fixes returned per diagnose |
| `HYBRID_VECTOR_WEIGHT` | `0.7` | Semantic search weight in hybrid blend |
| `HYBRID_KEYWORD_WEIGHT` | `0.3` | Keyword search weight in hybrid blend |
| `INFORMATIONAL_FAMILY_CODE` | `RUN_DIAGNOSTIC_EVENT` | Skip solutions for log-line errors |

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
