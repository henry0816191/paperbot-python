from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, settings
from .models import Paper
from .sources import ISOProber, ProbeHit, WG21Index
from .storage import ProbeState

log = logging.getLogger(__name__)


# ── Diff Engine ─────────────────────────────────────────────────────────────

@dataclass(slots=True)
class DiffResult:
    new_papers: list[Paper]
    updated_papers: list[Paper]


def diff_snapshots(
    previous: dict[str, Paper],
    current: dict[str, Paper],
) -> DiffResult:
    new_papers: list[Paper] = []
    updated_papers: list[Paper] = []
    prev_keys = set(previous.keys())

    for key, paper in current.items():
        if key not in prev_keys:
            new_papers.append(paper)
        else:
            old = previous[key]
            if (old.title != paper.title or old.author != paper.author
                    or old.date != paper.date or old.long_link != paper.long_link):
                updated_papers.append(paper)

    new_papers.sort(key=lambda p: p.date or "", reverse=True)
    return DiffResult(new_papers=new_papers, updated_papers=updated_papers)


# ── Watchlist ───────────────────────────────────────────────────────────────

class Watchlist:
    """Author watchlist with persistent JSON storage."""

    def __init__(self, path: Path):
        self.path = path
        self._authors: list[str] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._authors = [a.lower() for a in data.get("authors", [])]
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Failed to load watchlist: %s", exc)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"authors": self._authors}, indent=2),
            encoding="utf-8",
        )

    @property
    def authors(self) -> list[str]:
        return list(self._authors)

    def add_author(self, name: str) -> bool:
        key = name.lower().strip()
        if key and key not in self._authors:
            self._authors.append(key)
            self._save()
            return True
        return False

    def remove_author(self, name: str) -> bool:
        key = name.lower().strip()
        if key in self._authors:
            self._authors.remove(key)
            self._save()
            return True
        return False

    def matches(self, paper: Paper) -> list[str]:
        if not paper.author:
            return []
        author_lower = paper.author.lower()
        return [a for a in self._authors if a in author_lower]


# ── Poll Result ─────────────────────────────────────────────────────────────

class PollResult:
    def __init__(
        self,
        diff: DiffResult,
        probe_hits: list[ProbeHit],
        watchlist_matches: list[Paper],
        probe_watchlist_hits: list[ProbeHit] | None = None,
    ):
        self.diff = diff
        self.probe_hits = probe_hits
        self.watchlist_matches = watchlist_matches
        self.probe_watchlist_hits = probe_watchlist_hits or []


# ── Scheduler ───────────────────────────────────────────────────────────────

class Scheduler:
    """Coordinates periodic polling: index refresh + ISO probing + notifications."""

    def __init__(
        self,
        index: WG21Index,
        prober: ISOProber,
        watchlist: Watchlist,
        state: ProbeState,
        cfg: Settings | None = None,
        notify_callback=None,
    ):
        self.index = index
        self.prober = prober
        self.watchlist = watchlist
        self.state = state
        self.cfg = cfg or settings
        self.notify_callback = notify_callback
        self._previous_papers: dict[str, Paper] = {}
        self._seeded = False

    async def seed(self) -> None:
        """First-run: gather all current papers from all sources without notifying."""
        log.info("Seeding local paper database from all sources...")

        if self.cfg.enable_bulk_wg21:
            await self.index.refresh()
            log.info("Seeded %d papers from wg21.link", len(self.index.papers))

        self._previous_papers = dict(self.index.papers)

        if self.cfg.enable_iso_probe:
            hits = await self.prober.run_cycle()
            for hit in hits:
                self.state.mark_discovered(hit.url)
            log.info("Seed probe found %d existing papers on isocpp.org", len(hits))

        self._seeded = True
        log.info("Seed complete: %d papers in local database", len(self._previous_papers))

    async def poll_once(self) -> PollResult:
        log.info("Starting poll cycle")

        if not self._seeded:
            await self.seed()
            return PollResult(
                diff=DiffResult(new_papers=[], updated_papers=[]),
                probe_hits=[], watchlist_matches=[],
            )

        previous = dict(self._previous_papers)

        if self.cfg.enable_bulk_wg21:
            await self.index.refresh()
            log.info("Index refreshed: %d papers", len(self.index.papers))

        diff = diff_snapshots(previous, self.index.papers)
        self._previous_papers = dict(self.index.papers)

        probe_hits: list[ProbeHit] = []
        if self.cfg.enable_iso_probe:
            probe_hits = await self.prober.run_cycle()
            log.info("Probe hits: %d", len(probe_hits))

        # Filter probe hits: only truly new papers not in our local DB
        new_probe_hits: list[ProbeHit] = []
        for hit in probe_hits:
            paper_id = f"{hit.prefix}{hit.number:04d}R{hit.revision}"
            if paper_id not in self._previous_papers:
                new_probe_hits.append(hit)

        # Check index diff for watchlist author matches
        watchlist_matches: list[Paper] = []
        for paper in diff.new_papers:
            if self.watchlist.matches(paper):
                watchlist_matches.append(paper)

        # Check probe hits for watchlist author matches in the front text
        probe_watchlist_hits: list[ProbeHit] = []
        for hit in new_probe_hits:
            if hit.front_text:
                text_lower = hit.front_text.lower()
                if any(a in text_lower for a in self.watchlist.authors):
                    probe_watchlist_hits.append(hit)

        result = PollResult(
            diff=diff,
            probe_hits=new_probe_hits,
            watchlist_matches=watchlist_matches,
            probe_watchlist_hits=probe_watchlist_hits,
        )
        if self.notify_callback:
            self.notify_callback(result)

        log.info(
            "Poll complete: %d new, %d updated, %d new probe hits, "
            "%d watchlist matches, %d probe watchlist matches",
            len(diff.new_papers), len(diff.updated_papers),
            len(new_probe_hits), len(watchlist_matches),
            len(probe_watchlist_hits),
        )
        return result

    async def run_forever(self) -> None:
        interval = self.cfg.poll_interval_minutes * 60
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("Poll cycle failed")
            await asyncio.sleep(interval)
