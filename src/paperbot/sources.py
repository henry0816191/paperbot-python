from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import httpx

from .config import Settings, settings
from .models import Paper
from .storage import PaperCache, ProbeState, UserWatchlist

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# WG21 Index
# ═══════════════════════════════════════════════════════════════════════════

WG21_INDEX_URL = "https://wg21.link/index.json"


class WG21Index:
    """Fetch, cache, and parse the wg21.link paper index."""

    def __init__(self, pool):
        self._cache = PaperCache(pool, ttl_hours=settings.cache_ttl_hours)
        self.papers: dict[str, Paper] = {}
        self._max_rev: dict[int, int] = {}   # P-number -> highest revision
        self._max_p: int = 0                  # absolute highest P-number
        self._sorted_p_nums: list[int] = []   # sorted unique P-numbers, for gap analysis

    async def refresh(self) -> dict[str, Paper]:
        cached = self._cache.read_if_fresh()
        if cached is not None:
            log.info("Loaded %d entries from cache", len(cached))
            self.papers = self._parse_and_index(cached)
            return self.papers

        raw = await self._download()
        if raw is not None:
            self._cache.write(raw)
            log.info("Downloaded and cached %d entries", len(raw))
            self.papers = self._parse_and_index(raw)
            return self.papers

        stale = self._cache.read()
        if stale is not None:
            log.warning("Using stale cache (%d entries)", len(stale))
            self.papers = self._parse_and_index(stale)
            return self.papers

        log.error("No index data available")
        return self.papers

    async def _download(self) -> dict | None:
        try:
            async with httpx.AsyncClient(
                http2=settings.http_use_http2,
                timeout=30.0,
                follow_redirects=True,
            ) as client:
                resp = await client.get(WG21_INDEX_URL)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict):
                    return data
                log.warning("Index response is not a dict")
                return None
        except (httpx.HTTPError, ValueError) as exc:
            log.error("Failed to download index: %s", exc)
            return None

    def _parse_and_index(self, raw: dict) -> dict[str, Paper]:
        papers: dict[str, Paper] = {}
        max_rev: dict[int, int] = {}
        max_p = 0
        for key, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            paper = Paper.from_index_entry(key, entry)
            papers[key] = paper
            if paper.prefix == "P" and paper.number is not None:
                max_p = max(max_p, paper.number)
                if paper.revision is not None:
                    prev = max_rev.get(paper.number, -1)
                    if paper.revision > prev:
                        max_rev[paper.number] = paper.revision
        self._max_rev = max_rev
        self._max_p = max_p
        self._sorted_p_nums = sorted(max_rev.keys())
        return papers

    def highest_p_number(self) -> int:
        """Absolute highest P-number in the index (may include outliers)."""
        return self._max_p

    def effective_frontier(self, gap_threshold: int = 50) -> int:
        """Highest P-number in the main cluster of active papers.

        Walks backward from the absolute highest P-number and stops at the
        first number whose gap to its predecessor is within *gap_threshold*.
        This filters out isolated high-numbered outliers (e.g. a pre-assigned
        planning document at P5000 when active work is around P4030) that
        would otherwise push the frontier window far above actual activity.
        """
        nums = self._sorted_p_nums
        if not nums:
            return 0
        for i in range(len(nums) - 1, 0, -1):
            if nums[i] - nums[i - 1] <= gap_threshold:
                return nums[i]
        return nums[0]

    def latest_revision(self, number: int) -> int | None:
        rev = self._max_rev.get(number)
        return rev if rev is not None and rev >= 0 else None


# ═══════════════════════════════════════════════════════════════════════════
# ISO Paper Prober
# ═══════════════════════════════════════════════════════════════════════════

ISO_BASE = "https://isocpp.org/files/papers/"


@dataclass(slots=True)
class ProbeHit:
    url: str
    prefix: str
    number: int
    revision: int
    extension: str
    # Tier labels: "watchlist" | "frontier" | "recent" | "cold"
    tier: str
    front_text: str = ""
    last_modified: datetime | None = field(default=None)
    # True when Last-Modified is within alert_modified_hours of now,
    # or when the header is absent (first-ever discovery of a new file).
    is_recent: bool = False


_TAG_RE = re.compile(r"<[^>]+>")


async def _fetch_front_text(
    client: httpx.AsyncClient,
    prefix: str,
    number: int,
    revision: int,
) -> str:
    """GET the HTML version of a paper and return the first ~1 000 words as plain text."""
    html_url = f"{ISO_BASE}{prefix}{number:04d}R{revision}.html"
    try:
        resp = await client.get(html_url, timeout=15.0)
        if resp.status_code != 200:
            return ""
        raw = resp.text[:30_000]
        plain = _TAG_RE.sub(" ", raw)
        words = plain.split()[:1000]
        return " ".join(words)
    except (httpx.HTTPError, Exception) as exc:
        log.debug("Failed to fetch front text from %s: %s", html_url, exc)
        return ""


# ── Probe-list entry type ────────────────────────────────────────────────────
# (url, tier, prefix, number, revision, extension)
_Entry = tuple[str, str, str, int, int, str]


class ISOProber:
    """Two-frequency async HEAD prober for isocpp.org/files/papers/.

    Hot list (every cycle, ~30 min):
      • Watchlist papers
      • Frontier window around the effective-frontier P-number
      • Papers active within hot_lookback_months

    Cold list (distributed across cold_cycle_divisor cycles ≈ 24 h):
      • Every other known P-number (probe for the next unpublished draft)
      • Every gap number in 1..frontier (may be untracked new assignments)

    Alerting is driven by the HTTP Last-Modified response header rather than
    our own discovery state.  A hit is flagged is_recent=True when the server
    reports the file was modified within alert_modified_hours of now, ensuring
    we only notify about genuinely new or updated drafts.
    """

    # Keys that _stats is reset to at the start of every run_cycle().
    _STATS_TEMPLATE: dict[str, int] = {
        "skipped_discovered": 0,  # URL already in probe_state
        "skipped_in_index":   0,  # paper_id already in wg21.link index
        "miss":               0,  # server returned non-200
        "hit_recent":         0,  # 200 + Last-Modified within alert window
        "hit_old":            0,  # 200 + Last-Modified outside alert window
        "hit_no_lm":          0,  # 200 + no Last-Modified header (treated as recent)
        "error":              0,  # httpx / network exception
    }

    def __init__(
        self,
        index: WG21Index,
        state: ProbeState,
        user_watchlist: UserWatchlist,
        cfg: Settings | None = None,
    ):
        self.index = index
        self.state = state
        self.user_watchlist = user_watchlist
        self.cfg = cfg or settings
        self._cycle = 0
        self._stats: dict[str, int] = dict(self._STATS_TEMPLATE)

    # ── Public API ───────────────────────────────────────────────────────────

    async def run_cycle(self) -> list[ProbeHit]:
        self._cycle += 1
        self._stats = dict(self._STATS_TEMPLATE)
        t0 = time.monotonic()

        urls = self._build_probe_list()
        hot_count = sum(1 for u in urls if u[1] in ("watchlist", "frontier", "recent"))
        cold_count = sum(1 for u in urls if u[1] == "cold")
        slice_idx = (self._cycle - 1) % self.cfg.cold_cycle_divisor
        log.info(
            "PROBE-START  cycle=%d  total=%d  hot=%d  cold=%d  slice=%d/%d",
            self._cycle, len(urls), hot_count, cold_count,
            slice_idx, self.cfg.cold_cycle_divisor,
        )

        sem = asyncio.Semaphore(self.cfg.http_concurrency)
        hits: list[ProbeHit] = []

        async with httpx.AsyncClient(
            http2=self.cfg.http_use_http2,
            timeout=self.cfg.http_timeout_seconds,
            follow_redirects=True,
        ) as client:
            tasks = [
                self._probe_one(client, sem, url, prefix, num, rev, ext, tier)
                for url, tier, prefix, num, rev, ext in urls
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, ProbeHit):
                    hits.append(r)
                elif isinstance(r, Exception):
                    log.debug("Unhandled exception from _probe_one: %s", r)

        for hit in hits:
            lm_ts = hit.last_modified.timestamp() if hit.last_modified else None
            self.state.mark_discovered(hit.url, last_modified_ts=lm_ts)

        self.state.touch_poll()
        self.state.save()

        elapsed = time.monotonic() - t0
        s = self._stats
        hit_total = s["hit_recent"] + s["hit_old"] + s["hit_no_lm"]
        log.info(
            "PROBE-DONE  cycle=%d  elapsed=%.1fs  total=%d  "
            "hit=%d(recent=%d old=%d no-lm=%d)  miss=%d  "
            "skip-disc=%d  skip-idx=%d  err=%d",
            self._cycle, elapsed, len(urls),
            hit_total, s["hit_recent"], s["hit_old"], s["hit_no_lm"],
            s["miss"], s["skipped_discovered"], s["skipped_in_index"], s["error"],
        )
        return hits

    # ── Probe-list builders ──────────────────────────────────────────────────

    def _build_probe_list(self) -> list[_Entry]:
        frontier = self.index.effective_frontier(self.cfg.frontier_gap_threshold)
        hot_known, hot_unknown = self._hot_numbers(frontier)
        return (
            self._build_hot_list(frontier, hot_known, hot_unknown)
            + self._build_cold_slice(self._cycle, frontier, hot_known, hot_unknown)
        )

    def _hot_numbers(self, frontier: int) -> tuple[set[int], set[int]]:
        """Return (known_hot, unknown_hot) P-number sets to probe every cycle."""
        hot: set[int] = set()

        # Watchlist papers (union across all users)
        hot.update(self.user_watchlist.get_all_watched_paper_nums())

        # Frontier window
        lo = max(1, frontier - self.cfg.frontier_window_below + 1)
        hi = frontier + self.cfg.frontier_window_above
        hot.update(range(lo, hi + 1))
        for r in self.cfg.frontier_explicit_ranges:
            hot.update(range(r.get("min", 0), r.get("max", 0) + 1))

        # Recently active papers
        if self.cfg.hot_lookback_months > 0:
            cutoff = date.today() - timedelta(
                days=int(self.cfg.hot_lookback_months * 30.44)
            )
            for p in self.index.papers.values():
                if p.prefix != "P" or p.number is None or not p.date or p.date == "unknown":
                    continue
                try:
                    if date.fromisoformat(p.date[:10]) >= cutoff:
                        hot.add(p.number)
                except ValueError:
                    continue

        known_p_nums = set(self.index._max_rev.keys())
        return hot & known_p_nums, hot - known_p_nums

    def _tier_label(self, num: int, watchlist_set: set[int], frontier_range: set[int]) -> str:
        if num in watchlist_set:
            return "watchlist"
        if num in frontier_range:
            return "frontier"
        return "recent"

    def _build_hot_list(
        self,
        frontier: int,
        hot_known: set[int],
        hot_unknown: set[int],
    ) -> list[_Entry]:
        results: list[_Entry] = []
        watchlist_set = self.user_watchlist.get_all_watched_paper_nums()
        lo = max(1, frontier - self.cfg.frontier_window_below + 1)
        hi = frontier + self.cfg.frontier_window_above
        frontier_range: set[int] = set(range(lo, hi + 1))
        for r in self.cfg.frontier_explicit_ranges:
            frontier_range.update(range(r.get("min", 0), r.get("max", 0) + 1))

        # Known hot: probe D prefix, latest+1 .. latest+hot_revision_depth
        for num in sorted(hot_known):
            tier = self._tier_label(num, watchlist_set, frontier_range)
            latest = self.index.latest_revision(num)
            if latest is None:
                latest = -1
            for rev in range(latest + 1, latest + self.cfg.hot_revision_depth + 1):
                for ext in self.cfg.probe_extensions:
                    url = f"{ISO_BASE}D{num:04d}R{rev}{ext}"
                    results.append((url, tier, "D", num, rev, ext))

        # Unknown hot (frontier gaps): probe D+P, R0 .. gap_max_rev
        for num in sorted(hot_unknown):
            for prefix in self.cfg.probe_prefixes:
                for rev in range(0, self.cfg.gap_max_rev + 1):
                    for ext in self.cfg.probe_extensions:
                        url = f"{ISO_BASE}{prefix}{num:04d}R{rev}{ext}"
                        results.append((url, "frontier", prefix, num, rev, ext))

        return results

    def _build_cold_slice(
        self,
        cycle: int,
        frontier: int,
        hot_known: set[int],
        hot_unknown: set[int],
    ) -> list[_Entry]:
        """Return the 1/cold_cycle_divisor slice of cold numbers for this cycle."""
        slice_idx = (cycle - 1) % self.cfg.cold_cycle_divisor
        results: list[_Entry] = []

        known_p_nums = set(self.index._max_rev.keys())
        cold_known = known_p_nums - hot_known
        all_active = set(range(1, frontier + 1))
        cold_unknown = all_active - known_p_nums - hot_unknown

        # Cold known: D prefix, latest+1 .. latest+cold_revision_depth
        for num in sorted(cold_known):
            if num % self.cfg.cold_cycle_divisor != slice_idx:
                continue
            latest = self.index.latest_revision(num)
            if latest is None:
                continue
            for rev in range(latest + 1, latest + self.cfg.cold_revision_depth + 1):
                for ext in self.cfg.probe_extensions:
                    url = f"{ISO_BASE}D{num:04d}R{rev}{ext}"
                    results.append((url, "cold", "D", num, rev, ext))

        # Cold unknown gap numbers: D+P, R0 .. gap_max_rev
        for num in sorted(cold_unknown):
            if num % self.cfg.cold_cycle_divisor != slice_idx:
                continue
            for prefix in self.cfg.probe_prefixes:
                for rev in range(0, self.cfg.gap_max_rev + 1):
                    for ext in self.cfg.probe_extensions:
                        url = f"{ISO_BASE}{prefix}{num:04d}R{rev}{ext}"
                        results.append((url, "cold", prefix, num, rev, ext))

        return results

    # ── Single-URL probe ─────────────────────────────────────────────────────

    async def _probe_one(
        self,
        client: httpx.AsyncClient,
        sem: asyncio.Semaphore,
        url: str,
        prefix: str,
        num: int,
        rev: int,
        ext: str,
        tier: str,
    ) -> ProbeHit | None:
        if self.state.is_discovered(url):
            log.debug("SKIP  disc  %s", url)
            self._stats["skipped_discovered"] += 1
            return None
        paper_id = f"{prefix}{num:04d}R{rev}"
        if paper_id in self.index.papers:
            log.debug("SKIP  idx   %s", paper_id)
            self._stats["skipped_in_index"] += 1
            return None
        async with sem:
            try:
                resp = await client.head(url)
                if resp.status_code != 200:
                    log.debug("MISS  %d  %s", resp.status_code, url)
                    self._stats["miss"] += 1
                    return None

                # Determine recency from the Last-Modified response header.
                last_modified: datetime | None = None
                is_recent = False
                lm_str = resp.headers.get("last-modified")
                if lm_str:
                    try:
                        last_modified = parsedate_to_datetime(lm_str)
                        threshold = timedelta(hours=self.cfg.alert_modified_hours)
                        is_recent = (
                            datetime.now(timezone.utc) - last_modified
                        ) <= threshold
                    except Exception:
                        pass
                else:
                    # No Last-Modified: first-ever discovery of an untracked
                    # file; treat as recent so we don't silently drop it.
                    is_recent = True

                lm_display = (
                    last_modified.strftime("%Y-%m-%d %H:%M UTC")
                    if last_modified else "no-lm"
                )
                log.info(
                    "HIT  tier=%-10s  recent=%-5s  lm=%-20s  %s",
                    tier, is_recent, lm_display, url,
                )

                if is_recent and last_modified is not None:
                    self._stats["hit_recent"] += 1
                elif not is_recent:
                    self._stats["hit_old"] += 1
                else:
                    self._stats["hit_no_lm"] += 1

                # Only fetch front text when we intend to alert.
                front_text = ""
                if is_recent:
                    front_text = await _fetch_front_text(client, prefix, num, rev)

                return ProbeHit(
                    url=url, prefix=prefix, number=num,
                    revision=rev, extension=ext, tier=tier,
                    front_text=front_text,
                    last_modified=last_modified,
                    is_recent=is_recent,
                )
            except httpx.HTTPError as exc:
                log.debug("ERR   %s  %s", url, exc)
                self._stats["error"] += 1
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Open-std.org Scraper (optional)
# ═══════════════════════════════════════════════════════════════════════════

OPEN_STD_URL = "https://www.open-std.org/jtc1/sc22/wg21/docs/papers/{year}/"
_LINK_RE = re.compile(
    r'<a\s+href="[^"]*"[^>]*>\s*((?:P|N|D)\d+(?:R\d+)?)\s*</a>',
    re.IGNORECASE,
)


@dataclass(slots=True)
class OpenStdEntry:
    paper_id: str
    title: str
    author: str
    doc_date: str
    subgroup: str


async def scrape_open_std(year: int | None = None) -> list[OpenStdEntry]:
    year = year or date.today().year
    url = OPEN_STD_URL.format(year=year)
    try:
        async with httpx.AsyncClient(
            http2=settings.http_use_http2, timeout=30.0,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return _parse_open_std_html(resp.text)
    except httpx.HTTPError as exc:
        log.error("Failed to scrape open-std.org/%d: %s", year, exc)
        return []


def _parse_open_std_html(html: str) -> list[OpenStdEntry]:
    entries: list[OpenStdEntry] = []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL | re.IGNORECASE)
    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL | re.IGNORECASE)
        if len(cells) < 4:
            continue
        link_match = _LINK_RE.search(cells[0])
        if not link_match:
            continue
        paper_id = link_match.group(1).strip()
        title = re.sub(r"<[^>]+>", "", cells[1]).strip()
        author = re.sub(r"<[^>]+>", "", cells[2]).strip()
        doc_date = re.sub(r"<[^>]+>", "", cells[3]).strip()
        subgroup = re.sub(r"<[^>]+>", "", cells[6]).strip() if len(cells) > 6 else ""
        entries.append(OpenStdEntry(
            paper_id=paper_id, title=title, author=author,
            doc_date=doc_date, subgroup=subgroup,
        ))
    return entries
