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
