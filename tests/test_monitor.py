"""Tests for paperbot.monitor."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperbot.models import Paper
from paperbot.monitor import (
    DiffResult,
    PollResult,
    Scheduler,
    Watchlist,
    diff_snapshots,
)
from paperbot.sources import ISOProber, ProbeHit, WG21Index
from paperbot.storage import ProbeState
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


# ── Watchlist ─────────────────────────────────────────────────────────────────

class TestWatchlist:
    def test_initial_empty(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        assert wl.authors == []

    def test_add_author(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        assert wl.add_author("Niebler") is True
        assert "niebler" in wl.authors

    def test_add_author_duplicate_returns_false(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        wl.add_author("Niebler")
        assert wl.add_author("Niebler") is False
        assert wl.authors.count("niebler") == 1

    def test_add_author_case_insensitive_dedup(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        wl.add_author("NIEBLER")
        assert wl.add_author("niebler") is False

    def test_add_empty_string_returns_false(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        assert wl.add_author("") is False
        assert wl.add_author("   ") is False

    def test_remove_author(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        wl.add_author("Niebler")
        assert wl.remove_author("Niebler") is True
        assert "niebler" not in wl.authors

    def test_remove_nonexistent_returns_false(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        assert wl.remove_author("Nobody") is False

    def test_persists_to_file(self, tmp_path):
        path = tmp_path / "wl.json"
        wl = Watchlist(path)
        wl.add_author("Stroustrup")
        wl2 = Watchlist(path)
        assert "stroustrup" in wl2.authors

    def test_loads_from_existing_file(self, tmp_path):
        import json
        path = tmp_path / "wl.json"
        path.write_text(json.dumps({"authors": ["baker", "niebler"]}), encoding="utf-8")
        wl = Watchlist(path)
        assert "baker" in wl.authors

    def test_corrupt_file_starts_empty(self, tmp_path):
        path = tmp_path / "wl.json"
        path.write_text("not json", encoding="utf-8")
        wl = Watchlist(path)
        assert wl.authors == []

    def test_matches_paper_author(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        wl.add_author("Niebler")
        paper = Paper(id="P2300R10", author="Eric Niebler")
        assert "niebler" in wl.matches(paper)

    def test_matches_no_author(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        wl.add_author("Niebler")
        assert wl.matches(Paper(id="P2300R10", author="")) == []

    def test_authors_returns_copy(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        wl.add_author("Test")
        authors = wl.authors
        authors.clear()
        assert "test" in wl.authors


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

    def test_empty_to_empty(self):
        result = diff_snapshots({}, {})
        assert result.new_papers == [] and result.updated_papers == []


# ── PollResult ────────────────────────────────────────────────────────────────

class TestPollResult:
    def test_default_probe_watchlist_hits(self):
        diff = DiffResult(new_papers=[], updated_papers=[])
        result = PollResult(diff=diff, probe_hits=[], watchlist_matches=[])
        assert result.probe_watchlist_hits == []
        assert result.dp_transitions == []

    def test_explicit_probe_watchlist_hits(self):
        hit = _recent_hit()
        diff = DiffResult(new_papers=[], updated_papers=[])
        result = PollResult(diff=diff, probe_hits=[], watchlist_matches=[],
                            probe_watchlist_hits=[hit])
        assert len(result.probe_watchlist_hits) == 1


# ── Scheduler ─────────────────────────────────────────────────────────────────

def _make_scheduler(tmp_path, **cfg_overrides):
    index = MagicMock(spec=WG21Index)
    index.refresh = AsyncMock()
    index.papers = {}
    prober = MagicMock(spec=ISOProber)
    prober.run_cycle = AsyncMock(return_value=[])
    watchlist = Watchlist(tmp_path / "wl.json")
    state = ProbeState(tmp_path / "state.json")
    cfg = make_test_settings(**cfg_overrides)
    scheduler = Scheduler(
        index=index, prober=prober,
        watchlist=watchlist, state=state, cfg=cfg,
    )
    return scheduler, index, prober, watchlist, state


class TestScheduler:
    async def test_poll_once_seeds_on_first_call(self, tmp_path):
        scheduler, index, prober, _, _ = _make_scheduler(tmp_path)
        await scheduler.poll_once()
        index.refresh.assert_called_once()
        prober.run_cycle.assert_called_once()
        assert scheduler._seeded

    async def test_poll_once_seeded_returns_empty_on_seed(self, tmp_path):
        scheduler, _, _, _, _ = _make_scheduler(tmp_path)
        result = await scheduler.poll_once()
        assert result.diff.new_papers == []

    async def test_poll_once_detects_new_papers(self, tmp_path):
        scheduler, index, prober, _, _ = _make_scheduler(tmp_path)
        await scheduler.poll_once()

        new_paper = Paper(id="P9999R0", title="New", author="Author", date="2024-01-01")
        index.papers = {"P9999R0": new_paper}
        prober.run_cycle = AsyncMock(return_value=[])
        result = await scheduler.poll_once()
        assert len(result.diff.new_papers) == 1

    async def test_poll_once_surfaces_only_recent_probe_hits(self, tmp_path):
        """Probe hits with is_recent=False should not appear in result.probe_hits."""
        scheduler, index, prober, _, _ = _make_scheduler(tmp_path)
        await scheduler.poll_once()

        recent = _recent_hit()
        old = _old_hit()
        index.papers = {}
        prober.run_cycle = AsyncMock(return_value=[recent, old])
        result = await scheduler.poll_once()
        assert len(result.probe_hits) == 1
        assert result.probe_hits[0].is_recent is True

    async def test_poll_once_detects_dp_transition(self, tmp_path):
        """When a D-paper we probed appears as P-paper in the index, flag it."""
        scheduler, index, prober, _, state = _make_scheduler(tmp_path)
        await scheduler.poll_once()  # seed

        # Simulate: we previously probed D9999R0.pdf
        draft_url = "https://isocpp.org/files/papers/D9999R0.pdf"
        state.mark_discovered(draft_url, last_modified_ts=1_700_000_000.0)

        # Now P9999R0 appears in the wg21.link index
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

    async def test_poll_once_dp_skip_non_p_papers(self, tmp_path):
        """N-papers and other non-P entries in the diff must not cause a D→P check."""
        scheduler, index, prober, _, state = _make_scheduler(tmp_path)
        await scheduler.poll_once()

        # N-paper (no revision property → paper.revision is None)
        n_paper = Paper(id="N4950", title="Working Draft", author="Ed", date="2025-01-01")
        index.papers = {"N4950": n_paper}
        prober.run_cycle = AsyncMock(return_value=[])

        result = await scheduler.poll_once()
        assert result.dp_transitions == []

    async def test_poll_once_no_dp_transition_when_no_draft(self, tmp_path):
        """No D→P alert when the new P-paper has no matching discovered D-draft."""
        scheduler, index, prober, _, state = _make_scheduler(tmp_path)
        await scheduler.poll_once()

        new_paper = Paper(id="P8888R0", title="Entirely New", author="X", date="2025-01-01")
        index.papers = {"P8888R0": new_paper}
        prober.run_cycle = AsyncMock(return_value=[])

        result = await scheduler.poll_once()
        assert result.dp_transitions == []

    async def test_poll_once_dp_transition_logged(self, tmp_path, caplog):
        import logging
        scheduler, index, prober, _, state = _make_scheduler(tmp_path)
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

    async def test_poll_count_increments(self, tmp_path):
        scheduler, _, _, _, _ = _make_scheduler(tmp_path)
        assert scheduler._poll_count == 0
        await scheduler.poll_once()
        assert scheduler._poll_count == 1
        await scheduler.poll_once()
        assert scheduler._poll_count == 2

    async def test_poll_once_logs_updated_papers(self, tmp_path, caplog):
        import logging
        scheduler, index, prober, _, _ = _make_scheduler(tmp_path)
        await scheduler.poll_once()  # seed

        old_paper = Paper(id="P9999R0", title="Old Title", author="A", date="2024-01-01")
        scheduler._previous_papers = {"P9999R0": old_paper}
        updated_paper = Paper(id="P9999R0", title="New Title", author="A", date="2024-01-01")
        index.papers = {"P9999R0": updated_paper}
        prober.run_cycle = AsyncMock(return_value=[])
        with caplog.at_level(logging.DEBUG):
            await scheduler.poll_once()
        assert "INDEX-UPD" in caplog.text

    async def test_poll_old_hits_logged(self, tmp_path, caplog):
        import logging
        scheduler, index, prober, _, _ = _make_scheduler(tmp_path)
        await scheduler.poll_once()
        old = _old_hit()
        index.papers = {}
        prober.run_cycle = AsyncMock(return_value=[old])
        with caplog.at_level(logging.INFO):
            result = await scheduler.poll_once()
        assert result.probe_hits == []          # old hit not surfaced
        assert "PROBE-OLD" in caplog.text

    async def test_poll_once_detects_watchlist_match_from_index(self, tmp_path):
        scheduler, index, prober, watchlist, _ = _make_scheduler(tmp_path)
        watchlist.add_author("niebler")
        await scheduler.poll_once()

        new_paper = Paper(id="P9999R0", title="Senders", author="Eric Niebler", date="2024-01-01")
        index.papers = {"P9999R0": new_paper}
        prober.run_cycle = AsyncMock(return_value=[])
        result = await scheduler.poll_once()
        assert len(result.watchlist_matches) == 1

    async def test_poll_once_detects_probe_watchlist_hit(self, tmp_path):
        scheduler, index, prober, watchlist, _ = _make_scheduler(tmp_path)
        watchlist.add_author("niebler")
        await scheduler.poll_once()

        hit = _recent_hit(front_text="written by eric niebler")
        prober.run_cycle = AsyncMock(return_value=[hit])
        index.papers = {}
        result = await scheduler.poll_once()
        assert len(result.probe_watchlist_hits) == 1

    async def test_poll_once_calls_notify_callback(self, tmp_path):
        notified = []
        scheduler, _, _, _, _ = _make_scheduler(tmp_path)
        scheduler.notify_callback = notified.append
        await scheduler.poll_once()   # seed
        await scheduler.poll_once()   # real poll
        assert len(notified) == 1

    async def test_poll_once_skips_refresh_when_disabled(self, tmp_path):
        scheduler, index, _, _, _ = _make_scheduler(tmp_path, enable_bulk_wg21=False)
        scheduler._seeded = True
        scheduler._previous_papers = {}
        await scheduler.poll_once()
        index.refresh.assert_not_called()

    async def test_poll_once_skips_probe_when_disabled(self, tmp_path):
        scheduler, _, prober, _, _ = _make_scheduler(tmp_path, enable_iso_probe=False)
        scheduler._seeded = True
        scheduler._previous_papers = {}
        await scheduler.poll_once()
        prober.run_cycle.assert_not_called()

    async def test_seed_marks_discovered(self, tmp_path):
        scheduler, _, prober, _, state = _make_scheduler(tmp_path)
        hit = _recent_hit()
        prober.run_cycle = AsyncMock(return_value=[hit])
        await scheduler.seed()
        assert state.is_discovered(hit.url)

    async def test_run_forever_calls_poll_and_breaks_on_cancel(self, tmp_path):
        scheduler, _, _, _, _ = _make_scheduler(tmp_path)
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

    async def test_run_forever_continues_after_poll_exception(self, tmp_path):
        scheduler, _, _, _, _ = _make_scheduler(tmp_path, poll_interval_minutes=0)
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

    async def test_run_forever_adaptive_sleep_normal_cycle(self, tmp_path):
        """When poll finishes early, sleep = interval - elapsed (not full interval)."""
        scheduler, _, _, _, _ = _make_scheduler(tmp_path,
                                                 poll_interval_minutes=30,
                                                 poll_overrun_cooldown_seconds=300)
        call_count = 0
        slept: list[float] = []

        async def mock_poll_once():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError()  # cancel on 2nd call

        async def capture_sleep(duration: float):
            slept.append(duration)

        with patch("paperbot.monitor.time") as mock_time:
            # cycle 1: t0=0.0, elapsed=360.0  (poll took 360s)
            # cycle 2: t0=whatever (CancelledError raised before elapsed is read)
            mock_time.monotonic.side_effect = [0.0, 360.0, 0.0]
            scheduler.poll_once = mock_poll_once
            with patch("asyncio.sleep", capture_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.run_forever()

        # interval=1800s, elapsed=360s → sleep = max(1800-360, 300) = 1440s
        assert len(slept) == 1
        assert slept[0] == pytest.approx(1440.0)

    async def test_run_forever_adaptive_sleep_overrun_cycle(self, tmp_path):
        """When poll overruns the interval, sleep = cooldown (not negative)."""
        scheduler, _, _, _, _ = _make_scheduler(tmp_path,
                                                 poll_interval_minutes=30,
                                                 poll_overrun_cooldown_seconds=300)
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
            # cycle 1: poll took 2000s > 1800s (overrun)
            mock_time.monotonic.side_effect = [0.0, 2000.0, 0.0]
            scheduler.poll_once = mock_poll_once
            with patch("asyncio.sleep", capture_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await scheduler.run_forever()

        # interval=1800s, elapsed=2000s → sleep = max(1800-2000, 300) = max(-200, 300) = 300s
        assert len(slept) == 1
        assert slept[0] == pytest.approx(300.0)
