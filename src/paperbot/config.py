from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # -- Slack credentials --
    slack_signing_secret: str = ""
    slack_bot_token: str = ""
    port: int = 3000

    # -- Scheduling --
    poll_interval_minutes: int = 30
    enable_bulk_wg21: bool = True
    enable_bulk_openstd: bool = True
    enable_iso_probe: bool = True

    # -- Revision probing --
    probe_revision_depth: int = 3
    probe_unknown_max_rev: int = 2
    probe_prefixes: list[str] = Field(default_factory=lambda: ["D", "P"])
    probe_extensions: list[str] = Field(default_factory=lambda: [".pdf", ".html"])

    # -- Tier A: Watchlist --
    watchlist_papers: list[int] = Field(default_factory=list)
    watchlist_authors: list[str] = Field(default_factory=list)

    # -- Tier B: Frontier --
    frontier_window_above: int = 30
    frontier_window_below: int = 5
    frontier_explicit_ranges: list[dict[str, int]] = Field(default_factory=list)

    # -- Tier C: Recently active --
    tier_c_lookback_months: int = 18
    tier_c_probe_prefixes: list[str] = Field(default_factory=lambda: ["D"])
    tier_c_revision_depth: int = 1

    # -- Adaptive backoff --
    backoff_miss_threshold: int = 3
    backoff_multiplier: int = 2
    backoff_max_skip: int = 48
    backoff_reset_on_index_hit: bool = True

    # -- Tier C pruning --
    prune_inactive_months: int = 24

    # -- HTTP client --
    http_concurrency: int = 20
    http_timeout_seconds: int = 10
    http_use_http2: bool = True

    # -- Notifications --
    notification_channel: str = ""
    notify_on_watchlist_author: bool = True
    notify_on_watchlist_paper: bool = True
    notify_on_frontier_hit: bool = True
    notify_on_tier_c_hit: bool = True

    # -- Storage --
    data_dir: Path = Path("./data")
    cache_ttl_hours: int = 1


settings = Settings()
