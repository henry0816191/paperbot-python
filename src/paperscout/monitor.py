"""Polling scheduler: diff index snapshots, run probes, dispatch notifications."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

import httpx

from .concurrency import run_blocking_io
from .config import Settings, settings
from .errors import ConfigurationError, FailureCategory
from .models import CycleResult, CycleStatus, Paper, PerUserMatches, ProbeHit
from .sources import ISOProber, WG21Index
from .storage import ProbeState, UserWatchlist

log = logging.getLogger(__name__)


# ── Diff Engine ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class DiffResult:
    """New and updated papers between two index snapshots."""

    new_papers: list[Paper]
    updated_papers: list[Paper]


def diff_snapshots(
    previous: dict[str, Paper],
    current: dict[str, Paper],
) -> DiffResult:
    """Compare two id→paper maps; detect additions and metadata changes."""
    new_papers: list[Paper] = []
    updated_papers: list[Paper] = []
    prev_keys = set(previous.keys())

    for key, paper in current.items():
        if key not in prev_keys:
            new_papers.append(paper)
        else:
            old = previous[key]
            if (
                old.title != paper.title
                or old.author != paper.author
                or old.date != paper.date
                or old.long_link != paper.long_link
            ):
                updated_papers.append(paper)

    def _paper_sort_key(p: Paper) -> tuple[str, str]:
        return (p.date or "", p.id)

    new_papers.sort(key=_paper_sort_key, reverse=True)
    updated_papers.sort(key=_paper_sort_key, reverse=True)
    return DiffResult(new_papers=new_papers, updated_papers=updated_papers)


# ── Poll Result ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class DPTransition:
    """Index P entry that corresponds to a draft URL we previously probed on isocpp."""

    paper: Paper
    draft_url: str
    last_modified: float | None
    discovered_at: float


@dataclass(slots=True)
class SeedResult:
    """Outcome of ``seed()``: probe hits from the seed cycle and whether DB had prior state."""

    probe_hits: list[ProbeHit]
    had_prior_state: bool


class PollResult:
    """Outcome of one poll: index diff, probe hits, D→P transitions, per-user matches."""

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


# ── Health snapshot (issue 04) ───────────────────────────────────────────────


def _compute_probe_success_rate(stats: dict[str, int]) -> float | None:
    """HTTP 200 outcomes / non-skipped probe attempts."""
    hits = stats.get("hit_recent", 0) + stats.get("hit_old", 0) + stats.get("hit_no_lm", 0)
    attempted = hits + stats.get("miss", 0) + stats.get("error", 0)
    return hits / attempted if attempted > 0 else None


@dataclass(frozen=True, slots=True)
class SchedulerSnapshot:
    """Immutable scheduler state published for the health endpoint.

    ``probe_stats`` is a read-only ``MappingProxyType`` view so callers cannot
    mutate stats through the frozen snapshot object.
    """

    last_updated: str
    poll_count: int
    last_successful_poll: str | None
    last_cycle_status: str | None
    last_cycle_error: str | None
    probe_stats: Mapping[str, int]
    probe_success_rate: float | None


_HEALTH_SNAPSHOT_DEFAULTS: dict[str, Any] = {
    "last_updated": None,
    "poll_count": 0,
    "last_successful_poll": None,
    "last_cycle_status": None,
    "last_cycle_error": None,
    "probe_stats": {},
    "probe_success_rate": None,
}


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
        ops_alert_fn: Callable[[str], None] | None = None,
    ):
        self.index = index
        self.prober = prober
        self.user_watchlist = user_watchlist
        self.state = state
        self.cfg = cfg or settings
        self.notify_callback = notify_callback
        self.ops_alert_fn = ops_alert_fn
        self._previous_papers: dict[str, Paper] = {}
        self._seeded = False
        self._poll_count = 0
        self._last_successful_poll: float | None = None
        self._last_probe_stats: dict[str, int] = {}
        self._last_cycle_status: CycleStatus | None = None
        self._last_cycle_error: str | None = None
        self._last_ops_alert: float | None = None
        self._health_lock = threading.Lock()
        self._health_snapshot: SchedulerSnapshot | None = None

    def _probe_hits_from_cycle(self, cycle: CycleResult) -> list[ProbeHit]:
        """Extract hits and record last cycle status for health / staleness."""
        self._last_cycle_status = cycle.status
        self._last_cycle_error = cycle.error
        if cycle.status is CycleStatus.SUCCESS:
            return cycle.hits
        if cycle.status is CycleStatus.EMPTY:
            log.info("POLL  probe cycle empty")
            return []
        if cycle.status is CycleStatus.FAILED:
            log.error("POLL  probe cycle failed: %s", cycle.error)
            return []
        log.error("POLL  unknown cycle status: %s", cycle.status)
        return []

    def _record_probe_cycle_completion(self) -> None:
        """Update probe stats after any completed cycle (including FAILED)."""
        self._last_probe_stats = self.prober.snapshot_stats()

    def _mark_poll_successful_if_probe_ok(self) -> None:
        """Advance staleness clock only when the last probe cycle did not fail."""
        if self._last_cycle_status is not CycleStatus.FAILED:
            self._last_successful_poll = time.time()

    def _publish_health_snapshot(self) -> None:
        """Publish immutable snapshot for cross-thread health reads (event loop only)."""
        lsp = self._last_successful_poll
        stats = dict(self._last_probe_stats)
        snap = SchedulerSnapshot(
            last_updated=datetime.now(timezone.utc).isoformat(),
            poll_count=self._poll_count,
            last_successful_poll=(
                datetime.fromtimestamp(lsp, tz=timezone.utc).isoformat() if lsp else None
            ),
            last_cycle_status=(self._last_cycle_status.value if self._last_cycle_status else None),
            last_cycle_error=self._last_cycle_error,
            probe_stats=MappingProxyType(stats),
            probe_success_rate=_compute_probe_success_rate(stats),
        )
        with self._health_lock:
            self._health_snapshot = snap

    def health_snapshot(self) -> dict[str, Any]:
        """Return a consistent copy of scheduler fields for ``/health`` extras."""
        with self._health_lock:
            snap = self._health_snapshot
        if snap is None:
            return copy.deepcopy(_HEALTH_SNAPSHOT_DEFAULTS)
        # Build explicitly: probe_stats is a MappingProxyType (not deepcopy-able via asdict).
        return {
            "last_updated": snap.last_updated,
            "poll_count": snap.poll_count,
            "last_successful_poll": snap.last_successful_poll,
            "last_cycle_status": snap.last_cycle_status,
            "last_cycle_error": snap.last_cycle_error,
            "probe_stats": dict(snap.probe_stats),
            "probe_success_rate": snap.probe_success_rate,
        }

    async def seed(self) -> SeedResult:
        """Gather current index and probe state.

        Cold first deploy: no notifications from seed. On restart (prior poll or
        discovered URLs), ``poll_once`` may notify for recent probe hits from this seed cycle.
        """
        had_prior_state = self.state.last_poll > 0 or len(self.state.get_all_discovered()) > 0
        t0 = time.monotonic()
        log.info("SEED-START  seeding local database from all sources")

        if self.cfg.enable_bulk_wg21:
            await self.index.refresh()
            log.info("SEED  wg21.link loaded  papers=%d", len(self.index.papers))

        self._previous_papers = dict(self.index.papers)

        hits: list[ProbeHit] = []
        if self.cfg.enable_iso_probe:
            cycle = await self.prober.run_cycle()
            hits = self._probe_hits_from_cycle(cycle)
            self._record_probe_cycle_completion()
            log.info("SEED  isocpp.org probe  existing=%d", len(hits))

        self._seeded = True
        log.info(
            "SEED-DONE  elapsed=%.1fs  papers=%d  discovered=%d  had_prior_state=%s",
            time.monotonic() - t0,
            len(self._previous_papers),
            len(self.state.get_all_discovered()),
            had_prior_state,
        )
        return SeedResult(probe_hits=hits, had_prior_state=had_prior_state)

    async def poll_once(self) -> PollResult:
        """Refresh index (if enabled), diff, probe isocpp, compute matches, notify."""
        self._poll_count += 1
        t0 = time.monotonic()
        log.info("POLL-START  poll=%d", self._poll_count)

        if not self._seeded:
            seed_result = await self.seed()
            if not seed_result.had_prior_state:
                if self.cfg.enable_iso_probe:
                    # Stats already recorded in seed() after run_cycle.
                    self._mark_poll_successful_if_probe_ok()
                else:
                    self._last_successful_poll = time.time()
                    self._record_probe_cycle_completion()
                self._publish_health_snapshot()
                return PollResult(
                    diff=DiffResult(new_papers=[], updated_papers=[]),
                    probe_hits=[],
                )

            probe_hits = seed_result.probe_hits
            recent_hits = [h for h in probe_hits if h.is_recent]
            old_hits = [h for h in probe_hits if not h.is_recent]
            if old_hits:
                log.info(
                    "PROBE-OLD  %d hits with Last-Modified outside %dh window "
                    "(recorded to discovered, no alert)",
                    len(old_hits),
                    self.cfg.alert_modified_hours,
                )

            per_user_matches = await run_blocking_io(
                self.user_watchlist.matches_for_users,
                [],
                recent_hits,
            )
            for uid, m in per_user_matches.items():
                log.info(
                    "WATCHLIST-MATCH  user=%s  papers=%d  probe_hits=%d",
                    uid,
                    len(m.papers),
                    len(m.probe_hits),
                )

            result = PollResult(
                diff=DiffResult(new_papers=[], updated_papers=[]),
                probe_hits=recent_hits,
                dp_transitions=[],
                per_user_matches=per_user_matches,
            )
            if self.notify_callback:
                self.notify_callback(result)
            if self.cfg.enable_iso_probe:
                # Stats already recorded in seed() after run_cycle.
                self._mark_poll_successful_if_probe_ok()
            else:
                self._last_successful_poll = time.time()
                self._record_probe_cycle_completion()
            self._publish_health_snapshot()
            return result

        previous = dict(self._previous_papers)

        if self.cfg.enable_bulk_wg21:
            await self.index.refresh()
            log.info("INDEX-LOAD  papers=%d", len(self.index.papers))

        diff = diff_snapshots(previous, self.index.papers)
        self._previous_papers = dict(self.index.papers)

        for paper in diff.new_papers:
            log.info(
                "INDEX-NEW  id=%-14s  author=%-20s  date=%s  title=%r",
                paper.id,
                paper.author or "?",
                paper.date or "?",
                (paper.title or "")[:80],
            )
        for paper in diff.updated_papers:
            log.debug(
                "INDEX-UPD  id=%-14s  author=%-20s  date=%s",
                paper.id,
                paper.author or "?",
                paper.date or "?",
            )

        probe_hits: list[ProbeHit] = []
        if self.cfg.enable_iso_probe:
            cycle = await self.prober.run_cycle()
            probe_hits = self._probe_hits_from_cycle(cycle)
            self._record_probe_cycle_completion()

        recent_hits = [h for h in probe_hits if h.is_recent]
        old_hits = [h for h in probe_hits if not h.is_recent]

        if old_hits:
            log.info(
                "PROBE-OLD  %d hits with Last-Modified outside %dh window "
                "(recorded to discovered, no alert)",
                len(old_hits),
                self.cfg.alert_modified_hours,
            )

        # D→P transitions
        dp_transitions: list[DPTransition] = []
        for paper in diff.new_papers:
            if paper.number is None or paper.revision is None or paper.prefix != "P":
                continue
            for ext in self.cfg.probe_extensions:
                d_url = f"https://isocpp.org/files/papers/D{paper.number:04d}R{paper.revision}{ext}"
                info = self.state.discovered_info(d_url)
                if info is not None:
                    dp_transitions.append(
                        DPTransition(
                            paper=paper,
                            draft_url=d_url,
                            last_modified=info.get("last_modified"),
                            discovered_at=info.get("discovered_at", 0.0),
                        )
                    )
                    lm_ts = info.get("last_modified")
                    disc_ts = info.get("discovered_at", 0.0)
                    log.info(
                        "D-TO-P  id=%s  draft=%s  draft-lm=%s  draft-discovered=%s",
                        paper.id,
                        d_url,
                        (
                            datetime.fromtimestamp(lm_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                            if lm_ts
                            else "unknown"
                        ),
                        (
                            datetime.fromtimestamp(disc_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                            if disc_ts
                            else "unknown"
                        ),
                    )
                    break

        # Safe to run off the event loop: matches_for_users only performs blocking
        # PostgreSQL I/O via psycopg2 on its own pool connection — it does not
        # touch ISOProber._stats, WG21Index.papers, or other shared source state.
        per_user_matches = await run_blocking_io(
            self.user_watchlist.matches_for_users,
            diff.new_papers,
            recent_hits,
        )
        for uid, m in per_user_matches.items():
            log.info(
                "WATCHLIST-MATCH  user=%s  papers=%d  probe_hits=%d",
                uid,
                len(m.papers),
                len(m.probe_hits),
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
            self._poll_count,
            elapsed,
            len(diff.new_papers),
            len(diff.updated_papers),
            len(recent_hits),
            len(old_hits),
            len(dp_transitions),
            len(per_user_matches),
        )
        if self.cfg.enable_iso_probe:
            # Stats already recorded above after run_cycle.
            self._mark_poll_successful_if_probe_ok()
        else:
            self._last_successful_poll = time.time()
            self._record_probe_cycle_completion()
        self._publish_health_snapshot()
        return result

    async def _poll_once_or_cancel(self, shutdown_event: asyncio.Event) -> None:
        """Run ``poll_once`` or cancel promptly when *shutdown_event* is set."""
        poll_task = asyncio.create_task(self.poll_once())
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {poll_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_task in done:
                poll_task.cancel()
                await poll_task
            else:
                shutdown_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await shutdown_task
                await poll_task
        except asyncio.CancelledError:
            raise
        finally:
            for task in (poll_task, shutdown_task):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def run_forever(self, shutdown_event: asyncio.Event | None = None) -> None:
        """Run ``poll_once`` on an interval, with overrun cooldown between cycles."""
        interval = self.cfg.poll_interval_minutes * 60
        cooldown = self.cfg.poll_overrun_cooldown_seconds
        log.info(
            "SCHEDULER-START  interval=%dmin  overrun_cooldown=%ds  iso_probe=%s  wg21=%s",
            self.cfg.poll_interval_minutes,
            cooldown,
            self.cfg.enable_iso_probe,
            self.cfg.enable_bulk_wg21,
        )
        run_started_wall = time.time()
        shutdown_requested = False
        while shutdown_event is None or not shutdown_event.is_set():
            t0 = time.monotonic()
            try:
                if shutdown_event is not None:
                    await self._poll_once_or_cancel(shutdown_event)
                else:
                    await self.poll_once()
            except asyncio.CancelledError:
                if shutdown_event is None or not shutdown_event.is_set():
                    raise
                log.info("POLL-CANCELLED  poll=%d  reason=shutdown", self._poll_count)
                shutdown_requested = True
                break
            except ConfigurationError as exc:
                log.critical(
                    "POLL-FATAL  failure_category=%s  poll=%d  %s",
                    FailureCategory.CONFIGURATION.value,
                    self._poll_count,
                    exc,
                )
                return
            except httpx.TimeoutException as exc:
                log.error(
                    "POLL-ERROR  failure_category=%s  poll=%d  %s",
                    FailureCategory.TIMEOUT.value,
                    self._poll_count,
                    exc,
                )
            except httpx.HTTPStatusError as exc:
                cat = (
                    FailureCategory.RATE_LIMIT
                    if exc.response.status_code == 429
                    else FailureCategory.NETWORK
                )
                log.error(
                    "POLL-ERROR  failure_category=%s  poll=%d  status=%d",
                    cat.value,
                    self._poll_count,
                    exc.response.status_code,
                )
            except httpx.HTTPError as exc:
                log.error(
                    "POLL-ERROR  failure_category=%s  poll=%d  %s",
                    FailureCategory.NETWORK.value,
                    self._poll_count,
                    exc,
                )
            except Exception:
                log.exception(
                    "POLL-ERROR  failure_category=%s  poll=%d",
                    FailureCategory.UNKNOWN.value,
                    self._poll_count,
                )
            elapsed = time.monotonic() - t0

            if shutdown_event is not None and shutdown_event.is_set():
                shutdown_requested = True
                break

            if self.ops_alert_fn:
                alert_threshold = 2 * interval
                now_wall = time.time()
                now_m = time.monotonic()
                if self._last_successful_poll is not None:
                    stale = now_wall - self._last_successful_poll
                else:
                    # Never completed a poll: treat as stale from loop start.
                    stale = now_wall - run_started_wall
                if stale > alert_threshold and (
                    self._last_ops_alert is None or (now_m - self._last_ops_alert) > interval
                ):
                    try:
                        self.ops_alert_fn(
                            f"No successful poll in {stale / 60:.0f}min "
                            f"(threshold={2 * self.cfg.poll_interval_minutes}min)"
                        )
                    except Exception:
                        log.exception("OPS-ALERT  stale-poll notification failed")
                    self._last_ops_alert = now_m

            sleep_for = max(interval - elapsed, cooldown)
            log.info(
                "SCHEDULER-SLEEP  sleep=%.0fs  (poll=%.0fs  interval=%ds)",
                sleep_for,
                elapsed,
                interval,
            )
            if shutdown_event is not None:
                try:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=sleep_for)
                    shutdown_requested = True
                    break
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(sleep_for)

        if shutdown_requested or (shutdown_event is not None and shutdown_event.is_set()):
            log.info("SCHEDULER-STOP  reason=shutdown_event")
