from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import httpx

from .config import Settings, settings
from .models import Paper
from .storage import JsonCache, ProbeState

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# WG21 Index
# ═══════════════════════════════════════════════════════════════════════════

WG21_INDEX_URL = "https://wg21.link/index.json"


class WG21Index:
    """Fetch, cache, and parse the wg21.link paper index."""

    def __init__(self, data_dir: Path | None = None):
        data_dir = data_dir or settings.data_dir
        self._cache = JsonCache(
            data_dir / "paper_cache.json",
            ttl_hours=settings.cache_ttl_hours,
        )
        self.papers: dict[str, Paper] = {}
        self._max_rev: dict[int, int] = {}   # P-number -> highest revision
        self._max_p: int = 0                  # highest P-number

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
        return papers

    def highest_p_number(self) -> int:
        return self._max_p

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
    tier: str
    front_text: str = ""


_TAG_RE = re.compile(r"<[^>]+>")


async def _fetch_front_text(
    client: httpx.AsyncClient,
    prefix: str,
    number: int,
    revision: int,
) -> str:
    """GET the HTML version of a paper and return the first ~1000 words as plain text."""
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


class ISOProber:
    """Three-tier async HEAD prober for isocpp.org/files/papers/."""

    def __init__(
        self,
        index: WG21Index,
        state: ProbeState,
        cfg: Settings | None = None,
    ):
        self.index = index
        self.state = state
        self.cfg = cfg or settings
        self._cycle = 0

    async def run_cycle(self) -> list[ProbeHit]:
        self._cycle += 1
        urls = self._build_probe_list()
        log.info(
            "Probe cycle %d: %d URLs (A=%d, B=%d, C=%d)",
            self._cycle, len(urls),
            sum(1 for u in urls if u[1] == "A"),
            sum(1 for u in urls if u[1] == "B"),
            sum(1 for u in urls if u[1] == "C"),
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

        for hit in hits:
            self.state.mark_discovered(hit.url)
            self.state.reset_misses(str(hit.number))

        missed: set[str] = set()
        for _, tier, prefix, num, rev, ext in urls:
            key = str(num)
            if not any(h.number == num for h in hits):
                missed.add(key)
        for key in missed:
            self.state.record_miss(key)

        self.state.touch_poll()
        self.state.save()
        return hits

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
            return None
        paper_id = f"{prefix}{num:04d}R{rev}"
        if paper_id in self.index.papers:
            return None
        async with sem:
            try:
                resp = await client.head(url)
                if resp.status_code != 200:
                    return None
                log.info("HIT [%s] %s", tier, url)
                front_text = await _fetch_front_text(client, prefix, num, rev)
                return ProbeHit(
                    url=url, prefix=prefix, number=num,
                    revision=rev, extension=ext, tier=tier,
                    front_text=front_text,
                )
            except httpx.HTTPError as exc:
                log.debug("Error probing %s: %s", url, exc)
        return None

    def _build_probe_list(self) -> list[tuple[str, str, str, int, int, str]]:
        urls: list[tuple[str, str, str, int, int, str]] = []
        urls.extend(self._tier_a())
        urls.extend(self._tier_b())
        urls.extend(self._tier_c())
        return urls

    def _tier_a(self) -> list[tuple[str, str, str, int, int, str]]:
        results: list[tuple[str, str, str, int, int, str]] = []
        for num in self.cfg.watchlist_papers:
            latest = self.index.latest_revision(num)
            for prefix in self.cfg.probe_prefixes:
                for rev in self._revisions_for(latest):
                    for ext in self.cfg.probe_extensions:
                        url = f"{ISO_BASE}{prefix}{num:04d}R{rev}{ext}"
                        results.append((url, "A", prefix, num, rev, ext))
        return results

    def _tier_b(self) -> list[tuple[str, str, str, int, int, str]]:
        results: list[tuple[str, str, str, int, int, str]] = []
        frontier = self.index.highest_p_number()
        numbers: set[int] = set()
        lo = max(1, frontier - self.cfg.frontier_window_below + 1)
        hi = frontier + self.cfg.frontier_window_above
        numbers.update(range(lo, hi + 1))
        for r in self.cfg.frontier_explicit_ranges:
            numbers.update(range(r.get("min", 0), r.get("max", 0) + 1))
        for num in sorted(numbers):
            if self.state.should_skip(
                str(num), self.cfg.backoff_miss_threshold,
                self.cfg.backoff_multiplier, self.cfg.backoff_max_skip, self._cycle,
            ):
                continue
            latest = self.index.latest_revision(num)
            for prefix in self.cfg.probe_prefixes:
                for rev in self._revisions_for(latest):
                    for ext in self.cfg.probe_extensions:
                        url = f"{ISO_BASE}{prefix}{num:04d}R{rev}{ext}"
                        results.append((url, "B", prefix, num, rev, ext))
        return results

    def _tier_c(self) -> list[tuple[str, str, str, int, int, str]]:
        results: list[tuple[str, str, str, int, int, str]] = []
        cutoff = date.today() - timedelta(days=int(self.cfg.tier_c_lookback_months * 30.44))
        seen: set[int] = set()
        for p in self.index.papers.values():
            if p.prefix != "P" or p.number is None or not p.date or p.date == "unknown":
                continue
            try:
                if date.fromisoformat(p.date[:10]) >= cutoff:
                    seen.add(p.number)
            except ValueError:
                continue
        watchlist_set = set(self.cfg.watchlist_papers)
        for num in sorted(seen):
            if num in watchlist_set:
                continue
            if self.state.should_skip(
                str(num), self.cfg.backoff_miss_threshold,
                self.cfg.backoff_multiplier, self.cfg.backoff_max_skip, self._cycle,
            ):
                continue
            latest = self.index.latest_revision(num)
            if latest is None:
                continue
            start_rev = latest + 1
            end_rev = latest + self.cfg.tier_c_revision_depth
            for prefix in self.cfg.tier_c_probe_prefixes:
                for rev in range(start_rev, end_rev + 1):
                    for ext in self.cfg.probe_extensions:
                        url = f"{ISO_BASE}{prefix}{num:04d}R{rev}{ext}"
                        results.append((url, "C", prefix, num, rev, ext))
        return results

    def _revisions_for(self, latest_known: int | None) -> list[int]:
        if latest_known is None:
            return list(range(0, self.cfg.probe_unknown_max_rev + 1))
        return list(range(latest_known, latest_known + self.cfg.probe_revision_depth))


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
