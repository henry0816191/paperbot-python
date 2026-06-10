"""Lightweight HTTP health-check endpoint."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, cast

from . import __version__

log = logging.getLogger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    """Serves JSON ``GET /health`` with version, uptime, index and probe stats."""

    launch_time: datetime
    paper_count_fn: Callable[[], int]
    state: object  # ProbeState — kept generic to avoid circular import
    extra_fields_fn: Callable[[], dict]

    def do_GET(self) -> None:
        if self.path.rstrip("/") != "/health":
            self.send_error(404)
            return

        now = datetime.now(timezone.utc)
        uptime = (now - self.launch_time).total_seconds()

        from .config import settings

        last_poll = getattr(self.state, "last_poll", None)
        get_disc: Callable[[], dict[str, Any]] = getattr(
            self.state,
            "get_all_discovered",
            cast(Callable[[], dict[str, Any]], lambda: {}),
        )
        discovered = get_disc()

        base = {
            "version": __version__,
            "uptime_seconds": int(uptime),
            "launched_at": self.launch_time.isoformat(),
            "papers_loaded": self.paper_count_fn(),
            "last_poll": (
                datetime.fromtimestamp(last_poll, tz=timezone.utc).isoformat()
                if last_poll
                else None
            ),
            "discovered_via_probe": len(discovered),
            "iso_probe_enabled": settings.enable_iso_probe,
        }
        try:
            extra = self.extra_fields_fn()
            if not isinstance(extra, dict):
                extra = {}
        except Exception:
            log.exception("health: extra_fields_fn failed")
            extra = {}
        # Base handler fields win if extra_fields_fn returns overlapping keys.
        safe_extra = {k: v for k, v in extra.items() if k not in base}
        body = json.dumps({**base, **safe_extra}).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        log.debug("health: %s", format % args)


def start_health_server(
    port: int,
    launch_time: datetime,
    state,
    paper_count_fn: Callable[[], int],
    bind_host: str = "127.0.0.1",
    extra_fields_fn: Callable[[], dict] | None = None,
) -> HTTPServer:
    """Start the ``/health`` HTTP server on *bind_host*:*port* in a daemon thread."""

    _extra = extra_fields_fn or (lambda: {})

    handler = type(
        "_BoundHealthHandler",
        (_HealthHandler,),
        {
            "launch_time": launch_time,
            "paper_count_fn": staticmethod(paper_count_fn),
            "state": state,
            "extra_fields_fn": staticmethod(_extra),
        },
    )

    server = HTTPServer((bind_host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health")
    server._paperscout_thread = thread  # noqa: SLF001 — joined during graceful shutdown
    thread.start()
    log.info("Health endpoint listening on %s:%d", bind_host, port)
    return server
