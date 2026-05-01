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
    health_port: int = 8080

    # -- Scheduling --
    poll_interval_minutes: int = 30
    # Minimum seconds to sleep after an overrun cycle (poll took longer than
    # poll_interval_minutes).  Acts as a short cooldown before the next cycle.
    poll_overrun_cooldown_seconds: int = 300   # 5 min
    enable_bulk_wg21: bool = True
    enable_bulk_openstd: bool = True
    enable_iso_probe: bool = True

    # -- Paper prefixes / extensions (globals used for gap/unknown numbers) --
    probe_prefixes: list[str] = Field(default_factory=lambda: ["D", "P"])
    probe_extensions: list[str] = Field(default_factory=lambda: [".pdf", ".html"])

    # -- Database --
    database_url: str = ""

    # -- Frontier (Tier B equivalent) --
    frontier_window_above: int = 60
    frontier_window_below: int = 30
    frontier_explicit_ranges: list[dict[str, int]] = Field(default_factory=list)
    # Max gap between consecutive P-numbers before a number is treated as an
    # outlier (e.g. a pre-assigned planning doc at P5000 while work is at P4032).
    frontier_gap_threshold: int = 50

    # -- Hot probing (every poll cycle) --
    # Papers with a date within this window are probed every cycle.
    hot_lookback_months: int = 6
    # How many revisions ahead of the known latest to probe for hot papers.
    hot_revision_depth: int = 2

    # -- Cold probing (full coverage, distributed over N cycles ≈ once/day) --
    # How many revisions ahead of the known latest to probe for cold papers.
    cold_revision_depth: int = 1
    # Distribute the cold pool over this many cycles (48 × 30 min = 24 h).
    cold_cycle_divisor: int = 48

    # -- Gap / unknown numbers (no index entry) --
    # Probe R0 through this revision for numbers not in the index at all.
    gap_max_rev: int = 1

    # -- Timestamp-based alerting --
    # Only notify for probe hits where the server's Last-Modified header is
    # within this many hours of now.  Falls back to "alert" when the header
    # is absent (first-ever discovery of an untracked file).
    alert_modified_hours: int = 24

    # -- HTTP client --
    http_concurrency: int = 20
    http_timeout_seconds: int = 10
    http_use_http2: bool = True

    # -- Notifications --
    notification_channel: str = ""
    notify_on_frontier_hit: bool = True
    notify_on_any_draft: bool = True
    # Alert when a D-paper we previously probed appears in the wg21.link index
    # as its published P counterpart (D1234R1 → P1234R1).
    notify_on_dp_transition: bool = True

    # -- Storage --
    data_dir: Path = Path("./data")
    cache_ttl_hours: int = 1

    # -- Logging --
    # Console log level.  The rotating file (data_dir/paperscout.log) always
    # captures DEBUG so nothing is lost for post-hoc analysis.
    log_level: str = "INFO"
    # Days of log files to keep (one file per day).
    log_retention_days: int = 7


settings = Settings()
