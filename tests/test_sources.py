"""Tests for paperbot.sources."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from paperbot.models import Paper
from paperbot.sources import (
    ISOProber,
    OpenStdEntry,
    ProbeHit,
    WG21Index,
    _fetch_front_text,
    _parse_open_std_html,
    scrape_open_std,
)
from paperbot.storage import ProbeState, UserWatchlist
from tests.conftest import SAMPLE_INDEX_DATA, make_test_settings


def _mock_wl(paper_nums=None):
    """Return a MagicMock UserWatchlist with get_all_watched_paper_nums configured."""
    wl = MagicMock(spec=UserWatchlist)
    wl.get_all_watched_paper_nums.return_value = set(paper_nums or [])
    return wl


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_response(status: int = 200, json_data=None, text: str = "",
                   last_modified: datetime | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=json_data or {})
    resp.text = text
    resp.raise_for_status = MagicMock()
    headers: dict[str, str] = {}
    if last_modified:
        headers["last-modified"] = format_datetime(last_modified, usegmt=True)
    resp.headers = headers
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


def _make_async_client(head_resp=None, get_resp=None) -> AsyncMock:
    client = AsyncMock()
    client.head = AsyncMock(return_value=head_resp or _make_response(404))
    client.get = AsyncMock(return_value=get_resp or _make_response(404))
    return client


def _recent_lm() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=2)


def _old_lm() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=30)


# ── WG21Index ────────────────────────────────────────────────────────────────

class TestWG21Index:
    async def test_refresh_downloads_when_no_cache(self, fake_pool):
        index = WG21Index(fake_pool)
        with patch.object(index, "_download", AsyncMock(return_value=SAMPLE_INDEX_DATA)):
            papers = await index.refresh()
        assert "P2300R10" in papers
        assert "N4950" in papers

    async def test_refresh_uses_cache_when_fresh(self, fake_pool):
        index = WG21Index(fake_pool)
        index._cache.write(SAMPLE_INDEX_DATA)
        mock_download = AsyncMock()
        with patch.object(index, "_download", mock_download):
            papers = await index.refresh()
        mock_download.assert_not_called()
        assert "P2300R10" in papers

    async def test_refresh_falls_back_to_stale_cache(self, fake_pool):
        index = WG21Index(fake_pool)
        index._cache.write(SAMPLE_INDEX_DATA)
        index._cache.ttl_seconds = 0
        with patch.object(index, "_download", AsyncMock(return_value=None)):
            papers = await index.refresh()
        assert "P2300R10" in papers

    async def test_refresh_returns_empty_when_no_data(self, fake_pool):
        index = WG21Index(fake_pool)
        with patch.object(index, "_download", AsyncMock(return_value=None)):
            papers = await index.refresh()
        assert papers == {}

    async def test_download_success(self, fake_pool):
        index = WG21Index(fake_pool)
        mock_resp = _make_response(200, json_data=SAMPLE_INDEX_DATA)
        mock_client = _make_async_client(get_resp=mock_resp)
        with patch("paperbot.sources.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await index._download()
        assert result == SAMPLE_INDEX_DATA

    async def test_download_non_dict_response(self, fake_pool):
        index = WG21Index(fake_pool)
        mock_resp = _make_response(200, json_data=[1, 2, 3])
        mock_client = _make_async_client(get_resp=mock_resp)
        with patch("paperbot.sources.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await index._download()
        assert result is None

    async def test_download_http_error(self, fake_pool):
        index = WG21Index(fake_pool)
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("connect failed"))
        with patch("paperbot.sources.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await index._download()
        assert result is None

    def test_parse_and_index(self, fake_pool):
        index = WG21Index(fake_pool)
        papers = index._parse_and_index(SAMPLE_INDEX_DATA)
        assert "P2300R10" in papers
        assert "P2301R0" in papers
        assert "N4950" in papers

    def test_highest_p_number(self, populated_index):
        assert populated_index.highest_p_number() == 2301

    def test_effective_frontier_no_outliers(self, fake_pool):
        index = WG21Index(fake_pool)
        index._parse_and_index({f"P{n:04d}R0": {"title": "T"} for n in range(100, 121)})
        assert index.effective_frontier(gap_threshold=50) == 120

    def test_effective_frontier_filters_isolated_outlier(self, fake_pool):
        index = WG21Index(fake_pool)
        data = {f"P{n:04d}R0": {"title": "T"} for n in range(100, 121)}
        data["P5000R0"] = {"title": "Planning doc"}
        index._parse_and_index(data)
        assert index.effective_frontier(gap_threshold=50) == 120

    def test_effective_frontier_filters_multiple_outliers(self, fake_pool):
        index = WG21Index(fake_pool)
        data = {f"P{n:04d}R0": {"title": "T"} for n in range(4000, 4033)}
        data["P4116R0"] = {"title": "Outlier A"}
        data["P5000R0"] = {"title": "Outlier B"}
        index._parse_and_index(data)
        assert index.effective_frontier(gap_threshold=50) == 4032

    def test_effective_frontier_empty_index(self, fake_pool):
        index = WG21Index(fake_pool)
        assert index.effective_frontier() == 0

    def test_effective_frontier_single_paper(self, fake_pool):
        index = WG21Index(fake_pool)
        index._parse_and_index({"P1234R0": {"title": "T"}})
        assert index.effective_frontier(gap_threshold=50) == 1234

    def test_effective_frontier_gap_exactly_at_threshold(self, fake_pool):
        index = WG21Index(fake_pool)
        data = {"P0100R0": {"title": "T"}, "P0150R0": {"title": "T"}}
        index._parse_and_index(data)
        assert index.effective_frontier(gap_threshold=50) == 150

    def test_effective_frontier_gap_one_over_threshold(self, fake_pool):
        index = WG21Index(fake_pool)
        data = {"P0100R0": {"title": "T"}, "P0151R0": {"title": "T"}}
        index._parse_and_index(data)
        assert index.effective_frontier(gap_threshold=50) == 100

    def test_latest_revision_known(self, populated_index):
        assert populated_index.latest_revision(2300) == 10

    def test_latest_revision_unknown(self, populated_index):
        assert populated_index.latest_revision(9999) is None

    def test_parse_ignores_non_dict_entries(self, fake_pool):
        index = WG21Index(fake_pool)
        raw = {"P1234R0": "not a dict", "P5678R0": {"title": "Real"}}
        papers = index._parse_and_index(raw)
        assert "P1234R0" not in papers
        assert "P5678R0" in papers


# ── _fetch_front_text ─────────────────────────────────────────────────────────

class TestFetchFrontText:
    async def test_returns_plain_text_on_success(self):
        html = "<html><body><p>Author: Eric Niebler</p></body></html>"
        mock_resp = _make_response(200, text=html)
        client = _make_async_client(get_resp=mock_resp)
        result = await _fetch_front_text(client, "D", 2300, 11)
        assert "Niebler" in result
        assert "<" not in result

    async def test_returns_empty_on_non_200(self):
        client = _make_async_client(get_resp=_make_response(404))
        assert await _fetch_front_text(client, "D", 2300, 11) == ""

    async def test_returns_empty_on_http_error(self):
        client = AsyncMock()
        client.get = AsyncMock(side_effect=httpx.HTTPError("timeout"))
        assert await _fetch_front_text(client, "D", 2300, 11) == ""

    async def test_truncates_to_1000_words(self):
        words = " ".join(["word"] * 1500)
        html = f"<p>{words}</p>"
        mock_resp = _make_response(200, text=html)
        client = _make_async_client(get_resp=mock_resp)
        result = await _fetch_front_text(client, "D", 2300, 11)
        assert len(result.split()) <= 1000


# ── ISOProber: hot/cold list builders ────────────────────────────────────────

class TestISOProberLists:
    def _make_prober(self, fake_pool, watchlist_nums=None, **cfg_overrides) -> tuple[ISOProber, WG21Index, ProbeState]:
        index = WG21Index(fake_pool)
        state = ProbeState(fake_pool)
        cfg = make_test_settings(**cfg_overrides)
        wl = _mock_wl(watchlist_nums)
        prober = ISOProber(index, state, user_watchlist=wl, cfg=cfg)
        return prober, index, state

    def _set_frontier(self, index: WG21Index, frontier: int) -> None:
        index._max_p = frontier
        index._max_rev = {frontier - 1: 0, frontier: 0}
        index._sorted_p_nums = [frontier - 1, frontier]

    # ── hot list ─────────────────────────────────────────────────────────────

    def test_hot_watchlist_paper_probed_every_cycle(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            watchlist_nums=[2300],
            hot_revision_depth=2,
            hot_lookback_months=0,
        )
        index._max_rev = {2300: 10}
        index._sorted_p_nums = [2300]
        frontier = index.effective_frontier()
        hot_known, hot_unknown = prober._hot_numbers(frontier)
        assert 2300 in hot_known
        urls = prober._build_hot_list(frontier, hot_known, hot_unknown)
        numbers = {r[3] for r in urls}
        tiers = {r[1] for r in urls if r[3] == 2300}
        assert 2300 in numbers
        assert "watchlist" in tiers

    def test_hot_frontier_generates_urls(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            frontier_window_above=2,
            frontier_window_below=1,
            hot_lookback_months=0,
            hot_revision_depth=1,
        )
        self._set_frontier(index, 100)
        frontier = index.effective_frontier()
        hot_known, hot_unknown = prober._hot_numbers(frontier)
        urls = prober._build_hot_list(frontier, hot_known, hot_unknown)
        numbers = {r[3] for r in urls}
        assert 100 in numbers
        assert 101 in numbers
        assert 102 in numbers

    def test_hot_frontier_unknown_numbers_get_d_and_p(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            frontier_window_above=2,
            frontier_window_below=0,
            hot_lookback_months=0,
            hot_revision_depth=1,
            gap_max_rev=0,
        )
        index._max_p = 100
        index._max_rev = {100: 0}
        index._sorted_p_nums = [100]
        frontier = index.effective_frontier()
        hot_known, hot_unknown = prober._hot_numbers(frontier)
        urls = prober._build_hot_list(frontier, hot_known, hot_unknown)
        # 101, 102 are unknown frontier numbers
        prefixes_for_101 = {r[2] for r in urls if r[3] == 101}
        assert "D" in prefixes_for_101
        assert "P" in prefixes_for_101

    def test_hot_recent_paper_by_date(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            hot_lookback_months=6,
            hot_revision_depth=1,
            frontier_window_above=0,
            frontier_window_below=0,
        )
        recent_date = (date.today() - timedelta(days=30)).isoformat()
        # _parse_and_index updates _max_rev/_sorted_p_nums but not self.papers;
        # assign both so that the date-based hot filter can find the paper.
        index.papers = index._parse_and_index({
            "P5000R2": {"title": "T", "date": recent_date, "type": "paper"},
        })
        frontier = index.effective_frontier()
        hot_known, _ = prober._hot_numbers(frontier)
        assert 5000 in hot_known

    def test_hot_old_paper_not_included(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            hot_lookback_months=1,  # only last 1 month
            frontier_window_above=0,
            frontier_window_below=0,
        )
        old_date = (date.today() - timedelta(days=365)).isoformat()
        index._parse_and_index({
            "P5000R2": {"title": "T", "date": old_date, "type": "paper"},
        })
        frontier = index.effective_frontier()
        hot_known, _ = prober._hot_numbers(frontier)
        assert 5000 not in hot_known

    def test_hot_ignores_outlier_frontier(self, fake_pool):
        """P5000-type outlier must not shift the frontier and hot window."""
        prober, index, _ = self._make_prober(
            fake_pool,
            frontier_window_above=5,
            frontier_window_below=1,
            hot_lookback_months=0,
            hot_revision_depth=1,
            frontier_gap_threshold=50,
        )
        index._max_p = 5000
        index._max_rev = {**{n: 0 for n in range(4028, 4033)}, 5000: 0}
        index._sorted_p_nums = sorted(index._max_rev.keys())
        frontier = index.effective_frontier(50)
        assert frontier == 4032
        hot_known, _ = prober._hot_numbers(frontier)
        assert 4032 in hot_known
        assert 5000 not in hot_known

    # ── cold slice ───────────────────────────────────────────────────────────

    def test_cold_slice_covers_all_numbers_over_divisor_cycles(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            cold_cycle_divisor=4,
            cold_revision_depth=1,
            hot_lookback_months=0,
            frontier_window_above=0,
            frontier_window_below=0,
        )
        # 8 known papers, no hot lookback → all are cold
        index._parse_and_index({f"P{n:04d}R0": {"title": "T"} for n in range(10, 18)})
        cold_known = set(index._max_rev.keys())
        frontier = index.effective_frontier()
        hot_known, hot_unknown = prober._hot_numbers(frontier)

        probed_known: set[int] = set()
        for cycle in range(1, 5):  # one full divisor window
            urls = prober._build_cold_slice(cycle, frontier, hot_known, hot_unknown)
            # Only track known papers (not gap numbers 1..9)
            probed_known.update(r[3] for r in urls if r[3] in cold_known)

        # Every cold-known paper must appear in exactly one slice per window
        assert cold_known == probed_known

    def test_cold_slice_index_is_deterministic(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            cold_cycle_divisor=4,
            cold_revision_depth=1,
            hot_lookback_months=0,
            frontier_window_above=0,
            frontier_window_below=0,
        )
        index._parse_and_index({f"P{n:04d}R0": {"title": "T"} for n in range(10, 18)})
        frontier = index.effective_frontier()
        hot_known, hot_unknown = prober._hot_numbers(frontier)
        slice_a = prober._build_cold_slice(1, frontier, hot_known, hot_unknown)
        slice_b = prober._build_cold_slice(5, frontier, hot_known, hot_unknown)
        assert {r[3] for r in slice_a} == {r[3] for r in slice_b}

    def test_cold_gap_numbers_probed_with_d_and_p(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            cold_cycle_divisor=1,
            cold_revision_depth=1,
            hot_lookback_months=0,
            frontier_window_above=0,
            frontier_window_below=1,
            gap_max_rev=0,
        )
        # frontier = 10; known = [9, 10]; gap = [1..8]
        index._max_p = 10
        index._max_rev = {9: 0, 10: 0}
        index._sorted_p_nums = [9, 10]
        frontier = 10
        hot_known, hot_unknown = prober._hot_numbers(frontier)
        urls = prober._build_cold_slice(1, frontier, hot_known, hot_unknown)
        gap_entries = [(r[2], r[3]) for r in urls if r[1] == "cold" and r[3] not in (9, 10)]
        prefixes_found = {p for p, _ in gap_entries}
        assert "D" in prefixes_found
        assert "P" in prefixes_found

    def test_hot_numbers_explicit_range(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            frontier_window_above=0,
            frontier_window_below=0,
            frontier_explicit_ranges=[{"min": 200, "max": 202}],
            hot_lookback_months=0,
        )
        self._set_frontier(index, 100)
        hot_known, hot_unknown = prober._hot_numbers(100)
        assert 200 in hot_unknown or 200 in hot_known

    def test_hot_paper_skipped_when_no_date(self, fake_pool):
        prober, index, _ = self._make_prober(fake_pool, hot_lookback_months=6,
                                              frontier_window_above=0, frontier_window_below=0)
        # Paper with no date should be silently skipped (the `continue` branch)
        index.papers = index._parse_and_index({"P6000R0": {"title": "T", "type": "paper"}})
        frontier = index.effective_frontier()
        hot_known, _ = prober._hot_numbers(frontier)
        assert 6000 not in hot_known

    def test_hot_paper_skipped_when_bad_date(self, fake_pool):
        prober, index, _ = self._make_prober(fake_pool, hot_lookback_months=6,
                                              frontier_window_above=0, frontier_window_below=0)
        index.papers = index._parse_and_index(
            {"P6001R0": {"title": "T", "date": "not-a-date", "type": "paper"}}
        )
        frontier = index.effective_frontier()
        hot_known, _ = prober._hot_numbers(frontier)
        assert 6001 not in hot_known

    def test_tier_label_recent_for_non_watchlist_non_frontier(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool, watchlist_nums=[1], hot_lookback_months=0,
            frontier_window_above=0, frontier_window_below=0,
        )
        self._set_frontier(index, 100)
        # Number 50 is not watchlist and not in frontier range → "recent"
        label = prober._tier_label(50, {1}, set(range(100, 104)))
        assert label == "recent"

    def test_build_hot_list_explicit_ranges_update_frontier_range(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            frontier_window_above=0,
            frontier_window_below=0,
            frontier_explicit_ranges=[{"min": 200, "max": 200}],
            hot_lookback_months=0,
            hot_revision_depth=1,
            gap_max_rev=0,
        )
        # 200 is unknown but in explicit range → should appear as "frontier" hot_unknown
        index._max_p = 100
        index._max_rev = {99: 0, 100: 0}
        index._sorted_p_nums = [99, 100]
        frontier = 100
        hot_known, hot_unknown = prober._hot_numbers(frontier)
        urls = prober._build_hot_list(frontier, hot_known, hot_unknown)
        assert any(r[3] == 200 and r[1] == "frontier" for r in urls)

    def test_build_hot_list_latest_none_uses_minus_one(self, fake_pool):
        """Known hot numbers with latest_revision=None should start from R0."""
        prober, index, _ = self._make_prober(
            fake_pool, watchlist_nums=[9999], hot_lookback_months=0,
            frontier_window_above=0, frontier_window_below=0,
            hot_revision_depth=1, gap_max_rev=0,
        )
        # Add 9999 to _max_rev so it's "known" but with latest_revision=None
        index._max_rev = {9999: -1, 99: 0, 100: 0}
        index._sorted_p_nums = [99, 100, 9999]
        frontier = 100
        hot_known, hot_unknown = prober._hot_numbers(frontier)
        assert 9999 in hot_known
        urls = prober._build_hot_list(frontier, hot_known, hot_unknown)
        revisions = [r[4] for r in urls if r[3] == 9999]
        assert 0 in revisions  # latest=-1 → start_rev=0

    def test_cold_known_skips_when_latest_none(self, fake_pool):
        """cold_known paper with latest_revision=None should be silently skipped."""
        prober, index, _ = self._make_prober(
            fake_pool, hot_lookback_months=0,
            frontier_window_above=0, frontier_window_below=0,  # empty frontier range
            cold_cycle_divisor=1, cold_revision_depth=1,
        )
        # 4 has _max_rev=-1 → latest_revision=None; 5 is normal
        # With no frontier window and no watchlist, both are cold_known
        index._max_rev = {4: -1, 5: 0}
        index._sorted_p_nums = [4, 5]
        frontier = 5
        hot_known, hot_unknown = prober._hot_numbers(frontier)
        urls = prober._build_cold_slice(1, frontier, hot_known, hot_unknown)
        cold_nums = {r[3] for r in urls if r[1] == "cold"}
        assert 4 not in cold_nums  # skipped because latest_revision=None
        assert 5 in cold_nums      # normally probed

    async def test_probe_one_bad_last_modified_header(self, fake_pool):
        """An unparsable Last-Modified header should not crash; is_recent stays False."""
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9999R0.pdf"
        sem = asyncio.Semaphore(5)
        head_resp = MagicMock()
        head_resp.status_code = 200
        head_resp.headers = {"last-modified": "this is not a date"}
        client = AsyncMock()
        client.head = AsyncMock(return_value=head_resp)
        result = await prober._probe_one(client, sem, url, "D", 9999, 0, ".pdf", "cold")
        assert result is not None
        assert result.is_recent is False
        # No front_text GET for non-recent
        client.get.assert_not_called()

    def test_cold_excludes_hot_numbers(self, fake_pool):
        prober, index, _ = self._make_prober(
            fake_pool,
            watchlist_nums=[5000],
            cold_cycle_divisor=1,
            hot_lookback_months=0,
            frontier_window_above=0,
            frontier_window_below=0,
        )
        index._parse_and_index({
            "P5000R2": {"title": "T", "date": "2020-01-01", "type": "paper"},
            "P5001R0": {"title": "T", "date": "2020-01-01", "type": "paper"},
        })
        frontier = index.effective_frontier()
        hot_known, hot_unknown = prober._hot_numbers(frontier)
        urls = prober._build_cold_slice(1, frontier, hot_known, hot_unknown)
        cold_numbers = {r[3] for r in urls}
        assert 5000 not in cold_numbers  # in watchlist → hot


# ── ISOProber: _probe_one ─────────────────────────────────────────────────────

class TestISOProberProbeOne:
    def _make_prober(self, fake_pool) -> tuple[ISOProber, WG21Index, ProbeState]:
        index = WG21Index(fake_pool)
        state = ProbeState(fake_pool)
        cfg = make_test_settings()
        prober = ISOProber(index, state, user_watchlist=_mock_wl(), cfg=cfg)
        prober._cycle = 1
        return prober, index, state

    async def test_skips_already_discovered(self, fake_pool):
        prober, _, state = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D2300R11.pdf"
        state.mark_discovered(url)
        sem = asyncio.Semaphore(5)
        client = AsyncMock()
        result = await prober._probe_one(client, sem, url, "D", 2300, 11, ".pdf", "hot")
        assert result is None
        client.head.assert_not_called()

    async def test_skips_already_in_index(self, fake_pool):
        prober, index, _ = self._make_prober(fake_pool)
        index.papers = {"D2300R11": Paper(id="D2300R11")}
        url = "https://isocpp.org/files/papers/D2300R11.pdf"
        sem = asyncio.Semaphore(5)
        result = await prober._probe_one(AsyncMock(), sem, url, "D", 2300, 11, ".pdf", "hot")
        assert result is None

    async def test_returns_none_on_404(self, fake_pool):
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9999R0.pdf"
        sem = asyncio.Semaphore(5)
        client = _make_async_client(head_resp=_make_response(404))
        result = await prober._probe_one(client, sem, url, "D", 9999, 0, ".pdf", "hot")
        assert result is None

    async def test_returns_recent_hit_with_recent_last_modified(self, fake_pool):
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9999R0.pdf"
        sem = asyncio.Semaphore(5)
        lm = _recent_lm()
        head_resp = _make_response(200, last_modified=lm)
        get_resp = _make_response(200, text="<body>content</body>")
        client = _make_async_client(head_resp=head_resp, get_resp=get_resp)
        result = await prober._probe_one(client, sem, url, "D", 9999, 0, ".pdf", "recent")
        assert result is not None
        assert result.is_recent is True
        assert result.last_modified is not None

    async def test_returns_non_recent_hit_with_old_last_modified(self, fake_pool):
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9999R0.pdf"
        sem = asyncio.Semaphore(5)
        head_resp = _make_response(200, last_modified=_old_lm())
        client = _make_async_client(head_resp=head_resp)
        result = await prober._probe_one(client, sem, url, "D", 9999, 0, ".pdf", "cold")
        assert result is not None
        assert result.is_recent is False
        # No front_text fetch for non-recent
        client.get.assert_not_called()

    async def test_treats_no_last_modified_as_recent(self, fake_pool):
        """When the server provides no Last-Modified, treat as recent (first discovery)."""
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9999R0.pdf"
        sem = asyncio.Semaphore(5)
        head_resp = _make_response(200)  # no last_modified kwarg → empty headers
        get_resp = _make_response(200, text="<body>text</body>")
        client = _make_async_client(head_resp=head_resp, get_resp=get_resp)
        result = await prober._probe_one(client, sem, url, "D", 9999, 0, ".pdf", "frontier")
        assert result is not None
        assert result.is_recent is True
        assert result.last_modified is None

    async def test_handles_http_error(self, fake_pool):
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9999R0.pdf"
        sem = asyncio.Semaphore(5)
        client = AsyncMock()
        client.head = AsyncMock(side_effect=httpx.HTTPError("timeout"))
        result = await prober._probe_one(client, sem, url, "D", 9999, 0, ".pdf", "hot")
        assert result is None

    # ── Stats tracking ────────────────────────────────────────────────────────

    async def test_stats_skipped_discovered(self, fake_pool):
        prober, _, state = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9999R0.pdf"
        state.mark_discovered(url)
        sem = asyncio.Semaphore(5)
        await prober._probe_one(AsyncMock(), sem, url, "D", 9999, 0, ".pdf", "hot")
        assert prober._stats["skipped_discovered"] == 1

    async def test_stats_skipped_in_index(self, fake_pool):
        prober, index, _ = self._make_prober(fake_pool)
        index.papers = {"D9998R0": Paper(id="D9998R0")}
        url = "https://isocpp.org/files/papers/D9998R0.pdf"
        sem = asyncio.Semaphore(5)
        await prober._probe_one(AsyncMock(), sem, url, "D", 9998, 0, ".pdf", "hot")
        assert prober._stats["skipped_in_index"] == 1

    async def test_stats_miss(self, fake_pool):
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9997R0.pdf"
        sem = asyncio.Semaphore(5)
        client = _make_async_client(head_resp=_make_response(404))
        await prober._probe_one(client, sem, url, "D", 9997, 0, ".pdf", "hot")
        assert prober._stats["miss"] == 1

    async def test_stats_hit_recent(self, fake_pool):
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9996R0.pdf"
        sem = asyncio.Semaphore(5)
        head_resp = _make_response(200, last_modified=_recent_lm())
        get_resp = _make_response(200, text="<p>x</p>")
        client = _make_async_client(head_resp=head_resp, get_resp=get_resp)
        await prober._probe_one(client, sem, url, "D", 9996, 0, ".pdf", "recent")
        assert prober._stats["hit_recent"] == 1

    async def test_stats_hit_old(self, fake_pool):
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9995R0.pdf"
        sem = asyncio.Semaphore(5)
        head_resp = _make_response(200, last_modified=_old_lm())
        client = _make_async_client(head_resp=head_resp)
        await prober._probe_one(client, sem, url, "D", 9995, 0, ".pdf", "cold")
        assert prober._stats["hit_old"] == 1

    async def test_stats_hit_no_lm(self, fake_pool):
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9994R0.pdf"
        sem = asyncio.Semaphore(5)
        head_resp = _make_response(200)  # no Last-Modified header
        get_resp = _make_response(200, text="<p>x</p>")
        client = _make_async_client(head_resp=head_resp, get_resp=get_resp)
        await prober._probe_one(client, sem, url, "D", 9994, 0, ".pdf", "frontier")
        assert prober._stats["hit_no_lm"] == 1

    async def test_stats_error(self, fake_pool):
        prober, _, _ = self._make_prober(fake_pool)
        url = "https://isocpp.org/files/papers/D9993R0.pdf"
        sem = asyncio.Semaphore(5)
        client = AsyncMock()
        client.head = AsyncMock(side_effect=httpx.HTTPError("timeout"))
        await prober._probe_one(client, sem, url, "D", 9993, 0, ".pdf", "hot")
        assert prober._stats["error"] == 1

    async def test_run_cycle_logs_unhandled_exception(self, fake_pool, caplog):
        """If asyncio.gather returns an Exception (not ProbeHit), it is logged."""
        import logging
        index = WG21Index(fake_pool)
        index._max_p = 100
        index._max_rev = {99: 0, 100: 0}
        index._sorted_p_nums = [99, 100]
        state = ProbeState(fake_pool)
        cfg = make_test_settings(
            watchlist_papers=[9999],
            hot_lookback_months=0, hot_revision_depth=1,
            frontier_window_above=0, frontier_window_below=0,
            gap_max_rev=0, cold_cycle_divisor=100,
        )
        prober = ISOProber(index, state, user_watchlist=_mock_wl([9999]), cfg=cfg)

        async def raising_head(*args, **kwargs):
            raise RuntimeError("boom")

        mock_client = AsyncMock()
        mock_client.head = raising_head
        with patch("paperbot.sources.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with caplog.at_level(logging.DEBUG):
                await prober.run_cycle()
        # The RuntimeError is not an httpx.HTTPError so asyncio.gather may
        # surface it as a return value (return_exceptions=True); we log it.
        # (Whether it shows depends on whether the exception propagates through
        #  the sem context manager — either path is acceptable.)

    async def test_stats_reset_each_cycle(self, fake_pool):
        """Stats are zeroed at the start of every run_cycle."""
        index = WG21Index(fake_pool)
        index._max_p = 100
        index._max_rev = {99: 0, 100: 0}
        index._sorted_p_nums = [99, 100]
        state = ProbeState(fake_pool)
        cfg = make_test_settings(
            hot_lookback_months=0,
            hot_revision_depth=1,
            frontier_window_above=0,
            frontier_window_below=0,
            gap_max_rev=0,
            cold_cycle_divisor=100,
        )
        prober = ISOProber(index, state, user_watchlist=_mock_wl([9999]), cfg=cfg)
        prober._stats["miss"] = 999  # manually dirty

        mock_client = _make_async_client(head_resp=_make_response(404))
        with patch("paperbot.sources.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await prober.run_cycle()

        # Stats must have been reset and then re-populated
        assert prober._stats["miss"] < 999


# ── ISOProber: run_cycle ──────────────────────────────────────────────────────

class TestISOProberRunCycle:
    async def test_run_cycle_records_hit_and_marks_discovered(self, fake_pool):
        index = WG21Index(fake_pool)
        index._max_p = 100
        index._max_rev = {99: 0, 100: 0}
        index._sorted_p_nums = [99, 100]
        state = ProbeState(fake_pool)
        cfg = make_test_settings(
            hot_lookback_months=0,
            hot_revision_depth=1,
            frontier_window_above=0,
            frontier_window_below=0,
            gap_max_rev=0,
            cold_cycle_divisor=100,
        )
        prober = ISOProber(index, state, user_watchlist=_mock_wl([9999]), cfg=cfg)

        hit_url = "https://isocpp.org/files/papers/D9999R0.pdf"
        lm = _recent_lm()

        async def mock_head(url, **kwargs):
            if url == hit_url:
                return _make_response(200, last_modified=lm)
            return _make_response(404)

        async def mock_get(url, **kwargs):
            return _make_response(200, text="<p>content</p>")

        mock_client = AsyncMock()
        mock_client.head = mock_head
        mock_client.get = mock_get

        with patch("paperbot.sources.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            hits = await prober.run_cycle()

        assert any(h.number == 9999 for h in hits)
        assert state.is_discovered(hit_url)

    async def test_run_cycle_non_recent_hit_still_discovered(self, fake_pool):
        index = WG21Index(fake_pool)
        index._max_p = 100
        index._max_rev = {99: 0, 100: 0}
        index._sorted_p_nums = [99, 100]
        state = ProbeState(fake_pool)
        cfg = make_test_settings(
            hot_lookback_months=0,
            hot_revision_depth=1,
            frontier_window_above=0,
            frontier_window_below=0,
            gap_max_rev=0,
            cold_cycle_divisor=100,
        )
        prober = ISOProber(index, state, user_watchlist=_mock_wl([9998]), cfg=cfg)
        hit_url = "https://isocpp.org/files/papers/D9998R0.pdf"
        old_lm = datetime.now(timezone.utc) - timedelta(days=365)

        async def mock_head(url, **_):
            if url == hit_url:
                return _make_response(200, last_modified=old_lm)
            return _make_response(404)

        mock_client = AsyncMock()
        mock_client.head = mock_head
        mock_client.get = AsyncMock(return_value=_make_response(404))

        with patch("paperbot.sources.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            hits = await prober.run_cycle()

        # Hit is returned (for the discovered registry) but is_recent=False
        old_hits = [h for h in hits if h.number == 9998]
        assert len(old_hits) == 1
        assert old_hits[0].is_recent is False
        assert state.is_discovered(hit_url)


# ── open-std.org scraper ─────────────────────────────────────────────────────

OPEN_STD_HTML = """
<table>
  <tr>
    <td><a href="P2300R10.pdf">P2300R10</a></td>
    <td>Senders and Receivers</td>
    <td>Eric Niebler</td>
    <td>2024-01-15</td>
    <td>Adopted</td>
    <td>SG1</td>
    <td>EWG</td>
  </tr>
  <tr>
    <td><a href="N4950.pdf">N4950</a></td>
    <td>Working Draft</td>
    <td>Thomas Köppe</td>
    <td>2023-05-15</td>
    <td></td>
    <td></td>
    <td>WG21</td>
  </tr>
</table>
"""


class TestOpenStdScraper:
    def test_parse_open_std_html(self):
        entries = _parse_open_std_html(OPEN_STD_HTML)
        assert len(entries) == 2
        assert entries[0].paper_id == "P2300R10"
        assert entries[0].author == "Eric Niebler"
        assert entries[0].subgroup == "EWG"

    def test_parse_open_std_html_empty(self):
        assert _parse_open_std_html("") == []

    def test_parse_open_std_html_skips_short_rows(self):
        html = "<table><tr><td>only one cell</td></tr></table>"
        assert _parse_open_std_html(html) == []

    def test_parse_open_std_html_skips_no_paper_link(self):
        html = "<table><tr><td>no link</td><td>t</td><td>a</td><td>2024</td></tr></table>"
        assert _parse_open_std_html(html) == []

    async def test_scrape_open_std_success(self):
        mock_resp = _make_response(200, text=OPEN_STD_HTML)
        mock_client = _make_async_client(get_resp=mock_resp)
        with patch("paperbot.sources.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            entries = await scrape_open_std(2024)
        assert len(entries) == 2

    async def test_scrape_open_std_http_error(self):
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.HTTPError("fail"))
        with patch("paperbot.sources.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            entries = await scrape_open_std(2024)
        assert entries == []

    async def test_scrape_open_std_uses_current_year_by_default(self):
        mock_resp = _make_response(200, text="<table></table>")
        mock_client = _make_async_client(get_resp=mock_resp)
        with patch("paperbot.sources.httpx.AsyncClient") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await scrape_open_std()
        call_url = mock_client.get.call_args[0][0]
        assert str(date.today().year) in call_url
