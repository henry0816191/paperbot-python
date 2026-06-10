"""Environment-backed runtime configuration (see ``settings`` singleton)."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigurationError


class Settings(BaseSettings):
    """Application settings loaded from environment and optional ``.env``."""

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
    # Empty string means all interfaces (0.0.0.0); Docker Compose sets HEALTH_BIND_HOST=0.0.0.0.
    health_bind_host: str = "127.0.0.1"

    # -- Scheduling --
    poll_interval_minutes: int = 30
    # Minimum seconds to sleep after an overrun cycle (poll took longer than
    # poll_interval_minutes).  Acts as a short cooldown before the next cycle.
    poll_overrun_cooldown_seconds: int = Field(default=300, ge=1)  # 5 min
    enable_bulk_wg21: bool = True
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
    # Dedicated timeout for wg21.link/index.json (independent of ISO probe HEAD timeouts).
    wg21_index_timeout_s: float = Field(default=30.0, ge=0.1)

    # -- Notifications --
    notification_channel: str = ""
    # Slack channel ID for ops alerts (stale poll). Empty = disabled.
    ops_alert_channel: str = ""
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

    # -- Message queue (Slack outbound) --
    mq_max_retries: int = Field(default=8, ge=1)
    mq_circuit_breaker_threshold: int = Field(default=5, ge=1)
    mq_circuit_breaker_cooldown_seconds: int = Field(default=60, ge=1)
    mq_max_size: int = Field(default=1000, ge=1)

    # -- Graceful shutdown --
    shutdown_mq_drain_timeout_seconds: float = Field(default=30.0, ge=0.1)
    shutdown_thread_join_timeout_seconds: float = Field(default=5.0, ge=0.1)
    # Set to the container orchestrator's stop/grace period (seconds).
    # When non-zero, a startup warning is emitted if the combined shutdown budget
    # (mq_drain + 2 × thread_join) meets or exceeds this value.
    stop_grace_period_seconds: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _require_slack_credentials_unless_testing(self) -> Settings:
        """Slack tokens must be set for real runs; pytest sets ``_PAPERSCOUT_TESTING=1``."""
        if os.environ.get("_PAPERSCOUT_TESTING") == "1":
            return self
        if (
            not (self.slack_bot_token or "").strip()
            or not (self.slack_signing_secret or "").strip()
        ):
            raise ConfigurationError(
                "Slack is not configured: SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET must be "
                "set to non-empty values (see README / deployment docs)."
            )
        return self


settings = Settings()
