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

CREATE TABLE IF NOT EXISTS scheduler_config (
    id SERIAL PRIMARY KEY,
    min_delay_min INTEGER DEFAULT 5,
    max_delay_min INTEGER DEFAULT 60,
    is_paused INTEGER DEFAULT 0,
    next_scrape_at TEXT,
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
        await conn.execute(SCHEMA)
        await conn.execute(FAMILIES_SEED)
        await conn.execute(SCHEDULER_SEED)
        await conn.execute(FIX_SEQUENCES)
    finally:
        await conn.close()
