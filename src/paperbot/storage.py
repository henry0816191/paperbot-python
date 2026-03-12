from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


# ── JSON Cache ──────────────────────────────────────────────────────────────

class JsonCache:
    """JSON file cache with TTL and atomic writes."""

    def __init__(self, path: Path, ttl_hours: float = 1.0):
        self.path = path
        self.ttl_seconds = ttl_hours * 3600

    def is_fresh(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
            return age < self.ttl_seconds
        except FileNotFoundError:
            return False

    def read(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to read cache %s: %s", self.path, exc)
            return None

    def read_if_fresh(self) -> dict | None:
        if self.is_fresh():
            return self.read()
        return None

    def write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


# ── Probe State ─────────────────────────────────────────────────────────────

class ProbeState:
    """Persists probe results: discovered URLs, miss counters, last poll time.

    Schema for each entry in ``discovered``:
        {
          "last_modified": float | null,   # server Last-Modified as Unix timestamp
          "discovered_at": float           # our wall-clock time when first found
        }

    Older entries written as bare floats are migrated transparently on load:
    the float is treated as ``discovered_at`` with ``last_modified`` set to null.
    """

    def __init__(self, path: Path):
        self._cache = JsonCache(path, ttl_hours=float("inf"))
        raw: dict = self._cache.read() or {}
        self._data: dict = {
            "discovered": raw.get("discovered", {}),
            "miss_counts": raw.get("miss_counts", {}),
            "last_poll": raw.get("last_poll", 0.0),
        }
        self._migrate()

    def _migrate(self) -> None:
        """Upgrade any bare-float discovered entries to the current dict schema."""
        for url, val in list(self._data["discovered"].items()):
            if not isinstance(val, dict):
                self._data["discovered"][url] = {
                    "last_modified": None,
                    "discovered_at": float(val),
                }

    @property
    def discovered(self) -> dict[str, dict]:
        return self._data.setdefault("discovered", {})

    @property
    def miss_counts(self) -> dict[str, int]:
        return self._data.setdefault("miss_counts", {})

    @property
    def last_poll(self) -> float:
        return self._data.get("last_poll", 0.0)

    def mark_discovered(self, url: str, last_modified_ts: float | None = None) -> None:
        """Record *url* as discovered.

        *last_modified_ts* should be the server's ``Last-Modified`` header value
        converted to a Unix timestamp.  When absent, the field is stored as null.
        This is intentionally separate from ``discovered_at`` (our own wall-clock).
        """
        if url not in self.discovered:
            self.discovered[url] = {
                "last_modified": last_modified_ts,
                "discovered_at": time.time(),
            }

    def is_discovered(self, url: str) -> bool:
        return url in self.discovered

    def discovered_info(self, url: str) -> dict | None:
        """Return the stored metadata dict for *url*, or None if not recorded."""
        return self.discovered.get(url)

    def record_miss(self, paper_num: str) -> None:
        self.miss_counts[paper_num] = self.miss_counts.get(paper_num, 0) + 1

    def reset_misses(self, paper_num: str) -> None:
        self.miss_counts.pop(paper_num, None)

    def get_miss_count(self, paper_num: str) -> int:
        return self.miss_counts.get(paper_num, 0)

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

    def touch_poll(self) -> None:
        self._data["last_poll"] = time.time()

    def save(self) -> None:
        self._cache.write(self._data)
