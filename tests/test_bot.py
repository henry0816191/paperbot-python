"""Tests for paperbot.bot."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from paperbot.models import Paper
from paperbot.monitor import DiffResult, DPTransition, PerUserMatches, PollResult
from paperbot.sources import ProbeHit
from paperbot.storage import ProbeState, UserWatchlist
from paperbot.bot import (
    MessageQueue,
    _batch_lines,
    _fmt_lm,
    _handle_status,
    _handle_watchlist,
    _hit_label,
    _paper_link,
    _reply_opts,
    _show_watchlist,
    notify_channel,
    notify_users,
    register_handlers,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _empty_diff() -> DiffResult:
    return DiffResult(new_papers=[], updated_papers=[])


def _make_result(
    new_papers=None,
    probe_hits=None,
    dp_transitions=None,
    per_user_matches=None,
) -> PollResult:
    return PollResult(
        diff=DiffResult(new_papers=new_papers or [], updated_papers=[]),
        probe_hits=probe_hits or [],
        dp_transitions=dp_transitions or [],
        per_user_matches=per_user_matches or {},
    )


def _make_settings(channel="C123456", **overrides):
    defaults = dict(
        notification_channel=channel,
        notify_on_frontier_hit=True,
        notify_on_any_draft=True,
        notify_on_dp_transition=True,
        poll_interval_minutes=30,
        enable_iso_probe=True,
        alert_modified_hours=24,
        cold_cycle_divisor=48,
    )
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _recent_hit(tier="frontier", number=9999, **kwargs) -> ProbeHit:
    defaults = dict(
        url=f"https://isocpp.org/files/papers/D{number:04d}R0.pdf",
        prefix="D", number=number, revision=0, extension=".pdf",
        tier=tier, is_recent=True,
        last_modified=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    defaults.update(kwargs)
    return ProbeHit(**defaults)


# ── _fmt_lm ───────────────────────────────────────────────────────────────────

class TestFmtLm:
    def test_none(self):
        assert "unknown" in _fmt_lm(None)

    def test_minutes_ago(self):
        lm = datetime.now(timezone.utc) - timedelta(minutes=30)
        assert "30m ago" in _fmt_lm(lm)

    def test_hours_ago(self):
        lm = datetime.now(timezone.utc) - timedelta(hours=5)
        assert "5h ago" in _fmt_lm(lm)

    def test_days_ago_shows_date(self):
        lm = datetime(2025, 1, 15, tzinfo=timezone.utc)
        assert "2025-01-15" in _fmt_lm(lm)


# ── _paper_link / _hit_label ──────────────────────────────────────────────────

class TestHelpers:
    def test_paper_link_uses_url(self):
        paper = Paper(id="P2300R10", url="https://wg21.link/P2300R10")
        assert _paper_link(paper) == "<https://wg21.link/P2300R10|P2300R10>"

    def test_paper_link_falls_back_to_long_link(self):
        paper = Paper(id="P2300R10", url="", long_link="https://wg21.link/P2300R10.pdf")
        assert _paper_link(paper) == "<https://wg21.link/P2300R10.pdf|P2300R10>"

    def test_paper_link_synthesises_wg21_url(self):
        paper = Paper(id="P2300R10", url="", long_link="")
        link = _paper_link(paper)
        assert "wg21.link/P2300R10" in link
        assert "|P2300R10>" in link

    def test_hit_label(self):
        label = _hit_label("https://isocpp.org/files/papers/D2300R11.pdf",
                           "D", 2300, 11, ".pdf")
        assert label == "<https://isocpp.org/files/papers/D2300R11.pdf|D2300R11.pdf>"


# ── notify_channel ────────────────────────────────────────────────────────────

class TestNotifyChannel:
    def test_no_channel_returns_silently(self):
        app = MagicMock()
        mq = MagicMock()
        with patch("paperbot.bot.settings", _make_settings(channel="")):
            notify_channel(app, _make_result(), mq)
        mq.enqueue.assert_not_called()

    def test_empty_result_posts_nothing(self):
        app = MagicMock()
        mq = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, _make_result(), mq)
        mq.enqueue.assert_not_called()

    # ── Probe hits ────────────────────────────────────────────────────────────

    def test_frontier_hits_batched_with_count_and_links(self):
        app = MagicMock()
        mq = MagicMock()
        hits = [_recent_hit(tier="frontier", number=n) for n in (4033, 4034, 4035)]
        result = _make_result(probe_hits=hits)
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result, mq)
        mq.enqueue.assert_called_once()
        text = mq.enqueue.call_args[0][1]
        assert "3 new frontier draft(s)" in text
        for h in hits:
            assert h.url in text

    def test_other_probe_hits_batched(self):
        app = MagicMock()
        mq = MagicMock()
        hits = [_recent_hit(tier="recent", number=n) for n in (5000, 5001)]
        result = _make_result(probe_hits=hits)
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result, mq)
        text = mq.enqueue.call_args[0][1]
        assert "2 new draft(s) discovered" in text

    def test_cold_hits_batched_with_other(self):
        app = MagicMock()
        mq = MagicMock()
        hit = _recent_hit(tier="cold")
        result = _make_result(probe_hits=[hit])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result, mq)
        assert "1 new draft(s) discovered" in mq.enqueue.call_args[0][1]

    def test_frontier_suppressed_when_disabled(self):
        app = MagicMock()
        mq = MagicMock()
        result = _make_result(probe_hits=[_recent_hit(tier="frontier")])
        with patch("paperbot.bot.settings", _make_settings(notify_on_frontier_hit=False)):
            notify_channel(app, result, mq)
        mq.enqueue.assert_not_called()

    def test_any_draft_suppressed_when_disabled(self):
        app = MagicMock()
        mq = MagicMock()
        for tier in ("recent", "cold"):
            result = _make_result(probe_hits=[_recent_hit(tier=tier)])
            with patch("paperbot.bot.settings", _make_settings(notify_on_any_draft=False)):
                notify_channel(app, result, mq)
        mq.enqueue.assert_not_called()

    def test_last_modified_shown_in_batch(self):
        app = MagicMock()
        mq = MagicMock()
        lm = datetime.now(timezone.utc) - timedelta(hours=3)
        hit = _recent_hit(tier="frontier", last_modified=lm)
        result = _make_result(probe_hits=[hit])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result, mq)
        assert "3h ago" in mq.enqueue.call_args[0][1]

    # ── D→P transitions ───────────────────────────────────────────────────────

    def test_dp_all_transitions_are_batched(self):
        app = MagicMock()
        mq = MagicMock()
        paper = Paper(id="P2300R11", title="Senders", author="Unknown Author",
                      url="https://wg21.link/P2300R11")
        tr = DPTransition(paper=paper,
                          draft_url="https://isocpp.org/files/papers/D2300R11.pdf",
                          last_modified=1_700_000_000.0, discovered_at=1_699_900_000.0)
        result = _make_result(dp_transitions=[tr])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result, mq)
        text = mq.enqueue.call_args[0][1]
        assert "draft(s) now published" in text
        assert "<https://wg21.link/P2300R11|P2300R11>" in text
        assert "D2300R11" in text

    def test_dp_suppressed_when_disabled(self):
        app = MagicMock()
        mq = MagicMock()
        paper = Paper(id="P2300R11", title="X", author="Y")
        tr = DPTransition(paper=paper,
                          draft_url="https://isocpp.org/files/papers/D2300R11.pdf",
                          last_modified=None, discovered_at=0.0)
        result = _make_result(dp_transitions=[tr])
        with patch("paperbot.bot.settings", _make_settings(notify_on_dp_transition=False)):
            notify_channel(app, result, mq)
        mq.enqueue.assert_not_called()

    def test_dp_batch_contains_draft_link(self):
        app = MagicMock()
        mq = MagicMock()
        paper = Paper(id="P9999R0", title="Foo", author="Bar", url="")
        tr = DPTransition(paper=paper,
                          draft_url="https://isocpp.org/files/papers/D9999R0.pdf",
                          last_modified=None, discovered_at=0.0)
        result = _make_result(dp_transitions=[tr])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result, mq)
        text = mq.enqueue.call_args[0][1]
        assert "draft(s) now published" in text
        assert "D9999R0.pdf" in text


# ── notify_users ──────────────────────────────────────────────────────────────

class TestNotifyUsers:
    def test_no_matches_posts_nothing(self):
        app = MagicMock()
        mq = MagicMock()
        result = _make_result()
        notify_users(app, result, mq)
        mq.enqueue.assert_not_called()

    def test_author_match_sends_dm(self):
        app = MagicMock()
        mq = MagicMock()
        paper = Paper(id="P2300R11", title="Senders", author="Eric Niebler",
                      url="https://wg21.link/P2300R11")
        pum = PerUserMatches(papers=[(paper, "author")], probe_hits=[])
        result = _make_result(per_user_matches={"U123": pum})
        notify_users(app, result, mq)
        mq.enqueue.assert_called_once()
        channel, text = mq.enqueue.call_args[0]
        assert channel == "U123"
        assert "P2300R11" in text
        assert "author match" in text

    def test_paper_match_sends_dm(self):
        app = MagicMock()
        mq = MagicMock()
        paper = Paper(id="P2300R11", title="X", author="Someone",
                      url="https://wg21.link/P2300R11")
        pum = PerUserMatches(papers=[(paper, "paper")], probe_hits=[])
        result = _make_result(per_user_matches={"U456": pum})
        notify_users(app, result, mq)
        channel, text = mq.enqueue.call_args[0]
        assert channel == "U456"
        assert "paper match" in text

    def test_probe_hit_match_sends_dm(self):
        app = MagicMock()
        mq = MagicMock()
        hit = _recent_hit()
        pum = PerUserMatches(papers=[], probe_hits=[(hit, "author")])
        result = _make_result(per_user_matches={"U789": pum})
        notify_users(app, result, mq)
        mq.enqueue.assert_called_once()
        _, text = mq.enqueue.call_args[0]
        assert hit.url in text

    def test_multiple_users_get_separate_dms(self):
        app = MagicMock()
        mq = MagicMock()
        paper = Paper(id="P2300R11", title="X", author="Niebler")
        pum = PerUserMatches(papers=[(paper, "author")], probe_hits=[])
        result = _make_result(per_user_matches={"U1": pum, "U2": pum})
        notify_users(app, result, mq)
        assert mq.enqueue.call_count == 2
        channels = {call[0][0] for call in mq.enqueue.call_args_list}
        assert channels == {"U1", "U2"}


# ── _batch_lines ──────────────────────────────────────────────────────────────

class TestBatchLines:
    def test_single_batch_when_small(self):
        batches = _batch_lines(["line1", "line2", "line3"], max_len=1000)
        assert len(batches) == 1

    def test_splits_when_over_limit(self):
        lines = ["x" * 100] * 10
        batches = _batch_lines(lines, max_len=250)
        assert len(batches) > 1

    def test_empty_lines(self):
        assert _batch_lines([], max_len=1000) == []

    def test_single_line_exceeding_limit(self):
        batches = _batch_lines(["x" * 500], max_len=100)
        assert len(batches) == 1


# ── _reply_opts ───────────────────────────────────────────────────────────────

class TestReplyOpts:
    def test_no_thread(self):
        opts = _reply_opts({"ts": "123"})
        assert "thread_ts" not in opts
        assert opts["unfurl_links"] is False

    def test_with_thread(self):
        opts = _reply_opts({"ts": "123", "thread_ts": "456"})
        assert opts["thread_ts"] == "456"


# ── _handle_watchlist ─────────────────────────────────────────────────────────

class TestHandleWatchlist:
    def test_add_new_author(self, fake_pool):
        say = MagicMock()
        wl = UserWatchlist(fake_pool)
        _handle_watchlist(["add", "Niebler"], "U1", wl, say, {})
        text = say.call_args[1]["text"]
        assert "Added" in text
        assert "author" in text

    def test_add_paper_number(self, fake_pool):
        say = MagicMock()
        wl = UserWatchlist(fake_pool)
        _handle_watchlist(["add", "2300"], "U1", wl, say, {})
        text = say.call_args[1]["text"]
        assert "Added" in text
        assert "paper number" in text

    def test_add_existing_entry(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "Niebler")
        say = MagicMock()
        _handle_watchlist(["add", "Niebler"], "U1", wl, say, {})
        assert "already" in say.call_args[1]["text"]

    def test_add_multi_word_name(self, fake_pool):
        say = MagicMock()
        wl = UserWatchlist(fake_pool)
        _handle_watchlist(["add", "Eric", "Niebler"], "U1", wl, say, {})
        assert "Added" in say.call_args[1]["text"]

    def test_remove_existing(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "Niebler")
        say = MagicMock()
        _handle_watchlist(["remove", "Niebler"], "U1", wl, say, {})
        assert "Removed" in say.call_args[1]["text"]

    def test_remove_nonexistent(self, fake_pool):
        say = MagicMock()
        wl = UserWatchlist(fake_pool)
        _handle_watchlist(["remove", "Nobody"], "U1", wl, say, {})
        assert "not on your watchlist" in say.call_args[1]["text"]

    def test_list_shows_entries(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "Niebler")
        say = MagicMock()
        _handle_watchlist(["list"], "U1", wl, say, {})
        assert "niebler" in say.call_args[1]["text"]

    def test_no_args_shows_list(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "Stroustrup")
        say = MagicMock()
        _handle_watchlist([], "U1", wl, say, {})
        assert "stroustrup" in say.call_args[1]["text"]

    def test_add_without_name(self, fake_pool):
        say = MagicMock()
        wl = UserWatchlist(fake_pool)
        _handle_watchlist(["add"], "U1", wl, say, {})
        assert "Usage" in say.call_args[1]["text"]

    def test_invalid_action(self, fake_pool):
        say = MagicMock()
        wl = UserWatchlist(fake_pool)
        _handle_watchlist(["bogus", "name"], "U1", wl, say, {})
        assert "Usage" in say.call_args[1]["text"]

    def test_reply_opts_forwarded(self, fake_pool):
        say = MagicMock()
        wl = UserWatchlist(fake_pool)
        _handle_watchlist(["list"], "U1", wl, say, {"thread_ts": "t1"})
        assert say.call_args[1]["thread_ts"] == "t1"


# ── _show_watchlist ───────────────────────────────────────────────────────────

class TestShowWatchlist:
    def test_empty_watchlist(self, fake_pool):
        say = MagicMock()
        _show_watchlist("U1", UserWatchlist(fake_pool), say, {})
        assert "empty" in say.call_args[1]["text"].lower()

    def test_non_empty_watchlist(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "Baker")
        say = MagicMock()
        _show_watchlist("U1", wl, say, {})
        assert "baker" in say.call_args[1]["text"]

    def test_shows_type_labels(self, fake_pool):
        wl = UserWatchlist(fake_pool)
        wl.add("U1", "niebler")
        wl.add("U1", "2300")
        say = MagicMock()
        _show_watchlist("U1", wl, say, {})
        text = say.call_args[1]["text"]
        assert "author" in text
        assert "paper" in text


# ── _handle_status ────────────────────────────────────────────────────────────

class TestHandleStatus:
    def test_status_never_polled(self, fake_pool):
        state = ProbeState(fake_pool)
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            _handle_status(state, lambda: 42, say, {})
        text = say.call_args[1]["text"]
        assert "42" in text and "never" in text

    def test_status_after_poll(self, fake_pool):
        state = ProbeState(fake_pool)
        state.touch_poll()
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            _handle_status(state, lambda: 100, say, {})
        text = say.call_args[1]["text"]
        assert "100" in text and "never" not in text


# ── register_handlers ─────────────────────────────────────────────────────────

class TestRegisterHandlers:
    def _setup(self, fake_pool):
        app = MagicMock()
        registered: dict = {}

        def capture_event(name):
            def decorator(fn):
                registered[name] = fn
                return fn
            return decorator

        app.event.side_effect = capture_event
        user_watchlist = UserWatchlist(fake_pool)
        state = ProbeState(fake_pool)
        register_handlers(app, user_watchlist, state, lambda: 99)
        return registered, user_watchlist, state

    def test_app_mention_status(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            registered["app_mention"](
                event={"text": "<@U1> status", "ts": "1", "user": "U1"},
                context={"bot_user_id": "U1"},
                say=say,
            )
        assert "Status" in say.call_args[1]["text"]

    def test_app_mention_empty_text(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["app_mention"](
            event={"text": "", "ts": "1", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_app_mention_only_mention_no_command(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["app_mention"](
            event={"text": "<@U1>", "ts": "1", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_message_dm_dispatches(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            registered["message"](
                event={"text": "status", "channel_type": "im", "ts": "1", "user": "U1"},
                context={"bot_user_id": "U1"},
                say=say,
            )
        say.assert_called_once()

    def test_message_dm_strips_mention(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            registered["message"](
                event={"text": "<@U1> status", "channel_type": "im", "ts": "1", "user": "U1"},
                context={"bot_user_id": "U1"},
                say=say,
            )
        say.assert_called_once()

    def test_message_dm_empty_after_strip(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["message"](
            event={"text": "<@U1>", "channel_type": "im", "ts": "1", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_message_channel_with_mention_ignored_by_message_handler(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["message"](
            event={"text": "<@U1> status", "channel_type": "channel", "ts": "1", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_app_mention_channel_watchlist_silently_ignored(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["app_mention"](
            event={"text": "<@U1> watchlist list", "ts": "1",
                   "channel_type": "channel", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_message_mpim_watchlist_gets_error(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["message"](
            event={"text": "<@U1> watchlist add niebler", "channel_type": "mpim",
                   "ts": "1", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_called_once()
        assert "1:1 DM" in say.call_args[1]["text"]

    def test_message_mpim_status_responds(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            registered["message"](
                event={"text": "<@U1> status", "channel_type": "mpim",
                       "ts": "1", "user": "U1"},
                context={"bot_user_id": "U1"},
                say=say,
            )
        say.assert_called_once()
        assert "Status" in say.call_args[1]["text"]

    def test_message_subtype_ignored(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["message"](
            event={"text": "status", "subtype": "message_changed", "channel_type": "im",
                   "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_message_bot_id_ignored(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["message"](
            event={"text": "status", "bot_id": "B123", "channel_type": "im", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_message_empty_text_ignored(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["message"](
            event={"text": "", "channel_type": "im", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_dispatch_help(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["message"](
            event={"text": "help", "channel_type": "im", "ts": "1", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        assert "Commands" in say.call_args[1]["text"]

    def test_dispatch_unknown_command(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["message"](
            event={"text": "foobar", "channel_type": "im", "ts": "1", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        assert "Unknown" in say.call_args[1]["text"]

    def test_dispatch_watchlist_list_in_dm(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["message"](
            event={"text": "watchlist list", "channel_type": "im", "ts": "1", "user": "U1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_called_once()
        assert "empty" in say.call_args[1]["text"].lower()

    def test_dispatch_empty_text(self, fake_pool):
        registered, _, _ = self._setup(fake_pool)
        say = MagicMock()
        registered["message"](
            event={"text": "   ", "channel_type": "im", "ts": "1", "user": "U1"},
            context={"bot_user_id": ""},
            say=say,
        )
        say.assert_not_called()


# ── create_app ────────────────────────────────────────────────────────────────

class TestCreateApp:
    def test_create_app_uses_settings(self):
        from paperbot.bot import create_app
        mock_settings = MagicMock()
        mock_settings.slack_bot_token = "xoxb-test"
        mock_settings.slack_signing_secret = "secret"
        with patch("paperbot.bot.settings", mock_settings):
            with patch("paperbot.bot.App") as mock_app_cls:
                create_app()
        mock_app_cls.assert_called_once_with(
            token="xoxb-test",
            signing_secret="secret",
        )
