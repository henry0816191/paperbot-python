"""Slack Bolt app: outbound notifications, commands, and message queue."""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from slack_bolt import App
from slack_sdk.errors import SlackApiError

from .config import settings
from .models import Paper, Tier
from .monitor import PollResult
from .storage import ProbeState, UserWatchlist

log = logging.getLogger(__name__)


def create_app() -> App:
    """Construct a Slack Bolt ``App`` using configured bot token and signing secret."""
    return App(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )


SLACK_MAX_TEXT = 3000

_MQ_SENTINEL = object()


# ── Message Queue ─────────────────────────────────────────────────────────────


def _redact_channel(channel: str) -> str:
    """Short stable token for logs (no raw Slack channel/user id)."""
    digest = hashlib.sha256(channel.encode()).hexdigest()
    return f"ch:{digest[:8]}"


def _payload_meta(text: str, kwargs: dict | None = None) -> str:
    """Log-safe payload summary (length and kwargs keys only)."""
    parts = [f"text_len={len(text)}"]
    if kwargs:
        parts.append(f"kwargs_keys={','.join(sorted(kwargs))}")
    return " ".join(parts)


def _parse_retry_after(raw: str | None) -> int:
    """Parse Slack ``Retry-After`` (seconds only); default 5 on invalid values."""
    if raw is None:
        return 5
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        log.warning("MQ  invalid-retry-after  value=%r  using=5s", raw)
        return 5


def _log_enqueue_rejected(context: str) -> None:
    """Caller-side hint when ``enqueue()`` returns False (circuit already logged in MQ)."""
    log.warning("MQ  notify-enqueue-rejected  context=%s", context)


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Per-message 429 retry cap (from settings)."""

    max_retries: int


class CircuitBreaker:
    """Consecutive-failure circuit breaker with cooldown and half-open probe."""

    def __init__(
        self,
        threshold: int,
        cooldown_seconds: int,
    ) -> None:
        self._threshold = threshold
        self._cooldown_seconds = cooldown_seconds
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        with self._lock:
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    def record_success(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._state = CircuitState.CLOSED
            self._opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive_failures += 1
            if self._state == CircuitState.HALF_OPEN:
                self._trip_locked()
                return
            if self._consecutive_failures >= self._threshold:
                self._trip_locked()

    def _trip_locked(self) -> None:
        """Trip to OPEN; caller must hold ``_lock``."""
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        log.error(
            "MQ-CIRCUIT-OPEN  failures=%d  cooldown=%ds",
            self._consecutive_failures,
            self._cooldown_seconds,
        )

    def allow_send(self) -> bool:
        """Return whether a send attempt may proceed (may transition OPEN → HALF_OPEN)."""
        with self._lock:
            if self._state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                return True
            if self._opened_at is None:
                return True
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                log.info("MQ-CIRCUIT-HALF-OPEN  probing after cooldown")
                return True
            return False


class MessageQueue:
    """Background queue for Slack posts: throttle, capped 429 retries, circuit breaker."""

    def __init__(self, app: App):
        self._app = app
        self._q: queue.Queue[Any] = queue.Queue(maxsize=settings.mq_max_size)
        self._last_send: dict[str, float] = {}
        self._lock = threading.Lock()
        self._queue_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._retry = RetryPolicy(max_retries=settings.mq_max_retries)
        self._breaker = CircuitBreaker(
            threshold=settings.mq_circuit_breaker_threshold,
            cooldown_seconds=settings.mq_circuit_breaker_cooldown_seconds,
        )
        self._warned_high_water = False
        self._stop_requested = threading.Event()
        self._drain_sent_count = 0
        self._drain_sent_lock = threading.Lock()

    def start(self) -> None:
        """Start the background sender thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="mq-sender")
        self._thread.start()
        log.info("MessageQueue  started")

    def stop(self) -> None:
        """Signal the sender thread to exit after draining queued messages."""
        if self._stop_requested.is_set():
            return
        with self._drain_sent_lock:
            self._drain_sent_count = 0
        self._stop_requested.set()
        self._put_shutdown_sentinel()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the sender thread. Return True if still alive (timed out)."""
        if self._thread is None:
            return False
        self._thread.join(timeout)
        return self._thread.is_alive()

    def drain(self, timeout: float | None = None) -> int:
        """Stop the queue and block until drained or *timeout* expires.

        Returns the number of messages successfully sent during drain.
        """
        if not self._stop_requested.is_set():
            self.stop()
        drain_timeout = (
            timeout if timeout is not None else settings.shutdown_mq_drain_timeout_seconds
        )
        self.join(drain_timeout)
        with self._drain_sent_lock:
            return self._drain_sent_count

    def _put_shutdown_sentinel(self) -> None:
        """Enqueue shutdown sentinel, bypassing circuit breaker and drop-oldest."""
        with self._queue_lock:
            try:
                self._q.put_nowait(_MQ_SENTINEL)
            except queue.Full:
                pass  # _stop_requested still lets _run() exit via queue.Empty + flag

    def depth(self) -> int:
        """Approximate number of messages waiting to send."""
        with self._queue_lock:
            return self._q.qsize()

    def health_fields(self) -> dict[str, Any]:
        """Metrics for the ``/health`` endpoint (merged by ``__main__``)."""
        d = self.depth()
        m = settings.mq_max_size
        utilization = (d / m) if m else 0.0
        utilization = min(1.0, max(0.0, utilization))
        return {
            "mq_depth": d,
            "mq_max_size": m,
            "mq_utilization": round(utilization, 4),
            "mq_circuit_state": self._breaker.state.value,
        }

    def enqueue(self, channel: str, text: str, **kwargs) -> bool:
        """Queue a ``chat.postMessage``; return False when the circuit breaker rejects."""
        if self._stop_requested.is_set():
            log.warning(
                "MQ  enqueue-rejected  shutdown  %s  %s",
                _redact_channel(channel),
                _payload_meta(text, kwargs),
            )
            return False
        if not self._breaker.allow_send():
            log.warning(
                "MQ  enqueue-rejected  circuit=open  %s  %s",
                _redact_channel(channel),
                _payload_meta(text, kwargs),
            )
            return False

        item = (channel, text, kwargs)
        max_size = settings.mq_max_size
        with self._queue_lock:
            if self._stop_requested.is_set():
                log.warning(
                    "MQ  enqueue-rejected  shutdown  %s  %s",
                    _redact_channel(channel),
                    _payload_meta(text, kwargs),
                )
                return False
            while True:
                try:
                    self._q.put_nowait(item)
                    break
                except queue.Full:
                    try:
                        dropped = self._q.get_nowait()
                    except queue.Empty:
                        # Consumer may have taken an item between Full and get_nowait; retry put.
                        continue
                    if dropped is _MQ_SENTINEL:
                        try:
                            self._q.put_nowait(_MQ_SENTINEL)
                        except queue.Full:
                            pass
                        log.warning(
                            "MQ  enqueue-rejected  shutdown  %s  %s",
                            _redact_channel(channel),
                            _payload_meta(text, kwargs),
                        )
                        return False
                    dropped_ch, dropped_text, dropped_kwargs = dropped
                    log.warning(
                        "MQ  drop-oldest  %s  %s",
                        _redact_channel(dropped_ch),
                        _payload_meta(dropped_text, dropped_kwargs),
                    )
            if max_size > 0:
                depth = self._q.qsize()
                high = 0.8 * max_size
                low = 0.7 * max_size
                if depth >= high and not self._warned_high_water:
                    log.warning(
                        "MQ  high-water  depth=%d  max=%d  utilization=%.0f%%",
                        depth,
                        max_size,
                        100.0 * depth / max_size,
                    )
                    self._warned_high_water = True
                elif depth < low:
                    self._warned_high_water = False
        return True

    def _run(self) -> None:
        """Background sender loop; exits on sentinel or when stop is requested."""
        while True:
            try:
                item = self._q.get(timeout=1)
            except queue.Empty:
                if self._stop_requested.is_set():
                    break
                continue

            if item is _MQ_SENTINEL:
                self._q.task_done()
                break

            channel, text, kwargs = item
            self._throttle(channel)
            self._send_with_retry(channel, text, kwargs)
            self._q.task_done()

    def _throttle(self, channel: str) -> None:
        with self._lock:
            last = self._last_send.get(channel, 0.0)
        wait = 1.0 - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)

    def _dead_letter(
        self,
        channel: str,
        text: str,
        *,
        reason: str,
        attempts: int = 0,
        kwargs: dict | None = None,
    ) -> None:
        log.error(
            "MQ-DEAD-LETTER  %s  reason=%s  attempts=%d  %s",
            _redact_channel(channel),
            reason,
            attempts,
            _payload_meta(text, kwargs),
        )

    def _send_with_retry(self, channel: str, text: str, kwargs: dict) -> None:
        if not self._stop_requested.is_set() and not self._breaker.allow_send():
            self._dead_letter(channel, text, reason="circuit_open", kwargs=kwargs)
            return

        max_attempts = self._retry.max_retries + 1
        for attempt in range(max_attempts):
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
                self._breaker.record_success()
                if self._stop_requested.is_set():
                    with self._drain_sent_lock:
                        self._drain_sent_count += 1
                return
            except SlackApiError as exc:
                if exc.response.status_code == 429:
                    if attempt >= self._retry.max_retries:
                        self._dead_letter(
                            channel,
                            text,
                            reason="retry_exhausted",
                            attempts=attempt + 1,
                            kwargs=kwargs,
                        )
                        self._breaker.record_failure()
                        return
                    retry_after = _parse_retry_after(
                        exc.response.headers.get("Retry-After", "5"),
                    )
                    log.warning(
                        "MQ  429 rate-limited  %s  retry_after=%ds  attempt=%d",
                        _redact_channel(channel),
                        retry_after,
                        attempt + 1,
                    )
                    time.sleep(retry_after)
                    with self._lock:
                        self._last_send[channel] = time.monotonic()
                else:
                    log.exception(
                        "MQ  send-fail  %s",
                        _redact_channel(channel),
                    )
                    self._breaker.record_failure()
                    return
            except Exception:
                log.exception(
                    "MQ  send-fail  %s",
                    _redact_channel(channel),
                )
                self._breaker.record_failure()
                return


# ── Helpers ───────────────────────────────────────────────────────────────────


def _paper_link(paper: Paper) -> str:
    """Slack mrkdwn ``<url|id>`` for *paper* (wg21.link fallback if no URL)."""
    url = paper.url or paper.long_link
    if not url:
        url = f"https://wg21.link/{paper.id}"
    return f"<{url}|{paper.id}>"


def _hit_label(hit_url: str, prefix: str, number: int, revision: int, ext: str) -> str:
    """Slack mrkdwn link for an isocpp probe hit filename."""
    name = f"{prefix}{number:04d}R{revision}{ext}"
    return f"<{hit_url}|{name}>"


def _fmt_lm(lm: datetime | None) -> str:
    """Short human-readable age string from a Last-Modified time."""
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
                if tr.discovered_at
                else "?"
            )
            lm_str = _fmt_lm(
                datetime.fromtimestamp(tr.last_modified, tz=timezone.utc)
                if tr.last_modified
                else None
            )
            lines.append(
                f"• {p_link} — {tr.paper.title}"
                f" (by {tr.paper.author}) — {d_link}"
                f" (draft seen {disc_str}, {lm_str})"
            )

    # Frontier probe hits
    frontier_hits = [h for h in result.probe_hits if h.tier == Tier.FRONTIER]
    other_hits = [h for h in result.probe_hits if h.tier != Tier.FRONTIER]

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
        channel,
        len(batches),
        len(result.dp_transitions),
        len(frontier_hits),
        len(other_hits),
    )
    for batch in batches:
        if not mq.enqueue(channel, batch):
            _log_enqueue_rejected("notify_channel")


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
                tag = f"[{reason.value} match]"
                lines.append(f"• {p_link} — {paper.title} (by *{paper.author}*) {tag}")

        if matches.probe_hits:
            lines.append("*:rotating_light: New drafts matching your watchlist:*")
            for hit, reason in matches.probe_hits:
                h_link = _hit_label(hit.url, hit.prefix, hit.number, hit.revision, hit.extension)
                lm = _fmt_lm(hit.last_modified)
                tag = f"[{reason.value} match]"
                lines.append(f"• {h_link} — {lm} {tag}")

        if not lines:
            continue

        batches = _batch_lines(lines, SLACK_MAX_TEXT)
        log.info(
            "NOTIFY-USER  user=%s  messages=%d  papers=%d  hits=%d",
            user_id,
            len(batches),
            len(matches.papers),
            len(matches.probe_hits),
        )
        for batch in batches:
            if not mq.enqueue(user_id, batch):
                _log_enqueue_rejected("notify_users")


def _batch_lines(lines: list[str], max_len: int) -> list[str]:
    """Split *lines* into Slack-sized chunks under *max_len* characters."""
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
    """Wire Slack events for mentions, DMs, watchlist, status, version, uptime."""

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
                    _dispatch(
                        text,
                        user_id,
                        channel_type,
                        say=say,
                        reply_opts=_reply_opts(event),
                    )

        else:
            # Public/private channels: handled by app_mention; skip plain messages
            if bot_id and f"<@{bot_id}>" in text:
                return


def _reply_opts(event: dict) -> dict:
    """kwargs for ``say`` including ``thread_ts`` when replying in a thread."""
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
    """Parse ``watchlist`` subcommand: add, remove, list, or usage."""
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
    """Post the user's watchlist entries or an empty-state hint."""
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


def format_status_message(state: ProbeState, paper_count_fn) -> str:
    """Mrkdwn body for the interactive ``status`` command and startup channel post."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    last = state.last_poll
    last_str = (
        _dt.fromtimestamp(last, tz=_tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if last else "never"
    )
    return (
        f"*Paperscout Status*\n"
        f"• Papers loaded: {paper_count_fn():,}\n"
        f"• Last poll: {last_str}\n"
        f"• Poll interval: {settings.poll_interval_minutes} min\n"
        f"• Discovered via probe: {len(state.get_all_discovered())}\n"
        f"• ISO probing: {'enabled' if settings.enable_iso_probe else 'disabled'}\n"
        f"• Alert window: {settings.alert_modified_hours}h\n"
        f"• Cold cycle: 1/{settings.cold_cycle_divisor}"
    )


def _handle_status(state: ProbeState, paper_count_fn, say, reply_opts: dict) -> None:
    """Post loaded paper count, last poll, probe settings."""
    say(text=format_status_message(state, paper_count_fn), **reply_opts)


def enqueue_startup_status(
    mq: MessageQueue,
    state: ProbeState,
    paper_count_fn,
) -> None:
    """Post *status* summary to ``NOTIFICATION_CHANNEL`` once at process start."""
    channel = settings.notification_channel
    if not channel:
        return
    if not mq.enqueue(channel, format_status_message(state, paper_count_fn)):
        _log_enqueue_rejected("enqueue_startup_status")


def _handle_version(say, reply_opts: dict) -> None:
    """Post package version string."""
    from . import __version__

    say(text=f"Paperscout v{__version__}", **reply_opts)


def _format_uptime(delta) -> str:
    """Compact ``Nd Nh Nm`` string for a timedelta."""
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
    """Post time since process start (from *launch_time*)."""
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
