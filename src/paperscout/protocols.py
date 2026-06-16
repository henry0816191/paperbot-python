"""Structural typing contracts for pluggable data sources.

Known ``source_id`` values:

- ``"wg21_index"`` — :class:`~paperscout.sources.WG21Index`
- ``"iso_probe"`` — :class:`~paperscout.sources.ISOProber`
- ``"open_std"`` — :class:`~paperscout.sources.OpenStdSource`
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Well-known source identifiers (stable across releases).
SOURCE_WG21_INDEX = "wg21_index"
SOURCE_ISO_PROBE = "iso_probe"
SOURCE_OPEN_STD = "open_std"


@runtime_checkable
class DataSource(Protocol):
    """Contract for fetch/parse/diff data sources polled by :class:`~paperscout.monitor.Scheduler`."""

    @property
    def source_id(self) -> str: ...

    async def fetch(self) -> Any:
        """Fetch the latest snapshot from this source."""
        ...

    def diff(self, previous: Any, current: Any) -> Any:
        """Compare *previous* and *current* snapshots; return source-specific diff."""
        ...
