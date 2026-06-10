"""Tests for paperscout.shutdown."""

from __future__ import annotations

import logging
from http.server import HTTPServer
from unittest.mock import MagicMock

from paperscout.shutdown import shutdown_services, stop_bolt_server


class TestShutdownServices:
    def test_shutdown_services_drains_mq_and_logs(self, caplog):
        mq = MagicMock()
        mq.drain.return_value = 2
        with caplog.at_level(logging.INFO, logger="paperscout"):
            drained = shutdown_services(
                reason="SIGTERM",
                mq=mq,
                health_server=None,
                health_thread=None,
                app=None,
                bolt_thread=None,
                mq_drain_timeout=30.0,
                thread_join_timeout=5.0,
            )
        assert drained == 2
        mq.drain.assert_called_once_with(timeout=30.0)
        assert any("SIGTERM" in r.message and "drained 2" in r.message for r in caplog.records)

    def test_shutdown_services_skips_none_handles(self):
        shutdown_services(
            reason="unknown",
            mq=None,
            health_server=None,
            health_thread=None,
            app=None,
            bolt_thread=None,
            mq_drain_timeout=30.0,
            thread_join_timeout=5.0,
        )

    def test_stop_bolt_server_calls_shutdown(self):
        app = MagicMock()
        server = MagicMock()
        app._development_server = MagicMock(_server=server)
        stop_bolt_server(app)
        server.shutdown.assert_called_once()

    def test_shutdown_services_stops_health_server(self):
        health_server = MagicMock(spec=HTTPServer)
        shutdown_services(
            reason="SIGINT",
            mq=None,
            health_server=health_server,
            health_thread=None,
            app=None,
            bolt_thread=None,
            mq_drain_timeout=30.0,
            thread_join_timeout=5.0,
        )
        health_server.shutdown.assert_called_once()

    def test_shutdown_services_continues_after_mq_drain_failure(self, caplog):
        mq = MagicMock()
        mq.drain.side_effect = RuntimeError("drain boom")
        health_server = MagicMock(spec=HTTPServer)
        with caplog.at_level(logging.INFO, logger="paperscout"):
            drained = shutdown_services(
                reason="SIGTERM",
                mq=mq,
                health_server=health_server,
                health_thread=None,
                app=None,
                bolt_thread=None,
                mq_drain_timeout=30.0,
                thread_join_timeout=5.0,
            )
        assert drained == 0
        health_server.shutdown.assert_called_once()
        assert any("drained 0" in r.message for r in caplog.records)
