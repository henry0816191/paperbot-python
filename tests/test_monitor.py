"""Tests for paperscout.monitor."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from paperscout.errors import ConfigurationError
from paperscout.models import (
    CycleResult,
    CycleStatus,
    MatchReason,
    Paper,
    PerUserMatches,
    ProbeHit,
)
from paperscout.monitor import (
    DiffResult,
    PollResult,
    Scheduler,
    diff_snapshots,
)
from paperscout.protocols import SOURCE_ISO_PROBE, SOURCE_WG21_INDEX
from paperscout.sources import ISOProber, WG21Index
from paperscout.storage import ProbeState, UserWatchlist
from tests.conftest import make_test_settings


def _wait_for_timeout(awaitable, timeout=None):
    """Sync mock for asyncio.wait_for that times out immediately without orphaning coroutines."""
    if hasattr(awaitable, "close"):
        awaitable.close()
    raise asyncio.TimeoutError


def _recent_hit(**kwargs) -> ProbeHit:
    defaults = dict(
        url="https://isocpp.org/files/papers/D9999R0.pdf",
        prefix="D",
        number=9999,
        revision=0,
        extension=".pdf",
        tier="frontier",
        is_recent=True,
    )
    defaults.update(kwargs)
    return ProbeHit(**defaults)


def _empty_cycle() -> CycleResult:
    return CycleResult(CycleStatus.EMPTY)


def _success_cycle(hits: list[ProbeHit]) -> CycleResult:
    return CycleResult(CycleStatus.SUCCESS, results=tuple(hits))


def _failed_cycle(error: str = "probe failed") -> CycleResult:
    return CycleResult(CycleStatus.FAILED, error=error)


def _old_hit(**kwargs) -> ProbeHit:
    defaults = dict(
        url="https://isocpp.org/files/papers/D8888R0.pdf",
        prefix="D",
        number=8888,
        revision=0,
        extension=".pdf",
        tier="cold",
        is_recent=False,
        last_modified=datetime.now(timezone.utc) - timedelta(days=30),
    )
    defaults.update(kwargs)
    return ProbeHit(**defaults)


# ── diff_snapshots ────────────────────────────────────────────────────────────


class TestDiffSnapshots:
    def _paper(self, pid, **kwargs) -> Paper:
        defaults = dict(title="T", author="A", date="2024-01-01")
        defaults.update(kwargs)
        return Paper(id=pid, **defaults)

    def test_new_paper_detected(self):
        result = diff_snapshots({}, {"P2300R10": self._paper("P2300R10")})
        assert len(result.new_papers) == 1

    def test_updated_paper_detected_title_change(self):
        old = self._paper("P2300R10", title="Old")
        new = self._paper("P2300R10", title="New")
        result = diff_snapshots({"P2300R10": old}, {"P2300R10": new})
        assert len(result.updated_papers) == 1

    def test_updated_paper_detected_author_change(self):
        result = diff_snapshots(
            {"P2300R10": self._paper("P2300R10", author="Old")},
            {"P2300R10": self._paper("P2300R10", author="New")},
        )
        assert len(result.updated_papers) == 1

    def test_updated_paper_detected_date_change(self):
        result = diff_snapshots(
            {"P2300R10": self._paper("P2300R10", date="2024-01-01")},
            {"P2300R10": self._paper("P2300R10", date="2024-06-01")},
        )
        assert len(result.updated_papers) == 1

    def test_updated_paper_detected_long_link_change(self):
        result = diff_snapshots(
            {"P2300R10": Paper(id="P2300R10", long_link="old.pdf")},
            {"P2300R10": Paper(id="P2300R10", long_link="new.pdf")},
        )
        assert len(result.updated_papers) == 1

    def test_unchanged_paper_not_reported(self):
        paper = self._paper("P2300R10")
        result = diff_snapshots({"P2300R10": paper}, {"P2300R10": paper})
        assert result.new_papers == [] and result.updated_papers == []

    def test_new_papers_sorted_by_date_descending(self):
        prev = {}
        curr = {
            "P2300R10": self._paper("P2300R10", date="2024-01-01"),
            "P2301R0": self._paper("P2301R0", date="2024-06-01"),
            "P2302R0": self._paper("P2302R0", date="2024-03-01"),
        }
        result = diff_snapshots(prev, curr)
        dates = [p.date for p in result.new_papers]
        assert dates == sorted(dates, reverse=True)

    def test_updated_papers_sorted_by_date_descending(self):
        prev = {
            "P2300R10": self._paper("P2300R10", title="Old A", date="2024-01-01"),
            "P2301R0": self._paper("P2301R0", title="Old B", date="2024-03-01"),
            "P2302R0": self._paper("P2302R0", title="Old C", date="2024-06-01"),
        }
        curr = {
            "P2300R10": self._paper("P2300R10", title="New A", date="2024-01-01"),
            "P2301R0": self._paper("P2301R0", title="New B", date="2024-06-01"),
            "P2302R0": self._paper("P2302R0", title="New C", date="2024-03-01"),
        }
        result = diff_snapshots(prev, curr)
        dates = [p.date for p in result.updated_papers]
        assert dates == sorted(dates, reverse=True)

    def test_empty_to_empty(self):
        result = diff_snapshots({}, {})
        assert result.new_papers == [] and result.updated_papers == []

    @pytest.mark.parametrize(
        "field,new_val",
        [
            ("title", "New Title"),
            ("author", "New Author"),
            ("date", "2025-01-01"),
            ("long_link", "https://new.example/paper.pdf"),
        ],
    )
    def test_updated_paper_detected_single_field(self, field, new_val):
        base = dict(title="T", author="A", date="2024-01-01", long_link="")
        old_kw = dict(base)
        new_kw = dict(base)
        new_kw[field] = new_val
        old_p = Paper(id="P2300R10", **old_kw)
        new_p = Paper(id="P2300R10", **new_kw)
        result = diff_snapshots({"P2300R10": old_p}, {"P2300R10": new_p})
        assert len(result.updated_papers) == 1


# ── PollResult ────────────────────────────────────────────────────────────────


class TestPollResult:
    def test_defaults(self):
        diff = DiffResult(new_papers=[], updated_papers=[])
        result = PollResult(diff=diff, probe_hits=[])
        assert result.dp_transitions == []
        assert result.per_user_matches == {}

    def test_explicit_dp_transitions(self):
        from paperscout.monitor import DPTransition

        diff = DiffResult(new_papers=[], updated_papers=[])
        paper = Paper(id="P2300R11")
        tr = DPTransition(paper=paper, draft_url="http://x", last_modified=None, discovered_at=0.0)
        result = PollResult(diff=diff, probe_hits=[], dp_transitions=[tr])
        assert len(result.dp_transitions) == 1

    def test_explicit_per_user_matches(self):
        diff = DiffResult(new_papers=[], updated_papers=[])
        paper = Paper(id="P2300R11")
        pum = PerUserMatches(papers=[(paper, MatchReason.AUTHOR)], probe_hits=[])
        result = PollResult(diff=diff, probe_hits=[], per_user_matches={"U1": pum})
        assert "U1" in result.per_user_matches


# ── Scheduler ─────────────────────────────────────────────────────────────────


def _make_mock_wg21() -> MagicMock:
    mock = MagicMock(spec=WG21Index)
    mock.source_id = SOURCE_WG21_INDEX
    mock.papers = {}

    async def _fetch():
        return dict(mock.papers)

    mock.fetch = AsyncMock(side_effect=_fetch)
    mock.diff = lambda previous, current: diff_snapshots(previous or {}, current)
    return mock


def _make_mock_iso() -> MagicMock:
    mock = MagicMock(spec=ISOProber)
    mock.source_id = SOURCE_ISO_PROBE

    async def _fetch():
        return mock._cycle_result

    mock._cycle_result = _empty_cycle()
    mock.fetch = AsyncMock(side_effect=_fetch)
    mock.snapshot_stats = MagicMock(return_value={})
    mock._stats = {}

    def _diff(previous, current):
        del previous
        if current.status is CycleStatus.SUCCESS:
            return list(current.hits)
        return []

    mock.diff = _diff
    return mock


def _set_iso_cycle(prober: MagicMock, cycle: CycleResult) -> None:
    prober._cycle_result = cycle


def _make_scheduler(fake_pool, **cfg_overrides):
    wg21 = _make_mock_wg21()
    iso = _make_mock_iso()
    user_watchlist = MagicMock(spec=UserWatchlist)
    user_watchlist.matches_for_users.return_value = {}
    state = ProbeState(fake_pool)
    cfg = make_test_settings(**cfg_overrides)
    scheduler = Scheduler(
        sources=[wg21, iso],
        user_watchlist=user_watchlist,
        state=state,
        cfg=cfg,
    )
    return scheduler, wg21, iso, user_watchlist, state


class TestScheduler:
    async def test_poll_once_seeds_on_first_call(self, fake_pool):
        scheduler, index, prober, _, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()
        index.fetch.assert_called_once()
        prober.fetch.assert_called_once()
        assert scheduler._seeded

    async def test_poll_once_returns_empty_on_seed(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        result = await scheduler.poll_once()
        assert result.diff.new_papers == []

    async def test_poll_once_detects_new_papers(self, fake_pool):
        scheduler, index, prober, _, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        new_paper = Paper(id="P9999R0", title="New", author="Author", date="2024-01-01")
        index.papers = {"P9999R0": new_paper}
        _set_iso_cycle(prober, _empty_cycle())
        result = await scheduler.poll_once()
        assert len(result.diff.new_papers) == 1

    async def test_poll_once_surfaces_only_recent_probe_hits(self, fake_pool):
        scheduler, index, prober, _, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        recent = _recent_hit()
        old = _old_hit()
        index.papers = {}
        _set_iso_cycle(prober, _success_cycle([recent, old]))
        result = await scheduler.poll_once()
        assert len(result.probe_hits) == 1
        assert result.probe_hits[0].is_recent is True

    async def test_poll_once_detects_dp_transition(self, fake_pool):
        scheduler, index, prober, _, state = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        draft_url = "https://isocpp.org/files/papers/D9999R0.pdf"
        state.mark_discovered(draft_url, last_modified_ts=1_700_000_000.0)

        new_paper = Paper(
            id="P9999R0",
            title="New Published Paper",
            author="Author",
            date="2025-01-01",
        )
        index.papers = {"P9999R0": new_paper}
        _set_iso_cycle(prober, _empty_cycle())

        result = await scheduler.poll_once()
        assert len(result.dp_transitions) == 1
        tr = result.dp_transitions[0]
        assert tr.paper.id == "P9999R0"
        assert tr.draft_url == draft_url
        assert tr.last_modified == 1_700_000_000.0

    async def test_poll_once_dp_skip_non_p_papers(self, fake_pool):
        scheduler, index, prober, _, state = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        n_paper = Paper(id="N4950", title="Working Draft", author="Ed", date="2025-01-01")
        index.papers = {"N4950": n_paper}
        _set_iso_cycle(prober, _empty_cycle())

        result = await scheduler.poll_once()
        assert result.dp_transitions == []

    async def test_poll_once_no_dp_transition_when_no_draft(self, fake_pool):
        scheduler, index, prober, _, state = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        new_paper = Paper(id="P8888R0", title="Entirely New", author="X", date="2025-01-01")
        index.papers = {"P8888R0": new_paper}
        _set_iso_cycle(prober, _empty_cycle())

        result = await scheduler.poll_once()
        assert result.dp_transitions == []

    async def test_poll_once_dp_transition_logged(self, fake_pool, caplog):
        import logging

        scheduler, index, prober, _, state = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        draft_url = "https://isocpp.org/files/papers/D7777R0.pdf"
        state.mark_discovered(draft_url)
        new_paper = Paper(id="P7777R0", title="X", author="Y", date="2025-01-01")
        index.papers = {"P7777R0": new_paper}
        _set_iso_cycle(prober, _empty_cycle())

        with caplog.at_level(logging.INFO):
            result = await scheduler.poll_once()
        assert result.dp_transitions
        assert "D-TO-P" in caplog.text

    async def test_poll_count_increments(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        assert scheduler._poll_count == 0
        await scheduler.poll_once()
        assert scheduler._poll_count == 1
        await scheduler.poll_once()
        assert scheduler._poll_count == 2

    async def test_poll_once_logs_updated_papers(self, fake_pool, caplog):
        import logging

        scheduler, index, prober, _, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        old_paper = Paper(id="P9999R0", title="Old Title", author="A", date="2024-01-01")
        scheduler._snapshots[SOURCE_WG21_INDEX] = {"P9999R0": old_paper}
        updated_paper = Paper(id="P9999R0", title="New Title", author="A", date="2024-01-01")
        index.papers = {"P9999R0": updated_paper}
        _set_iso_cycle(prober, _empty_cycle())
        with caplog.at_level(logging.DEBUG):
            await scheduler.poll_once()
        assert "INDEX-UPD" in caplog.text

    async def test_poll_old_hits_logged(self, fake_pool, caplog):
        import logging

        scheduler, index, prober, _, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()
        old = _old_hit()
        index.papers = {}
        _set_iso_cycle(prober, _success_cycle([old]))
        with caplog.at_level(logging.INFO):
            result = await scheduler.poll_once()
        assert result.probe_hits == []
        assert "PROBE-OLD" in caplog.text

    async def test_poll_once_populates_per_user_matches(self, fake_pool):
        scheduler, index, prober, user_watchlist, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        new_paper = Paper(id="P9999R0", title="Senders", author="Eric Niebler", date="2024-01-01")
        index.papers = {"P9999R0": new_paper}
        _set_iso_cycle(prober, _empty_cycle())

        user_watchlist.matches_for_users.return_value = {
            "U123": PerUserMatches(papers=[(new_paper, MatchReason.AUTHOR)], probe_hits=[])
        }
        result = await scheduler.poll_once()
        assert "U123" in result.per_user_matches
        assert len(result.per_user_matches["U123"].papers) == 1

    async def test_poll_once_per_user_probe_hit(self, fake_pool):
        scheduler, index, prober, user_watchlist, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        hit = _recent_hit(front_text="written by eric niebler")
        _set_iso_cycle(prober, _success_cycle([hit]))
        index.papers = {}

        user_watchlist.matches_for_users.return_value = {
            "U123": PerUserMatches(papers=[], probe_hits=[(hit, MatchReason.AUTHOR)])
        }
        result = await scheduler.poll_once()
        assert "U123" in result.per_user_matches
        assert len(result.per_user_matches["U123"].probe_hits) == 1

    async def test_poll_once_calls_notify_callback(self, fake_pool):
        notified = []
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        scheduler.notify_callback = notified.append
        await scheduler.poll_once()  # seed
        await scheduler.poll_once()  # real poll
        assert len(notified) == 1

    async def test_cold_start_first_poll_does_not_notify(self, fake_pool):
        notified = []
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        scheduler.notify_callback = notified.append
        result = await scheduler.poll_once()
        assert notified == []
        assert result.probe_hits == []

    async def test_restart_with_prior_poll_notifies_seed_hits(self, fake_pool):
        notified = []
        scheduler, _, prober, user_watchlist, state = _make_scheduler(fake_pool)
        scheduler.notify_callback = notified.append
        state.touch_poll()
        hit = _recent_hit()
        _set_iso_cycle(prober, _success_cycle([hit]))
        user_watchlist.matches_for_users.return_value = {
            "U123": PerUserMatches(papers=[], probe_hits=[(hit, MatchReason.AUTHOR)])
        }
        result = await scheduler.poll_once()
        assert len(notified) == 1
        assert len(result.probe_hits) == 1
        assert result.probe_hits[0].is_recent is True

    async def test_restart_with_discovered_urls_notifies(self, fake_pool):
        notified = []
        scheduler, _, prober, user_watchlist, state = _make_scheduler(fake_pool)
        scheduler.notify_callback = notified.append
        state.mark_discovered("https://isocpp.org/files/papers/D1111R0.pdf")
        hit = _recent_hit()
        _set_iso_cycle(prober, _success_cycle([hit]))
        user_watchlist.matches_for_users.return_value = {
            "U123": PerUserMatches(papers=[], probe_hits=[(hit, MatchReason.AUTHOR)])
        }
        result = await scheduler.poll_once()
        assert len(notified) == 1
        assert len(result.probe_hits) == 1

    async def test_restart_seed_old_hits_not_in_result(self, fake_pool, caplog):
        import logging

        notified = []
        scheduler, _, prober, _, state = _make_scheduler(fake_pool)
        scheduler.notify_callback = notified.append
        state.touch_poll()
        old = _old_hit()
        _set_iso_cycle(prober, _success_cycle([old]))
        with caplog.at_level(logging.INFO):
            result = await scheduler.poll_once()
        assert result.probe_hits == []
        assert "PROBE-OLD" in caplog.text

    async def test_poll_once_skips_refresh_when_disabled(self, fake_pool):
        scheduler, index, _, _, _ = _make_scheduler(fake_pool, enable_bulk_wg21=False)
        scheduler._seeded = True
        scheduler._snapshots[SOURCE_WG21_INDEX] = {}
        await scheduler.poll_once()
        index.fetch.assert_not_called()

    async def test_poll_once_skips_probe_when_disabled(self, fake_pool):
        scheduler, _, prober, _, _ = _make_scheduler(fake_pool, enable_iso_probe=False)
        scheduler._seeded = True
        scheduler._snapshots[SOURCE_WG21_INDEX] = {}
        await scheduler.poll_once()
        prober.fetch.assert_not_called()

    async def test_seed_does_not_log_index_new_on_baseline(self, fake_pool, caplog):
        import logging

        scheduler, wg21, _, _, _ = _make_scheduler(fake_pool)
        paper = Paper(id="P9999R0", title="Baseline", author="Author", date="2024-01-01")
        wg21.papers = {"P9999R0": paper}

        with caplog.at_level(logging.INFO):
            await scheduler.seed()

        assert "INDEX-NEW" not in caplog.text
        assert scheduler._snapshots[SOURCE_WG21_INDEX] == {"P9999R0": paper}

    async def test_seed_marks_discovered(self, fake_pool):
        scheduler, _, prober, _, state = _make_scheduler(fake_pool)
        hit = _recent_hit()

        async def fake_fetch():
            state.mark_discovered(hit.url)
            return _success_cycle([hit])

        prober.fetch = AsyncMock(side_effect=fake_fetch)
        seed_result = await scheduler.seed()
        assert seed_result.probe_hits == [hit]
        assert state.is_discovered(hit.url)

    async def test_run_forever_reraises_cancelled_error_without_shutdown_event(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)

        async def mock_poll_once():
            raise asyncio.CancelledError()

        scheduler.poll_once = mock_poll_once
        with patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await scheduler.run_forever()

    async def test_run_forever_calls_poll_and_breaks_on_shutdown(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        shutdown_event = asyncio.Event()
        call_count = 0

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            shutdown_event.set()

        scheduler.poll_once = mock_poll_once
        with patch("asyncio.sleep", AsyncMock()):
            await scheduler.run_forever(shutdown_event)
        assert call_count == 1

    async def test_run_forever_continues_after_poll_exception(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(
            fake_pool, poll_interval_minutes=0, poll_overrun_cooldown_seconds=0
        )
        shutdown_event = asyncio.Event()
        call_count = 0

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("poll failed")
            shutdown_event.set()

        scheduler.poll_once = mock_poll_once
        with patch("asyncio.wait_for", _wait_for_timeout):
            await scheduler.run_forever(shutdown_event)
        assert call_count == 2

    async def test_run_forever_emits_timeout_failure_category(self, fake_pool, caplog):
        scheduler, _, _, _, _ = _make_scheduler(
            fake_pool, poll_interval_minutes=0, poll_overrun_cooldown_seconds=0
        )
        shutdown_event = asyncio.Event()
        call_count = 0

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.TimeoutException("boom", request=MagicMock())
            shutdown_event.set()

        scheduler.poll_once = mock_poll_once
        with caplog.at_level(logging.ERROR, logger="paperscout.monitor"):
            with patch("asyncio.wait_for", _wait_for_timeout):
                await scheduler.run_forever(shutdown_event)
        assert "failure_category=TIMEOUT" in caplog.text
        assert call_count == 2

    async def test_run_forever_emits_network_failure_category(self, fake_pool, caplog):
        scheduler, _, _, _, _ = _make_scheduler(
            fake_pool, poll_interval_minutes=0, poll_overrun_cooldown_seconds=0
        )
        shutdown_event = asyncio.Event()
        call_count = 0

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("no route", request=MagicMock())
            shutdown_event.set()

        scheduler.poll_once = mock_poll_once
        with caplog.at_level(logging.ERROR, logger="paperscout.monitor"):
            with patch("asyncio.wait_for", _wait_for_timeout):
                await scheduler.run_forever(shutdown_event)
        assert "failure_category=NETWORK" in caplog.text
        assert call_count == 2

    async def test_run_forever_exits_on_shutdown_event_during_sleep(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool, poll_interval_minutes=30)
        shutdown_event = asyncio.Event()

        async def mock_poll_once():
            shutdown_event.set()

        scheduler.poll_once = mock_poll_once
        with patch("asyncio.sleep", AsyncMock()) as sleep_m:
            await scheduler.run_forever(shutdown_event)
        sleep_m.assert_not_called()

    async def test_run_forever_exits_when_event_set_before_first_poll(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        shutdown_event = asyncio.Event()
        shutdown_event.set()
        scheduler.poll_once = AsyncMock()
        await scheduler.run_forever(shutdown_event)
        scheduler.poll_once.assert_not_called()

    async def test_run_forever_cancels_in_flight_poll(self, fake_pool, caplog):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        shutdown_event = asyncio.Event()
        poll_started = asyncio.Event()

        async def slow_poll_once():
            poll_started.set()
            await asyncio.Event().wait()

        async def request_shutdown():
            await poll_started.wait()
            shutdown_event.set()

        scheduler.poll_once = slow_poll_once
        stopper = asyncio.create_task(request_shutdown())
        try:
            with caplog.at_level(logging.INFO, logger="paperscout.monitor"):
                await scheduler.run_forever(shutdown_event)
        finally:
            stopper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopper
        assert "POLL-CANCELLED" in caplog.text

    async def test_failed_probe_cycle_does_not_advance_last_successful_poll_normal_path(
        self, fake_pool
    ):
        """Main poll path: FAILED cycle must not advance staleness clock."""
        scheduler, index, prober, _, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()
        before = scheduler._last_successful_poll
        index.papers = {}
        _set_iso_cycle(prober, _failed_cycle("network down"))
        await scheduler.poll_once()
        assert scheduler._last_successful_poll == before
        assert scheduler._last_cycle_status == CycleStatus.FAILED

    async def test_failed_probe_cycle_does_not_advance_last_successful_poll_seed_early_return(
        self, fake_pool
    ):
        """Fresh deploy (line 172 path): FAILED seed probe must not set last_successful_poll."""
        scheduler, _, prober, _, state = _make_scheduler(fake_pool)
        assert state.last_poll == 0
        assert len(state.get_all_discovered()) == 0
        _set_iso_cycle(prober, _failed_cycle("connect error"))
        await scheduler.poll_once()
        assert scheduler._last_successful_poll is None
        assert scheduler._last_cycle_status == CycleStatus.FAILED

    async def test_health_snapshot_after_poll(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()
        snap = scheduler.health_snapshot()
        assert snap["last_updated"] is not None
        assert snap["poll_count"] >= 1

    def test_health_snapshot_defaults_are_independent_copies(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        a = scheduler.health_snapshot()
        b = scheduler.health_snapshot()
        a["probe_stats"]["miss"] = 999
        assert b["probe_stats"] == {}
        assert a is not b

    async def test_publish_health_snapshot_probe_stats_readonly(self, fake_pool):
        from paperscout.monitor import SchedulerSnapshot

        scheduler, _, prober, _, _ = _make_scheduler(fake_pool)
        prober.snapshot_stats = MagicMock(return_value={"miss": 1, "error": 0})
        await scheduler.poll_once()
        with scheduler._health_lock:
            stored = scheduler._health_snapshot
        assert isinstance(stored, SchedulerSnapshot)
        with pytest.raises(TypeError):
            stored.probe_stats["miss"] = 999  # type: ignore[index]
        assert scheduler.health_snapshot()["probe_stats"]["miss"] == 1

    async def test_run_forever_halts_on_configuration_error(self, fake_pool, caplog):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool, poll_interval_minutes=30)

        async def mock_poll_once():
            raise ConfigurationError("simulated credential failure")

        scheduler.poll_once = mock_poll_once
        with caplog.at_level(logging.CRITICAL, logger="paperscout.monitor"):
            with patch("asyncio.sleep", AsyncMock()) as sleep_m:
                await scheduler.run_forever()
        sleep_m.assert_not_called()
        assert "failure_category=CONFIGURATION" in caplog.text

    async def test_run_forever_adaptive_sleep_normal_cycle(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(
            fake_pool, poll_interval_minutes=30, poll_overrun_cooldown_seconds=300
        )
        shutdown_event = asyncio.Event()
        call_count = 0
        slept: list[float] = []

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                shutdown_event.set()

        def capture_wait_for(awaitable, timeout=None):
            if hasattr(awaitable, "close"):
                awaitable.close()
            if timeout is not None:
                slept.append(timeout)
            raise asyncio.TimeoutError

        with patch("paperscout.monitor.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 360.0, 0.0, 1.0]
            scheduler.poll_once = mock_poll_once
            with patch("asyncio.wait_for", side_effect=capture_wait_for):
                await scheduler.run_forever(shutdown_event)

        assert len(slept) == 1
        assert slept[0] == pytest.approx(1440.0)

    async def test_run_forever_adaptive_sleep_overrun_cycle(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(
            fake_pool, poll_interval_minutes=30, poll_overrun_cooldown_seconds=300
        )
        shutdown_event = asyncio.Event()
        call_count = 0
        slept: list[float] = []

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                shutdown_event.set()

        def capture_wait_for(awaitable, timeout=None):
            if hasattr(awaitable, "close"):
                awaitable.close()
            if timeout is not None:
                slept.append(timeout)
            raise asyncio.TimeoutError

        with patch("paperscout.monitor.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 2000.0, 0.0, 1.0]
            scheduler.poll_once = mock_poll_once
            with patch("asyncio.wait_for", side_effect=capture_wait_for):
                await scheduler.run_forever(shutdown_event)

        assert len(slept) == 1
        assert slept[0] == pytest.approx(300.0)
