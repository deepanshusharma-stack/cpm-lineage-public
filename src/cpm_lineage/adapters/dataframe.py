"""DataFrame reference adapter: column mapping in, schema records out.

Works with any frame-like object exposing ``to_dict("records")`` (pandas,
and libraries that mimic it) or with a plain list of dicts, so pandas is
never imported for reading.  ``to_dataframe`` converts records back to a
pandas frame and imports pandas lazily (install the ``pandas`` extra).
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable, Mapping, TypeVar

from ..schema import BudgetDayRow, Entity, MarketRow, OfftakeRow, PerfRow, Product, ShelfRow, TargetingUnit
from .base import BaseAdapter, Dataset, PlatformProfile, build_records
from .csv import _normalise_perf, derive_entities

__all__ = ["DataFrameAdapter", "records_from_frame", "frame_to_records", "to_dataframe"]

_T = TypeVar("_T")


def frame_to_records(frame: Any) -> list[Mapping[str, Any]]:
    """Rows of a frame-like object (or an iterable of mappings) as a list of dicts."""
    if frame is None:
        return []
    if hasattr(frame, "to_dict"):
        return list(frame.to_dict("records"))
    if hasattr(frame, "to_dicts"):
        return list(frame.to_dicts())
    return [dict(r) for r in frame]


def records_from_frame(
    frame: Any,
    cls: type[_T],
    column_map: Mapping[str, str] | None = None,
    defaults: Mapping[str, Any] | None = None,
    skip_invalid: bool = False,
) -> list[_T]:
    """Convert a frame into schema records of type ``cls``.

    ``column_map`` is schema field -> frame column.  NaN / NaT become
    ``None``; timestamps become ``date``.
    """
    return build_records(cls, frame_to_records(frame), column_map, defaults, skip_invalid=skip_invalid)


def to_dataframe(records: Iterable[Any]) -> Any:
    """Schema records -> ``pandas.DataFrame`` (lazy pandas import)."""
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover - exercised only without pandas
        raise ImportError("to_dataframe needs pandas: pip install 'cpm-lineage[pandas]'") from exc
    return pd.DataFrame([dataclasses.asdict(r) for r in records])


class DataFrameAdapter(BaseAdapter):
    """Reference adapter over in-memory frames.

    Only ``perf`` is required.  Optional feeds take a frame and a column
    map each; absent feeds are left out of ``profile.feeds``.  Entities are
    derived from the fact rows when no entity frame is given.
    """

    def __init__(
        self,
        perf: Any,
        column_map: Mapping[str, str] | None = None,
        profile: PlatformProfile | None = None,
        *,
        entities: Any = None,
        entities_map: Mapping[str, str] | None = None,
        targeting_units: Any = None,
        targeting_units_map: Mapping[str, str] | None = None,
        products: Any = None,
        products_map: Mapping[str, str] | None = None,
        market: Any = None,
        market_map: Mapping[str, str] | None = None,
        offtake: Any = None,
        offtake_map: Mapping[str, str] | None = None,
        shelf: Any = None,
        shelf_map: Mapping[str, str] | None = None,
        budget_days: Any = None,
        budget_map: Mapping[str, str] | None = None,
        defaults: Mapping[str, Any] | None = None,
    ) -> None:
        profile = profile or PlatformProfile()
        ds = Dataset(profile=profile)
        ds.perf = _normalise_perf(records_from_frame(perf, PerfRow, column_map, defaults), profile)
        feeds = {"perf", "entities"}
        if entities is not None:
            ds.entities = records_from_frame(entities, Entity, entities_map)
        else:
            ds.entities = derive_entities(ds.perf)
        optional = (
            ("targeting_units", targeting_units, targeting_units_map, TargetingUnit),
            ("products", products, products_map, Product),
            ("market", market, market_map, MarketRow),
            ("offtake", offtake, offtake_map, OfftakeRow),
            ("shelf", shelf, shelf_map, ShelfRow),
            ("budget_days", budget_days, budget_map, BudgetDayRow),
        )
        for name, frame, cmap, cls in optional:
            if frame is not None:
                setattr(ds, name, records_from_frame(frame, cls, cmap))
                feeds.add(name)
        ds.profile = dataclasses.replace(profile, feeds=frozenset(feeds))
        super().__init__(ds)
