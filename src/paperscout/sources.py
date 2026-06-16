"""WG21 index fetch/cache and async HTTP probing of isocpp.org drafts."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType

import httpx

from .config import Settings, settings
from .errors import FailureCategory
from .models import CycleResult, CycleStatus, Paper, ProbeHit, Tier
from .protocols import SOURCE_ISO_PROBE, SOURCE_OPEN_STD, SOURCE_WG21_INDEX
from .storage import PaperCache, ProbeState, UserWatchlist

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# WG21 Index
# ═══════════════════════════════════════════════════════════════════════════

WG21_INDEX_URL = "https://wg21.link/index.json"


class WG21Index:
    """Fetch, cache, and parse the wg21.link paper index.

    This class is designed for single-threaded async use. Do not access
    from the Bolt daemon thread.

    Likewise, do not access from the MessageQueue sender thread or any
    other sync worker — ``refresh()`` mutates internal state and would
    race with cross-thread reads on ``papers`` / ``_max_rev``.
    """

    source_id: str = SOURCE_WG21_INDEX

    def __init__(self, pool, cfg: Settings | None = None):
        self._cfg = cfg or settings
        self._cache = PaperCache(pool, ttl_hours=self._cfg.cache_ttl_hours)
        # Replaced wholesale on every refresh(); never mutate in place.
        self.papers: dict[str, Paper] = {}
        self._max_rev: dict[int, int] = {}  # P-number -> highest revision
        self._max_p: int = 0  # absolute highest P-number
        self._sorted_p_nums: list[int] = []  # sorted unique P-numbers, for gap analysis

    async def refresh(self) -> dict[str, Paper]:
        """Load index from cache or network; populate ``self.papers``."""
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
            log.warning(
                "INDEX-STALE-FALLBACK  entries=%d  (download failed; using persisted cache)",
                len(stale),
            )
            self.papers = self._parse_and_index(stale)
            return self.papers

        log.error("No index data available")
        return self.papers

    async def _download(self) -> dict | None:
        timeout = httpx.Timeout(self._cfg.wg21_index_timeout_s)
        try:
            async with httpx.AsyncClient(
                http2=self._cfg.http_use_http2,
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                resp = await client.get(WG21_INDEX_URL)
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, dict):
                    return data
                log.warning("Index response is not a dict")
                return None
        except httpx.TimeoutException as exc:
            log.warning(
                "INDEX-FETCH  failure_category=%s  url=%s  %s",
                FailureCategory.TIMEOUT.value,
                WG21_INDEX_URL,
                exc,
            )
            return None
        except httpx.HTTPStatusError as exc:
            cat = (
                FailureCategory.RATE_LIMIT
                if exc.response.status_code == 429
                else FailureCategory.NETWORK
            )
            log.error(
                "INDEX-FETCH  failure_category=%s  url=%s  status=%d",
                cat.value,
                WG21_INDEX_URL,
                exc.response.status_code,
            )
            return None
        except (httpx.HTTPError, ValueError) as exc:
            log.error(
                "INDEX-FETCH  failure_category=%s  url=%s  %s",
                FailureCategory.NETWORK.value,
                WG21_INDEX_URL,
                exc,
            )
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

    def effective_frontier(
        self,
        gap_threshold: int = 50,
        extra_p_numbers: Iterable[int] | None = None,
    ) -> int:
        """Largest P-number in the “main cluster”, ignoring huge gaps/outliers."""
        nums_set = set(self._max_rev.keys())
        if extra_p_numbers:
            nums_set |= set(extra_p_numbers)
        nums = sorted(nums_set)
        if not nums:
            return 0
        for i in range(len(nums) - 1, 0, -1):
            if nums[i] - nums[i - 1] <= gap_threshold:
                return nums[i]
        return nums[0]

    def get_max_revision(self, paper_number: int) -> int | None:
        """Highest revision *R*n* seen in the index for P-number *paper_number*.

        Returns ``None`` if the number is unknown or only a sentinel ``-1``
        revision is recorded (treated as no published revision in the index).
        """
        rev = self._max_rev.get(paper_number)
        return rev if rev is not None and rev >= 0 else None

    def known_p_numbers(self) -> set[int]:
        """Set of all P-numbers present in the wg21.link index."""
        return set(self._max_rev.keys())

    def get_known_paper_ids(self) -> frozenset[str]:
        """Immutable snapshot of all paper ids currently in the index."""
        return frozenset(self.papers)

    def get_papers_snapshot(self) -> Mapping[str, Paper]:
        """Read-only mapping copy of ``papers`` (not the live dict object)."""
        return MappingProxyType(dict(self.papers))

    async def fetch(self) -> dict[str, Paper]:
        """DataSource: load index and return the current paper map."""
        return await self.refresh()

    def diff(
        self,
        previous: dict[str, Paper] | None,
        current: dict[str, Paper],
    ):
        """DataSource: compare two index snapshots."""
        from .monitor import _diff_paper_maps

        return _diff_paper_maps(previous or {}, current)


# ═══════════════════════════════════════════════════════════════════════════
# ISO Paper Prober
# ═══════════════════════════════════════════════════════════════════════════

ISO_BASE = "https://isocpp.org/files/papers/"


_TAG_RE = re.compile(r"<[^>]+>")
_PDF_MAX_BYTES = 2 * 1024 * 1024  # 2 MB cap to avoid huge downloads


async def _fetch_pdf_text(client: httpx.AsyncClient, pdf_url: str) -> str:
    """First ~1000 words from a PDF via PyMuPDF, or empty if unavailable."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        log.debug("PyMuPDF not installed; skipping PDF text extraction for %s", pdf_url)
        return ""
    try:
        chunks: list[bytes] = []
        total = 0
        async with client.stream("GET", pdf_url, timeout=30.0) as resp:
            if resp.status_code != 200:
                return ""
            async for chunk in resp.aiter_bytes(65536):
                chunks.append(chunk)
                total += len(chunk)
                if total >= _PDF_MAX_BYTES:
                    log.debug("PDF truncated at %d bytes for %s", total, pdf_url)
                    break
        content = b"".join(chunks)
        doc = fitz.open(stream=content, filetype="pdf")
        words: list[str] = []
        for page in doc:
            words.extend(page.get_text().split())
            if len(words) >= 1000:
                break
        doc.close()
        return " ".join(words[:1000])
    except Exception as exc:
        log.debug("Failed to extract PDF text from %s: %s", pdf_url, exc)
        return ""


async def _fetch_front_text(
    client: httpx.AsyncClient,
    prefix: str,
    number: int,
    revision: int,
) -> str:
    """Opening text from HTML if present, else from PDF (for watchlist author match)."""
    html_url = f"{ISO_BASE}{prefix}{number:04d}R{revision}.html"
    try:
        resp = await client.get(html_url, timeout=15.0)
        if resp.status_code == 200:
            raw = resp.text[:30_000]
            plain = _TAG_RE.sub(" ", raw)
            words = plain.split()[:1000]
            return " ".join(words)
    except Exception as exc:
        log.debug("Failed to fetch front text from %s: %s", html_url, exc)

    # HTML not available — fall back to PDF text extraction
    pdf_url = f"{ISO_BASE}{prefix}{number:04d}R{revision}.pdf"
    return await _fetch_pdf_text(client, pdf_url)


# ── Probe-list entry type ────────────────────────────────────────────────────
# (url, tier, prefix, number, revision, extension)
_Entry = tuple[str, Tier, str, int, int, str]


class ISOProber:
    """Async HEAD probe of isocpp draft URLs: hot every cycle, cold in rotating slices.

    Designed for single-threaded async use on the event loop. Do not call from
    threads, ``asyncio.to_thread()``, or thread-pool executors.
    """

    source_id: str = SOURCE_ISO_PROBE

    # Keys that _stats is reset to at the start of every run_cycle().
    _STATS_TEMPLATE: dict[str, int] = {
        "skipped_discovered": 0,  # URL already in probe_state
        "skipped_in_index": 0,  # paper_id already in wg21.link index
        "miss": 0,  # server returned non-200
        "hit_recent": 0,  # 200 + Last-Modified within alert window
        "hit_old": 0,  # 200 + Last-Modified outside alert window
        "hit_no_lm": 0,  # 200 + no or unusable Last-Modified (treated as recent)
        "error": 0,  # httpx / network exception
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
        self._stats_lock = threading.Lock()
        # This dict is mutated from async coroutines on the event loop.
        # Thread-safety depends on asyncio cooperative scheduling. Do NOT access
        # from asyncio.to_thread() or thread pool executors.
        self._stats: dict[str, int] = dict(self._STATS_TEMPLATE)

    def _bump_stat(self, key: str, n: int = 1) -> None:
        with self._stats_lock:
            self._stats[key] += n

    def _reset_stats(self) -> None:
        with self._stats_lock:
            self._stats = dict(self._STATS_TEMPLATE)

    def snapshot_stats(self) -> dict[str, int]:
        """Return a copy of per-cycle probe counters (lock-protected)."""
        with self._stats_lock:
            return dict(self._stats)

    async def fetch(self) -> CycleResult:
        """DataSource: run one probe cycle."""
        return await self.run_cycle()

    def diff(
        self,
        previous: CycleResult | None,
        current: CycleResult,
    ) -> list[ProbeHit]:
        """DataSource: extract hits from the current probe cycle.

        Probe diff is not snapshot comparison like WG21; ``previous`` is ignored.
        """
        del previous
        if current.status is not CycleStatus.SUCCESS:
            return []
        return list(current.hits)

    # ── Public API ───────────────────────────────────────────────────────────

    async def run_cycle(self) -> CycleResult:
        """HEAD all scheduled URLs; return discriminated cycle outcome.

        Per-URL ``error`` stats alone do not fail the cycle if the cycle
        finished and persisted state; only cycle-level failures return ``FAILED``.
        """
        self._cycle += 1
        self._reset_stats()
        t0 = time.monotonic()
        urls: list[_Entry] = []
        hot_count = 0
        cold_count = 0

        try:
            urls = self._build_probe_list()
            known_ids = self.index.get_known_paper_ids()
            hot_count = sum(1 for u in urls if u[1] in (Tier.WATCHLIST, Tier.FRONTIER, Tier.RECENT))
            cold_count = sum(1 for u in urls if u[1] == Tier.COLD)
            slice_idx = (self._cycle - 1) % self.cfg.cold_cycle_divisor
            log.info(
                "PROBE-START  cycle=%d  total=%d  hot=%d  cold=%d  slice=%d/%d",
                self._cycle,
                len(urls),
                hot_count,
                cold_count,
                slice_idx,
                self.cfg.cold_cycle_divisor,
            )

            sem = asyncio.Semaphore(self.cfg.http_concurrency)
            hits: list[ProbeHit] = []

            async with httpx.AsyncClient(
                http2=self.cfg.http_use_http2,
                timeout=self.cfg.http_timeout_seconds,
                follow_redirects=True,
            ) as client:
                tasks = [
                    self._probe_one(client, sem, url, prefix, num, rev, ext, tier, known_ids)
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
        except Exception as exc:
            elapsed = time.monotonic() - t0
            log.error(
                "PROBE-FAILED  cycle=%d  elapsed=%.1fs  error=%s",
                self._cycle,
                elapsed,
                exc,
            )
            return CycleResult(CycleStatus.FAILED, error=str(exc))

        elapsed = time.monotonic() - t0
        s = self.snapshot_stats()
        hit_total = s["hit_recent"] + s["hit_old"] + s["hit_no_lm"]
        log.info(
            "PROBE-DONE  cycle=%d  elapsed=%.1fs  total=%d  "
            "hit=%d(recent=%d old=%d no-lm=%d)  miss=%d  "
            "skip-disc=%d  skip-idx=%d  err=%d",
            self._cycle,
            elapsed,
            len(urls),
            hit_total,
            s["hit_recent"],
            s["hit_old"],
            s["hit_no_lm"],
            s["miss"],
            s["skipped_discovered"],
            s["skipped_in_index"],
            s["error"],
        )

        if hits:
            status = CycleStatus.SUCCESS
            log.info("PROBE-SUCCESS  cycle=%d  hits=%d", self._cycle, len(hits))
        else:
            status = CycleStatus.EMPTY
            log.info(
                "PROBE-EMPTY  cycle=%d  total=%d  err=%d",
                self._cycle,
                len(urls),
                s["error"],
            )

        log.info(
            "PROBE-CYCLE-SUMMARY %s",
            json.dumps(
                {
                    "cycle": self._cycle,
                    "cycle_status": status.value,
                    "cycle_requests": len(urls),
                    "cycle_duration_s": round(elapsed, 2),
                    "hot_probes": hot_count,
                    "cold_probes": cold_count,
                    "errors": s["error"],
                    "hit_total": hit_total,
                    "hit_recent": s["hit_recent"],
                    "hit_old": s["hit_old"],
                    "hit_no_lm": s["hit_no_lm"],
                    "miss": s["miss"],
                    "skipped_discovered": s["skipped_discovered"],
                    "skipped_in_index": s["skipped_in_index"],
                }
            ),
        )
        if status == CycleStatus.SUCCESS:
            return CycleResult(CycleStatus.SUCCESS, results=tuple(hits))
        return CycleResult(CycleStatus.EMPTY)

    # ── Probe-list builders ──────────────────────────────────────────────────

    def _build_probe_list(self) -> list[_Entry]:
        frontier = self.index.effective_frontier(
            self.cfg.frontier_gap_threshold,
            extra_p_numbers=self.state.paper_nums_from_discovered_iso_urls(),
        )
        hot_known, hot_unknown = self._hot_numbers(frontier)
        return self._build_hot_list(frontier, hot_known, hot_unknown) + self._build_cold_slice(
            self._cycle, frontier, hot_known, hot_unknown
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
            cutoff = date.today() - timedelta(days=int(self.cfg.hot_lookback_months * 30.44))
            for p in self.index.get_papers_snapshot().values():
                if p.prefix != "P" or p.number is None or not p.date or p.date == "unknown":
                    continue
                try:
                    if date.fromisoformat(p.date[:10]) >= cutoff:
                        hot.add(p.number)
                except ValueError:
                    continue

        known_p_nums = self.index.known_p_numbers()
        return hot & known_p_nums, hot - known_p_nums

    def _tier_label(self, num: int, watchlist_set: set[int], frontier_range: set[int]) -> Tier:
        if num in watchlist_set:
            return Tier.WATCHLIST
        if num in frontier_range:
            return Tier.FRONTIER
        return Tier.RECENT

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
            latest = self.index.get_max_revision(num)
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
                        results.append((url, Tier.FRONTIER, prefix, num, rev, ext))

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

        known_p_nums = self.index.known_p_numbers()
        cold_known = known_p_nums - hot_known
        all_active = set(range(1, frontier + 1))
        cold_unknown = all_active - known_p_nums - hot_unknown

        # Cold known: D prefix, latest+1 .. latest+cold_revision_depth
        for num in sorted(cold_known):
            if num % self.cfg.cold_cycle_divisor != slice_idx:
                continue
            latest = self.index.get_max_revision(num)
            if latest is None:
                continue
            for rev in range(latest + 1, latest + self.cfg.cold_revision_depth + 1):
                for ext in self.cfg.probe_extensions:
                    url = f"{ISO_BASE}D{num:04d}R{rev}{ext}"
                    results.append((url, Tier.COLD, "D", num, rev, ext))

        # Cold unknown gap numbers: D+P, R0 .. gap_max_rev
        for num in sorted(cold_unknown):
            if num % self.cfg.cold_cycle_divisor != slice_idx:
                continue
            for prefix in self.cfg.probe_prefixes:
                for rev in range(0, self.cfg.gap_max_rev + 1):
                    for ext in self.cfg.probe_extensions:
                        url = f"{ISO_BASE}{prefix}{num:04d}R{rev}{ext}"
                        results.append((url, Tier.COLD, prefix, num, rev, ext))

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
        tier: Tier,
        known_ids: frozenset[str] | None = None,
    ) -> ProbeHit | None:
        if self.state.is_discovered(url):
            log.debug("SKIP  disc  %s", url)
            self._bump_stat("skipped_discovered")
            return None
        paper_id = f"{prefix}{num:04d}R{rev}"
        ids = known_ids if known_ids is not None else self.index.get_known_paper_ids()
        if paper_id in ids:
            log.debug("SKIP  idx   %s", paper_id)
            self._bump_stat("skipped_in_index")
            return None
        async with sem:
            _max_retries = 3
            for _attempt in range(_max_retries):
                try:
                    resp = await client.head(url)
                    break
                except httpx.HTTPError as exc:
                    if _attempt < _max_retries - 1:
                        await asyncio.sleep(0.5 * (2**_attempt))
                        continue
                    cat = (
                        FailureCategory.TIMEOUT
                        if isinstance(exc, httpx.TimeoutException)
                        else FailureCategory.NETWORK
                    )
                    log.debug(
                        "PROBE-ERR  failure_category=%s  url=%s  %s",
                        cat.value,
                        url,
                        exc,
                    )
                    self._bump_stat("error")
                    return None
            else:
                return None

            if resp.status_code != 200:
                log.debug("MISS  %d  %s", resp.status_code, url)
                self._bump_stat("miss")
                return None

            # Determine recency from the Last-Modified response header.
            last_modified: datetime | None = None
            is_recent = False
            lm_str = resp.headers.get("last-modified")
            if lm_str:
                try:
                    last_modified = parsedate_to_datetime(lm_str)
                    # Naive datetimes from parsedate_to_datetime are UTC.
                    if last_modified.tzinfo is None:
                        last_modified = last_modified.replace(tzinfo=timezone.utc)
                    threshold = timedelta(hours=self.cfg.alert_modified_hours)
                    is_recent = (datetime.now(timezone.utc) - last_modified) <= threshold
                except (TypeError, ValueError):
                    # Bad Last-Modified: merge with no-LM so we don't silently drop hits.
                    last_modified = None
                    is_recent = True
            else:
                # No Last-Modified: first-ever discovery of an untracked
                # file; treat as recent so we don't silently drop it.
                is_recent = True

            lm_display = last_modified.strftime("%Y-%m-%d %H:%M UTC") if last_modified else "no-lm"
            log.info(
                "HIT  tier=%-10s  recent=%-5s  lm=%-20s  %s",
                tier,
                is_recent,
                lm_display,
                url,
            )

            if is_recent and last_modified is not None:
                self._bump_stat("hit_recent")
            elif not is_recent:
                self._bump_stat("hit_old")
            else:
                self._bump_stat("hit_no_lm")

            # Only fetch front text when we intend to alert.
            front_text = ""
            if is_recent:
                front_text = await _fetch_front_text(client, prefix, num, rev)

            return ProbeHit(
                url=url,
                prefix=prefix,
                number=num,
                revision=rev,
                extension=ext,
                tier=tier,
                front_text=front_text,
                last_modified=last_modified,
                is_recent=is_recent,
            )


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
    """One row scraped from open-std.org WG21 paper listings."""

    paper_id: str
    title: str
    author: str
    doc_date: str
    subgroup: str


async def scrape_open_std(year: int | None = None) -> list[OpenStdEntry]:
    """Fetch and parse the open-std.org WG21 papers index page for *year*."""
    year = year or date.today().year
    url = OPEN_STD_URL.format(year=year)
    try:
        async with httpx.AsyncClient(
            http2=settings.http_use_http2,
            timeout=30.0,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return _parse_open_std_html(resp.text)
    except httpx.HTTPError as exc:
        log.error("Failed to scrape open-std.org/%d: %s", year, exc)
        return []


def _parse_open_std_html(html: str) -> list[OpenStdEntry]:
    """Parse WG21 paper listing rows from an open-std.org HTML page."""
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
        entries.append(
            OpenStdEntry(
                paper_id=paper_id,
                title=title,
                author=author,
                doc_date=doc_date,
                subgroup=subgroup,
            )
        )
    return entries


class OpenStdSource:
    """DataSource wrapper for the open-std.org yearly paper table scraper."""

    source_id: str = SOURCE_OPEN_STD

    def __init__(self, year: int | None = None):
        self._year = year

    async def fetch(self) -> list[OpenStdEntry]:
        return await scrape_open_std(self._year)

    def diff(
        self,
        previous: list[OpenStdEntry] | None,
        current: list[OpenStdEntry],
    ) -> list[OpenStdEntry]:
        if not previous:
            return list(current)
        prev_ids = {e.paper_id for e in previous}
        return [e for e in current if e.paper_id not in prev_ids]
