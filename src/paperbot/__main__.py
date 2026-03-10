"""Entry point: python -m paperbot"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading

from .config import settings
from .bot import create_app, notify_channel, register_handlers
from .monitor import Scheduler, Watchlist
from .sources import ISOProber, WG21Index
from .storage import ProbeState

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("paperbot")

# Suppress per-request httpx noise (1,800+ lines per cycle)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


async def _async_main() -> None:
    data_dir = settings.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)

    index = WG21Index(data_dir)
    state = ProbeState(data_dir / "probe_state.json")
    watchlist = Watchlist(data_dir / "watchlist.json")

    for author in settings.watchlist_authors:
        watchlist.add_author(author)

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
        log.info("Shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
