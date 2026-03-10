from __future__ import annotations

import logging
from datetime import datetime

from slack_bolt import App

from .config import settings
from .monitor import PollResult, Watchlist
from .storage import ProbeState

log = logging.getLogger(__name__)


def create_app() -> App:
    return App(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )


SLACK_MAX_TEXT = 3000


def notify_channel(app: App, result: PollResult, watchlist: Watchlist | None = None) -> None:
    channel = settings.notification_channel
    if not channel:
        return

    watched_authors = watchlist.authors if watchlist else []
    lines: list[str] = []

    if settings.notify_on_watchlist_author and result.watchlist_matches:
        lines.append("*:rotating_light: Watched author matches (from index):*")
        for paper in result.watchlist_matches:
            lines.append(f"• *{paper.id}* — {paper.title} (by {paper.author})")

    if settings.notify_on_watchlist_author and result.probe_watchlist_hits:
        lines.append("*:rotating_light: Watched author matches (from probe):*")
        for hit in result.probe_watchlist_hits:
            matched = [a for a in watched_authors if a in hit.front_text.lower()] if hit.front_text else []
            names = ", ".join(matched) if matched else "watchlist author"
            lines.append(f"• `{hit.prefix}{hit.number:04d}R{hit.revision}{hit.extension}` — mentions *{names}* — {hit.url}")

    probe_lines: list[str] = []
    for hit in result.probe_hits:
        if hit in (result.probe_watchlist_hits or []):
            continue
        label = {"A": "Watched paper", "B": "Frontier", "C": "D-paper draft"}.get(hit.tier, "Probe hit")
        should_notify = (
            (hit.tier == "A" and settings.notify_on_watchlist_paper)
            or (hit.tier == "B" and settings.notify_on_frontier_hit)
            or (hit.tier == "C" and settings.notify_on_tier_c_hit)
        )
        if should_notify:
            probe_lines.append(f"• [{label}] `{hit.prefix}{hit.number:04d}R{hit.revision}{hit.extension}` — {hit.url}")

    if probe_lines:
        lines.append("*:mag: Probe discoveries:*")
        lines.extend(probe_lines)

    if not lines:
        return

    for batch in _batch_lines(lines, SLACK_MAX_TEXT):
        try:
            app.client.chat_postMessage(channel=channel, text=batch)
        except Exception:
            log.exception("Failed to post notification")


def _batch_lines(lines: list[str], max_len: int) -> list[str]:
    batches: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and current_len + line_len > max_len:
            batches.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len
    if current:
        batches.append("\n".join(current))
    return batches


def register_handlers(
    app: App,
    watchlist: Watchlist,
    state: ProbeState,
    paper_count_fn,
) -> None:

    def _dispatch(text: str, say, reply_opts: dict) -> None:
        words = [w for w in text.split() if w]
        if not words:
            return
        cmd = words[0].lower()
        if cmd == "watchlist":
            _handle_watchlist(words[1:], watchlist, say, reply_opts)
        elif cmd == "status":
            _handle_status(state, paper_count_fn, say, reply_opts)
        elif cmd == "help":
            say(text="Commands: `watchlist add|remove|list [name]`, `status`", **reply_opts)
        else:
            say(text="Unknown command. Try `help` for usage.", **reply_opts)

    @app.event("app_mention")
    def handle_app_mention(event, context, say):
        text = event.get("text", "")
        if not text:
            return
        bot_id = context.get("bot_user_id", "")
        if bot_id and f"<@{bot_id}>" in text:
            text = text.split(f"<@{bot_id}>", 1)[-1].strip()
        log.debug("app_mention handler firing, ts=%s", event.get("ts"))
        _dispatch(text, say=say, reply_opts=_reply_opts(event))

    @app.event("message")
    def handle_message(event, context, say):
        if event.get("subtype") or event.get("bot_id"):
            return
        text = event.get("text", "")
        if not text:
            return
        bot_id = context.get("bot_user_id", "")
        is_dm = event.get("channel_type") == "im"

        if is_dm:
            # In DMs, strip the mention prefix if present and handle
            if bot_id and f"<@{bot_id}>" in text:
                text = text.split(f"<@{bot_id}>", 1)[-1].strip()
            if text:
                _dispatch(text, say=say, reply_opts=_reply_opts(event))
        else:
            # In channels, skip mentions (app_mention handler covers those)
            if bot_id and f"<@{bot_id}>" in text:
                return


def _reply_opts(event: dict) -> dict:
    opts: dict = {"unfurl_links": False, "unfurl_media": False}
    thread_ts = event.get("thread_ts")
    if thread_ts:
        opts["thread_ts"] = thread_ts
    return opts


def _handle_watchlist(args: list[str], watchlist: Watchlist, say, reply_opts: dict) -> None:
    if not args:
        _show_watchlist(watchlist, say, reply_opts)
        return
    action = args[0].lower()
    name = " ".join(args[1:]).strip()
    if action == "add" and name:
        if watchlist.add_author(name):
            say(text=f"Added *{name}* to the watchlist.", **reply_opts)
        else:
            say(text=f"*{name}* is already on the watchlist.", **reply_opts)
    elif action == "remove" and name:
        if watchlist.remove_author(name):
            say(text=f"Removed *{name}* from the watchlist.", **reply_opts)
        else:
            say(text=f"*{name}* was not on the watchlist.", **reply_opts)
    elif action == "list":
        _show_watchlist(watchlist, say, reply_opts)
    else:
        say(text="Usage: `watchlist add|remove|list [name]`", **reply_opts)


def _show_watchlist(watchlist: Watchlist, say, reply_opts: dict) -> None:
    authors = watchlist.authors
    if authors:
        say(text="Watched authors:\n" + "\n".join(f"• {a}" for a in authors), **reply_opts)
    else:
        say(text="Watchlist is empty. Use `watchlist add <name>` to add an author.", **reply_opts)


def _handle_status(state: ProbeState, paper_count_fn, say, reply_opts: dict) -> None:
    last = state.last_poll
    last_str = datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S") if last else "never"
    say(
        text=(
            f"*Paperbot Status*\n"
            f"• Papers loaded: {paper_count_fn():,}\n"
            f"• Last poll: {last_str}\n"
            f"• Poll interval: {settings.poll_interval_minutes} min\n"
            f"• Discovered via probe: {len(state.discovered)}\n"
            f"• ISO probing: {'enabled' if settings.enable_iso_probe else 'disabled'}"
        ),
        **reply_opts,
    )
