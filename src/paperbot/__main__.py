"""Entry point: python -m paperbot"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
import threading
from pathlib import Path

from .config import settings
from .bot import create_app, notify_channel, register_handlers
from .monitor import Scheduler, Watchlist
from .sources import ISOProber, WG21Index
from .storage import ProbeState

log = logging.getLogger("paperbot")


def _setup_logging(data_dir: Path, console_level: str = "INFO",
                   retention_days: int = 7) -> None:
    """Configure root logger with:

    • Console (stderr) — at *console_level*, for interactive monitoring.
    • Rotating file (data_dir/paperbot.log) — always at DEBUG, rotated
      midnight each day, keeping *retention_days* days of history.

    Noisy third-party libraries are silenced to WARNING regardless.
    """
    data_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── File handler ─────────────────────────────────────────────────────────
    fh = logging.handlers.TimedRotatingFileHandler(
        filename=data_dir / "paperbot.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        utc=True,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # ── Console handler ───────────────────────────────────────────────────────
    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)          # let handlers decide their own cutoff
    root.addHandler(fh)
    root.addHandler(ch)

    # Silence noisy libraries
    for lib in ("httpx", "httpcore", "slack_bolt", "slack_sdk",
                "apscheduler", "urllib3"):
        logging.getLogger(lib).setLevel(logging.WARNING)


async def _async_main() -> None:
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    _setup_logging(
        data_dir,
        console_level=settings.log_level,
        retention_days=settings.log_retention_days,
    )

    log.info(
        "=== Paperbot starting  port=%d  poll=%dmin  data=%s  log=%s ===",
        settings.port, settings.poll_interval_minutes,
        data_dir, data_dir / "paperbot.log",
    )
    log.info(
        "Settings: hot_lookback=%dmo  hot_depth=%d  cold_divisor=%d  "
        "alert_hours=%d  gap_max_rev=%d  frontier_gap=%d",
        settings.hot_lookback_months, settings.hot_revision_depth,
        settings.cold_cycle_divisor, settings.alert_modified_hours,
        settings.gap_max_rev, settings.frontier_gap_threshold,
    )

    index = WG21Index(data_dir)
    state = ProbeState(data_dir / "probe_state.json")
    watchlist = Watchlist(data_dir / "watchlist.json")

    for author in settings.watchlist_authors:
        watchlist.add_author(author)

    log.info(
        "Watchlist: %d authors  %d watched papers",
        len(watchlist.authors), len(settings.watchlist_papers),
    )

    prober = ISOProber(index, state)
    app = create_app()

    scheduler = Scheduler(
        index=index, prober=prober,
        watchlist=watchlist, state=state,
        notify_callback=lambda result: notify_channel(app, result, watchlist),
    )

    register_handlers(app, watchlist, state, lambda: len(index.papers))

    log.info("Starting Slack Bolt app on port %d", settings.port)
    bolt_thread = threading.Thread(
        target=app.start, kwargs={"port": settings.port}, daemon=True,
    )
    bolt_thread.start()

    await scheduler.run_forever()


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        log.info("=== Paperbot shutting down (KeyboardInterrupt) ===")
        sys.exit(0)


if __name__ == "__main__":
    main()
