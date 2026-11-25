"""CSV reference adapter: column mapping in, schema records out.

File I/O is allowed here (adapters only).  Uses the standard library ``csv``
module; no pandas.

Example::

    adapter = CsvAdapter(
        "perf.csv",
        column_map={"date": "day", "entity_id": "campaign_id",
                    "spend": "cost", "impressions": "impr",
                    "sales": "revenue", "units": "orders",
                    "targeting_unit": "keyword"},
        profile=PlatformProfile(name="my-platform"),
    )
    rows = adapter.perf_rows()
"""

from __future__ import annotations

import csv
import os
from typing import Any, Mapping, TypeVar

from ..metrics import normalise_keyword
from ..schema import BudgetDayRow, Entity, MarketRow, OfftakeRow, PerfRow, Product, ShelfRow, TargetingUnit
from .base import BaseAdapter, Dataset, PlatformProfile, build_records

__all__ = ["CsvAdapter", "read_csv_records", "read_perf_csv", "derive_entities"]

_T = TypeVar("_T")
PathLike = str | os.PathLike


def read_csv_records(
    path: PathLike,
    cls: type[_T],
    column_map: Mapping[str, str] | None = None,
    defaults: Mapping[str, Any] | None = None,
    *,
    delimiter: str = ",",
    encoding: str = "utf-8-sig",
    skip_invalid: bool = False,
) -> list[_T]:
    """Read a CSV file into schema records of type ``cls``.

    ``column_map`` is schema field -> CSV header (see
    ``adapters.base.build_records``).  Returns a list of ``cls``.
    """
    with open(path, newline="", encoding=encoding) as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        return build_records(cls, reader, column_map, defaults, skip_invalid=skip_invalid)


def _normalise_perf(rows: list[PerfRow], profile: PlatformProfile) -> list[PerfRow]:
    """Apply the profile's ad-type alias map and keyword normalisation at ingestion."""
    import dataclasses

    out = []
    for r in rows:
        changes: dict[str, Any] = {}
        if r.targeting_unit is not None:
            kw = normalise_keyword(r.targeting_unit)
            if kw != r.targeting_unit:
                changes["targeting_unit"] = kw
        if r.ad_type is not None:
            # Only fold labels the profile knows; unknown labels stay as supplied
            # (``canonical_ad_type`` title-cases them for display on demand).
            key = r.ad_type.strip().lower()
            known = {a.strip().lower() for a in profile.ad_type_aliases} | {n.lower() for n in profile.ad_types}
            if key in known:
                canon = profile.canonical_ad_type(r.ad_type)
                if canon != r.ad_type:
                    changes["ad_type"] = canon
        if r.geo is not None:
            g = profile.rollup_geo(r.geo)
            if g != r.geo:
                changes["geo"] = g
        out.append(dataclasses.replace(r, **changes) if changes else r)
    return out


def read_perf_csv(
    path: PathLike,
    column_map: Mapping[str, str] | None = None,
    profile: PlatformProfile | None = None,
    defaults: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> list[PerfRow]:
    """Read the performance fact from a CSV into ``PerfRow`` records.

    Targeting units are normalised (trimmed, lower-cased), known ad-type
    labels folded through the profile's alias map (unknown labels are left
    as supplied) and geos through its roll-up.
    """
    rows = read_csv_records(path, PerfRow, column_map, defaults, **kwargs)
    return _normalise_perf(rows, profile or PlatformProfile())


def derive_entities(rows: list[PerfRow]) -> list[Entity]:
    """Minimal ``Entity`` list from fact rows when no entity file exists.

    Takes the name, ad type, segment and advertiser from each entity's rows
    and the status from its latest row.  Budget and rolling-spend fields
    stay ``None``.
    """
    seen: dict[str, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda x: x.date):
        e = seen.setdefault(r.entity_id, {"start_date": r.date})
        for src, dst in (("entity_name", "name"), ("ad_type", "ad_type"), ("segment", "segment"),
                         ("advertiser_id", "advertiser_id"), ("status", "status")):
            v = getattr(r, src)
            if v is not None:
                e[dst] = v
    return [Entity(entity_id=k, **v) for k, v in seen.items()]


class CsvAdapter(BaseAdapter):
    """Reference adapter over CSV files.

    Only ``perf_path`` is required.  Each optional feed takes its own path
    and column map; feeds without a path are reported as absent in
    ``profile.feeds``.  Entities are derived from the fact rows when no
    entity file is given.
    """

    def __init__(
        self,
        perf_path: PathLike,
        column_map: Mapping[str, str] | None = None,
        profile: PlatformProfile | None = None,
        *,
        entities_path: PathLike | None = None,
        entities_map: Mapping[str, str] | None = None,
        targeting_units_path: PathLike | None = None,
        targeting_units_map: Mapping[str, str] | None = None,
        products_path: PathLike | None = None,
        products_map: Mapping[str, str] | None = None,
        market_path: PathLike | None = None,
        market_map: Mapping[str, str] | None = None,
        offtake_path: PathLike | None = None,
        offtake_map: Mapping[str, str] | None = None,
        shelf_path: PathLike | None = None,
        shelf_map: Mapping[str, str] | None = None,
        budget_path: PathLike | None = None,
        budget_map: Mapping[str, str] | None = None,
        defaults: Mapping[str, Any] | None = None,
        delimiter: str = ",",
    ) -> None:
        import dataclasses

        profile = profile or PlatformProfile()
        ds = Dataset(profile=profile)
        ds.perf = read_perf_csv(perf_path, column_map, profile, defaults, delimiter=delimiter)
        feeds = {"perf", "entities"}
        if entities_path:
            ds.entities = read_csv_records(entities_path, Entity, entities_map, delimiter=delimiter)
        else:
            ds.entities = derive_entities(ds.perf)
        optional = (
            ("targeting_units", targeting_units_path, targeting_units_map, TargetingUnit),
            ("products", products_path, products_map, Product),
            ("market", market_path, market_map, MarketRow),
            ("offtake", offtake_path, offtake_map, OfftakeRow),
            ("shelf", shelf_path, shelf_map, ShelfRow),
            ("budget_days", budget_path, budget_map, BudgetDayRow),
        )
        for name, path, cmap, cls in optional:
            if path:
                setattr(ds, name, read_csv_records(path, cls, cmap, delimiter=delimiter))
                feeds.add(name)
        ds.profile = dataclasses.replace(profile, feeds=frozenset(feeds))
        super().__init__(ds)
