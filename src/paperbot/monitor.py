from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
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

@dataclass(slots=True)
class DPTransition:
    """A D-paper draft that has been formally published as its P counterpart.

    *paper*        -- the new P-paper entry from the wg21.link index
    *draft_url*    -- the D-paper URL we originally probed
    *last_modified -- server Last-Modified of the draft (Unix timestamp), or None
    *discovered_at* -- our wall-clock time when we first found the draft
    """
    paper: Paper
    draft_url: str
    last_modified: float | None
    discovered_at: float


class PollResult:
    def __init__(
        self,
        diff: DiffResult,
        probe_hits: list[ProbeHit],
        watchlist_matches: list[Paper],
        probe_watchlist_hits: list[ProbeHit] | None = None,
        dp_transitions: list[DPTransition] | None = None,
    ):
        self.diff = diff
        # Only hits flagged is_recent=True are actionable
        self.probe_hits = probe_hits
        self.watchlist_matches = watchlist_matches
        self.probe_watchlist_hits = probe_watchlist_hits or []
        # D-papers that have now appeared in the wg21.link index as P-papers
        self.dp_transitions = dp_transitions or []


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
        self._poll_count = 0

    async def seed(self) -> None:
        """First-run: gather all current papers from all sources without notifying."""
        t0 = time.monotonic()
        log.info("SEED-START  seeding local database from all sources")

        if self.cfg.enable_bulk_wg21:
            await self.index.refresh()
            log.info("SEED  wg21.link loaded  papers=%d", len(self.index.papers))

        self._previous_papers = dict(self.index.papers)

        if self.cfg.enable_iso_probe:
            hits = await self.prober.run_cycle()
            for hit in hits:
                self.state.mark_discovered(hit.url)
            log.info("SEED  isocpp.org probe  existing=%d", len(hits))

        self._seeded = True
        log.info(
            "SEED-DONE  elapsed=%.1fs  papers=%d  discovered=%d",
            time.monotonic() - t0,
            len(self._previous_papers),
            len(self.state.discovered),
        )

    async def poll_once(self) -> PollResult:
        self._poll_count += 1
        t0 = time.monotonic()
        log.info("POLL-START  poll=%d", self._poll_count)

        if not self._seeded:
            await self.seed()
            return PollResult(
                diff=DiffResult(new_papers=[], updated_papers=[]),
                probe_hits=[], watchlist_matches=[],
            )

        previous = dict(self._previous_papers)

        if self.cfg.enable_bulk_wg21:
            await self.index.refresh()
            log.info("INDEX-LOAD  papers=%d", len(self.index.papers))

        diff = diff_snapshots(previous, self.index.papers)
        self._previous_papers = dict(self.index.papers)

        for paper in diff.new_papers:
            log.info(
                "INDEX-NEW  id=%-14s  author=%-20s  date=%s  title=%r",
                paper.id, paper.author or "?", paper.date or "?",
                (paper.title or "")[:80],
            )
        for paper in diff.updated_papers:
            log.debug(
                "INDEX-UPD  id=%-14s  author=%-20s  date=%s",
                paper.id, paper.author or "?", paper.date or "?",
            )

        probe_hits: list[ProbeHit] = []
        if self.cfg.enable_iso_probe:
            probe_hits = await self.prober.run_cycle()

        # Only surface hits the server says are recently modified.
        recent_hits = [h for h in probe_hits if h.is_recent]
        old_hits    = [h for h in probe_hits if not h.is_recent]

        if old_hits:
            log.info(
                "PROBE-OLD  %d hits with Last-Modified outside %dh window "
                "(recorded to discovered, no alert)",
                len(old_hits), self.cfg.alert_modified_hours,
            )

        # Index diff: watchlist author matches in newly published papers
        watchlist_matches: list[Paper] = []
        for paper in diff.new_papers:
            if self.watchlist.matches(paper):
                watchlist_matches.append(paper)
                log.info(
                    "WATCHLIST-MATCH  id=%s  author=%r",
                    paper.id, paper.author,
                )

        # Probe hits: watchlist author name appears in the draft's front text
        probe_watchlist_hits: list[ProbeHit] = []
        for hit in recent_hits:
            if hit.front_text:
                text_lower = hit.front_text.lower()
                if any(a in text_lower for a in self.watchlist.authors):
                    probe_watchlist_hits.append(hit)
                    log.info(
                        "PROBE-WATCHLIST  %s  tier=%s",
                        hit.url, hit.tier,
                    )

        # D→P transitions: newly indexed P-papers whose D-paper URL we already
        # have on record in probe_state.  Each revision of a paper has distinct
        # URLs so one paper can have at most one transition event per revision.
        dp_transitions: list[DPTransition] = []
        for paper in diff.new_papers:
            if paper.number is None or paper.revision is None or paper.prefix != "P":
                continue
            for ext in self.cfg.probe_extensions:
                d_url = (
                    f"https://isocpp.org/files/papers/"
                    f"D{paper.number:04d}R{paper.revision}{ext}"
                )
                info = self.state.discovered_info(d_url)
                if info is not None:
                    dp_transitions.append(DPTransition(
                        paper=paper,
                        draft_url=d_url,
                        last_modified=info.get("last_modified"),
                        discovered_at=info.get("discovered_at", 0.0),
                    ))
                    lm_ts = info.get("last_modified")
                    disc_ts = info.get("discovered_at", 0.0)
                    log.info(
                        "D-TO-P  id=%s  draft=%s  "
                        "draft-lm=%s  draft-discovered=%s",
                        paper.id, d_url,
                        datetime.fromtimestamp(lm_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                        if lm_ts else "unknown",
                        datetime.fromtimestamp(disc_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                        if disc_ts else "unknown",
                    )
                    break  # one transition per paper is enough

        result = PollResult(
            diff=diff,
            probe_hits=recent_hits,
            watchlist_matches=watchlist_matches,
            probe_watchlist_hits=probe_watchlist_hits,
            dp_transitions=dp_transitions,
        )
        if self.notify_callback:
            self.notify_callback(result)

        elapsed = time.monotonic() - t0
        log.info(
            "POLL-DONE  poll=%d  elapsed=%.1fs  "
            "index-new=%d  index-upd=%d  "
            "probe-recent=%d  probe-old=%d  "
            "watchlist=%d  probe-watchlist=%d  dp-transitions=%d",
            self._poll_count, elapsed,
            len(diff.new_papers), len(diff.updated_papers),
            len(recent_hits), len(old_hits),
            len(watchlist_matches), len(probe_watchlist_hits),
            len(dp_transitions),
        )
        return result

    async def run_forever(self) -> None:
        interval  = self.cfg.poll_interval_minutes * 60
        cooldown  = self.cfg.poll_overrun_cooldown_seconds
        log.info(
            "SCHEDULER-START  interval=%dmin  overrun_cooldown=%ds  "
            "iso_probe=%s  wg21=%s",
            self.cfg.poll_interval_minutes, cooldown,
            self.cfg.enable_iso_probe, self.cfg.enable_bulk_wg21,
        )
        while True:
            t0 = time.monotonic()
            try:
                await self.poll_once()
            except Exception:
                log.exception("POLL-ERROR  poll=%d", self._poll_count)
            elapsed = time.monotonic() - t0

            # Sleep the remainder of the target interval so that the effective
            # period is poll_interval_minutes regardless of how long probing
            # took.  If the cycle overran the interval, sleep poll_overrun_cooldown_seconds
            # as a brief cooldown before immediately starting the next cycle.
            sleep_for = max(interval - elapsed, cooldown)
            log.info(
                "SCHEDULER-SLEEP  sleep=%.0fs  (poll=%.0fs  interval=%ds)",
                sleep_for, elapsed, interval,
            )
            await asyncio.sleep(sleep_for)
