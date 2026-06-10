"""Tests for MessageQueue graceful shutdown."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from slack_sdk.errors import SlackApiError

from paperscout.scout import CircuitState, MessageQueue


def _make_mq() -> MessageQueue:
    app = MagicMock()
    app.client.chat_postMessage = MagicMock()
    return MessageQueue(app)


class TestMessageQueueShutdown:
    def test_stop_drains_pending_messages(self):
        mq = _make_mq()
        mq.start()
        for i in range(3):
            mq.enqueue("C1", f"msg-{i}")
        drained = mq.drain(timeout=5.0)
        assert drained == 3
        assert mq._app.client.chat_postMessage.call_count == 3
        assert not mq.join(timeout=0.1)

    def test_stop_is_idempotent(self):
        mq = _make_mq()
        mq.start()
        mq.enqueue("C1", "hello")
        mq.stop()
        mq.stop()
        drained = mq.drain(timeout=5.0)
        assert drained == 1
        assert not mq.join(timeout=0.1)

    def test_drain_counts_only_successful_sends(self):
        mq = _make_mq()
        response = MagicMock()
        response.status_code = 500
        err = SlackApiError("fail", response)

        def side_effect(**_kwargs):
            if mq._app.client.chat_postMessage.call_count == 1:
                raise err
            return MagicMock()

        mq._app.client.chat_postMessage.side_effect = side_effect
        mq.start()
        mq.enqueue("C1", "fail")
        mq.enqueue("C1", "ok")
        drained = mq.drain(timeout=5.0)
        assert drained == 1

    def test_drain_times_out(self):
        mq = _make_mq()

        def slow_send(**_kwargs):
            time.sleep(2.0)

        mq._app.client.chat_postMessage.side_effect = slow_send
        mq.start()
        mq.enqueue("C1", "slow")
        assert mq.join(timeout=0.1)
        drained = mq.drain(timeout=0.1)
        assert drained == 0
        mq.join(timeout=5.0)
        assert not mq.join(timeout=0.1)

    def test_start_guard_no_double_thread(self):
        mq = _make_mq()
        mq.start()
        first = mq._thread
        mq.start()
        assert mq._thread is first
        assert threading.active_count() >= 1
        mq.drain(timeout=2.0)

    def test_stop_bypasses_open_circuit(self):
        mq = _make_mq()
        mq.start()
        for _ in range(mq._breaker._threshold):
            mq._breaker.record_failure()
        assert mq._breaker.state == CircuitState.OPEN
        assert not mq.enqueue("C1", "blocked")
        mq.stop()
        assert mq.drain(timeout=2.0) == 0
        assert not mq.join(timeout=0.1)

    def test_drain_sends_despite_open_circuit(self):
        mq = _make_mq()
        gate = threading.Event()
        sender_started = threading.Event()

        def gated_send(**_kwargs):
            sender_started.set()
            gate.wait(timeout=2.0)

        mq._app.client.chat_postMessage.side_effect = gated_send
        mq.start()
        mq.enqueue("C1", "queued")
        assert sender_started.wait(timeout=2.0), "sender did not start"
        for _ in range(mq._breaker._threshold):
            mq._breaker.record_failure()
        assert mq._breaker.state == CircuitState.OPEN
        mq.stop()
        gate.set()
        drained = mq.drain(timeout=5.0)
        assert drained == 1

    def test_enqueue_rejected_after_stop(self):
        mq = _make_mq()
        mq.start()
        mq.stop()
        assert mq.enqueue("C1", "too-late") is False
        assert mq._app.client.chat_postMessage.call_count == 0
        mq.drain(timeout=2.0)

    def test_enqueue_rejects_when_stop_races_under_lock(self):
        """Re-check under _queue_lock rejects enqueues that passed the pre-lock check."""
        mq = _make_mq()
        mq.start()

        class _GateLock:
            def __init__(self, real: threading.Lock):
                self._real = real
                self.entered = threading.Event()
                self.proceed = threading.Event()

            def __enter__(self):
                self._real.acquire()
                self.entered.set()
                self.proceed.wait(timeout=2.0)
                return self

            def __exit__(self, *_args):
                self._real.release()

        gate = _GateLock(mq._queue_lock)
        mq._queue_lock = gate

        result: list[bool] = []

        def try_enqueue():
            result.append(mq.enqueue("C1", "late"))

        waiter = threading.Thread(target=try_enqueue)
        waiter.start()
        assert gate.entered.wait(timeout=2.0), "enqueue did not reach lock"
        mq.stop()
        gate.proceed.set()
        waiter.join(timeout=2.0)

        assert result == [False]
        assert mq._app.client.chat_postMessage.call_count == 0
        mq.drain(timeout=2.0)

    def test_stop_does_not_block_on_full_queue(self, monkeypatch):
        monkeypatch.setattr("paperscout.scout.settings.mq_max_size", 1)
        mq = _make_mq()
        sender_busy = threading.Event()

        def slow_send(**_kwargs):
            sender_busy.set()
            time.sleep(10)

        mq._app.client.chat_postMessage.side_effect = slow_send
        mq.start()
        mq.enqueue("C1", "first")
        assert sender_busy.wait(timeout=2.0)
        with mq._queue_lock:
            mq._q.put_nowait(("C1", "second", {}))

        stop_done = threading.Event()

        def call_stop():
            mq.stop()
            stop_done.set()

        stopper = threading.Thread(target=call_stop)
        stopper.start()
        assert stop_done.wait(timeout=1.0)
        stopper.join(timeout=1.0)
        mq.drain(timeout=2.0)
