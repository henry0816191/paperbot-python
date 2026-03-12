"""Shared fixtures and helpers for the paperbot test suite."""
from __future__ import annotations

from pathlib import Path

import pytest

from paperbot.config import Settings
from paperbot.models import Paper
from paperbot.storage import ProbeState
from paperbot.sources import WG21Index


# ── Settings factory ────────────────────────────────────────────────────────

def make_test_settings(**overrides) -> Settings:
    """Build a Settings instance with safe test defaults (no I/O, no credentials)."""
    base: dict = dict(
        slack_signing_secret="test-secret",
        slack_bot_token="xoxb-test",
        port=3000,
        poll_interval_minutes=30,
        poll_overrun_cooldown_seconds=300,
        enable_bulk_wg21=True,
        enable_bulk_openstd=True,
        enable_iso_probe=True,
        probe_prefixes=["D", "P"],
        probe_extensions=[".pdf"],
        watchlist_papers=[],
        watchlist_authors=[],
        frontier_window_above=3,
        frontier_window_below=1,
        frontier_explicit_ranges=[],
        frontier_gap_threshold=50,
        hot_lookback_months=6,
        hot_revision_depth=2,
        cold_revision_depth=1,
        cold_cycle_divisor=48,
        gap_max_rev=1,
        alert_modified_hours=24,
        http_concurrency=5,
        http_timeout_seconds=5,
        http_use_http2=False,
        notification_channel="",
        notify_on_watchlist_author=True,
        notify_on_watchlist_paper=True,
        notify_on_frontier_hit=True,
        notify_on_any_draft=True,
        data_dir=Path("/tmp/paperbot-test"),
        cache_ttl_hours=1,
    )
    base.update(overrides)
    return Settings.model_construct(**base)


# ── Common data ──────────────────────────────────────────────────────────────

SAMPLE_INDEX_DATA: dict = {
    "P2300R10": {
        "title": "Senders/Receivers",
        "author": "Niebler",
        "date": "2024-01-01",
        "type": "paper",
        "link": "https://wg21.link/P2300R10",
        "long_link": "https://wg21.link/P2300R10.pdf",
        "github_url": "",
        "issues": [],
    },
    "P2301R0": {
        "title": "Some Other Paper",
        "author": "Doe",
        "date": "2024-06-01",
        "type": "paper",
        "link": "https://wg21.link/P2301R0",
        "long_link": "",
        "github_url": "",
    },
    "N4950": {
        "title": "Working Draft",
        "author": "Editor",
        "date": "2023-05-01",
        "type": "paper",
        "link": "https://wg21.link/N4950",
    },
}


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_index_data() -> dict:
    return dict(SAMPLE_INDEX_DATA)


@pytest.fixture
def probe_state(tmp_path) -> ProbeState:
    return ProbeState(tmp_path / "state.json")


@pytest.fixture
def populated_index(tmp_path) -> WG21Index:
    index = WG21Index(tmp_path)
    index._parse_and_index(SAMPLE_INDEX_DATA)
    return index
