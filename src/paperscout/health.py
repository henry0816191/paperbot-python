"""Lightweight HTTP health-check endpoint."""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable

from . import __version__

log = logging.getLogger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    launch_time: datetime
    paper_count_fn: Callable[[], int]
    state: object  # ProbeState — kept generic to avoid circular import

    def do_GET(self) -> None:
        if self.path.rstrip("/") != "/health":
            self.send_error(404)
            return

        now = datetime.now(timezone.utc)
        uptime = (now - self.launch_time).total_seconds()

        from .config import settings

        last_poll = getattr(self.state, "last_poll", None)
        discovered = getattr(self.state, "discovered", {})

        body = json.dumps({
            "version": __version__,
            "uptime_seconds": int(uptime),
            "launched_at": self.launch_time.isoformat(),
            "papers_loaded": self.paper_count_fn(),
            "last_poll": (
                datetime.fromtimestamp(last_poll, tz=timezone.utc).isoformat()
                if last_poll else None
            ),
            "discovered_via_probe": len(discovered),
            "iso_probe_enabled": settings.enable_iso_probe,
        }).encode()

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
) -> HTTPServer:
    """Start the ``/health`` HTTP server on *port* in a daemon thread."""

    handler = type(
        "_BoundHealthHandler",
        (_HealthHandler,),
        {
            "launch_time": launch_time,
            "paper_count_fn": staticmethod(paper_count_fn),
            "state": state,
        },
    )

    server = HTTPServer(("", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="health")
    thread.start()
    log.info("Health endpoint listening on port %d", port)
    return server
