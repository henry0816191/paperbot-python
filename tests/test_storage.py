"""Tests for paperscout.storage (PostgreSQL-backed via FakePool)."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from paperscout.models import Paper
from paperscout.storage import (
    PaperCache,
    ProbeState,
    UserWatchlist,
    iso_paper_number_from_discovered_url,
)
from tests.conftest import FakePool


# ── PaperCache ────────────────────────────────────────────────────────────────

class TestPaperCache:
    def test_is_fresh_when_empty(self, fake_pool):
        cache = PaperCache(fake_pool, ttl_hours=1.0)
        assert not cache.is_fresh()

    def test_is_fresh_after_write(self, fake_pool):
        cache = PaperCache(fake_pool, ttl_hours=1.0)
        cache.write({"x": 1})
        assert cache.is_fresh()

    def test_is_stale_with_zero_ttl(self, fake_pool):
        cache = PaperCache(fake_pool, ttl_hours=0.0)
        cache.write({"x": 1})
        assert not cache.is_fresh()

    def test_is_stale_when_old(self, fake_pool):
        cache = PaperCache(fake_pool, ttl_hours=1.0)
        cache.write({"x": 1})
        with patch("paperscout.storage.time") as mock_time:
            mock_time.time.return_value = 1e12
            assert not cache.is_fresh()

    def test_read_when_empty(self, fake_pool):
        cache = PaperCache(fake_pool)
        assert cache.read() is None

    def test_read_after_write(self, fake_pool):
        cache = PaperCache(fake_pool)
        data = {"key": "value", "num": 42}
        cache.write(data)
        assert cache.read() == data

    def test_read_if_fresh_returns_data_when_fresh(self, fake_pool):
        cache = PaperCache(fake_pool, ttl_hours=1.0)
        cache.write({"a": 1})
        assert cache.read_if_fresh() == {"a": 1}

    def test_read_if_fresh_returns_none_when_stale(self, fake_pool):
        cache = PaperCache(fake_pool, ttl_hours=1.0)
        cache.write({"a": 1})
        with patch("paperscout.storage.time") as mock_time:
            mock_time.time.return_value = 1e12
            assert cache.read_if_fresh() is None

    def test_write_upserts_on_second_write(self, fake_pool):
        cache = PaperCache(fake_pool)
        cache.write({"version": 1})
        cache.write({"version": 2})
        assert cache.read() == {"version": 2}

    def test_write_non_ascii(self, fake_pool):
        cache = PaperCache(fake_pool)
        data = {"author": "Bjørn Stroustrup"}
        cache.write(data)
        assert cache.read() == data


# ── ProbeState ────────────────────────────────────────────────────────────────

class TestProbeState:
    def test_initial_state(self, fake_pool):
        state = ProbeState(fake_pool)
        assert state.discovered == {}
        assert state.miss_counts == {}
        assert state.last_poll == 0.0

    def test_mark_discovered_stores_entry(self, fake_pool):
        state = ProbeState(fake_pool)
        url = "https://isocpp.org/files/papers/D2300R11.pdf"
        assert not state.is_discovered(url)
        state.mark_discovered(url)
        assert state.is_discovered(url)
        entry = state.discovered[url]
        assert isinstance(entry, dict)
        assert "discovered_at" in entry
        assert entry["last_modified"] is None

    def test_mark_discovered_stores_last_modified(self, fake_pool):
        state = ProbeState(fake_pool)
        url = "https://isocpp.org/files/papers/D2300R11.pdf"
        lm_ts = 1_700_000_000.0
        state.mark_discovered(url, last_modified_ts=lm_ts)
        entry = state.discovered[url]
        assert entry["last_modified"] == lm_ts
        assert entry["discovered_at"] > 0

    def test_iso_paper_number_from_discovered_url(self):
        assert iso_paper_number_from_discovered_url(
            "https://isocpp.org/files/papers/D4165R0.pdf"
        ) == 4165
        assert iso_paper_number_from_discovered_url(
            "https://isocpp.org/files/papers/P1234R0.html"
        ) == 1234
        assert iso_paper_number_from_discovered_url("https://example.com/") is None

    def test_paper_nums_from_discovered_iso_urls(self, fake_pool):
        state = ProbeState(fake_pool)
        state.mark_discovered("https://isocpp.org/files/papers/D4165R0.pdf")
        state.mark_discovered("https://isocpp.org/files/papers/D2300R11.pdf")
        assert state.paper_nums_from_discovered_iso_urls() == {4165, 2300}

    def test_mark_discovered_is_idempotent(self, fake_pool):
        state = ProbeState(fake_pool)
        url = "https://isocpp.org/files/papers/D2300R11.pdf"
        state.mark_discovered(url, last_modified_ts=111.0)
        first_entry = dict(state.discovered[url])
        time.sleep(0.01)
        state.mark_discovered(url, last_modified_ts=999.0)
        assert state.discovered[url] == first_entry

    def test_discovered_info_returns_entry(self, fake_pool):
        state = ProbeState(fake_pool)
        url = "https://isocpp.org/files/papers/D2300R11.pdf"
        state.mark_discovered(url, last_modified_ts=42.0)
        info = state.discovered_info(url)
        assert info is not None
        assert info["last_modified"] == 42.0

    def test_discovered_info_returns_none_for_unknown(self, fake_pool):
        state = ProbeState(fake_pool)
        assert state.discovered_info("https://example.com/nope.pdf") is None

    def test_discovered_property_returns_all(self, fake_pool):
        state = ProbeState(fake_pool)
        state.mark_discovered("https://example.com/A.pdf")
        state.mark_discovered("https://example.com/B.pdf")
        disc = state.discovered
        assert len(disc) == 2
        assert "https://example.com/A.pdf" in disc
        assert "https://example.com/B.pdf" in disc

    def test_miss_counter_increments(self, fake_pool):
        state = ProbeState(fake_pool)
        assert state.get_miss_count("1234") == 0
        state.record_miss("1234")
        assert state.get_miss_count("1234") == 1
        state.record_miss("1234")
        assert state.get_miss_count("1234") == 2

    def test_reset_misses(self, fake_pool):
        state = ProbeState(fake_pool)
        state.record_miss("1234")
        state.record_miss("1234")
        state.reset_misses("1234")
        assert state.get_miss_count("1234") == 0

    def test_reset_misses_nonexistent_is_safe(self, fake_pool):
        state = ProbeState(fake_pool)
        state.reset_misses("9999")  # must not raise

    def test_should_skip_below_threshold(self, fake_pool):
        state = ProbeState(fake_pool)
        state.record_miss("1")
        state.record_miss("1")
        assert not state.should_skip("1", threshold=3, multiplier=2, max_skip=48, cycle=1)

    def test_should_skip_at_threshold(self, fake_pool):
        state = ProbeState(fake_pool)
        for _ in range(3):
            state.record_miss("1")
        assert not state.should_skip("1", threshold=3, multiplier=2, max_skip=48, cycle=1)

    def test_should_skip_above_threshold(self, fake_pool):
        state = ProbeState(fake_pool)
        for _ in range(4):
            state.record_miss("1")
        assert state.should_skip("1", threshold=3, multiplier=2, max_skip=48, cycle=1)
        assert not state.should_skip("1", threshold=3, multiplier=2, max_skip=48, cycle=2)

    def test_should_skip_respects_max_skip(self, fake_pool):
        state = ProbeState(fake_pool)
        for _ in range(20):
            state.record_miss("1")
        assert not state.should_skip("1", threshold=3, multiplier=2, max_skip=4, cycle=4)
        assert state.should_skip("1", threshold=3, multiplier=2, max_skip=4, cycle=1)

    def test_touch_poll(self, fake_pool):
        state = ProbeState(fake_pool)
        before = time.time()
        state.touch_poll()
        assert state.last_poll >= before

    def test_save_is_noop(self, fake_pool):
        """save() is a no-op; data persists immediately via the pool."""
        state = ProbeState(fake_pool)
        state.mark_discovered("https://example.com/D1234R0.pdf")
        state.save()  # must not raise

        state2 = ProbeState(fake_pool)  # same pool → same store
        assert state2.is_discovered("https://example.com/D1234R0.pdf")

    def test_miss_counts_property_returns_all(self, fake_pool):
        state = ProbeState(fake_pool)
        state.record_miss("100")
        state.record_miss("200")
        state.record_miss("200")
        mc = state.miss_counts
        assert mc["100"] == 1
        assert mc["200"] == 2


# ── UserWatchlist ─────────────────────────────────────────────────────────────

class TestUserWatchlist:
    def test_add_author_returns_true(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        assert wl.add("U1", "Niebler") is True

    def test_add_author_stored_lowercase(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "NIEBLER")
        entries = wl.list_entries("U1")
        assert ("niebler", "author") in entries

    def test_add_paper_number_detected_as_paper(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "2300")
        entries = wl.list_entries("U1")
        assert ("2300", "paper") in entries

    def test_add_duplicate_returns_false(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "Niebler")
        assert wl.add("U1", "Niebler") is False

    def test_add_case_insensitive_dedup(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "NIEBLER")
        assert wl.add("U1", "niebler") is False

    def test_add_empty_string_returns_false(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        assert wl.add("U1", "") is False
        assert wl.add("U1", "   ") is False

    def test_remove_existing_returns_true(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "Niebler")
        assert wl.remove("U1", "Niebler") is True
        assert wl.list_entries("U1") == []

    def test_remove_nonexistent_returns_false(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        assert wl.remove("U1", "Nobody") is False

    def test_list_entries_empty(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        assert wl.list_entries("U1") == []

    def test_list_entries_only_for_requested_user(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "niebler")
        wl.add("U2", "baker")
        assert wl.list_entries("U1") == [("niebler", "author")]
        assert wl.list_entries("U2") == [("baker", "author")]

    def test_list_entries_sorted_by_type_then_entry(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "niebler")
        wl.add("U1", "2300")
        wl.add("U1", "baker")
        entries = wl.list_entries("U1")
        types = [t for _, t in entries]
        # authors come after paper (alphabetically "author" < "paper")
        assert types == sorted(types)

    def test_get_all_watched_paper_nums_empty(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        assert wl.get_all_watched_paper_nums() == set()

    def test_get_all_watched_paper_nums_union(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "2300")
        wl.add("U2", "2301")
        wl.add("U2", "niebler")  # author — should not appear
        nums = wl.get_all_watched_paper_nums()
        assert nums == {2300, 2301}

    def test_matches_for_users_author_match(self, fake_pool):
        from paperscout.monitor import PerUserMatches
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "niebler")
        paper = Paper(id="P2300R11", title="X", author="Eric Niebler")
        result = wl.matches_for_users([paper], [])
        assert "U1" in result
        matched_papers = [p for p, _ in result["U1"].papers]
        assert paper in matched_papers

    def test_matches_for_users_paper_match(self, fake_pool):
        from paperscout.monitor import PerUserMatches
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "2300")
        paper = Paper(id="P2300R11", title="X", author="Unknown")
        result = wl.matches_for_users([paper], [])
        assert "U1" in result

    def test_matches_for_users_no_match(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "baker")
        paper = Paper(id="P2300R11", title="X", author="Unknown Author")
        result = wl.matches_for_users([paper], [])
        assert "U1" not in result

    def test_matches_for_users_empty_watchlist(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        paper = Paper(id="P2300R11", title="X", author="Niebler")
        assert wl.matches_for_users([paper], []) == {}

    def test_matches_for_users_probe_hit_author(self, fake_pool):
        from paperscout.sources import ProbeHit
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "niebler")
        hit = ProbeHit(
            url="https://isocpp.org/files/papers/D9999R0.pdf",
            prefix="D", number=9999, revision=0, extension=".pdf",
            tier="frontier", front_text="written by niebler", is_recent=True,
        )
        result = wl.matches_for_users([], [hit])
        assert "U1" in result
        assert len(result["U1"].probe_hits) == 1

    def test_matches_for_users_probe_hit_paper_number(self, fake_pool):
        from paperscout.sources import ProbeHit
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "9999")
        hit = ProbeHit(
            url="https://isocpp.org/files/papers/D9999R0.pdf",
            prefix="D", number=9999, revision=0, extension=".pdf",
            tier="watchlist", is_recent=True,
        )
        result = wl.matches_for_users([], [hit])
        assert "U1" in result
