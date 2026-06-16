"""Entry point: python -m paperscout"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import settings
from .db import init_db, init_pool
from .health import start_health_server
from .monitor import Scheduler
from .scout import (
    MessageQueue,
    create_app,
    enqueue_startup_status,
    notify_channel,
    notify_users,
    register_handlers,
)
from .shutdown import shutdown_services
from .sources import ISOProber, OpenStdSource, WG21Index
from .storage import ProbeState, UserWatchlist

log = logging.getLogger("paperscout")

# MessageQueue keys allowed in /health extras (must not overlap scheduler.health_snapshot()).
_MQ_HEALTH_FIELD_NAMES = frozenset(
    {
        "mq_depth",
        "mq_max_size",
        "mq_utilization",
        "mq_circuit_state",
    }
)


def _mq_health_fields(mq: MessageQueue) -> dict:
    """MQ metrics for /health; from health_fields() when present, else depth only."""
    if hasattr(mq, "health_fields"):
        try:
            raw = mq.health_fields()
        except Exception as exc:
            log.warning(
                "health: mq.health_fields() failed for %s id=%s: %s",
                type(mq).__name__,
                id(mq),
                exc,
                exc_info=True,
            )
            try:
                return {"mq_depth": mq.depth()}
            except Exception:
                log.warning(
                    "health: mq.depth() fallback failed; omitting MQ fields",
                    exc_info=True,
                )
                return {}
        if isinstance(raw, dict):
            return raw
        log.warning("health: mq.health_fields() returned non-dict, using mq_depth only")
    try:
        return {"mq_depth": mq.depth()}
    except Exception:
        log.warning("health: mq.depth() failed; omitting MQ fields", exc_info=True)
        return {}


def _merge_extra_health_fields(
    scheduler_snap: dict,
    mq_extra: dict,
    db_pool: dict,
) -> dict:
    """Merge health JSON with scheduler winning on key conflicts."""
    scheduler_keys = set(scheduler_snap)
    mq_filtered: dict = {}
    for key, value in mq_extra.items():
        if key in _MQ_HEALTH_FIELD_NAMES:
            if key in scheduler_keys:
                log.debug(
                    "health: mq_extra key %r conflicts with scheduler snapshot; scheduler wins",
                    key,
                )
            else:
                mq_filtered[key] = value
        elif key in scheduler_keys:
            log.debug(
                "health: mq_extra key %r not allow-listed; scheduler snapshot kept",
                key,
            )
        else:
            log.debug("health: mq_extra key %r not allow-listed, dropping", key)
    return {**scheduler_snap, **mq_filtered, "db_pool": db_pool}


def _setup_logging(data_dir: Path, console_level: str = "INFO", retention_days: int = 7) -> None:
    """Console + daily rotating file logging; third-party loggers capped at WARNING."""
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

    for lib in ("httpx", "httpcore", "slack_bolt", "slack_sdk", "urllib3", "psycopg2"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def _register_shutdown_signals(
    loop: asyncio.AbstractEventLoop,
    shutdown_event: asyncio.Event,
    shutdown_reason: list[str | None],
) -> None:
    """Register SIGTERM/SIGINT handlers that set *shutdown_event*."""

    def _on_signal(signame: str) -> None:
        """Record the first shutdown signal and wake the scheduler."""
        if shutdown_reason[0] is None:
            shutdown_reason[0] = signame
        shutdown_event.set()

    for sig, name in ((signal.SIGTERM, "SIGTERM"), (signal.SIGINT, "SIGINT")):
        try:
            loop.add_signal_handler(sig, lambda n=name: _on_signal(n))
        except NotImplementedError:
            signal.signal(sig, lambda *_a, n=name: _on_signal(n))


async def _async_main() -> None:
    """Start DB, Slack app, health server, and the polling scheduler."""
    shutdown_event = asyncio.Event()
    shutdown_reason: list[str | None] = [None]
    health_server = None
    bolt_thread = None
    mq = None
    app = None

    _register_shutdown_signals(asyncio.get_running_loop(), shutdown_event, shutdown_reason)

    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    _setup_logging(
        data_dir,
        console_level=settings.log_level,
        retention_days=settings.log_retention_days,
    )

    log.info(
        "=== Paperscout starting  port=%d  poll=%dmin  data=%s  log=%s ===",
        settings.port,
        settings.poll_interval_minutes,
        data_dir,
        data_dir / "paperscout.log",
    )
    log.info(
        "Settings: hot_lookback=%dmo  hot_depth=%d  cold_divisor=%d  "
        "alert_hours=%d  gap_max_rev=%d  frontier_gap=%d",
        settings.hot_lookback_months,
        settings.hot_revision_depth,
        settings.cold_cycle_divisor,
        settings.alert_modified_hours,
        settings.gap_max_rev,
        settings.frontier_gap_threshold,
    )

    _shutdown_budget = (
        settings.shutdown_mq_drain_timeout_seconds
        + 2 * settings.shutdown_thread_join_timeout_seconds
    )
    log.info(
        "Shutdown budget: %.0fs (mq_drain=%.0f + 2×thread_join=%.0f)",
        _shutdown_budget,
        settings.shutdown_mq_drain_timeout_seconds,
        settings.shutdown_thread_join_timeout_seconds,
    )
    if (
        settings.stop_grace_period_seconds > 0
        and _shutdown_budget >= settings.stop_grace_period_seconds
    ):
        log.warning(
            "Shutdown budget %.0fs ≥ stop_grace_period %.0fs — increase "
            "STOP_GRACE_PERIOD_SECONDS or reduce SHUTDOWN_*_TIMEOUT_SECONDS",
            _shutdown_budget,
            settings.stop_grace_period_seconds,
        )

    if not settings.database_url:
        log.error("DATABASE_URL is not set — cannot start")
        sys.exit(1)

    launch_time = datetime.now(timezone.utc)

    pool = init_pool(settings.database_url)
    init_db(pool)

    state = ProbeState(pool)
    user_watchlist = UserWatchlist(pool)
    index = WG21Index(pool, cfg=settings)
    prober = ISOProber(index, state, user_watchlist)
    sources: list = [index, prober]
    if settings.enable_open_std:
        sources.append(OpenStdSource())
    app = create_app()
    mq = MessageQueue(app)
    mq.start()

    def paper_count_fn() -> int:
        return len(index.papers)

    def _on_poll_result(result):
        notify_channel(app, result, mq)
        notify_users(app, result, mq)

    def _ops_alert(msg: str) -> None:
        if settings.ops_alert_channel:
            mq.enqueue(
                settings.ops_alert_channel,
                f":rotating_light: PaperScout alert: {msg}",
            )

    def _pool_status(p) -> dict:
        """Best-effort pool stats (psycopg2 ThreadedConnectionPool uses private attrs)."""
        status: dict = {"max": getattr(p, "maxconn", None)}
        try:
            status["in_use"] = len(p._used)
            status["available"] = len(p._pool)
        except AttributeError:
            status["in_use"] = None
            status["available"] = None
        return status

    scheduler = Scheduler(
        sources=sources,
        user_watchlist=user_watchlist,
        state=state,
        cfg=settings,
        notify_callback=_on_poll_result,
        ops_alert_fn=_ops_alert,
    )

    def _extra_health_fields() -> dict:
        return _merge_extra_health_fields(
            scheduler.health_snapshot(),
            _mq_health_fields(mq),
            _pool_status(pool),
        )

    try:
        register_handlers(app, user_watchlist, state, paper_count_fn, launch_time)

        health_server = start_health_server(
            settings.health_port,
            launch_time,
            state,
            paper_count_fn,
            bind_host=settings.health_bind_host,
            extra_fields_fn=_extra_health_fields,
        )
        log.info("Starting Slack Bolt app on port %d", settings.port)
        bolt_thread = threading.Thread(
            target=app.start,
            kwargs={"port": settings.port},
            daemon=True,
            name="bolt",
        )
        bolt_thread.start()

        enqueue_startup_status(mq, state, paper_count_fn)

        await scheduler.run_forever(shutdown_event)
    finally:
        shutdown_services(
            reason=shutdown_reason[0] or "unknown",
            mq=mq,
            health_server=health_server,
            health_thread=(
                getattr(health_server, "_paperscout_thread", None) if health_server else None
            ),
            app=app,
            bolt_thread=bolt_thread,
            mq_drain_timeout=settings.shutdown_mq_drain_timeout_seconds,
            thread_join_timeout=settings.shutdown_thread_join_timeout_seconds,
        )


def main() -> None:
    """CLI entry: run ``_async_main`` until interrupt."""
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        log.info("=== Paperscout shutting down (KeyboardInterrupt) ===")
    sys.exit(0)


if __name__ == "__main__":
    main()
