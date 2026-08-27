"""Environment configuration."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root (this file's directory)
load_dotenv(Path(__file__).parent / ".env")

# PostgreSQL (Aurora RDS)
DB_HOST = os.getenv("DB_HOST", "")
DB_HOST_RO = os.getenv("DB_HOST_RO", DB_HOST)  # reader endpoint (unused for now)
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "postgres")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-2")

# Auth (app login). JWT_SECRET signs the session token; TTL in seconds (default 7d).
JWT_SECRET = os.getenv("JWT_SECRET", "")
AUTH_TOKEN_TTL = int(os.getenv("AUTH_TOKEN_TTL", str(7 * 24 * 3600)))
# OAuth client_credentials access tokens (default 1h).
OAUTH_TOKEN_TTL = int(os.getenv("OAUTH_TOKEN_TTL", "3600"))

# ---- Error diagnose (services/error_diagnose.py) ----
# L2 — min cosine similarity to merge a new error into an existing cluster.
# Higher than the solution floor on purpose: a wrong merge poisons a cluster's
# raw-message history, a missed merge only costs a duplicate row.
ERROR_CLUSTER_THRESHOLD = float(os.getenv("ERROR_CLUSTER_THRESHOLD", "0.75"))
# L2 — vector candidates pulled from distinct_error_embeddings.
ERROR_VECTOR_SEARCH_LIMIT = int(os.getenv("ERROR_VECTOR_SEARCH_LIMIT", "5"))
# L3 — min cosine similarity for a solution note to be returned at all.
ERROR_SOLUTION_THRESHOLD = float(os.getenv("ERROR_SOLUTION_THRESHOLD", "0.50"))
# L3 — max solution notes returned per diagnose (no family filter).
ERROR_SOLUTION_LIMIT = int(os.getenv("ERROR_SOLUTION_LIMIT", "10"))

# Vector search over summary_embeddings (MCP hybrid_search tool + L3).
# ponytail: the tool is still named hybrid_search for its MCP clients; the
# keyword leg is gone, scoring is pure cosine.
HYBRID_SEARCH_DEFAULT_LIMIT = int(os.getenv("HYBRID_SEARCH_DEFAULT_LIMIT", "5"))
HYBRID_SEARCH_MAX_LIMIT = int(os.getenv("HYBRID_SEARCH_MAX_LIMIT", "20"))

# LLM — L1 expand (retrieval query + signature + family + problem).
ERROR_EXPAND_TEMPERATURE = float(os.getenv("ERROR_EXPAND_TEMPERATURE", "0.2"))
ERROR_EXPAND_MAX_TOKENS = int(os.getenv("ERROR_EXPAND_MAX_TOKENS", "2000"))
ERROR_EXPAND_TIMEOUT = float(os.getenv("ERROR_EXPAND_TIMEOUT", "60"))

# LLM — ZHC fallback when the RAG returns zero solution notes.
ERROR_FALLBACK_TEMPERATURE = float(os.getenv("ERROR_FALLBACK_TEMPERATURE", "0.2"))
ERROR_FALLBACK_MAX_TOKENS = int(os.getenv("ERROR_FALLBACK_MAX_TOKENS", "8192"))
ERROR_FALLBACK_TIMEOUT = float(os.getenv("ERROR_FALLBACK_TIMEOUT", "120"))

# Reversible encryption for stored SAP account passwords (Fernet key derived from this).
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

# Where openclaw/headless-Chrome drops attachment downloads (env-specific). We read
# the extracted text from here then delete the files. Override per host in .env.
SCRAPE_DOWNLOAD_DIR = os.getenv("SCRAPE_DOWNLOAD_DIR", os.path.expanduser("~/Downloads"))

# LLM API. LLM_API_URL is the legacy full chat-completions path (raw httpx callers);
# LangChain's ChatOpenAI wants the base — derived from it unless LLM_BASE_URL is set.
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.deepseek.com/v1/chat/completions")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "") or LLM_API_URL.split("/chat/completions")[0]
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

# ---- Ingest chain (services/ingest_chain.py) ----
# Pause between chain steps — lets Chrome settle and keeps the trace readable.
CHAIN_STEP_DELAY_SEC = float(os.getenv("CHAIN_STEP_DELAY_SEC", "3"))
# Step 3 re-verify attempts when the page is thin / still rendering.
CHAIN_PAGE_RETRIES = int(os.getenv("CHAIN_PAGE_RETRIES", "3"))
# Step 5 — LangChain summarize call.
SUMMARIZE_TEMPERATURE = float(os.getenv("SUMMARIZE_TEMPERATURE", "0.3"))
SUMMARIZE_MAX_TOKENS = int(os.getenv("SUMMARIZE_MAX_TOKENS", "4000"))
SUMMARIZE_TIMEOUT = float(os.getenv("SUMMARIZE_TIMEOUT", "120"))
# Max article chars handed to the LLM.
SUMMARIZE_MAX_INPUT_CHARS = int(os.getenv("SUMMARIZE_MAX_INPUT_CHARS", "15000"))
# Community drain — pause between URLs so Chrome reclaims RAM on small boxes.
COMMUNITY_INTER_ITEM_SLEEP_SEC = float(os.getenv("COMMUNITY_INTER_ITEM_SLEEP_SEC", "60"))
# Community sign-in lands here first (Khoros hands off to accounts.sap.com, the same
# IdP the notes scraper already drives). An existing session redirects straight back.
COMMUNITY_LOGIN_URL = os.getenv(
    "COMMUNITY_LOGIN_URL", "https://community.sap.com/t5/user/userloginpage/tab/user")
# A community thread shorter than this is a shell/redirect, not the article.
COMMUNITY_MIN_CHARS = int(os.getenv("COMMUNITY_MIN_CHARS", "600"))
# Step 3 attempts for community pages — Cloudflare needs longer than a note.
COMMUNITY_PAGE_RETRIES = int(os.getenv("COMMUNITY_PAGE_RETRIES", "10"))

# Embeddings — Amazon Titan Text Embeddings V2 via Bedrock (same AWS creds as Aurora IAM).
EMBED_MODEL_ID = os.getenv("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIMENSIONS = int(os.getenv("EMBED_DIMENSIONS", "1024"))
EMBED_REGION = os.getenv("EMBED_REGION", AWS_REGION)

# Server
PORT = int(os.getenv("PORT", "3000"))
HOST = os.getenv("HOST", "0.0.0.0")

# MCP (streamable-http). Public URL shown in Developer Settings; override in .env.
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "3333"))
MCP_PUBLIC_URL = os.getenv("MCP_PUBLIC_URL", "").rstrip("/")

# Scheduler defaults
MIN_DELAY_MIN = int(os.getenv("MIN_DELAY_MIN", "5"))
MAX_DELAY_MIN = int(os.getenv("MAX_DELAY_MIN", "60"))
# Auto-rotate active SAP credential after N hours (0 = disabled). Needs ≥2 credentials.
ACCOUNT_ROTATE_HOURS = int(os.getenv("ACCOUNT_ROTATE_HOURS", "24"))

# OpenClaw
OPENCLAW_BROWSER_TIMEOUT = int(os.getenv("OPENCLAW_BROWSER_TIMEOUT", "30"))

# Login: which profile to pick on the "Account Selection" page.
# Empty = pick any S-user tile (S + 7+ digits). Set to an exact id (e.g. S0012345678)
# to force one specific profile. ponytail: env, not a DB column — one value, no migration.
PREFERRED_SUSER = os.getenv("PREFERRED_SUSER", "")
