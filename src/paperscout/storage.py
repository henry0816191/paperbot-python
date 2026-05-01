"""PostgreSQL-backed storage: PaperCache, ProbeState, UserWatchlist."""
from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from psycopg2.pool import ThreadedConnectionPool

log = logging.getLogger(__name__)

# isocpp.org draft URLs (same path shape as ISOProber)
_ISO_PAPER_PATH_RE = re.compile(
    r"/files/papers/[DP](\d{4})R\d+\.(?:pdf|html)",
    re.IGNORECASE,
)


def iso_paper_number_from_discovered_url(url: str) -> int | None:
    """Extract WG21 paper number from an isocpp.org ``.../papers/[DP]####R#`` URL, or None."""
    m = _ISO_PAPER_PATH_RE.search(url)
    return int(m.group(1)) if m else None


# ── Connection helper ────────────────────────────────────────────────────────

@contextmanager
def _conn(pool: "ThreadedConnectionPool") -> Generator:
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ── Paper Cache ──────────────────────────────────────────────────────────────

_CACHE_KEY = "wg21_index"


class PaperCache:
    """TTL-based cache for the wg21.link JSON index stored in PostgreSQL.

    Provides the same ``is_fresh`` / ``read`` / ``read_if_fresh`` / ``write``
    interface as the old ``JsonCache`` so that ``WG21Index`` needs no further
    changes.
    """

    def __init__(self, pool: "ThreadedConnectionPool", ttl_hours: float = 1.0):
        self._pool = pool
        self.ttl_seconds = ttl_hours * 3600

    def is_fresh(self) -> bool:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT written_at FROM paper_cache WHERE key = %s",
                    (_CACHE_KEY,),
                )
                row = cur.fetchone()
        if row is None:
            return False
        return (time.time() - row[0]) < self.ttl_seconds

    def read(self) -> dict | None:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT data FROM paper_cache WHERE key = %s",
                    (_CACHE_KEY,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        data = row[0]
        if isinstance(data, str):
            try:
                return json.loads(data)
            except json.JSONDecodeError as exc:
                log.warning("Failed to parse cached index JSON: %s", exc)
                return None
        return data  # psycopg2 returns JSONB as a Python dict already

    def read_if_fresh(self) -> dict | None:
        if self.is_fresh():
            return self.read()
        return None

    def write(self, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False)
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO paper_cache (key, data, written_at)
                    VALUES (%s, %s::jsonb, %s)
                    ON CONFLICT (key) DO UPDATE
                        SET data = EXCLUDED.data,
                            written_at = EXCLUDED.written_at
                    """,
                    (_CACHE_KEY, payload, time.time()),
                )
        log.debug("PaperCache  written  entries=%d", len(data))


# ── Probe State ──────────────────────────────────────────────────────────────

class ProbeState:
    """PostgreSQL-backed probe state: discovered URLs, miss counters, last-poll.

    All existing methods are preserved with identical signatures.
    """

    def __init__(self, pool: "ThreadedConnectionPool"):
        self._pool = pool
        self._ensure_poll_row()

    def _ensure_poll_row(self) -> None:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO poll_state (id, last_poll) VALUES (1, 0)"
                    " ON CONFLICT (id) DO NOTHING"
                )

    # ── discovered ───────────────────────────────────────────────────────────

    @property
    def discovered(self) -> dict[str, dict]:
        """Return full discovered map as a dict (for status display / iteration)."""
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT url, last_modified, discovered_at FROM discovered_urls")
                rows = cur.fetchall()
        return {
            url: {"last_modified": lm, "discovered_at": da}
            for url, lm, da in rows
        }

    def mark_discovered(self, url: str, last_modified_ts: float | None = None) -> None:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO discovered_urls (url, last_modified, discovered_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (url) DO NOTHING
                    """,
                    (url, last_modified_ts, time.time()),
                )

    def is_discovered(self, url: str) -> bool:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM discovered_urls WHERE url = %s",
                    (url,),
                )
                return cur.fetchone() is not None

    def discovered_info(self, url: str) -> dict | None:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT last_modified, discovered_at FROM discovered_urls WHERE url = %s",
                    (url,),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return {"last_modified": row[0], "discovered_at": row[1]}

    def paper_nums_from_discovered_iso_urls(self) -> set[int]:
        """Paper numbers parsed from isocpp.org draft URLs in ``discovered_urls``."""
        out: set[int] = set()
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT url FROM discovered_urls")
                for row in cur.fetchall():
                    url = row[0]
                    n = iso_paper_number_from_discovered_url(url)
                    if n is not None:
                        out.add(n)
        return out

    # ── miss counts ──────────────────────────────────────────────────────────

    @property
    def miss_counts(self) -> dict[str, int]:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT paper_num, count FROM probe_miss_counts")
                return {row[0]: row[1] for row in cur.fetchall()}

    def record_miss(self, paper_num: str) -> None:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO probe_miss_counts (paper_num, count) VALUES (%s, 1)
                    ON CONFLICT (paper_num) DO UPDATE
                        SET count = probe_miss_counts.count + 1
                    """,
                    (paper_num,),
                )

    def reset_misses(self, paper_num: str) -> None:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM probe_miss_counts WHERE paper_num = %s",
                    (paper_num,),
                )

    def get_miss_count(self, paper_num: str) -> int:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count FROM probe_miss_counts WHERE paper_num = %s",
                    (paper_num,),
                )
                row = cur.fetchone()
        return row[0] if row else 0

    def should_skip(
        self,
        paper_num: str,
        threshold: int,
        multiplier: int,
        max_skip: int,
        cycle: int,
    ) -> bool:
        misses = self.get_miss_count(paper_num)
        if misses < threshold:
            return False
        skip_cycles = min(multiplier ** (misses - threshold), max_skip)
        return (cycle % skip_cycles) != 0

    # ── poll timestamp ────────────────────────────────────────────────────────

    @property
    def last_poll(self) -> float:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT last_poll FROM poll_state WHERE id = 1")
                row = cur.fetchone()
        return row[0] if row else 0.0

    def touch_poll(self) -> None:
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE poll_state SET last_poll = %s WHERE id = 1",
                    (time.time(),),
                )

    def save(self) -> None:
        """No-op: PostgreSQL writes are committed immediately."""


# ── User Watchlist ───────────────────────────────────────────────────────────

class UserWatchlist:
    """Per-user watchlist stored in the ``user_watchlist`` table.

    Each entry is either an author name substring (``entry_type='author'``)
    or a paper number string (``entry_type='paper'``).  The type is
    auto-detected: pure digit strings → paper, anything else → author.
    """

    def __init__(self, pool: "ThreadedConnectionPool"):
        self._pool = pool

    @staticmethod
    def _detect_type(raw: str) -> str:
        return "paper" if raw.strip().isdigit() else "author"

    def add(self, user_id: str, raw_entry: str) -> bool:
        """Add an entry for *user_id*.  Returns True if newly inserted."""
        entry = raw_entry.strip().lower()
        if not entry:
            return False
        etype = self._detect_type(entry)
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO user_watchlist (slack_user_id, entry, entry_type)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (slack_user_id, entry) DO NOTHING
                    """,
                    (user_id, entry, etype),
                )
                return cur.rowcount > 0

    def remove(self, user_id: str, raw_entry: str) -> bool:
        """Remove an entry for *user_id*.  Returns True if it existed."""
        entry = raw_entry.strip().lower()
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM user_watchlist WHERE slack_user_id = %s AND entry = %s",
                    (user_id, entry),
                )
                return cur.rowcount > 0

    def list_entries(self, user_id: str) -> list[tuple[str, str]]:
        """Return ``[(entry, entry_type), ...]`` for *user_id*, sorted."""
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT entry, entry_type FROM user_watchlist"
                    " WHERE slack_user_id = %s ORDER BY entry_type, entry",
                    (user_id,),
                )
                return [(row[0], row[1]) for row in cur.fetchall()]

    def get_all_watched_paper_nums(self) -> set[int]:
        """Return the union of all watched paper numbers across all users."""
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT entry FROM user_watchlist WHERE entry_type = 'paper'"
                )
                rows = cur.fetchall()
        result: set[int] = set()
        for (entry,) in rows:
            try:
                result.add(int(entry))
            except ValueError:
                pass
        return result

    def _get_all_entries(self) -> list[tuple[str, str, str]]:
        """Return all rows as ``[(slack_user_id, entry, entry_type)]``."""
        with _conn(self._pool) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT slack_user_id, entry, entry_type FROM user_watchlist"
                )
                return [(row[0], row[1], row[2]) for row in cur.fetchall()]

    def matches_for_users(
        self,
        new_papers: list,      # list[Paper]
        probe_hits: list,      # list[ProbeHit]
    ) -> dict[str, "PerUserMatches"]:
        """Compute per-user matched papers and probe hits.

        Returns a dict keyed by ``slack_user_id``.  Only users with at least
        one match appear in the result.
        """
        from .monitor import PerUserMatches  # local import to avoid circular

        all_entries = self._get_all_entries()
        if not all_entries:
            return {}

        # Build per-user lookup structures
        user_authors: dict[str, list[str]] = {}
        user_papers: dict[str, set[int]] = {}
        for uid, entry, etype in all_entries:
            if etype == "author":
                user_authors.setdefault(uid, []).append(entry)
            else:
                try:
                    user_papers.setdefault(uid, set()).add(int(entry))
                except ValueError:
                    pass

        all_users = set(user_authors) | set(user_papers)
        result: dict[str, PerUserMatches] = {}

        for uid in all_users:
            authors = user_authors.get(uid, [])
            paper_nums = user_papers.get(uid, set())

            matched_papers: list = []
            for paper in new_papers:
                # Author match
                if authors and paper.author:
                    author_lower = paper.author.lower()
                    if any(a in author_lower for a in authors):
                        matched_papers.append((paper, "author"))
                        continue
                # Paper-number match
                if paper_nums and paper.number is not None and paper.number in paper_nums:
                    matched_papers.append((paper, "paper"))

            matched_hits: list = []
            for hit in probe_hits:
                # Author match via front_text
                if authors and hit.front_text:
                    text_lower = hit.front_text.lower()
                    if any(a in text_lower for a in authors):
                        matched_hits.append((hit, "author"))
                        continue
                # Paper-number match via probe hit number
                if paper_nums and hit.number in paper_nums:
                    matched_hits.append((hit, "paper"))

            if matched_papers or matched_hits:
                result[uid] = PerUserMatches(
                    papers=matched_papers,
                    probe_hits=matched_hits,
                )

        return result
