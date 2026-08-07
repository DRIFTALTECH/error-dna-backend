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

# Error diagnose — distinct-error vector match threshold (0–1).
ERROR_MATCH_THRESHOLD = float(os.getenv("ERROR_MATCH_THRESHOLD", "0.70"))
# Vector candidates when matching raw/generalized text to distinct_errors.
ERROR_VECTOR_SEARCH_LIMIT = int(os.getenv("ERROR_VECTOR_SEARCH_LIMIT", "5"))
# Semantic solution notes returned per diagnose (no family filter).
ERROR_SOLUTION_LIMIT = int(os.getenv("ERROR_SOLUTION_LIMIT", "10"))
# Informational run lines — skip semantic note search + fallback.
INFORMATIONAL_FAMILY_CODE = os.getenv("INFORMATIONAL_FAMILY_CODE", "RUN_DIAGNOSTIC_EVENT")

# Hybrid search — vector + keyword blend for SAP note solutions.
HYBRID_VECTOR_WEIGHT = float(os.getenv("HYBRID_VECTOR_WEIGHT", "0.7"))
HYBRID_KEYWORD_WEIGHT = float(os.getenv("HYBRID_KEYWORD_WEIGHT", "0.3"))
HYBRID_CANDIDATE_LIMIT = int(os.getenv("HYBRID_CANDIDATE_LIMIT", "20"))
HYBRID_SEARCH_DEFAULT_LIMIT = int(os.getenv("HYBRID_SEARCH_DEFAULT_LIMIT", "5"))
HYBRID_SEARCH_MAX_LIMIT = int(os.getenv("HYBRID_SEARCH_MAX_LIMIT", "20"))
HYBRID_KEYWORD_SCORE_TITLE = float(os.getenv("HYBRID_KEYWORD_SCORE_TITLE", "1.0"))
HYBRID_KEYWORD_SCORE_ISSUE = float(os.getenv("HYBRID_KEYWORD_SCORE_ISSUE", "0.85"))
HYBRID_KEYWORD_SCORE_TAGS = float(os.getenv("HYBRID_KEYWORD_SCORE_TAGS", "0.75"))
HYBRID_KEYWORD_SCORE_SUMMARY = float(os.getenv("HYBRID_KEYWORD_SCORE_SUMMARY", "0.55"))
HYBRID_KEYWORD_TOKEN_MIN_LEN = int(os.getenv("HYBRID_KEYWORD_TOKEN_MIN_LEN", "3"))
HYBRID_KEYWORD_TOKEN_SCORE = float(os.getenv("HYBRID_KEYWORD_TOKEN_SCORE", "0.4"))

# LLM — error generalize (distinct cluster creation).
ERROR_GENERALIZE_TEMPERATURE = float(os.getenv("ERROR_GENERALIZE_TEMPERATURE", "0.2"))
ERROR_GENERALIZE_MAX_TOKENS = int(os.getenv("ERROR_GENERALIZE_MAX_TOKENS", "16384"))
ERROR_GENERALIZE_TIMEOUT = float(os.getenv("ERROR_GENERALIZE_TIMEOUT", "60"))

# LLM — ZHC fallback when hybrid_search returns zero solutions.
ERROR_FALLBACK_TEMPERATURE = float(os.getenv("ERROR_FALLBACK_TEMPERATURE", "0.2"))
ERROR_FALLBACK_MAX_TOKENS = int(os.getenv("ERROR_FALLBACK_MAX_TOKENS", "8192"))
ERROR_FALLBACK_TIMEOUT = float(os.getenv("ERROR_FALLBACK_TIMEOUT", "120"))

# Reversible encryption for stored SAP account passwords (Fernet key derived from this).
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")

# Where openclaw/headless-Chrome drops attachment downloads (env-specific). We read
# the extracted text from here then delete the files. Override per host in .env.
SCRAPE_DOWNLOAD_DIR = os.getenv("SCRAPE_DOWNLOAD_DIR", os.path.expanduser("~/Downloads"))

# LLM API
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.deepseek.com/v1/chat/completions")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-chat")

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
