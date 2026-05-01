"""PostgreSQL connection pool and schema initialisation."""
from __future__ import annotations

import logging

import psycopg2
from psycopg2 import pool as pg_pool

log = logging.getLogger(__name__)

# Module-level pool; set by __main__ before anything else runs.
pool: pg_pool.ThreadedConnectionPool | None = None

_DDL = """
CREATE TABLE IF NOT EXISTS paper_cache (
    key         TEXT PRIMARY KEY,
    data        JSONB NOT NULL,
    written_at  DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS discovered_urls (
    url             TEXT PRIMARY KEY,
    last_modified   DOUBLE PRECISION,
    discovered_at   DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS probe_miss_counts (
    paper_num   TEXT PRIMARY KEY,
    count       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poll_state (
    id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_poll   DOUBLE PRECISION NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS user_watchlist (
    slack_user_id   TEXT NOT NULL,
    entry           TEXT NOT NULL,
    entry_type      TEXT NOT NULL CHECK (entry_type IN ('author', 'paper')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (slack_user_id, entry)
);
"""


def init_pool(dsn: str, minconn: int = 1, maxconn: int = 10) -> pg_pool.ThreadedConnectionPool:
    """Create and return a threaded connection pool."""
    p = pg_pool.ThreadedConnectionPool(minconn, maxconn, dsn)
    log.info("DB  pool created  minconn=%d  maxconn=%d", minconn, maxconn)
    return p


def init_db(p: pg_pool.ThreadedConnectionPool) -> None:
    """Create all tables (idempotent)."""
    conn = p.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute(_DDL)
        conn.commit()
        log.info("DB  schema initialised")
    finally:
        p.putconn(conn)
