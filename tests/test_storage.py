"""Tests for paperbot.storage."""
from __future__ import annotations

import json
import time
from unittest.mock import patch

import pytest

from paperbot.storage import JsonCache, ProbeState


# ── JsonCache ────────────────────────────────────────────────────────────────

class TestJsonCache:
    def test_is_fresh_when_file_missing(self, tmp_path):
        cache = JsonCache(tmp_path / "cache.json", ttl_hours=1.0)
        assert not cache.is_fresh()

    def test_is_fresh_after_write(self, tmp_path):
        cache = JsonCache(tmp_path / "cache.json", ttl_hours=1.0)
        cache.write({"x": 1})
        assert cache.is_fresh()

    def test_is_stale_with_zero_ttl(self, tmp_path):
        cache = JsonCache(tmp_path / "cache.json", ttl_hours=0.0)
        cache.write({"x": 1})
        assert not cache.is_fresh()

    def test_read_missing_file(self, tmp_path):
        cache = JsonCache(tmp_path / "missing.json")
        assert cache.read() is None

    def test_read_after_write(self, tmp_path):
        cache = JsonCache(tmp_path / "cache.json")
        data = {"key": "value", "num": 42}
        cache.write(data)
        assert cache.read() == data

    def test_read_corrupt_file(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("this is not json", encoding="utf-8")
        cache = JsonCache(path)
        assert cache.read() is None

    def test_read_if_fresh_when_fresh(self, tmp_path):
        cache = JsonCache(tmp_path / "cache.json", ttl_hours=1.0)
        cache.write({"a": 1})
        assert cache.read_if_fresh() == {"a": 1}

    def test_read_if_fresh_when_stale(self, tmp_path):
        cache = JsonCache(tmp_path / "cache.json", ttl_hours=1.0)
        cache.write({"a": 1})
        # Simulate a far-future call to time.time() so age >> ttl_seconds.
        # Patching avoids Windows filesystem clock-skew flakiness (mtime precision
        # can make age appear slightly negative when ttl_seconds=0).
        with patch("paperbot.storage.time") as mock_time:
            mock_time.time.return_value = 1e12  # far future → huge positive age
            assert cache.read_if_fresh() is None

    def test_write_creates_parent_dirs(self, tmp_path):
        nested = tmp_path / "a" / "b" / "cache.json"
        cache = JsonCache(nested)
        cache.write({"nested": True})
        assert nested.exists()
        assert json.loads(nested.read_text()) == {"nested": True}

    def test_write_is_atomic(self, tmp_path):
        """No .tmp file should be left behind after a successful write."""
        path = tmp_path / "cache.json"
        cache = JsonCache(path)
        cache.write({"ok": True})
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_write_cleans_up_tmp_on_failure(self, tmp_path):
        """The except BaseException block should unlink the .tmp file and re-raise."""
        cache = JsonCache(tmp_path / "cache.json")

        with patch("paperbot.storage.os.replace", side_effect=OSError("simulated failure")):
            with pytest.raises(OSError):
                cache.write({"x": 1})
        # The .tmp file should have been cleaned up
        assert list(tmp_path.glob("*.tmp")) == []

    def test_write_cleanup_survives_unlink_failure(self, tmp_path):
        """If os.unlink also raises, the original exception is still re-raised."""
        cache = JsonCache(tmp_path / "cache.json")

        with patch("paperbot.storage.os.replace", side_effect=OSError("replace failed")):
            with patch("paperbot.storage.os.unlink", side_effect=OSError("unlink failed")):
                with pytest.raises(OSError, match="replace failed"):
                    cache.write({"x": 1})

    def test_write_non_ascii(self, tmp_path):
        cache = JsonCache(tmp_path / "cache.json")
        data = {"author": "Bjørn Stroustrup"}
        cache.write(data)
        assert cache.read() == data


# ── ProbeState ───────────────────────────────────────────────────────────────

class TestProbeState:
    def test_initial_state(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        assert state.discovered == {}
        assert state.miss_counts == {}
        assert state.last_poll == 0.0

    def test_mark_discovered_stores_dict(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        url = "https://isocpp.org/files/papers/D2300R11.pdf"
        assert not state.is_discovered(url)
        state.mark_discovered(url)
        assert state.is_discovered(url)
        entry = state.discovered[url]
        assert isinstance(entry, dict)
        assert "discovered_at" in entry
        assert entry["last_modified"] is None

    def test_mark_discovered_stores_last_modified(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        url = "https://isocpp.org/files/papers/D2300R11.pdf"
        lm_ts = 1_700_000_000.0
        state.mark_discovered(url, last_modified_ts=lm_ts)
        entry = state.discovered[url]
        assert entry["last_modified"] == lm_ts
        assert entry["discovered_at"] > 0

    def test_mark_discovered_is_idempotent(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        url = "https://isocpp.org/files/papers/D2300R11.pdf"
        state.mark_discovered(url, last_modified_ts=111.0)
        first_entry = dict(state.discovered[url])
        time.sleep(0.01)
        state.mark_discovered(url, last_modified_ts=999.0)  # second call ignored
        assert state.discovered[url] == first_entry

    def test_discovered_info_returns_entry(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        url = "https://isocpp.org/files/papers/D2300R11.pdf"
        state.mark_discovered(url, last_modified_ts=42.0)
        info = state.discovered_info(url)
        assert info is not None
        assert info["last_modified"] == 42.0

    def test_discovered_info_returns_none_for_unknown(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        assert state.discovered_info("https://example.com/nope.pdf") is None

    def test_migration_converts_old_float_entries(self, tmp_path):
        """Existing float entries in discovered are migrated to the dict schema."""
        path = tmp_path / "state.json"
        old_data = {
            "discovered": {
                "https://isocpp.org/files/papers/D0085R4.html": 1_773_180_357.0,
                "https://isocpp.org/files/papers/D2300R11.pdf": 1_700_000_000.0,
            },
            "miss_counts": {},
            "last_poll": 0.0,
        }
        path.write_text(json.dumps(old_data), encoding="utf-8")
        state = ProbeState(path)

        url = "https://isocpp.org/files/papers/D0085R4.html"
        assert state.is_discovered(url)
        entry = state.discovered[url]
        assert isinstance(entry, dict)
        assert entry["discovered_at"] == 1_773_180_357.0
        assert entry["last_modified"] is None

    def test_miss_counter_increments(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        assert state.get_miss_count("1234") == 0
        state.record_miss("1234")
        assert state.get_miss_count("1234") == 1
        state.record_miss("1234")
        assert state.get_miss_count("1234") == 2

    def test_reset_misses(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        state.record_miss("1234")
        state.record_miss("1234")
        state.reset_misses("1234")
        assert state.get_miss_count("1234") == 0

    def test_reset_misses_nonexistent_is_safe(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        state.reset_misses("9999")  # Should not raise

    def test_should_skip_below_threshold(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        # 2 misses < threshold of 3 → never skip
        state.record_miss("1")
        state.record_miss("1")
        assert not state.should_skip("1", threshold=3, multiplier=2, max_skip=48, cycle=1)

    def test_should_skip_at_threshold(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        # Exactly threshold misses → no skip yet (need > threshold to start skipping)
        for _ in range(3):
            state.record_miss("1")
        assert not state.should_skip("1", threshold=3, multiplier=2, max_skip=48, cycle=1)

    def test_should_skip_above_threshold(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        # 4 misses, threshold=3 → skip_cycles = 2^(4-3) = 2
        for _ in range(4):
            state.record_miss("1")
        # cycle=1: 1 % 2 != 0 → skip
        assert state.should_skip("1", threshold=3, multiplier=2, max_skip=48, cycle=1)
        # cycle=2: 2 % 2 == 0 → don't skip
        assert not state.should_skip("1", threshold=3, multiplier=2, max_skip=48, cycle=2)

    def test_should_skip_respects_max_skip(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        # Many misses → capped at max_skip=4
        for _ in range(20):
            state.record_miss("1")
        # skip_cycles = min(2^17, 4) = 4; cycle=4 → 4%4==0 → don't skip
        assert not state.should_skip("1", threshold=3, multiplier=2, max_skip=4, cycle=4)
        # cycle=1 → 1%4!=0 → skip
        assert state.should_skip("1", threshold=3, multiplier=2, max_skip=4, cycle=1)

    def test_touch_poll(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        before = time.time()
        state.touch_poll()
        assert state.last_poll >= before

    def test_save_and_reload(self, tmp_path):
        path = tmp_path / "state.json"
        state = ProbeState(path)
        state.mark_discovered("https://example.com/D1234R0.pdf")
        state.record_miss("5678")
        state.touch_poll()
        state.save()

        state2 = ProbeState(path)
        assert state2.is_discovered("https://example.com/D1234R0.pdf")
        assert state2.get_miss_count("5678") == 1
        assert state2.last_poll > 0.0

    def test_loads_existing_file(self, tmp_path):
        path = tmp_path / "state.json"
        data = {
            "discovered": {"https://example.com/D1.pdf": 1234567890.0},
            "miss_counts": {"42": 5},
            "last_poll": 9876543210.0,
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        state = ProbeState(path)
        assert state.is_discovered("https://example.com/D1.pdf")
        assert state.get_miss_count("42") == 5
        assert state.last_poll == 9876543210.0
