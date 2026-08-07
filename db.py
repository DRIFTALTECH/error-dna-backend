"""PostgreSQL (asyncpg) access layer for Aurora RDS.

Public API — import these only:
  read(sql, params=None)  → list[dict]
  write(sql, params=None) → list[dict]  (RETURNING rows, else [])
  init_db()               → schema + seed (startup)
"""

import re
import asyncpg
from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, AWS_REGION

_rds_client = None

# ponytail: routes still use ? + datetime('now','localtime'); translate here.
NOW_IST = "to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')"


def _password() -> str:
    if DB_PASSWORD:
        return DB_PASSWORD
    global _rds_client
    if _rds_client is None:
        import boto3
        _rds_client = boto3.client("rds", region_name=AWS_REGION)
    return _rds_client.generate_db_auth_token(
        DBHostname=DB_HOST, Port=DB_PORT, DBUsername=DB_USER, Region=AWS_REGION
    )


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=_password(), database=DB_NAME, ssl="require",
    )


def _translate(sql: str) -> str:
    sql = sql.replace("datetime('now', 'localtime')", NOW_IST)
    sql = sql.replace("datetime('now','localtime')", NOW_IST)
    sql = sql.replace("datetime('now')", NOW_IST)
    sql = re.sub(r"date\(([\w.]+)\)", r"substr(\1, 1, 10)", sql)
    n = 0

    def repl(_):
        nonlocal n
        n += 1
        return f"${n}"

    return re.sub(r"\?", repl, sql)


async def _run(sql: str, params=None) -> list[dict]:
    conn = await _connect()
    try:
        rows = await conn.fetch(_translate(sql), *(tuple(params) if params else ()))
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def read(sql: str, params=None) -> list[dict]:
    """SELECT → list of row dicts."""
    return await _run(sql, params)


async def write(sql: str, params=None) -> list[dict]:
    """INSERT / UPDATE / DELETE → RETURNING rows (else [])."""
    return await _run(sql, params)


SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    id SERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    title TEXT,
    source_url TEXT NOT NULL,
    component TEXT,
    category TEXT,
    priority TEXT,
    released_on TEXT,
    status TEXT DEFAULT 'pending',
    scraped_at TEXT,
    error_message TEXT,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS summaries (
    id SERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    url_id INTEGER REFERENCES urls(id),
    title TEXT NOT NULL,
    family TEXT,
    area TEXT,
    type TEXT,
    issue TEXT,
    summary TEXT,
    steps TEXT,
    gotchas TEXT,
    tags TEXT,
    source_version INTEGER,
    source_date TEXT,
    source_url TEXT,
    component TEXT,
    environment TEXT DEFAULT '[]',
    see_also TEXT DEFAULT '[]',
    attachments TEXT,
    is_latest INTEGER DEFAULT 1,
    superseded_by_id INTEGER,
    verification_status TEXT DEFAULT 'current',
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS error_families (
    id SERIAL PRIMARY KEY,
    code TEXT UNIQUE,
    family_name TEXT NOT NULL,
    severity TEXT,
    description TEXT,
    match_patterns TEXT DEFAULT '[]',
    match_priority INTEGER DEFAULT 999000,
    color TEXT,
    icon TEXT
);

CREATE TABLE IF NOT EXISTS credentials (
    id SERIAL PRIMARY KEY,
    label TEXT NOT NULL,
    login_url TEXT NOT NULL DEFAULT 'https://me.sap.com',
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

-- app_users, NOT users: a different app already owns a `users` table in this DB.
CREATE TABLE IF NOT EXISTS app_users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS scheduler_config (
    id SERIAL PRIMARY KEY,
    min_delay_min INTEGER DEFAULT 5,
    max_delay_min INTEGER DEFAULT 60,
    is_paused INTEGER DEFAULT 0,
    next_scrape_at TEXT,
    account_activated_at TEXT,
    updated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS scrape_log (
    id SERIAL PRIMARY KEY,
    url_id INTEGER,
    source_id TEXT,
    status TEXT,
    action TEXT,
    old_version INTEGER,
    new_version INTEGER,
    duration_ms INTEGER,
    error_message TEXT,
    account_label TEXT,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

-- ---- SAP Community: same shape as the notes tables, own rows. No auth, no
-- ---- scheduler — public pages scraped one-by-one via the browser (Cloudflare).
CREATE TABLE IF NOT EXISTS community_urls (
    id SERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    title TEXT,
    source_url TEXT NOT NULL,
    component TEXT,
    category TEXT,
    priority TEXT,
    released_on TEXT,
    status TEXT DEFAULT 'pending',
    scraped_at TEXT,
    error_message TEXT,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS community_summaries (
    id SERIAL PRIMARY KEY,
    source_id TEXT NOT NULL,
    url_id INTEGER REFERENCES community_urls(id),
    title TEXT NOT NULL,
    family TEXT,
    area TEXT,
    type TEXT,
    issue TEXT,
    summary TEXT,
    steps TEXT,
    gotchas TEXT,
    tags TEXT,
    source_version INTEGER,
    source_date TEXT,
    source_url TEXT,
    component TEXT,
    environment TEXT DEFAULT '[]',
    see_also TEXT DEFAULT '[]',
    is_latest INTEGER DEFAULT 1,
    superseded_by_id INTEGER,
    verification_status TEXT DEFAULT 'current',
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS community_scrape_log (
    id SERIAL PRIMARY KEY,
    url_id INTEGER,
    source_id TEXT,
    status TEXT,
    action TEXT,
    duration_ms INTEGER,
    error_message TEXT,
    trace TEXT,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

-- Vector store for summary chunks (title+family+issue+summary+tags+gotchas).
-- Requires: CREATE EXTENSION vector; (applied in init_db).
CREATE TABLE IF NOT EXISTS summary_embeddings (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    summary_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    UNIQUE (source, summary_id)
);

-- Key/value app settings (MCP bearer, public MCP URL, etc.).
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

-- OAuth 2.0 client_credentials clients (secret stored as pbkdf2 hash only).
CREATE TABLE IF NOT EXISTS oauth_clients (
    id SERIAL PRIMARY KEY,
    client_id TEXT UNIQUE NOT NULL,
    secret_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

-- Runtime errors — one row per distinct generalized fingerprint.
CREATE TABLE IF NOT EXISTS distinct_errors (
    id SERIAL PRIMARY KEY,
    fingerprint_hash TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    generalized_text TEXT NOT NULL,
    summary TEXT,
    family_code TEXT DEFAULT 'UNCLASSIFIED_ERROR',
    occurrence_count INTEGER DEFAULT 1,
    first_seen_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    last_seen_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

-- Titan vectors for distinct_errors (title + generalized + summary).
CREATE TABLE IF NOT EXISTS distinct_error_embeddings (
    id SERIAL PRIMARY KEY,
    distinct_error_id INTEGER UNIQUE NOT NULL REFERENCES distinct_errors(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    model TEXT NOT NULL,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

-- Audit — every POST /api/errors/diagnose call.
CREATE TABLE IF NOT EXISTS error_events (
    id SERIAL PRIMARY KEY,
    raw_text TEXT NOT NULL,
    generalized_text TEXT,
    distinct_error_id INTEGER REFERENCES distinct_errors(id),
    family_code TEXT,
    error_match_percent REAL,
    created_new INTEGER DEFAULT 0,
    caller TEXT,
    source TEXT,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

-- Persisted semantic links: distinct error cluster → solution note (summaries.id).
CREATE TABLE IF NOT EXISTS error_cluster_solutions (
    id SERIAL PRIMARY KEY,
    distinct_error_id INTEGER NOT NULL REFERENCES distinct_errors(id) ON DELETE CASCADE,
    summary_id INTEGER NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
    match_percent REAL NOT NULL,
    hit_count INTEGER DEFAULT 1,
    last_seen_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    UNIQUE (distinct_error_id, summary_id)
);

-- One-shot family re-tag audit (services/migrate_families.py).
CREATE TABLE IF NOT EXISTS family_migration_log (
    id SERIAL PRIMARY KEY,
    summary_id INTEGER NOT NULL,
    source_id TEXT,
    old_family TEXT,
    new_family TEXT NOT NULL,
    confidence REAL,
    reason TEXT,
    migrated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);
"""

SCHEDULER_SEED = """
INSERT INTO scheduler_config (id, min_delay_min, max_delay_min, is_paused)
VALUES (1, 5, 60, 1)
ON CONFLICT (id) DO NOTHING;
"""

FIX_SEQUENCES = """
SELECT setval(pg_get_serial_sequence('scheduler_config','id'), GREATEST((SELECT COALESCE(MAX(id),1) FROM scheduler_config), 1));
"""

OAUTH_CLIENTS_DDL = """
CREATE TABLE oauth_clients (
    id SERIAL PRIMARY KEY,
    client_id TEXT UNIQUE NOT NULL,
    secret_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);
"""


async def _migrate_oauth_clients(conn) -> None:
    """Replace legacy oauth_clients (client_id, data, created_at) with current schema."""
    row = await conn.fetchrow(
        """SELECT
             EXISTS(
               SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'oauth_clients' AND column_name = 'data'
             ) AS legacy,
             EXISTS(
               SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = 'oauth_clients' AND column_name = 'secret_hash'
             ) AS current_schema"""
    )
    if row["current_schema"]:
        return
    if row["legacy"]:
        await conn.execute("DROP TABLE IF EXISTS oauth_clients CASCADE")
        print("🔄 oauth_clients: dropped legacy table (client_id, data, created_at)")
    await conn.execute(OAUTH_CLIENTS_DDL)
    print("✅ oauth_clients table ready")


async def init_db():
    """Create schema + seed rows (idempotent). Uses private connect — DDL only."""
    conn = await _connect()
    try:
        # pgvector — required before summary_embeddings (vector column type).
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        await conn.execute(SCHEMA)
        await _migrate_oauth_clients(conn)
        # Added after scrape_log shipped — store the full per-run step trace as JSON.
        await conn.execute("ALTER TABLE scrape_log ADD COLUMN IF NOT EXISTS trace TEXT;")
        # Which SAP credential ran this scrape (not the current active account).
        await conn.execute("ALTER TABLE scrape_log ADD COLUMN IF NOT EXISTS account_label TEXT;")
        # Community images manifest: {"image_1": {"key":..., "alt":...}, ...}
        await conn.execute("ALTER TABLE community_summaries ADD COLUMN IF NOT EXISTS images TEXT;")
        # Note attachments: [{"name":..., "key":"doc/…", "ext":...}, ...]
        await conn.execute("ALTER TABLE summaries ADD COLUMN IF NOT EXISTS attachments TEXT;")
        # Clock for 24h (configurable) auto account rotate.
        await conn.execute(
            "ALTER TABLE scheduler_config ADD COLUMN IF NOT EXISTS account_activated_at TEXT;"
        )
        # Older app_settings rows were created without updated_at.
        await conn.execute(
            """ALTER TABLE app_settings ADD COLUMN IF NOT EXISTS updated_at TEXT
               DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS');"""
        )
        # error_families — CSV catalog columns
        for stmt in (
            "ALTER TABLE error_families ADD COLUMN IF NOT EXISTS code TEXT;",
            "ALTER TABLE error_families ADD COLUMN IF NOT EXISTS severity TEXT;",
            "ALTER TABLE error_families ADD COLUMN IF NOT EXISTS match_patterns TEXT DEFAULT '[]';",
            "ALTER TABLE error_families ADD COLUMN IF NOT EXISTS match_priority INTEGER DEFAULT 999000;",
            "CREATE UNIQUE INDEX IF NOT EXISTS error_families_code_uidx ON error_families(code);",
        ):
            await conn.execute(stmt)
        await conn.execute(SCHEDULER_SEED)
        await conn.execute(FIX_SEQUENCES)
    finally:
        await conn.close()

    from services.error_families import seed_families
    n = await seed_families()
    if n:
        print(f"📂 error_families seeded/updated: {n} from CSV")
    await write("DELETE FROM error_families WHERE code IS NULL")
