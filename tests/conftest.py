"""Shared fixtures: the synthetic dataset and small record factories.

The synthetic dataset is generated once per session (seeded, deterministic)
and must be treated as read-only.  ``synthetic_events`` exposes the planted
ground truth documented in ``cpm_lineage.adapters.synthetic``.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

import pytest

from cpm_lineage.adapters.base import PlatformProfile
from cpm_lineage.adapters.synthetic import InjectedEvent, SyntheticAdapter, SyntheticDataset
from cpm_lineage.config import EngineConfig
from cpm_lineage.schema import BudgetDayRow, Entity, PerfRow, WindowPair
from cpm_lineage.windows import trailing_windows


@pytest.fixture(scope="session")
def synthetic_adapter() -> SyntheticAdapter:
    """The seeded synthetic platform adapter (seed 7, 90 days)."""
    return SyntheticAdapter()


@pytest.fixture(scope="session")
def synthetic_dataset(synthetic_adapter: SyntheticAdapter) -> SyntheticDataset:
    """All synthetic feeds: perf, entities, targeting_units, products, market, offtake, shelf, budget_days."""
    return synthetic_adapter.dataset  # type: ignore[return-value]


@pytest.fixture(scope="session")
def synthetic_events(synthetic_dataset: SyntheticDataset) -> dict[str, InjectedEvent]:
    """Planted ground truth: cpm_spike, availability_drop, budget_capped_winner, bleeding_keyword, own_overlap."""
    return synthetic_dataset.events


@pytest.fixture(scope="session")
def synthetic_profile(synthetic_dataset: SyntheticDataset) -> PlatformProfile:
    return synthetic_dataset.profile


@pytest.fixture(scope="session")
def perf_rows(synthetic_dataset: SyntheticDataset) -> list[PerfRow]:
    """Synthetic performance fact rows (entity x targeting unit x geo x day)."""
    return synthetic_dataset.perf


@pytest.fixture(scope="session")
def entities(synthetic_dataset: SyntheticDataset) -> list[Entity]:
    return synthetic_dataset.entities


@pytest.fixture(scope="session")
def budget_days(synthetic_dataset: SyntheticDataset) -> list[BudgetDayRow]:
    return synthetic_dataset.budget_days


@pytest.fixture(scope="session")
def windows_30(synthetic_dataset: SyntheticDataset) -> WindowPair:
    """Last 30 days versus the 30 before, anchored to the data's latest date."""
    return trailing_windows(synthetic_dataset.latest_date, 30)


@pytest.fixture(scope="session")
def windows_14(synthetic_dataset: SyntheticDataset) -> WindowPair:
    """Last 14 days versus the 14 before, anchored to the data's latest date."""
    return trailing_windows(synthetic_dataset.latest_date, 14)


@pytest.fixture()
def engine_config() -> EngineConfig:
    """Default shared configuration."""
    return EngineConfig()


@pytest.fixture()
def make_perf_row() -> Callable[..., PerfRow]:
    """Factory for hand-built ``PerfRow`` records with sensible defaults."""

    def _make(day: date | int = 0, entity_id: str = "cmp-x", spend: float = 100.0,
              impressions: float = 1000.0, sales: float = 300.0, units: float = 3.0,
              **kw: Any) -> PerfRow:
        d = day if isinstance(day, date) else date(2026, 1, 1) + timedelta(days=day)
        return PerfRow(date=d, entity_id=entity_id, spend=spend, impressions=impressions,
                       sales=sales, units=units, **kw)

    return _make
