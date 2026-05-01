from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Settings, settings
from .models import Paper
from .sources import ISOProber, ProbeHit, WG21Index
from .storage import ProbeState, UserWatchlist

log = logging.getLogger(__name__)


# ── Diff Engine ──────────────────────────────────────────────────────────────

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


# ── Per-User Matches ─────────────────────────────────────────────────────────

@dataclass
class PerUserMatches:
    """Watchlist matches for a single Slack user in one poll cycle.

    Each entry in *papers* and *probe_hits* is a ``(item, match_reason)``
    tuple where ``match_reason`` is ``'author'`` or ``'paper'``.
    """
    papers: list[tuple[Paper, str]] = field(default_factory=list)
    probe_hits: list[tuple[ProbeHit, str]] = field(default_factory=list)


# ── Poll Result ──────────────────────────────────────────────────────────────

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
        dp_transitions: list[DPTransition] | None = None,
        per_user_matches: dict[str, PerUserMatches] | None = None,
    ):
        self.diff = diff
        self.probe_hits = probe_hits
        self.dp_transitions = dp_transitions or []
        self.per_user_matches = per_user_matches or {}


# ── Scheduler ────────────────────────────────────────────────────────────────

class Scheduler:
    """Coordinates periodic polling: index refresh + ISO probing + notifications."""

    def __init__(
        self,
        index: WG21Index,
        prober: ISOProber,
        user_watchlist: UserWatchlist,
        state: ProbeState,
        cfg: Settings | None = None,
        notify_callback=None,
    ):
        self.index = index
        self.prober = prober
        self.user_watchlist = user_watchlist
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
                probe_hits=[],
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

        recent_hits = [h for h in probe_hits if h.is_recent]
        old_hits    = [h for h in probe_hits if not h.is_recent]

        if old_hits:
            log.info(
                "PROBE-OLD  %d hits with Last-Modified outside %dh window "
                "(recorded to discovered, no alert)",
                len(old_hits), self.cfg.alert_modified_hours,
            )

        # D→P transitions
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
                    break

        # Per-user watchlist matching
        per_user_matches = await asyncio.to_thread(
            self.user_watchlist.matches_for_users,
            diff.new_papers,
            recent_hits,
        )
        for uid, m in per_user_matches.items():
            log.info(
                "WATCHLIST-MATCH  user=%s  papers=%d  probe_hits=%d",
                uid, len(m.papers), len(m.probe_hits),
            )

        result = PollResult(
            diff=diff,
            probe_hits=recent_hits,
            dp_transitions=dp_transitions,
            per_user_matches=per_user_matches,
        )
        if self.notify_callback:
            self.notify_callback(result)

        elapsed = time.monotonic() - t0
        log.info(
            "POLL-DONE  poll=%d  elapsed=%.1fs  "
            "index-new=%d  index-upd=%d  "
            "probe-recent=%d  probe-old=%d  "
            "dp-transitions=%d  users-notified=%d",
            self._poll_count, elapsed,
            len(diff.new_papers), len(diff.updated_papers),
            len(recent_hits), len(old_hits),
            len(dp_transitions), len(per_user_matches),
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

            sleep_for = max(interval - elapsed, cooldown)
            log.info(
                "SCHEDULER-SLEEP  sleep=%.0fs  (poll=%.0fs  interval=%ds)",
                sleep_for, elapsed, interval,
            )
            await asyncio.sleep(sleep_for)
