from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone

from slack_bolt import App
from slack_sdk.errors import SlackApiError

from .config import settings
from .models import Paper
from .monitor import DPTransition, PerUserMatches, PollResult
from .sources import ProbeHit
from .storage import ProbeState, UserWatchlist

log = logging.getLogger(__name__)


def create_app() -> App:
    return App(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )


SLACK_MAX_TEXT = 3000


# ── Message Queue ─────────────────────────────────────────────────────────────

class MessageQueue:
    """Thread-safe, rate-limited Slack ``chat.postMessage`` queue.

    Maintains a 1-message-per-second-per-channel limit and honours the
    ``Retry-After`` header on HTTP 429 responses.  All channel and DM posts
    go through this queue so the polling loop is never blocked by Slack I/O.
    """

    def __init__(self, app: App):
        self._app = app
        self._q: queue.Queue[tuple[str, str, dict]] = queue.Queue()
        # Maps channel → Unix timestamp of the last successful send.
        self._last_send: dict[str, float] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="mq-sender")
        self._thread.start()
        log.info("MessageQueue  started")

    def enqueue(self, channel: str, text: str, **kwargs) -> None:
        self._q.put((channel, text, kwargs))

    def _run(self) -> None:
        while True:
            try:
                channel, text, kwargs = self._q.get(timeout=1)
            except queue.Empty:
                continue

            self._throttle(channel)
            self._send_with_retry(channel, text, kwargs)
            self._q.task_done()

    def _throttle(self, channel: str) -> None:
        with self._lock:
            last = self._last_send.get(channel, 0.0)
        wait = 1.0 - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)

    def _send_with_retry(self, channel: str, text: str, kwargs: dict) -> None:
        while True:
            try:
                self._app.client.chat_postMessage(
                    channel=channel,
                    text=text,
                    unfurl_links=False,
                    unfurl_media=False,
                    **kwargs,
                )
                with self._lock:
                    self._last_send[channel] = time.monotonic()
                return
            except SlackApiError as exc:
                if exc.response.status_code == 429:
                    retry_after = int(exc.response.headers.get("Retry-After", "5"))
                    log.warning(
                        "MQ  429 rate-limited  channel=%s  retry_after=%ds",
                        channel, retry_after,
                    )
                    time.sleep(retry_after)
                    # Re-throttle per-channel timer after sleeping
                    with self._lock:
                        self._last_send[channel] = time.monotonic()
                else:
                    log.exception("MQ  send-fail  channel=%s", channel)
                    return
            except Exception:
                log.exception("MQ  send-fail  channel=%s", channel)
                return


# ── Helpers ───────────────────────────────────────────────────────────────────

def _paper_link(paper: Paper) -> str:
    url = paper.url or paper.long_link
    if not url:
        url = f"https://wg21.link/{paper.id}"
    return f"<{url}|{paper.id}>"


def _hit_label(hit_url: str, prefix: str, number: int, revision: int, ext: str) -> str:
    name = f"{prefix}{number:04d}R{revision}{ext}"
    return f"<{hit_url}|{name}>"


def _fmt_lm(lm: datetime | None) -> str:
    if lm is None:
        return "modified: unknown"
    now = datetime.now(timezone.utc)
    delta = now - lm
    if delta.total_seconds() < 3600:
        minutes = int(delta.total_seconds() / 60)
        return f"modified {minutes}m ago"
    if delta.days == 0:
        hours = int(delta.total_seconds() / 3600)
        return f"modified {hours}h ago"
    return f"modified {lm.strftime('%Y-%m-%d')}"


# ── Channel notification ──────────────────────────────────────────────────────

def notify_channel(app: App, result: PollResult, mq: MessageQueue) -> None:
    """Post batch/non-watchlist events to the configured notification channel."""
    channel = settings.notification_channel
    if not channel:
        return

    lines: list[str] = []

    # D→P transitions (all in one batch — watchlist-related ones also go to DMs)
    if settings.notify_on_dp_transition and result.dp_transitions:
        lines.append(f"*:books: {len(result.dp_transitions)} draft(s) now published:*")
        for tr in result.dp_transitions:
            p_link = _paper_link(tr.paper)
            d_link = f"<{tr.draft_url}|draft>"
            disc_str = (
                datetime.fromtimestamp(tr.discovered_at, tz=timezone.utc).strftime("%Y-%m-%d")
                if tr.discovered_at else "?"
            )
            lm_str = _fmt_lm(
                datetime.fromtimestamp(tr.last_modified, tz=timezone.utc)
                if tr.last_modified else None
            )
            lines.append(
                f"• {p_link} — {tr.paper.title}"
                f" (by {tr.paper.author}) — {d_link}"
                f" (draft seen {disc_str}, {lm_str})"
            )

    # Frontier probe hits
    frontier_hits = [h for h in result.probe_hits if h.tier == "frontier"]
    other_hits    = [h for h in result.probe_hits if h.tier != "frontier"]

    if settings.notify_on_frontier_hit and frontier_hits:
        lines.append(f"*:mag: {len(frontier_hits)} new frontier draft(s):*")
        for hit in frontier_hits:
            h_link = _hit_label(hit.url, hit.prefix, hit.number, hit.revision, hit.extension)
            lm = _fmt_lm(hit.last_modified)
            lines.append(f"• {h_link} — {lm}")

    if settings.notify_on_any_draft and other_hits:
        lines.append(f"*:mag: {len(other_hits)} new draft(s) discovered:*")
        for hit in other_hits:
            h_link = _hit_label(hit.url, hit.prefix, hit.number, hit.revision, hit.extension)
            lm = _fmt_lm(hit.last_modified)
            lines.append(f"• {h_link} — {lm}")

    if not lines:
        return

    batches = _batch_lines(lines, SLACK_MAX_TEXT)
    log.info(
        "NOTIFY  channel=%s  messages=%d  dp=%d  frontier=%d  other=%d",
        channel, len(batches),
        len(result.dp_transitions), len(frontier_hits), len(other_hits),
    )
    for batch in batches:
        mq.enqueue(channel, batch)


# ── Per-user DM notifications ─────────────────────────────────────────────────

def notify_users(app: App, result: PollResult, mq: MessageQueue) -> None:
    """Send DMs to users whose watchlist matched new papers or probe hits."""
    if not result.per_user_matches:
        return

    for user_id, matches in result.per_user_matches.items():
        lines: list[str] = []

        if matches.papers:
            lines.append("*:rotating_light: Papers matching your watchlist:*")
            for paper, reason in matches.papers:
                p_link = _paper_link(paper)
                tag = f"[{reason} match]"
                lines.append(f"• {p_link} — {paper.title} (by *{paper.author}*) {tag}")

        if matches.probe_hits:
            lines.append("*:rotating_light: New drafts matching your watchlist:*")
            for hit, reason in matches.probe_hits:
                h_link = _hit_label(hit.url, hit.prefix, hit.number, hit.revision, hit.extension)
                lm = _fmt_lm(hit.last_modified)
                tag = f"[{reason} match]"
                lines.append(f"• {h_link} — {lm} {tag}")

        if not lines:
            continue

        batches = _batch_lines(lines, SLACK_MAX_TEXT)
        log.info(
            "NOTIFY-USER  user=%s  messages=%d  papers=%d  hits=%d",
            user_id, len(batches), len(matches.papers), len(matches.probe_hits),
        )
        for batch in batches:
            mq.enqueue(user_id, batch)


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


# ── Command handlers ──────────────────────────────────────────────────────────

def register_handlers(
    app: App,
    user_watchlist: UserWatchlist,
    state: ProbeState,
    paper_count_fn,
    launch_time: datetime | None = None,
) -> None:

    def _dispatch(text: str, user_id: str, channel_type: str, say, reply_opts: dict) -> None:
        words = [w for w in text.split() if w]
        if not words:
            return
        cmd = words[0].lower()
        if cmd == "watchlist":
            _route_watchlist(words[1:], user_id, channel_type, say, reply_opts)
        elif cmd == "status":
            _handle_status(state, paper_count_fn, say, reply_opts)
        elif cmd == "version":
            _handle_version(say, reply_opts)
        elif cmd == "uptime":
            _handle_uptime(launch_time, say, reply_opts)
        elif cmd == "help":
            say(
                text=(
                    "Commands:\n"
                    "• `watchlist add|remove|list [name-or-paper-number]` — "
                    "manage your personal watchlist (DM only)\n"
                    "• `status` — show scout status\n"
                    "• `version` — show scout version\n"
                    "• `uptime` — show how long the scout has been running\n"
                    "• `help` — this message"
                ),
                **reply_opts,
            )
        else:
            say(text="Unknown command. Try `help` for usage.", **reply_opts)

    def _route_watchlist(
        args: list[str],
        user_id: str,
        channel_type: str,
        say,
        reply_opts: dict,
    ) -> None:
        if channel_type == "im":
            _handle_watchlist(args, user_id, user_watchlist, say, reply_opts)
        elif channel_type == "mpim":
            say(
                text="Watchlist commands only work in a 1:1 DM with me.",
                **reply_opts,
            )
        # For public/private channels: silently ignore

    @app.event("app_mention")
    def handle_app_mention(event, context, say):
        text = event.get("text", "")
        if not text:
            return
        bot_id = context.get("bot_user_id", "")
        if bot_id and f"<@{bot_id}>" in text:
            text = text.split(f"<@{bot_id}>", 1)[-1].strip()
        if not text:
            return
        user_id = event.get("user", "")
        channel_type = event.get("channel_type", "channel")
        log.debug("app_mention handler firing, ts=%s", event.get("ts"))
        _dispatch(text, user_id, channel_type, say=say, reply_opts=_reply_opts(event))

    @app.event("message")
    def handle_message(event, context, say):
        if event.get("subtype") or event.get("bot_id"):
            return
        text = event.get("text", "")
        if not text:
            return
        bot_id = context.get("bot_user_id", "")
        channel_type = event.get("channel_type", "")
        user_id = event.get("user", "")

        if channel_type == "im":
            # Strip scout mention if present (e.g. user typed @scout watchlist ...)
            if bot_id and f"<@{bot_id}>" in text:
                text = text.split(f"<@{bot_id}>", 1)[-1].strip()
            if text:
                _dispatch(text, user_id, channel_type, say=say, reply_opts=_reply_opts(event))

        elif channel_type == "mpim":
            # Only respond if the scout is mentioned
            if bot_id and f"<@{bot_id}>" in text:
                text = text.split(f"<@{bot_id}>", 1)[-1].strip()
                if text:
                    _dispatch(text, user_id, channel_type, say=say, reply_opts=_reply_opts(event))

        else:
            # Public/private channels: handled by app_mention; skip plain messages
            if bot_id and f"<@{bot_id}>" in text:
                return


def _reply_opts(event: dict) -> dict:
    opts: dict = {"unfurl_links": False, "unfurl_media": False}
    thread_ts = event.get("thread_ts")
    if thread_ts:
        opts["thread_ts"] = thread_ts
    return opts


def _handle_watchlist(
    args: list[str],
    user_id: str,
    user_watchlist: UserWatchlist,
    say,
    reply_opts: dict,
) -> None:
    if not args:
        _show_watchlist(user_id, user_watchlist, say, reply_opts)
        return
    action = args[0].lower()
    raw = " ".join(args[1:]).strip()

    if action == "add" and raw:
        if user_watchlist.add(user_id, raw):
            etype = "paper number" if raw.strip().isdigit() else "author"
            say(text=f"Added *{raw}* ({etype}) to your watchlist.", **reply_opts)
        else:
            say(text=f"*{raw}* is already on your watchlist.", **reply_opts)
    elif action == "remove" and raw:
        if user_watchlist.remove(user_id, raw):
            say(text=f"Removed *{raw}* from your watchlist.", **reply_opts)
        else:
            say(text=f"*{raw}* was not on your watchlist.", **reply_opts)
    elif action == "list":
        _show_watchlist(user_id, user_watchlist, say, reply_opts)
    else:
        say(
            text="Usage: `watchlist add|remove|list [name-or-paper-number]`",
            **reply_opts,
        )


def _show_watchlist(
    user_id: str,
    user_watchlist: UserWatchlist,
    say,
    reply_opts: dict,
) -> None:
    entries = user_watchlist.list_entries(user_id)
    if entries:
        lines = [f"• {entry} ({etype})" for entry, etype in entries]
        say(
            text="Your watchlist:\n" + "\n".join(lines),
            **reply_opts,
        )
    else:
        say(
            text=(
                "Your watchlist is empty.\n"
                "Use `watchlist add <author-name>` or `watchlist add <paper-number>` to add entries."
            ),
            **reply_opts,
        )


def _handle_status(state: ProbeState, paper_count_fn, say, reply_opts: dict) -> None:
    from datetime import datetime as _dt
    last = state.last_poll
    last_str = _dt.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S") if last else "never"
    say(
        text=(
            f"*Paperscout Status*\n"
            f"• Papers loaded: {paper_count_fn():,}\n"
            f"• Last poll: {last_str}\n"
            f"• Poll interval: {settings.poll_interval_minutes} min\n"
            f"• Discovered via probe: {len(state.discovered)}\n"
            f"• ISO probing: {'enabled' if settings.enable_iso_probe else 'disabled'}\n"
            f"• Alert window: {settings.alert_modified_hours}h\n"
            f"• Cold cycle: 1/{settings.cold_cycle_divisor}"
        ),
        **reply_opts,
    )


def _handle_version(say, reply_opts: dict) -> None:
    from . import __version__
    say(text=f"Paperscout v{__version__}", **reply_opts)


def _format_uptime(delta) -> str:
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def _handle_uptime(launch_time: datetime | None, say, reply_opts: dict) -> None:
    if launch_time is None:
        say(text="Uptime information is not available.", **reply_opts)
        return
    now = datetime.now(timezone.utc)
    delta = now - launch_time
    started_str = launch_time.strftime("%Y-%m-%d %H:%M:%S UTC")
    say(
        text=f"Paperscout started {_format_uptime(delta)} ago ({started_str})",
        **reply_opts,
    )
