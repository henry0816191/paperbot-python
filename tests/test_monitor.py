"""Tests for paperbot.monitor."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperbot.models import Paper
from paperbot.monitor import (
    DiffResult,
    PerUserMatches,
    PollResult,
    Scheduler,
    diff_snapshots,
)
from paperbot.sources import ISOProber, ProbeHit, WG21Index
from paperbot.storage import ProbeState, UserWatchlist
from tests.conftest import make_test_settings


def _recent_hit(**kwargs) -> ProbeHit:
    defaults = dict(
        url="https://isocpp.org/files/papers/D9999R0.pdf",
        prefix="D", number=9999, revision=0, extension=".pdf",
        tier="frontier", is_recent=True,
    )
    defaults.update(kwargs)
    return ProbeHit(**defaults)


def _old_hit(**kwargs) -> ProbeHit:
    defaults = dict(
        url="https://isocpp.org/files/papers/D8888R0.pdf",
        prefix="D", number=8888, revision=0, extension=".pdf",
        tier="cold", is_recent=False,
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
            "P2301R0":  self._paper("P2301R0",  date="2024-06-01"),
            "P2302R0":  self._paper("P2302R0",  date="2024-03-01"),
        }
        result = diff_snapshots(prev, curr)
        dates = [p.date for p in result.new_papers]
        assert dates == sorted(dates, reverse=True)

    def test_empty_to_empty(self):
        result = diff_snapshots({}, {})
        assert result.new_papers == [] and result.updated_papers == []


# ── PollResult ────────────────────────────────────────────────────────────────

class TestPollResult:
    def test_defaults(self):
        diff = DiffResult(new_papers=[], updated_papers=[])
        result = PollResult(diff=diff, probe_hits=[])
        assert result.dp_transitions == []
        assert result.per_user_matches == {}

    def test_explicit_dp_transitions(self):
        from paperbot.monitor import DPTransition
        diff = DiffResult(new_papers=[], updated_papers=[])
        paper = Paper(id="P2300R11")
        tr = DPTransition(paper=paper, draft_url="http://x", last_modified=None, discovered_at=0.0)
        result = PollResult(diff=diff, probe_hits=[], dp_transitions=[tr])
        assert len(result.dp_transitions) == 1

    def test_explicit_per_user_matches(self):
        diff = DiffResult(new_papers=[], updated_papers=[])
        paper = Paper(id="P2300R11")
        pum = PerUserMatches(papers=[(paper, "author")], probe_hits=[])
        result = PollResult(diff=diff, probe_hits=[], per_user_matches={"U1": pum})
        assert "U1" in result.per_user_matches


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _make_scheduler(fake_pool, **cfg_overrides):
    index = MagicMock(spec=WG21Index)
    index.refresh = AsyncMock()
    index.papers = {}
    prober = MagicMock(spec=ISOProber)
    prober.run_cycle = AsyncMock(return_value=[])
    user_watchlist = MagicMock(spec=UserWatchlist)
    user_watchlist.matches_for_users.return_value = {}
    state = ProbeState(fake_pool)
    cfg = make_test_settings(**cfg_overrides)
    scheduler = Scheduler(
        index=index, prober=prober,
        user_watchlist=user_watchlist, state=state, cfg=cfg,
    )
    return scheduler, index, prober, user_watchlist, state


class TestScheduler:
    async def test_poll_once_seeds_on_first_call(self, fake_pool):
        scheduler, index, prober, _, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()
        index.refresh.assert_called_once()
        prober.run_cycle.assert_called_once()
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
        prober.run_cycle = AsyncMock(return_value=[])
        result = await scheduler.poll_once()
        assert len(result.diff.new_papers) == 1

    async def test_poll_once_surfaces_only_recent_probe_hits(self, fake_pool):
        scheduler, index, prober, _, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        recent = _recent_hit()
        old = _old_hit()
        index.papers = {}
        prober.run_cycle = AsyncMock(return_value=[recent, old])
        result = await scheduler.poll_once()
        assert len(result.probe_hits) == 1
        assert result.probe_hits[0].is_recent is True

    async def test_poll_once_detects_dp_transition(self, fake_pool):
        scheduler, index, prober, _, state = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        draft_url = "https://isocpp.org/files/papers/D9999R0.pdf"
        state.mark_discovered(draft_url, last_modified_ts=1_700_000_000.0)

        new_paper = Paper(id="P9999R0", title="New Published Paper",
                          author="Author", date="2025-01-01")
        index.papers = {"P9999R0": new_paper}
        prober.run_cycle = AsyncMock(return_value=[])

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
        prober.run_cycle = AsyncMock(return_value=[])

        result = await scheduler.poll_once()
        assert result.dp_transitions == []

    async def test_poll_once_no_dp_transition_when_no_draft(self, fake_pool):
        scheduler, index, prober, _, state = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        new_paper = Paper(id="P8888R0", title="Entirely New", author="X", date="2025-01-01")
        index.papers = {"P8888R0": new_paper}
        prober.run_cycle = AsyncMock(return_value=[])

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
        prober.run_cycle = AsyncMock(return_value=[])

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
        scheduler._previous_papers = {"P9999R0": old_paper}
        updated_paper = Paper(id="P9999R0", title="New Title", author="A", date="2024-01-01")
        index.papers = {"P9999R0": updated_paper}
        prober.run_cycle = AsyncMock(return_value=[])
        with caplog.at_level(logging.DEBUG):
            await scheduler.poll_once()
        assert "INDEX-UPD" in caplog.text

    async def test_poll_old_hits_logged(self, fake_pool, caplog):
        import logging
        scheduler, index, prober, _, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()
        old = _old_hit()
        index.papers = {}
        prober.run_cycle = AsyncMock(return_value=[old])
        with caplog.at_level(logging.INFO):
            result = await scheduler.poll_once()
        assert result.probe_hits == []
        assert "PROBE-OLD" in caplog.text

    async def test_poll_once_populates_per_user_matches(self, fake_pool):
        scheduler, index, prober, user_watchlist, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        new_paper = Paper(id="P9999R0", title="Senders", author="Eric Niebler", date="2024-01-01")
        index.papers = {"P9999R0": new_paper}
        prober.run_cycle = AsyncMock(return_value=[])

        user_watchlist.matches_for_users.return_value = {
            "U123": PerUserMatches(papers=[(new_paper, "author")], probe_hits=[])
        }
        result = await scheduler.poll_once()
        assert "U123" in result.per_user_matches
        assert len(result.per_user_matches["U123"].papers) == 1

    async def test_poll_once_per_user_probe_hit(self, fake_pool):
        scheduler, index, prober, user_watchlist, _ = _make_scheduler(fake_pool)
        await scheduler.poll_once()

        hit = _recent_hit(front_text="written by eric niebler")
        prober.run_cycle = AsyncMock(return_value=[hit])
        index.papers = {}

        user_watchlist.matches_for_users.return_value = {
            "U123": PerUserMatches(papers=[], probe_hits=[(hit, "author")])
        }
        result = await scheduler.poll_once()
        assert "U123" in result.per_user_matches
        assert len(result.per_user_matches["U123"].probe_hits) == 1

    async def test_poll_once_calls_notify_callback(self, fake_pool):
        notified = []
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        scheduler.notify_callback = notified.append
        await scheduler.poll_once()   # seed
        await scheduler.poll_once()   # real poll
        assert len(notified) == 1

    async def test_poll_once_skips_refresh_when_disabled(self, fake_pool):
        scheduler, index, _, _, _ = _make_scheduler(fake_pool, enable_bulk_wg21=False)
        scheduler._seeded = True
        scheduler._previous_papers = {}
        await scheduler.poll_once()
        index.refresh.assert_not_called()

    async def test_poll_once_skips_probe_when_disabled(self, fake_pool):
        scheduler, _, prober, _, _ = _make_scheduler(fake_pool, enable_iso_probe=False)
        scheduler._seeded = True
        scheduler._previous_papers = {}
        await scheduler.poll_once()
        prober.run_cycle.assert_not_called()

    async def test_seed_marks_discovered(self, fake_pool):
        scheduler, _, prober, _, state = _make_scheduler(fake_pool)
        hit = _recent_hit()
        prober.run_cycle = AsyncMock(return_value=[hit])
        await scheduler.seed()
        assert state.is_discovered(hit.url)

    async def test_run_forever_calls_poll_and_breaks_on_cancel(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool)
        call_count = 0

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError()

        scheduler.poll_once = mock_poll_once
        with patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await scheduler.run_forever()
        assert call_count == 1

    async def test_run_forever_continues_after_poll_exception(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(fake_pool, poll_interval_minutes=0)
        call_count = 0

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("poll failed")
            raise asyncio.CancelledError()

        scheduler.poll_once = mock_poll_once
        with patch("asyncio.sleep", AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await scheduler.run_forever()
        assert call_count == 2

    async def test_run_forever_adaptive_sleep_normal_cycle(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(
            fake_pool, poll_interval_minutes=30, poll_overrun_cooldown_seconds=300
        )
        call_count = 0
        slept: list[float] = []

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()

        async def capture_sleep(duration: float):
            slept.append(duration)

        with patch("paperbot.monitor.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 360.0, 0.0]
            scheduler.poll_once = mock_poll_once
            with patch("asyncio.sleep", capture_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.run_forever()

        assert len(slept) == 1
        assert slept[0] == pytest.approx(1440.0)

    async def test_run_forever_adaptive_sleep_overrun_cycle(self, fake_pool):
        scheduler, _, _, _, _ = _make_scheduler(
            fake_pool, poll_interval_minutes=30, poll_overrun_cooldown_seconds=300
        )
        call_count = 0
        slept: list[float] = []

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()

        async def capture_sleep(duration: float):
            slept.append(duration)

        with patch("paperbot.monitor.time") as mock_time:
            mock_time.monotonic.side_effect = [0.0, 2000.0, 0.0]
            scheduler.poll_once = mock_poll_once
            with patch("asyncio.sleep", capture_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.run_forever()

        assert len(slept) == 1
        assert slept[0] == pytest.approx(300.0)
