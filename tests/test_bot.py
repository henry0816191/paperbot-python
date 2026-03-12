"""Tests for paperbot.bot."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from paperbot.models import Paper
from paperbot.monitor import DiffResult, DPTransition, PollResult, Watchlist
from paperbot.sources import ProbeHit
from paperbot.storage import ProbeState
from paperbot.bot import (
    _batch_lines,
    _fmt_lm,
    _handle_status,
    _handle_watchlist,
    _hit_label,
    _is_watchlist_related,
    _paper_link,
    _reply_opts,
    _show_watchlist,
    notify_channel,
    register_handlers,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _empty_diff() -> DiffResult:
    return DiffResult(new_papers=[], updated_papers=[])


def _make_result(
    new_papers=None,
    probe_hits=None,
    watchlist_matches=None,
    probe_watchlist_hits=None,
    dp_transitions=None,
) -> PollResult:
    return PollResult(
        diff=DiffResult(new_papers=new_papers or [], updated_papers=[]),
        probe_hits=probe_hits or [],
        watchlist_matches=watchlist_matches or [],
        probe_watchlist_hits=probe_watchlist_hits or [],
        dp_transitions=dp_transitions or [],
    )


def _make_settings(channel="C123456", **overrides):
    defaults = dict(
        notification_channel=channel,
        notify_on_watchlist_author=True,
        notify_on_watchlist_paper=True,
        notify_on_frontier_hit=True,
        notify_on_any_draft=True,
        notify_on_dp_transition=True,
        poll_interval_minutes=30,
        enable_iso_probe=True,
        alert_modified_hours=24,
        cold_cycle_divisor=48,
        watchlist_papers=[],   # empty by default; override per test
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
        result = _fmt_lm(lm)
        assert "30m ago" in result

    def test_hours_ago(self):
        lm = datetime.now(timezone.utc) - timedelta(hours=5)
        result = _fmt_lm(lm)
        assert "5h ago" in result

    def test_days_ago_shows_date(self):
        lm = datetime(2025, 1, 15, tzinfo=timezone.utc)
        result = _fmt_lm(lm)
        assert "2025-01-15" in result


# ── _paper_link / _hit_label / _is_watchlist_related ─────────────────────────

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

    def test_is_watchlist_related_by_number(self):
        assert _is_watchlist_related(2300, "Unknown", {2300}, [])

    def test_is_watchlist_related_by_author(self):
        assert _is_watchlist_related(9999, "Eric Niebler", set(), ["niebler"])

    def test_is_watchlist_related_no_match(self):
        assert not _is_watchlist_related(9999, "Joe Bloggs", {2300}, ["niebler"])

    def test_is_watchlist_related_no_number(self):
        assert not _is_watchlist_related(None, "Eric Niebler", {2300}, ["baker"])


# ── notify_channel ────────────────────────────────────────────────────────────

class TestNotifyChannel:
    def test_no_channel_returns_silently(self):
        app = MagicMock()
        with patch("paperbot.bot.settings", _make_settings(channel="")):
            notify_channel(app, _make_result())
        app.client.chat_postMessage.assert_not_called()

    def test_empty_result_posts_nothing(self):
        app = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, _make_result())
        app.client.chat_postMessage.assert_not_called()

    # ── Watchlist author: index match ─────────────────────────────────────────

    def test_watchlist_author_index_match_contains_slack_link(self):
        app = MagicMock()
        paper = Paper(id="P2300R11", title="Senders", author="Eric Niebler",
                      url="https://wg21.link/P2300R11")
        result = _make_result(watchlist_matches=[paper])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "Watched author" in text
        assert "<https://wg21.link/P2300R11|P2300R11>" in text
        assert "Niebler" in text

    def test_watchlist_author_index_fallback_link(self):
        app = MagicMock()
        paper = Paper(id="P2300R11", title="X", author="Y", url="")
        result = _make_result(watchlist_matches=[paper])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "wg21.link/P2300R11" in text

    def test_watchlist_author_suppressed_when_disabled(self):
        app = MagicMock()
        result = _make_result(watchlist_matches=[Paper(id="P2300R11", author="Niebler")])
        with patch("paperbot.bot.settings", _make_settings(notify_on_watchlist_author=False)):
            notify_channel(app, result)
        app.client.chat_postMessage.assert_not_called()

    # ── Watchlist author: probe hit ───────────────────────────────────────────

    def test_probe_watchlist_hit_contains_url(self):
        app = MagicMock()
        wl = MagicMock()
        wl.authors = ["niebler"]
        hit = _recent_hit(tier="recent", front_text="written by niebler")
        result = _make_result(probe_watchlist_hits=[hit])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result, watchlist=wl)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "niebler" in text
        assert hit.url in text

    # ── Watchlist paper: individual probe hit ─────────────────────────────────

    def test_watchlist_tier_probe_hit_is_individual_with_link(self):
        app = MagicMock()
        hit = _recent_hit(tier="watchlist")
        result = _make_result(probe_hits=[hit])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "Watched paper" in text
        assert hit.url in text

    def test_watchlist_paper_suppressed_when_disabled(self):
        app = MagicMock()
        hit = _recent_hit(tier="watchlist")
        result = _make_result(probe_hits=[hit])
        with patch("paperbot.bot.settings", _make_settings(notify_on_watchlist_paper=False)):
            notify_channel(app, result)
        app.client.chat_postMessage.assert_not_called()

    # ── Probe hits: batched with hyperlinks ───────────────────────────────────

    def test_frontier_hits_batched_with_count_and_links(self):
        app = MagicMock()
        hits = [_recent_hit(tier="frontier", number=n) for n in (4033, 4034, 4035)]
        result = _make_result(probe_hits=hits)
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "3 new frontier draft(s)" in text
        for h in hits:
            assert h.url in text

    def test_other_probe_hits_batched(self):
        app = MagicMock()
        hits = [_recent_hit(tier="recent", number=n) for n in (5000, 5001)]
        result = _make_result(probe_hits=hits)
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "2 new draft(s) discovered" in text

    def test_cold_hits_batched_with_other(self):
        app = MagicMock()
        hit = _recent_hit(tier="cold")
        result = _make_result(probe_hits=[hit])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result)
        assert "1 new draft(s) discovered" in app.client.chat_postMessage.call_args[1]["text"]

    def test_frontier_suppressed_when_disabled(self):
        app = MagicMock()
        result = _make_result(probe_hits=[_recent_hit(tier="frontier")])
        with patch("paperbot.bot.settings", _make_settings(notify_on_frontier_hit=False)):
            notify_channel(app, result)
        app.client.chat_postMessage.assert_not_called()

    def test_any_draft_suppressed_when_disabled(self):
        app = MagicMock()
        for tier in ("recent", "cold"):
            result = _make_result(probe_hits=[_recent_hit(tier=tier)])
            with patch("paperbot.bot.settings", _make_settings(notify_on_any_draft=False)):
                notify_channel(app, result)
        app.client.chat_postMessage.assert_not_called()

    def test_watchlist_probe_hit_not_in_batch(self):
        """Probe hit already in probe_watchlist_hits is excluded from batch section."""
        app = MagicMock()
        wl = MagicMock()
        wl.authors = ["niebler"]
        hit = _recent_hit(tier="frontier", front_text="niebler")
        result = _make_result(probe_hits=[hit], probe_watchlist_hits=[hit])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result, watchlist=wl)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "frontier draft" not in text.lower()

    # ── D→P transitions ───────────────────────────────────────────────────────

    def test_dp_non_watchlist_is_batched_with_links(self):
        app = MagicMock()
        paper = Paper(id="P2300R11", title="Senders", author="Unknown Author",
                      url="https://wg21.link/P2300R11")
        tr = DPTransition(paper=paper,
                          draft_url="https://isocpp.org/files/papers/D2300R11.pdf",
                          last_modified=1_700_000_000.0, discovered_at=1_699_900_000.0)
        result = _make_result(dp_transitions=[tr])
        with patch("paperbot.bot.settings", _make_settings(watchlist_papers=[])):
            notify_channel(app, result)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "draft(s) now published" in text
        assert "<https://wg21.link/P2300R11|P2300R11>" in text
        assert "D2300R11" in text
        assert "Watched paper published" not in text

    def test_dp_watchlist_paper_number_is_individual(self):
        app = MagicMock()
        paper = Paper(id="P2300R11", title="Senders", author="Unknown",
                      url="https://wg21.link/P2300R11")
        tr = DPTransition(paper=paper,
                          draft_url="https://isocpp.org/files/papers/D2300R11.pdf",
                          last_modified=1_700_000_000.0, discovered_at=1_699_900_000.0)
        result = _make_result(dp_transitions=[tr])
        with patch("paperbot.bot.settings", _make_settings(watchlist_papers=[2300])):
            notify_channel(app, result)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "Watched paper published" in text
        assert "<https://wg21.link/P2300R11|P2300R11>" in text
        assert "draft(s) now published" not in text

    def test_dp_watchlist_author_is_individual(self):
        app = MagicMock()
        paper = Paper(id="P9999R0", title="X", author="Eric Niebler", url="")
        tr = DPTransition(paper=paper,
                          draft_url="https://isocpp.org/files/papers/D9999R0.pdf",
                          last_modified=None, discovered_at=0.0)
        result = _make_result(dp_transitions=[tr])
        wl = MagicMock()
        wl.authors = ["niebler"]
        with patch("paperbot.bot.settings", _make_settings(watchlist_papers=[])):
            notify_channel(app, result, watchlist=wl)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "Watched paper published" in text
        assert "draft(s) now published" not in text

    def test_dp_suppressed_when_disabled(self):
        app = MagicMock()
        paper = Paper(id="P2300R11", title="X", author="Y")
        tr = DPTransition(paper=paper,
                          draft_url="https://isocpp.org/files/papers/D2300R11.pdf",
                          last_modified=None, discovered_at=0.0)
        result = _make_result(dp_transitions=[tr])
        with patch("paperbot.bot.settings", _make_settings(notify_on_dp_transition=False)):
            notify_channel(app, result)
        app.client.chat_postMessage.assert_not_called()

    def test_dp_no_last_modified_individual_shows_unknown(self):
        """Individual (watchlist) D→P with no Last-Modified shows 'unknown' age."""
        app = MagicMock()
        paper = Paper(id="P9999R0", title="X", author="Niebler", url="")
        tr = DPTransition(paper=paper,
                          draft_url="https://isocpp.org/files/papers/D9999R0.pdf",
                          last_modified=None, discovered_at=0.0)
        result = _make_result(dp_transitions=[tr])
        wl = MagicMock()
        wl.authors = ["niebler"]
        with patch("paperbot.bot.settings", _make_settings(watchlist_papers=[])):
            notify_channel(app, result, watchlist=wl)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "Watched paper published" in text
        assert "unknown" in text   # _fmt_lm(None) → "modified: unknown"

    def test_dp_batch_contains_draft_link(self):
        """Batch D→P entry must link to both the P-paper and the draft."""
        app = MagicMock()
        paper = Paper(id="P9999R0", title="Foo", author="Bar", url="")
        tr = DPTransition(paper=paper,
                          draft_url="https://isocpp.org/files/papers/D9999R0.pdf",
                          last_modified=None, discovered_at=0.0)
        result = _make_result(dp_transitions=[tr])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result)
        text = app.client.chat_postMessage.call_args[1]["text"]
        assert "draft(s) now published" in text
        assert "D9999R0.pdf" in text   # draft URL in the link

    def test_post_failure_does_not_raise(self):
        app = MagicMock()
        app.client.chat_postMessage.side_effect = Exception("Slack down")
        result = _make_result(watchlist_matches=[Paper(id="P2300R11", author="Niebler")])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result)

    def test_no_watchlist_arg_probe_wl_hit_still_posts(self):
        app = MagicMock()
        hit = _recent_hit(tier="frontier", front_text="some content")
        result = _make_result(probe_watchlist_hits=[hit])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result)  # no watchlist kwarg
        app.client.chat_postMessage.assert_called_once()

    def test_last_modified_shown_in_batch(self):
        app = MagicMock()
        lm = datetime.now(timezone.utc) - timedelta(hours=3)
        hit = _recent_hit(tier="frontier", last_modified=lm)
        result = _make_result(probe_hits=[hit])
        with patch("paperbot.bot.settings", _make_settings()):
            notify_channel(app, result)
        assert "3h ago" in app.client.chat_postMessage.call_args[1]["text"]


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
    def _wl(self, tmp_path) -> Watchlist:
        return Watchlist(tmp_path / "wl.json")

    def test_add_new_author(self, tmp_path):
        say = MagicMock()
        _handle_watchlist(["add", "Niebler"], self._wl(tmp_path), say, {})
        assert "Added" in say.call_args[1]["text"]

    def test_add_existing_author(self, tmp_path):
        wl = self._wl(tmp_path)
        wl.add_author("Niebler")
        say = MagicMock()
        _handle_watchlist(["add", "Niebler"], wl, say, {})
        assert "already" in say.call_args[1]["text"]

    def test_add_multi_word_name(self, tmp_path):
        say = MagicMock()
        _handle_watchlist(["add", "Eric", "Niebler"], self._wl(tmp_path), say, {})
        assert "Added" in say.call_args[1]["text"]

    def test_remove_existing_author(self, tmp_path):
        wl = self._wl(tmp_path)
        wl.add_author("Niebler")
        say = MagicMock()
        _handle_watchlist(["remove", "Niebler"], wl, say, {})
        assert "Removed" in say.call_args[1]["text"]

    def test_remove_nonexistent_author(self, tmp_path):
        say = MagicMock()
        _handle_watchlist(["remove", "Nobody"], self._wl(tmp_path), say, {})
        assert "not on the watchlist" in say.call_args[1]["text"]

    def test_list_shows_authors(self, tmp_path):
        wl = self._wl(tmp_path)
        wl.add_author("Niebler")
        say = MagicMock()
        _handle_watchlist(["list"], wl, say, {})
        assert "niebler" in say.call_args[1]["text"]

    def test_no_args_shows_list(self, tmp_path):
        wl = self._wl(tmp_path)
        wl.add_author("Stroustrup")
        say = MagicMock()
        _handle_watchlist([], wl, say, {})
        assert "stroustrup" in say.call_args[1]["text"]

    def test_add_without_name(self, tmp_path):
        say = MagicMock()
        _handle_watchlist(["add"], self._wl(tmp_path), say, {})
        assert "Usage" in say.call_args[1]["text"]

    def test_invalid_action(self, tmp_path):
        say = MagicMock()
        _handle_watchlist(["bogus", "name"], self._wl(tmp_path), say, {})
        assert "Usage" in say.call_args[1]["text"]

    def test_reply_opts_forwarded(self, tmp_path):
        say = MagicMock()
        _handle_watchlist(["list"], self._wl(tmp_path), say, {"thread_ts": "t1"})
        assert say.call_args[1]["thread_ts"] == "t1"


# ── _show_watchlist ───────────────────────────────────────────────────────────

class TestShowWatchlist:
    def test_empty_watchlist(self, tmp_path):
        say = MagicMock()
        _show_watchlist(Watchlist(tmp_path / "wl.json"), say, {})
        assert "empty" in say.call_args[1]["text"].lower()

    def test_non_empty_watchlist(self, tmp_path):
        wl = Watchlist(tmp_path / "wl.json")
        wl.add_author("Baker")
        say = MagicMock()
        _show_watchlist(wl, say, {})
        assert "baker" in say.call_args[1]["text"]


# ── _handle_status ────────────────────────────────────────────────────────────

class TestHandleStatus:
    def test_status_never_polled(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            _handle_status(state, lambda: 42, say, {})
        text = say.call_args[1]["text"]
        assert "42" in text and "never" in text

    def test_status_after_poll(self, tmp_path):
        state = ProbeState(tmp_path / "state.json")
        state.touch_poll()
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            _handle_status(state, lambda: 100, say, {})
        text = say.call_args[1]["text"]
        assert "100" in text and "never" not in text


# ── register_handlers ─────────────────────────────────────────────────────────

class TestRegisterHandlers:
    def _setup(self, tmp_path):
        app = MagicMock()
        registered: dict = {}

        def capture_event(name):
            def decorator(fn):
                registered[name] = fn
                return fn
            return decorator

        app.event.side_effect = capture_event
        wl = Watchlist(tmp_path / "wl.json")
        state = ProbeState(tmp_path / "state.json")
        register_handlers(app, wl, state, lambda: 99)
        return registered, wl, state

    def test_app_mention_status(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            registered["app_mention"](
                event={"text": "<@U1> status", "ts": "1"},
                context={"bot_user_id": "U1"},
                say=say,
            )
        assert "Status" in say.call_args[1]["text"]

    def test_app_mention_empty_text(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        registered["app_mention"](
            event={"text": "", "ts": "1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_app_mention_no_bot_id_in_text(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            registered["app_mention"](
                event={"text": "status", "ts": "1"},
                context={"bot_user_id": ""},
                say=say,
            )
        say.assert_called_once()

    def test_message_dm_dispatches(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            registered["message"](
                event={"text": "status", "channel_type": "im", "ts": "1"},
                context={"bot_user_id": "U1"},
                say=say,
            )
        say.assert_called_once()

    def test_message_dm_strips_mention(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        with patch("paperbot.bot.settings", _make_settings()):
            registered["message"](
                event={"text": "<@U1> status", "channel_type": "im", "ts": "1"},
                context={"bot_user_id": "U1"},
                say=say,
            )
        say.assert_called_once()

    def test_message_dm_empty_after_strip(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        registered["message"](
            event={"text": "<@U1>", "channel_type": "im", "ts": "1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_message_channel_with_mention_ignored(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        registered["message"](
            event={"text": "<@U1> status", "channel_type": "channel", "ts": "1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_message_subtype_ignored(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        registered["message"](
            event={"text": "status", "subtype": "message_changed", "channel_type": "im"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_message_bot_id_ignored(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        registered["message"](
            event={"text": "status", "bot_id": "B123", "channel_type": "im"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_message_empty_text_ignored(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        registered["message"](
            event={"text": "", "channel_type": "im"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_not_called()

    def test_dispatch_help(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        registered["message"](
            event={"text": "help", "channel_type": "im", "ts": "1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        assert "Commands" in say.call_args[1]["text"]

    def test_dispatch_unknown_command(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        registered["message"](
            event={"text": "foobar", "channel_type": "im", "ts": "1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        assert "Unknown" in say.call_args[1]["text"]

    def test_dispatch_watchlist_command(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        registered["message"](
            event={"text": "watchlist list", "channel_type": "im", "ts": "1"},
            context={"bot_user_id": "U1"},
            say=say,
        )
        say.assert_called_once()
        assert "empty" in say.call_args[1]["text"].lower()

    def test_dispatch_empty_text(self, tmp_path):
        registered, _, _ = self._setup(tmp_path)
        say = MagicMock()
        registered["message"](
            event={"text": "   ", "channel_type": "im", "ts": "1"},
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
