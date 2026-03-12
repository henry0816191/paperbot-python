from __future__ import annotations

import logging
from datetime import datetime, timezone

from slack_bolt import App

from .config import settings
from .models import Paper
from .monitor import DPTransition, PollResult, Watchlist
from .storage import ProbeState

log = logging.getLogger(__name__)


def create_app() -> App:
    return App(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )


SLACK_MAX_TEXT = 3000


# ── Helpers ──────────────────────────────────────────────────────────────────

def _paper_link(paper: Paper) -> str:
    """Return a Slack ``<url|id>`` hyperlink for a Paper.

    Prefers the wg21.link short URL stored in ``paper.url``, falls back to
    ``paper.long_link``, then to a synthesised ``https://wg21.link/{id}`` URL.
    """
    url = paper.url or paper.long_link
    if not url:
        url = f"https://wg21.link/{paper.id}"
    return f"<{url}|{paper.id}>"


def _hit_label(hit_url: str, prefix: str, number: int, revision: int, ext: str) -> str:
    """Return a Slack ``<url|filename>`` link for a probe hit URL."""
    name = f"{prefix}{number:04d}R{revision}{ext}"
    return f"<{hit_url}|{name}>"


def _fmt_lm(lm: datetime | None) -> str:
    """Format Last-Modified as a compact human-readable age string."""
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


# ── Notification logic ────────────────────────────────────────────────────────

def _is_watchlist_related(
    paper_number: int | None,
    author: str | None,
    watchlist_paper_nums: set[int],
    watched_authors: list[str],
) -> bool:
    if paper_number is not None and paper_number in watchlist_paper_nums:
        return True
    if author and any(a in author.lower() for a in watched_authors):
        return True
    return False


def notify_channel(app: App, result: PollResult, watchlist: Watchlist | None = None) -> None:
    channel = settings.notification_channel
    if not channel:
        return

    watched_authors = watchlist.authors if watchlist else []
    watchlist_paper_nums = set(settings.watchlist_papers)
    lines: list[str] = []

    # ── 1. Individual: new P-paper by a watched author (wg21.link index) ─────
    if settings.notify_on_watchlist_author and result.watchlist_matches:
        lines.append("*:rotating_light: Watched author — new publication:*")
        for paper in result.watchlist_matches:
            p_link = _paper_link(paper)
            lines.append(f"• {p_link} — {paper.title} (by *{paper.author}*)")

    # ── 2. Individual: watched author found in draft front-text ───────────────
    if settings.notify_on_watchlist_author and result.probe_watchlist_hits:
        lines.append("*:rotating_light: Watched author — new draft:*")
        for hit in result.probe_watchlist_hits:
            matched = [a for a in watched_authors if a in (hit.front_text or "").lower()]
            names = ", ".join(f"*{a}*" for a in matched) if matched else "*watchlist author*"
            h_link = _hit_label(hit.url, hit.prefix, hit.number, hit.revision, hit.extension)
            lm = _fmt_lm(hit.last_modified)
            lines.append(f"• {h_link} — mentions {names} — {lm}")

    # ── 3. Individual: watchlist-number probe hit ─────────────────────────────
    wl_hit_ids = {id(h) for h in result.probe_watchlist_hits}
    wl_probe   = [h for h in result.probe_hits if h.tier == "watchlist"
                  and id(h) not in wl_hit_ids]
    batch_probe = [h for h in result.probe_hits if h.tier != "watchlist"
                   and id(h) not in wl_hit_ids]

    if settings.notify_on_watchlist_paper and wl_probe:
        lines.append("*:rotating_light: Watched paper — new draft:*")
        for hit in wl_probe:
            h_link = _hit_label(hit.url, hit.prefix, hit.number, hit.revision, hit.extension)
            lm = _fmt_lm(hit.last_modified)
            lines.append(f"• {h_link} — {lm}")

    # ── 4a. Individual: D→P transition for watched paper / author ─────────────
    # ── 4b. Batch:      D→P transition for everything else ────────────────────
    dp_wl, dp_batch = [], []
    for tr in result.dp_transitions:
        if _is_watchlist_related(
            tr.paper.number, tr.paper.author,
            watchlist_paper_nums, watched_authors,
        ):
            dp_wl.append(tr)
        else:
            dp_batch.append(tr)

    if settings.notify_on_dp_transition and dp_wl:
        lines.append("*:white_check_mark: Watched paper published (D→P):*")
        for tr in dp_wl:
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
                f"• {p_link} — {tr.paper.title} (by *{tr.paper.author}*)"
                f" — {d_link} (draft seen {disc_str}, {lm_str})"
            )

    if settings.notify_on_dp_transition and dp_batch:
        lines.append(f"*:books: {len(dp_batch)} draft(s) now published:*")
        for tr in dp_batch:
            p_link = _paper_link(tr.paper)
            d_link = f"<{tr.draft_url}|draft>"
            lines.append(
                f"• {p_link} — {tr.paper.title}"
                f" (by {tr.paper.author}) — {d_link}"
            )

    # ── 5a. Batch: frontier probe hits ────────────────────────────────────────
    frontier_hits = [h for h in batch_probe if h.tier == "frontier"]
    other_hits    = [h for h in batch_probe if h.tier not in ("frontier",)]

    if settings.notify_on_frontier_hit and frontier_hits:
        lines.append(f"*:mag: {len(frontier_hits)} new frontier draft(s):*")
        for hit in frontier_hits:
            h_link = _hit_label(hit.url, hit.prefix, hit.number, hit.revision, hit.extension)
            lm = _fmt_lm(hit.last_modified)
            lines.append(f"• {h_link} — {lm}")

    # ── 5b. Batch: other probe hits (recent / cold) ───────────────────────────
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
        "NOTIFY  channel=%s  messages=%d  "
        "watchlist=%d  probe-wl=%d  dp-wl=%d  dp-batch=%d  "
        "frontier=%d  other=%d",
        channel, len(batches),
        len(result.watchlist_matches),
        len(result.probe_watchlist_hits),
        len(dp_wl), len(dp_batch),
        len(frontier_hits), len(other_hits),
    )
    for batch in batches:
        try:
            app.client.chat_postMessage(channel=channel, text=batch)
        except Exception:
            log.exception("NOTIFY-FAIL  channel=%s", channel)


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
            if bot_id and f"<@{bot_id}>" in text:
                text = text.split(f"<@{bot_id}>", 1)[-1].strip()
            if text:
                _dispatch(text, say=say, reply_opts=_reply_opts(event))
        else:
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
    from datetime import datetime as _dt
    last = state.last_poll
    last_str = _dt.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S") if last else "never"
    say(
        text=(
            f"*Paperbot Status*\n"
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
