"""Tests for paperbot.health."""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone

import pytest

from paperbot.health import start_health_server


def _find_free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class _FakeState:
    def __init__(self, last_poll=None, discovered=None):
        self.last_poll = last_poll
        self.discovered = discovered or {}


@pytest.fixture()
def health_url():
    port = _find_free_port()
    launch = datetime(2026, 3, 16, 10, 0, 0, tzinfo=timezone.utc)
    state = _FakeState(last_poll=1742119200.0, discovered={"u1": 1, "u2": 2})
    server = start_health_server(port, launch, state, lambda: 42)
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


class TestHealthEndpoint:
    def test_health_returns_200_with_json(self, health_url):
        resp = urllib.request.urlopen(f"{health_url}/health")
        assert resp.status == 200
        data = json.loads(resp.read())
        assert "version" in data
        assert "uptime_seconds" in data
        assert "launched_at" in data
        assert "papers_loaded" in data
        assert "last_poll" in data
        assert "discovered_via_probe" in data
        assert "iso_probe_enabled" in data

    def test_health_values(self, health_url):
        data = json.loads(urllib.request.urlopen(f"{health_url}/health").read())
        assert data["papers_loaded"] == 42
        assert data["discovered_via_probe"] == 2
        assert data["launched_at"] == "2026-03-16T10:00:00+00:00"
        assert isinstance(data["uptime_seconds"], int)

    def test_health_trailing_slash(self, health_url):
        resp = urllib.request.urlopen(f"{health_url}/health/")
        assert resp.status == 200

    def test_other_path_returns_404(self, health_url):
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{health_url}/notfound")
        assert exc_info.value.code == 404
