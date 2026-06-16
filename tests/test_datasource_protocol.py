"""Tests for DataSource protocol conformance and scheduler extensibility."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from paperscout.models import CycleResult, CycleStatus, Paper
from paperscout.monitor import DiffResult, Scheduler
from paperscout.protocols import SOURCE_ISO_PROBE, SOURCE_WG21_INDEX, DataSource
from paperscout.sources import ISOProber, WG21Index
from paperscout.storage import ProbeState, UserWatchlist
from tests.conftest import make_test_settings


@dataclass
class MockDataSource:
    """Minimal DataSource for integration tests without modifying sources.py."""

    _source_id: str
    fetch_result: Any = None
    diff_result: Any = None
    fetch_calls: int = field(default=0, init=False)
    diff_calls: int = field(default=0, init=False)

    @property
    def source_id(self) -> str:
        return self._source_id

    async def fetch(self) -> Any:
        self.fetch_calls += 1
        return self.fetch_result

    def diff(self, previous: Any, current: Any) -> Any:
        del previous, current
        self.diff_calls += 1
        return self.diff_result


class TestDataSourceProtocol:
    def test_mock_datasource_isinstance(self):
        mock = MockDataSource(_source_id="custom", fetch_result={}, diff_result=DiffResult([], []))
        assert isinstance(mock, DataSource)

    def test_wg21_index_isinstance(self, fake_pool):
        index = WG21Index(fake_pool)
        assert isinstance(index, DataSource)
        assert index.source_id == SOURCE_WG21_INDEX

    def test_iso_prober_isinstance(self, fake_pool):
        index = WG21Index(fake_pool)
        state = ProbeState(fake_pool)
        wl = MagicMock(spec=UserWatchlist)
        prober = ISOProber(index, state, user_watchlist=wl, cfg=make_test_settings())
        assert isinstance(prober, DataSource)
        assert prober.source_id == SOURCE_ISO_PROBE

    async def test_scheduler_polls_custom_datasource(self, fake_pool):
        custom = MockDataSource(
            _source_id="custom",
            fetch_result={"P1R0": Paper(id="P1R0")},
            diff_result=DiffResult(
                new_papers=[Paper(id="P1R0")],
                updated_papers=[],
            ),
        )
        wg21 = MagicMock(spec=WG21Index)
        wg21.source_id = SOURCE_WG21_INDEX
        wg21.fetch = AsyncMock(return_value={})
        wg21.diff = MagicMock(return_value=DiffResult([], []))

        iso = MagicMock(spec=ISOProber)
        iso.source_id = SOURCE_ISO_PROBE
        iso.fetch = AsyncMock(return_value=CycleResult(CycleStatus.EMPTY))
        iso.diff = MagicMock(return_value=[])
        iso.snapshot_stats = MagicMock(return_value={})

        state = ProbeState(fake_pool)
        scheduler = Scheduler(
            sources=[wg21, iso, custom],
            user_watchlist=MagicMock(spec=UserWatchlist),
            state=state,
            cfg=make_test_settings(enable_bulk_wg21=False, enable_iso_probe=False),
        )
        scheduler._seeded = True
        await scheduler.poll_once()
        assert custom.fetch_calls == 1
        assert custom.diff_calls == 1
