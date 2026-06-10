"""Graceful process shutdown: drain MQ, stop HTTP servers, join worker threads."""

from __future__ import annotations

import logging
import threading
from http.server import HTTPServer

from slack_bolt import App

from .scout import MessageQueue

log = logging.getLogger("paperscout")


def stop_bolt_server(app: App) -> None:
    """Stop Slack Bolt HTTP dev server started via ``app.start()``.

    Bolt has no public graceful-shutdown API for the dev server; this uses the
    private ``_development_server._server`` handle (slack-bolt pinned in uv.lock).
    """
    dev = getattr(app, "_development_server", None)
    if dev is None:
        return
    server = getattr(dev, "_server", None)
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            log.exception("shutdown: bolt server shutdown failed")


def _join_thread(thread: threading.Thread | None, timeout: float, label: str) -> None:
    """Wait for *thread* to finish; log a warning if it exceeds *timeout*."""
    if thread is None or not thread.is_alive():
        return
    thread.join(timeout)
    if thread.is_alive():
        log.warning("shutdown: %s thread did not exit within %.1fs", label, timeout)


def shutdown_services(
    *,
    reason: str,
    mq: MessageQueue | None,
    health_server: HTTPServer | None,
    health_thread: threading.Thread | None,
    app: App | None,
    bolt_thread: threading.Thread | None,
    mq_drain_timeout: float,
    thread_join_timeout: float,
) -> int:
    """Ordered teardown. Returns the number of messages drained from the queue."""
    drained = 0
    if mq is not None:
        try:
            drained = mq.drain(timeout=mq_drain_timeout)
        except Exception:
            log.exception("shutdown: MQ drain failed")

    if health_server is not None:
        try:
            health_server.shutdown()
        except Exception:
            log.exception("shutdown: health server shutdown failed")
    try:
        _join_thread(health_thread, thread_join_timeout, "health")
    except Exception:
        log.exception("shutdown: health thread join failed")

    if app is not None:
        try:
            stop_bolt_server(app)
        except Exception:
            log.exception("shutdown: bolt server stop failed")
    try:
        _join_thread(bolt_thread, thread_join_timeout, "bolt")
    except Exception:
        log.exception("shutdown: bolt thread join failed")

    log.info(
        "=== Paperscout shutting down (%s) — drained %d queued message(s) ===",
        reason,
        drained,
    )
    return drained
