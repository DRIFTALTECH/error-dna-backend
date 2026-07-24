-- =============================================================================
-- Error DNA Knowledge Base — full database schema (Aurora PostgreSQL)
-- =============================================================================
-- Source of truth mirror of what init_db() in db.py applies.
-- Shared Aurora DB may also contain unrelated app tables (users, tasks, …);
-- this file documents ONLY Error DNA tables.
--
-- Pipeline:
--   urls                → scrape queue (SAP Notes)
--   summaries           → LLM-structured KB entries (notes)
--   summary_embeddings  → Titan V2 vectors for notes + community
--   community_urls      → scrape queue (SAP Community)
--   community_summaries → LLM-structured KB entries (community)
--   credentials         → encrypted SAP login accounts
--   scheduler_config    → scrape cadence / pause / account rotate clock
--   scrape_log          → per-run notes scrape audit + JSON trace
--   community_scrape_log→ per-run community scrape audit + JSON trace
--   error_families      → taxonomy seed (10 families)
--   app_users           → app login (NOT the shared `users` table)
-- =============================================================================

-- Required for summary_embeddings.embedding
CREATE EXTENSION IF NOT EXISTS vector;


-- -----------------------------------------------------------------------------
-- SAP Notes — URL queue
-- status: pending | scraping | completed | failed | skipped
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS urls (
    id              SERIAL PRIMARY KEY,
    source_id       TEXT NOT NULL,                 -- SAP note number (e.g. 3780883)
    title           TEXT,
    source_url      TEXT NOT NULL,
    component       TEXT,
    category        TEXT,
    priority        TEXT,
    released_on     TEXT,
    status          TEXT DEFAULT 'pending',
    scraped_at      TEXT,
    error_message   TEXT,
    created_at      TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at      TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);


-- -----------------------------------------------------------------------------
-- SAP Notes — structured knowledge-base summaries (LLM output)
-- steps / gotchas / tags / environment / see_also stored as JSON text
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS summaries (
    id                   SERIAL PRIMARY KEY,
    source_id            TEXT NOT NULL,
    url_id               INTEGER REFERENCES urls(id),
    title                TEXT NOT NULL,
    family               TEXT,                      -- error family name
    area                 TEXT,
    type                 TEXT,                      -- Problem | How To | FAQ | …
    issue                TEXT,                      -- "the problem"
    summary              TEXT,                      -- "what's going on"
    steps                TEXT,                      -- JSON array of fix steps
    gotchas              TEXT,                      -- JSON array of warnings
    tags                 TEXT,                      -- JSON array of keywords
    source_version       INTEGER,
    source_date          TEXT,
    source_url           TEXT,
    component            TEXT,
    environment          TEXT DEFAULT '[]',
    see_also             TEXT DEFAULT '[]',
    attachments          TEXT,                      -- JSON [{name,key,ext}] S3/local docs
    is_latest            INTEGER DEFAULT 1,
    superseded_by_id     INTEGER,
    verification_status  TEXT DEFAULT 'current',
    created_at           TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at           TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);


-- -----------------------------------------------------------------------------
-- Error family taxonomy (seeded)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS error_families (
    id            SERIAL PRIMARY KEY,
    family_name   TEXT UNIQUE NOT NULL,
    description   TEXT,
    color         TEXT,
    icon          TEXT
);


-- -----------------------------------------------------------------------------
-- SAP scrape credentials (password Fernet-encrypted at app layer)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credentials (
    id           SERIAL PRIMARY KEY,
    label        TEXT NOT NULL,
    login_url    TEXT NOT NULL DEFAULT 'https://me.sap.com',
    username     TEXT NOT NULL,
    password     TEXT NOT NULL,
    is_active    INTEGER DEFAULT 1,
    created_at   TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);


-- -----------------------------------------------------------------------------
-- App login users (named app_users — `users` belongs to another app on this DB)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app_users (
    id              SERIAL PRIMARY KEY,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,
    created_at      TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);


-- -----------------------------------------------------------------------------
-- Background notes scraper config (single row id=1)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scheduler_config (
    id                     SERIAL PRIMARY KEY,
    min_delay_min          INTEGER DEFAULT 5,
    max_delay_min          INTEGER DEFAULT 60,
    is_paused              INTEGER DEFAULT 0,
    next_scrape_at         TEXT,
    account_activated_at   TEXT,                   -- clock for auto credential rotate
    updated_at             TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);


-- -----------------------------------------------------------------------------
-- SAP Notes scrape audit log
-- trace: JSON array of {at, phase, status, message, detail}
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scrape_log (
    id              SERIAL PRIMARY KEY,
    url_id          INTEGER,
    source_id       TEXT,
    status          TEXT,                          -- success | failed | …
    action          TEXT,                          -- create | skip | …
    old_version     INTEGER,
    new_version     INTEGER,
    duration_ms     INTEGER,
    error_message   TEXT,
    trace           TEXT,                          -- JSON step trace
    created_at      TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);


-- -----------------------------------------------------------------------------
-- SAP Community — URL queue (public pages, no SAP login)
-- status: pending | scraping | completed | failed | skipped
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS community_urls (
    id              SERIAL PRIMARY KEY,
    source_id       TEXT NOT NULL,                 -- community post id from URL
    title           TEXT,
    source_url      TEXT NOT NULL,
    component       TEXT,
    category        TEXT,
    priority        TEXT,
    released_on     TEXT,
    status          TEXT DEFAULT 'pending',
    scraped_at      TEXT,
    error_message   TEXT,
    created_at      TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at      TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);


-- -----------------------------------------------------------------------------
-- SAP Community — structured summaries (same shape as summaries + images)
-- images: JSON manifest {"image_1": {"key": "img/…", "alt": "…"}, …}
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS community_summaries (
    id                   SERIAL PRIMARY KEY,
    source_id            TEXT NOT NULL,
    url_id               INTEGER REFERENCES community_urls(id),
    title                TEXT NOT NULL,
    family               TEXT,
    area                 TEXT,
    type                 TEXT,
    issue                TEXT,
    summary              TEXT,
    steps                TEXT,
    gotchas              TEXT,
    tags                 TEXT,
    source_version       INTEGER,
    source_date          TEXT,
    source_url           TEXT,
    component            TEXT,
    environment          TEXT DEFAULT '[]',
    see_also             TEXT DEFAULT '[]',
    is_latest            INTEGER DEFAULT 1,
    superseded_by_id     INTEGER,
    verification_status  TEXT DEFAULT 'current',
    images               TEXT,                     -- S3/local image manifest JSON
    created_at           TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at           TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);


-- -----------------------------------------------------------------------------
-- SAP Community scrape audit log
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS community_scrape_log (
    id              SERIAL PRIMARY KEY,
    url_id          INTEGER,
    source_id       TEXT,
    status          TEXT,
    action          TEXT,
    duration_ms     INTEGER,
    error_message   TEXT,
    trace           TEXT,                          -- JSON step trace
    created_at      TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS')
);


-- -----------------------------------------------------------------------------
-- Vector store — one embedding per summary chunk
-- Blob embedded: title + family + issue + summary + tags + gotchas
-- Model: amazon.titan-embed-text-v2:0 → vector(1024)
-- source: 'notes' | 'community'
-- content_hash: sha256 of blob (skip re-embed when unchanged)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS summary_embeddings (
    id              SERIAL PRIMARY KEY,
    source          TEXT NOT NULL,                 -- 'notes' | 'community'
    summary_id      INTEGER NOT NULL,              -- id in summaries / community_summaries
    source_id       TEXT NOT NULL,                 -- note number / community post id
    content_hash    TEXT NOT NULL,
    embedding       vector(1024) NOT NULL,
    model           TEXT NOT NULL,
    created_at      TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    updated_at      TEXT DEFAULT to_char(now() AT TIME ZONE 'Asia/Kolkata', 'YYYY-MM-DD HH24:MI:SS'),
    UNIQUE (source, summary_id)
);


-- =============================================================================
-- Seed data
-- =============================================================================

INSERT INTO error_families (id, family_name, description, color, icon) VALUES
(1,  'HTTP & Status Codes',      'HTTP 4xx, 5xx, redirect errors',       '#58a6ff', '🌐'),
(2,  'Authentication',           'SAML, OAuth, SSO, login, XSUAA',       '#d29922', '🔐'),
(3,  'Certificate & TLS',        'SSL, certificates, HTTPS, trust',      '#3fb950', '📜'),
(4,  'Connection',               'SMTP, SFTP, JDBC, timeout',            '#f85149', '🔌'),
(5,  'Groovy & Script',          'Groovy, JavaScript failures',          '#d4760e', '📝'),
(6,  'Mapping & Transformation', 'XML, JSON, XSLT, mapping',             '#a371f7', '🔄'),
(7,  'Messaging',                'JMS, queues, message processing',      '#f778ba', '📨'),
(8,  'Database',                 'JDBC, SQL, DB connections',            '#8b949e', '🗄️'),
(9,  'Security',                 'Roles, authorization, XSS',            '#dc2626', '🛡️'),
(10, 'Configuration',            'Setup, deployment, migration',         '#2563eb', '⚙️')
ON CONFLICT (id) DO NOTHING;

INSERT INTO scheduler_config (id, min_delay_min, max_delay_min, is_paused)
VALUES (1, 5, 60, 1)
ON CONFLICT (id) DO NOTHING;


-- =============================================================================
-- Relationships (logical)
-- =============================================================================
-- urls.id                    ← summaries.url_id
-- community_urls.id          ← community_summaries.url_id
-- summaries.id               ← summary_embeddings.summary_id  (when source='notes')
-- community_summaries.id     ← summary_embeddings.summary_id  (when source='community')
-- summaries.family           ↔ error_families.family_name     (soft link, no FK)
-- community_summaries.family ↔ error_families.family_name     (soft link, no FK)
-- =============================================================================
