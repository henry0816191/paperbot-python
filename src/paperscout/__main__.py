"""Entry point: python -m paperscout"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
import threading
from pathlib import Path

from datetime import datetime, timezone

from .config import settings
from .scout import MessageQueue, create_app, notify_channel, notify_users, register_handlers
from .db import init_db, init_pool
from .health import start_health_server
from .monitor import Scheduler
from .sources import ISOProber, WG21Index
from .storage import ProbeState, UserWatchlist

log = logging.getLogger("paperscout")


def _setup_logging(data_dir: Path, console_level: str = "INFO",
                   retention_days: int = 7) -> None:
    """Configure root logger with:

    • Console (stderr) — at *console_level*, for interactive monitoring.
    • Rotating file (data_dir/paperscout.log) — at *console_level*, rotated
      midnight each day, keeping *retention_days* days of history.

    Noisy third-party libraries are silenced to WARNING regardless.
    """
    data_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)-22s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.handlers.TimedRotatingFileHandler(
        filename=data_dir / "paperscout.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        utc=True,
    )
    fh.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(getattr(logging, console_level.upper(), logging.INFO))
    ch.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(fh)
    root.addHandler(ch)

    for lib in ("httpx", "httpcore", "slack_bolt", "slack_sdk",
                "apscheduler", "urllib3", "psycopg2"):
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
        "=== Paperscout starting  port=%d  poll=%dmin  data=%s  log=%s ===",
        settings.port, settings.poll_interval_minutes,
        data_dir, data_dir / "paperscout.log",
    )
    log.info(
        "Settings: hot_lookback=%dmo  hot_depth=%d  cold_divisor=%d  "
        "alert_hours=%d  gap_max_rev=%d  frontier_gap=%d",
        settings.hot_lookback_months, settings.hot_revision_depth,
        settings.cold_cycle_divisor, settings.alert_modified_hours,
        settings.gap_max_rev, settings.frontier_gap_threshold,
    )

    if not settings.database_url:
        log.error("DATABASE_URL is not set — cannot start")
        sys.exit(1)

    launch_time = datetime.now(timezone.utc)

    pool = init_pool(settings.database_url)
    init_db(pool)

    state = ProbeState(pool)
    user_watchlist = UserWatchlist(pool)
    index = WG21Index(pool)
    prober = ISOProber(index, state, user_watchlist)
    app = create_app()
    mq = MessageQueue(app)
    mq.start()

    paper_count_fn = lambda: len(index.papers)

    scheduler = Scheduler(
        index=index,
        prober=prober,
        user_watchlist=user_watchlist,
        state=state,
        notify_callback=lambda result: (
            notify_channel(app, result, mq),
            notify_users(app, result, mq),
        ),
    )

    register_handlers(app, user_watchlist, state, paper_count_fn, launch_time)

    start_health_server(settings.health_port, launch_time, state, paper_count_fn)
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
        log.info("=== Paperscout shutting down (KeyboardInterrupt) ===")
        sys.exit(0)


if __name__ == "__main__":
    main()
