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
    is_latest INTEGER DEFAULT 1,
    superseded_by_id INTEGER,
    verification_status TEXT DEFAULT 'current',
    created_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);

CREATE TABLE IF NOT EXISTS error_families (
    id SERIAL PRIMARY KEY,
    family_name TEXT UNIQUE NOT NULL,
    description TEXT,
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
"""

FAMILIES_SEED = """
INSERT INTO error_families (id, family_name, description, color, icon) VALUES
(1,'HTTP & Status Codes','HTTP 4xx, 5xx, redirect errors','#58a6ff','🌐'),
(2,'Authentication','SAML, OAuth, SSO, login, XSUAA','#d29922','🔐'),
(3,'Certificate & TLS','SSL, certificates, HTTPS, trust','#3fb950','📜'),
(4,'Connection','SMTP, SFTP, JDBC, timeout','#f85149','🔌'),
(5,'Groovy & Script','Groovy, JavaScript failures','#d4760e','📝'),
(6,'Mapping & Transformation','XML, JSON, XSLT, mapping','#a371f7','🔄'),
(7,'Messaging','JMS, queues, message processing','#f778ba','📨'),
(8,'Database','JDBC, SQL, DB connections','#8b949e','🗄️'),
(9,'Security','Roles, authorization, XSS','#dc2626','🛡️'),
(10,'Configuration','Setup, deployment, migration','#2563eb','⚙️')
ON CONFLICT (id) DO NOTHING;
"""

SCHEDULER_SEED = """
INSERT INTO scheduler_config (id, min_delay_min, max_delay_min, is_paused)
VALUES (1, 5, 60, 1)
ON CONFLICT (id) DO NOTHING;
"""

FIX_SEQUENCES = """
SELECT setval(pg_get_serial_sequence('error_families','id'), GREATEST((SELECT COALESCE(MAX(id),1) FROM error_families), 1));
SELECT setval(pg_get_serial_sequence('scheduler_config','id'), GREATEST((SELECT COALESCE(MAX(id),1) FROM scheduler_config), 1));
"""


async def init_db():
    """Create schema + seed rows (idempotent). Uses private connect — DDL only."""
    conn = await _connect()
    try:
        # pgvector — required before summary_embeddings (vector column type).
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        await conn.execute(SCHEMA)
        # Added after scrape_log shipped — store the full per-run step trace as JSON.
        await conn.execute("ALTER TABLE scrape_log ADD COLUMN IF NOT EXISTS trace TEXT;")
        # Community images manifest: {"image_1": {"key":..., "alt":...}, ...}
        await conn.execute("ALTER TABLE community_summaries ADD COLUMN IF NOT EXISTS images TEXT;")
        # Clock for 24h (configurable) auto account rotate.
        await conn.execute(
            "ALTER TABLE scheduler_config ADD COLUMN IF NOT EXISTS account_activated_at TEXT;"
        )
        await conn.execute(FAMILIES_SEED)
        await conn.execute(SCHEDULER_SEED)
        await conn.execute(FIX_SEQUENCES)
    finally:
        await conn.close()
