"""Shared fixtures and helpers for the paperscout test suite."""
from __future__ import annotations

import json as _json
from pathlib import Path

import pytest

from paperscout.config import Settings
from paperscout.models import Paper
from paperscout.storage import ProbeState, UserWatchlist
from paperscout.sources import WG21Index


# ── FakePool ─────────────────────────────────────────────────────────────────
# An in-memory substitute for psycopg2.pool.ThreadedConnectionPool that
# interprets the exact SQL patterns used by PaperCache, ProbeState, and
# UserWatchlist.  No real PostgreSQL server is required.

class _FakeStore:
    def __init__(self):
        self.paper_cache: dict = {}    # key -> (data_dict, written_at)
        self.discovered: dict = {}     # url -> (last_modified, discovered_at)
        self.misses: dict = {}         # paper_num -> count
        self.last_poll: float = 0.0
        self.watchlist: dict = {}      # (user_id, entry) -> entry_type


class _FakeCursor:
    def __init__(self, store: _FakeStore):
        self._s = store
        self.rowcount = 0
        self._row = None
        self._rows: list = []

    def __enter__(self): return self
    def __exit__(self, *_): pass

    def execute(self, sql: str, params=()):
        self._row = None
        self._rows = []
        self.rowcount = 0
        su = " ".join(sql.split()).upper()

        # ── paper_cache ───────────────────────────────────────────────────────
        if "SELECT WRITTEN_AT FROM PAPER_CACHE" in su:
            r = self._s.paper_cache.get(params[0])
            self._row = (r[1],) if r else None

        elif "SELECT DATA FROM PAPER_CACHE" in su:
            r = self._s.paper_cache.get(params[0])
            self._row = (r[0],) if r else None  # already a dict

        elif "INSERT INTO PAPER_CACHE" in su:
            key, data_str, ts = params[0], params[1], params[2]
            data = _json.loads(data_str) if isinstance(data_str, str) else data_str
            self._s.paper_cache[key] = (data, ts)

        # ── poll_state ────────────────────────────────────────────────────────
        elif "INSERT INTO POLL_STATE" in su and "DO NOTHING" in su:
            pass  # already initialised in _FakeStore

        elif "SELECT LAST_POLL FROM POLL_STATE" in su:
            self._row = (self._s.last_poll,)

        elif "UPDATE POLL_STATE SET LAST_POLL" in su:
            self._s.last_poll = params[0]

        # ── discovered_urls ───────────────────────────────────────────────────
        elif "SELECT 1 FROM DISCOVERED_URLS WHERE URL" in su:
            self._row = (1,) if params[0] in self._s.discovered else None

        elif "SELECT LAST_MODIFIED, DISCOVERED_AT FROM DISCOVERED_URLS WHERE URL" in su:
            r = self._s.discovered.get(params[0])
            self._row = (r[0], r[1]) if r else None

        elif "SELECT URL, LAST_MODIFIED, DISCOVERED_AT FROM DISCOVERED_URLS" in su:
            self._rows = [
                (url, lm, da)
                for url, (lm, da) in self._s.discovered.items()
            ]

        elif "SELECT URL FROM DISCOVERED_URLS" in su and "LAST_MODIFIED" not in su:
            self._rows = [(url,) for url in self._s.discovered]

        elif "INSERT INTO DISCOVERED_URLS" in su and "DO NOTHING" in su:
            url, lm, da = params[0], params[1], params[2]
            if url not in self._s.discovered:
                self._s.discovered[url] = (lm, da)
                self.rowcount = 1

        # ── probe_miss_counts ─────────────────────────────────────────────────
        elif "SELECT COUNT FROM PROBE_MISS_COUNTS WHERE PAPER_NUM" in su:
            c = self._s.misses.get(params[0])
            self._row = (c,) if c is not None else None

        elif "SELECT PAPER_NUM, COUNT FROM PROBE_MISS_COUNTS" in su:
            self._rows = list(self._s.misses.items())

        elif "INSERT INTO PROBE_MISS_COUNTS" in su and "DO UPDATE" in su:
            pn = params[0]
            self._s.misses[pn] = self._s.misses.get(pn, 0) + 1

        elif "DELETE FROM PROBE_MISS_COUNTS WHERE PAPER_NUM" in su:
            existed = params[0] in self._s.misses
            self._s.misses.pop(params[0], None)
            self.rowcount = 1 if existed else 0

        # ── user_watchlist ────────────────────────────────────────────────────
        elif "INSERT INTO USER_WATCHLIST" in su and "DO NOTHING" in su:
            uid, entry, etype = params[0], params[1], params[2]
            key = (uid, entry)
            if key not in self._s.watchlist:
                self._s.watchlist[key] = etype
                self.rowcount = 1

        elif "DELETE FROM USER_WATCHLIST WHERE SLACK_USER_ID" in su and "AND ENTRY" in su:
            uid, entry = params[0], params[1]
            key = (uid, entry)
            if key in self._s.watchlist:
                del self._s.watchlist[key]
                self.rowcount = 1

        elif "SELECT ENTRY, ENTRY_TYPE FROM USER_WATCHLIST WHERE SLACK_USER_ID" in su:
            uid = params[0]
            rows = [(e, t) for (u, e), t in self._s.watchlist.items() if u == uid]
            self._rows = sorted(rows, key=lambda x: (x[1], x[0]))

        elif "SELECT ENTRY FROM USER_WATCHLIST WHERE ENTRY_TYPE" in su:
            self._rows = [(e,) for (_, e), t in self._s.watchlist.items() if t == "paper"]

        elif "SELECT SLACK_USER_ID, ENTRY, ENTRY_TYPE FROM USER_WATCHLIST" in su:
            self._rows = [(u, e, t) for (u, e), t in self._s.watchlist.items()]

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, store: _FakeStore):
        self._cur = _FakeCursor(store)

    def cursor(self):
        return self._cur

    def commit(self): pass
    def rollback(self): pass


class FakePool:
    """In-memory substitute for psycopg2.pool.ThreadedConnectionPool.

    Each instance has its own isolated store.  Pass the same instance to
    multiple storage objects when they need to share state.
    """

    def __init__(self):
        self._store = _FakeStore()

    def getconn(self):
        return _FakeConn(self._store)

    def putconn(self, conn):
        pass


# ── Settings factory ──────────────────────────────────────────────────────────

def make_test_settings(**overrides) -> Settings:
    """Build a Settings instance with safe test defaults (no I/O, no credentials)."""
    base: dict = dict(
        slack_signing_secret="test-secret",
        slack_bot_token="xoxb-test",
        port=3000,
        database_url="",
        poll_interval_minutes=30,
        poll_overrun_cooldown_seconds=300,
        enable_bulk_wg21=True,
        enable_bulk_openstd=True,
        enable_iso_probe=True,
        probe_prefixes=["D", "P"],
        probe_extensions=[".pdf"],
        frontier_window_above=3,
        frontier_window_below=1,
        frontier_explicit_ranges=[],
        frontier_gap_threshold=50,
        hot_lookback_months=6,
        hot_revision_depth=2,
        cold_revision_depth=1,
        cold_cycle_divisor=48,
        gap_max_rev=1,
        alert_modified_hours=24,
        http_concurrency=5,
        http_timeout_seconds=5,
        http_use_http2=False,
        notification_channel="",
        notify_on_frontier_hit=True,
        notify_on_any_draft=True,
        notify_on_dp_transition=True,
        data_dir=Path("/tmp/paperscout-test"),
        cache_ttl_hours=1,
    )
    base.update(overrides)
    return Settings.model_construct(**base)


# ── Common data ───────────────────────────────────────────────────────────────

SAMPLE_INDEX_DATA: dict = {
    "P2300R10": {
        "title": "Senders/Receivers",
        "author": "Niebler",
        "date": "2024-01-01",
        "type": "paper",
        "link": "https://wg21.link/P2300R10",
        "long_link": "https://wg21.link/P2300R10.pdf",
        "github_url": "",
        "issues": [],
    },
    "P2301R0": {
        "title": "Some Other Paper",
        "author": "Doe",
        "date": "2024-06-01",
        "type": "paper",
        "link": "https://wg21.link/P2301R0",
        "long_link": "",
        "github_url": "",
    },
    "N4950": {
        "title": "Working Draft",
        "author": "Editor",
        "date": "2023-05-01",
        "type": "paper",
        "link": "https://wg21.link/N4950",
    },
}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_pool() -> FakePool:
    """Fresh in-memory pool for each test."""
    return FakePool()


@pytest.fixture
def sample_index_data() -> dict:
    return dict(SAMPLE_INDEX_DATA)


@pytest.fixture
def probe_state(fake_pool) -> ProbeState:
    return ProbeState(fake_pool)


@pytest.fixture
def populated_index(fake_pool) -> WG21Index:
    index = WG21Index(fake_pool)
    index._parse_and_index(SAMPLE_INDEX_DATA)
    return index
