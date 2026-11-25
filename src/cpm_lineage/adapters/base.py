"""Platform adapter contract.

Nothing platform-specific is hard-coded in the core.  Everything that varies
by platform is declared here, once, by the adapter:

* the raw-status vocabulary and its mapping to ``CanonicalStatus``;
* the ad-type alias table and the per-type capability matrix (which levers
  exist, whether sales are attributed, whether targeting units are visible,
  whether clicks exist, any attribution deflator);
* geography: grain, roll-up map, and which labels are aggregate buckets;
* position semantics (kind, direction, valid range) or its absence;
* auction type (first / second price) -- sizing that assumes "you pay your
  bid" is only valid for first-price platforms;
* trading-day hours, currency, and which optional feeds are available.

``PlatformProfile`` is that declaration as a frozen dataclass.
``PlatformAdapter`` is the protocol the engine reads data through, and
``BaseAdapter`` a convenience base class holding in-memory records.

Example (documentation only): on a quick-commerce marketplace a "sponsored
keyword listing" maps to the ``keyword_listing`` type with the
``keyword_auction`` capability class, and a "home feed banner" maps to a
reach-class type with ``sales_attributed=False``.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Protocol, Sequence, TypeVar, runtime_checkable

from ..liveness import canonical_status as _canonical_status
from ..liveness import liveness_class as _liveness_class
from ..schema import (
    AuctionType,
    BudgetDayRow,
    CanonicalStatus,
    CapabilityClass,
    ChangeEvent,
    Entity,
    Lever,
    LivenessClass,
    MarketRow,
    OfftakeRow,
    PerfRow,
    Product,
    ShelfRow,
    TargetingUnit,
)

__all__ = [
    "DEFAULT_PLAYBOOK", "AdTypeCapability", "PositionSemantics",
    "PlatformProfile", "PlatformAdapter", "BaseAdapter", "Dataset",
    "build_records", "coerce_value", "FEED_NAMES",
]

# Levers each capability class exposes.  A card whose lever is not in its
# entity's set cannot be executed and is vetoed before any other gate.
DEFAULT_PLAYBOOK: Mapping[str, frozenset[str]] = {
    CapabilityClass.KEYWORD_AUCTION.value: frozenset(
        {Lever.BUDGET.value, Lever.KEYWORD.value, Lever.BID.value,
         Lever.EXPERIMENT.value, Lever.GEO.value, Lever.WATCH.value}
    ),
    CapabilityClass.RETARGETING.value: frozenset({Lever.BUDGET.value, Lever.WATCH.value}),
    CapabilityClass.PLACEMENT.value: frozenset({Lever.BUDGET.value, Lever.WATCH.value}),
    CapabilityClass.REACH.value: frozenset({Lever.WATCH.value}),
}

FEED_NAMES: tuple[str, ...] = (
    "perf", "entities", "targeting_units", "products", "market", "offtake",
    "shelf", "budget_days", "change_events",
)


GEO_GRAINS: tuple[str, ...] = ("city", "region", "country", "none")
BID_UNITS: tuple[str, ...] = ("cpm", "per_impression", "cpc", "other")
TARGETING_KINDS: tuple[str, ...] = ("keyword", "auto", "none")
JUDGED_ON: tuple[str, ...] = ("roas", "deflated_roas", "reach")


def _member(field_name: str, value: Any, allowed: Sequence[str]) -> None:
    if getattr(value, "value", value) not in allowed:
        raise ValueError(f"{field_name}={value!r} is not valid; expected one of {sorted(allowed)}")


@dataclass(frozen=True)
class AdTypeCapability:
    """Capability row for one canonical ad type."""

    name: str                                         # canonical ad-type name
    capability_class: str = CapabilityClass.KEYWORD_AUCTION.value  # playbook key: which levers the type exposes
    targeting: str = "keyword"                        # "keyword" | "auto" | "none"
    keyword_drillable: bool = True                    # targeting-unit grain data exists
    sales_attributed: bool = True                     # False: reach-class, never judged on ROAS
    judged_on: str = "roas"                           # "roas" | "deflated_roas" | "reach"
    roas_deflator: float = 1.0                        # multiply reported ROAS (retargeting over-attributes)
    has_clicks: bool = False  # descriptive: the type reports clicks (no rule needs clicks)
    has_reach: bool = False  # descriptive: the type reports reach
    levers: frozenset[str] | None = None              # None -> the class's playbook entry

    def __post_init__(self) -> None:
        _member("capability_class", self.capability_class, [c.value for c in CapabilityClass])
        _member("targeting", self.targeting, TARGETING_KINDS)
        _member("judged_on", self.judged_on, JUDGED_ON)
        if self.levers is not None:
            for lever in self.levers:
                _member("levers", lever, [x.value for x in Lever])

    @property
    def auto_targeted(self) -> bool:
        """True when the advertiser has no targeting-unit control."""
        return self.targeting == "auto"

    def allowed_levers(self, playbook: Mapping[str, frozenset[str]] | None = None) -> frozenset[str]:
        """Levers a card may pull on an entity of this type."""
        if self.levers is not None:
            return self.levers
        return (playbook or DEFAULT_PLAYBOOK).get(self.capability_class, frozenset({Lever.WATCH.value}))


@dataclass(frozen=True)
class PositionSemantics:
    """What the platform's ``position`` field means (or that it has none)."""

    available: bool = True  # False: the platform reports no position; every position rule abstains
    kind: str = "average"                # "most_viewed" | "average" | "impression_share" ...
    lower_is_better: bool = True  # True: 1 is the top slot
    valid_min: float = 1.0  # positions outside [valid_min, valid_max] are sentinels and are ignored
    valid_max: float = 100.0  # upper end of the valid position range
    top_value: float = 1.0               # value of the best slot
    cap: int = 21                        # bucket cap for per-position tables
    page_one_depth: int = 12             # slots on the first results page


@dataclass(frozen=True)
class PlatformProfile:
    """Everything platform-specific, declared once.

    The defaults describe a generic single-auction CPM platform with no
    clicks, a 1-is-best position and an unknown auction type; an adapter
    overrides what differs.  Enum-like strings are validated on
    construction, so a misspelt value fails here and not deep inside a run.

    Read by the engine: ``auction_type``, ``bid_unit`` (the overpay check
    compares bids with CPM only when it is ``"cpm"``), ``position``,
    ``ad_types`` / aliases / playbook, ``status_map``, the geo fields, the
    placement fields, ``biddable_targeting_types``, ``currency_unit`` (used
    when ``Economics.currency_unit`` is not set), the three trading-day
    hours (used when ``EngineConfig.trading_day`` is not set) and
    ``timezone`` (the default local-date bucketing of bidder decision
    timestamps in the CPM guardrail audit and the overpay check).

    Descriptive only (carried for adapters and reports, read by no rule):
    ``has_clicks`` / ``AdTypeCapability.has_clicks``, ``units_kind``,
    ``feed_lag_days``, ``feeds``, ``currency``.
    """

    name: str = "generic"  # platform name; namespaces ledger rows
    has_clicks: bool = False  # descriptive: the platform reports clicks (no rule needs clicks)
    auction_type: str = AuctionType.UNKNOWN.value  # "first_price" | "second_price" | "unknown"; the self-floor check runs only on second price
    bid_unit: str = "cpm"                                  # unit of ``PerfRow.bid``
    units_kind: str = "units"                              # "units" | "orders"
    position: PositionSemantics = field(default_factory=PositionSemantics)
    ad_types: Mapping[str, AdTypeCapability] = field(default_factory=dict)  # canonical ad-type name -> capability row
    ad_type_aliases: Mapping[str, str] = field(default_factory=dict)   # raw label (any case) -> canonical name
    default_capability_class: str = CapabilityClass.KEYWORD_AUCTION.value  # unknown types are fully levered
    playbook: Mapping[str, frozenset[str]] = field(default_factory=lambda: dict(DEFAULT_PLAYBOOK))  # capability class -> allowed levers
    status_map: Mapping[str, str] = field(default_factory=dict)        # raw status -> CanonicalStatus value
    geo_grain: str = "none"                                # "city" | "region" | "country" | "none"
    geo_rollup: Mapping[str, str] = field(default_factory=dict)        # child geo -> parent geo (terminal)
    aggregate_geo_labels: frozenset[str] = frozenset({"others", "other"})  # long-tail roll-up labels (lower case): never a treatment, control or target
    aggregate_geo_prefixes: tuple[str, ...] = ("other ",)  # long-tail roll-ups are never a target
    placement_targets: frozenset[str] = frozenset()        # pseudo targeting values that are not search terms
    placement_target_prefixes: tuple[str, ...] = ()  # prefixes of pseudo targeting values that are not search terms
    biddable_targeting_types: frozenset[str] = frozenset({"keyword"})  # PerfRow.targeting_type values that are biddable units (add "audience", "category" ...)
    currency: str = ""                                     # display only; no currency in core maths
    currency_unit: float = 1.0                             # see ``config.Economics.currency_unit``
    timezone: str = "UTC"  # local-date bucketing of bidder decision timestamps in the CPM audit and overpay check
    trading_day_start_hour: float = 6.0  # serving starts (local hour)
    trading_day_end_hour: float = 18.0  # last hour that still converts to same-day orders
    funding_target_hour: float = 22.0  # a raise funds serving up to this hour
    feed_lag_days: int = 0  # descriptive: days the feed trails the calendar
    feeds: frozenset[str] = frozenset({"perf"})            # which feeds the adapter can supply
    automated_sources: frozenset[str] = frozenset()        # change-log sources that are automations

    def __post_init__(self) -> None:
        _member("auction_type", self.auction_type, [a.value for a in AuctionType])
        _member("default_capability_class", self.default_capability_class, [c.value for c in CapabilityClass])
        _member("geo_grain", self.geo_grain, GEO_GRAINS)
        _member("bid_unit", self.bid_unit, BID_UNITS)
        for raw, canon in self.status_map.items():
            _member(f"status_map[{raw!r}]", getattr(canon, "value", canon), [s.value for s in CanonicalStatus])
        if not self.currency_unit > 0:
            raise ValueError("PlatformProfile.currency_unit must be positive")
        if not 0 <= self.trading_day_start_hour < self.trading_day_end_hour <= 24:
            raise ValueError("trading-day hours must satisfy 0 <= start < end <= 24")
        object.__setattr__(self, "biddable_targeting_types",
                           frozenset(str(t).strip().lower() for t in self.biddable_targeting_types))

    # -- auction ---------------------------------------------------------
    @property
    def pay_your_bid(self) -> bool:
        """True on first-price platforms (realised CPM tracks the bid)."""
        return self.auction_type == AuctionType.FIRST_PRICE.value

    def has_feed(self, name: str) -> bool:
        """True when the adapter declared the feed ``name`` (descriptive; the engine checks the data itself)."""
        return name in self.feeds

    # -- ad types --------------------------------------------------------
    def canonical_ad_type(self, raw: str | None) -> str:
        """Fold a raw ad-type label to its canonical name.

        Lookup is case- and whitespace-insensitive over ``ad_type_aliases``
        and the canonical names themselves.  Unmapped labels stay visible
        as their title-cased raw form (never silently misfiled); a missing
        label becomes ``"Unknown"``.
        """
        key = (raw or "").strip().lower()
        if not key:
            return "Unknown"
        for name in self.ad_types:
            if name.lower() == key:
                return name
        for alias, canon in self.ad_type_aliases.items():
            if alias.strip().lower() == key:
                return canon
        return (raw or "").strip().title()

    def capability(self, raw_or_canonical: str | None) -> AdTypeCapability:
        """Capability row for an ad type; unknown types get the default (fully levered) class."""
        name = self.canonical_ad_type(raw_or_canonical)
        cap = self.ad_types.get(name)
        if cap is not None:
            return cap
        return AdTypeCapability(name=name, capability_class=self.default_capability_class)

    def allowed_levers(self, raw_or_canonical: str | None) -> frozenset[str]:
        """Levers available on an entity of this ad type."""
        return self.capability(raw_or_canonical).allowed_levers(self.playbook)

    def keyword_grain_types(self) -> frozenset[str]:
        """Canonical ad types with targeting-unit grain data."""
        return frozenset(n for n, c in self.ad_types.items() if c.keyword_drillable)

    def no_attribution_types(self) -> frozenset[str]:
        """Canonical ad types whose attributed sales are structurally absent."""
        return frozenset(n for n, c in self.ad_types.items() if not c.sales_attributed)

    def type_has_clicks(self, raw_or_canonical: str | None) -> bool:
        """True when the platform or this ad type reports clicks (descriptive; no rule needs clicks)."""
        return self.has_clicks or self.capability(raw_or_canonical).has_clicks

    # -- status ----------------------------------------------------------
    def canonical_status(self, raw: str | None) -> CanonicalStatus:
        """Map a raw status string to ``CanonicalStatus`` (unknown -> ``UNKNOWN`` = live)."""
        return _canonical_status(raw, self.status_map)

    def liveness_class(self, raw: str | None) -> LivenessClass:
        """Liveness class (live, soft-dead, hard-dead) of a raw status string."""
        return _liveness_class(raw, self.status_map)

    # -- geography -------------------------------------------------------
    @property
    def has_geo(self) -> bool:
        """True when the platform reports a geographic grain."""
        return self.geo_grain != "none"

    def rollup_geo(self, geo: str | None) -> str | None:
        """Parent geo from ``geo_rollup`` (one hop; the map must be terminal), else the geo itself."""
        if geo is None:
            return None
        return self.geo_rollup.get(geo, geo)

    def is_aggregate_geo(self, geo: str | None) -> bool:
        """True for long-tail roll-up buckets, which are never a treatment, control or target."""
        g = (geo or "").strip().lower()
        if not g:
            return False
        return g in self.aggregate_geo_labels or any(g.startswith(p) for p in self.aggregate_geo_prefixes)

    # -- targeting -------------------------------------------------------
    def is_search_keyword(self, target: str | None) -> bool:
        """False for missing values and placement pseudo-targets."""
        t = (target or "").strip().lower()
        if not t:
            return False
        return t not in self.placement_targets and not any(t.startswith(p) for p in self.placement_target_prefixes)


@runtime_checkable
class PlatformAdapter(Protocol):
    """What the engine needs from a platform.

    ``profile`` and ``perf_rows`` are required.  Every other feed is
    optional: return an empty sequence when the platform has no such data,
    and leave its name out of ``profile.feeds`` so consumers can state "not
    available" instead of "nothing found".
    """

    profile: PlatformProfile

    def perf_rows(self) -> Sequence[PerfRow]:
        """Delivery facts (required): one row per entity, day and optional targeting unit / geo."""
        ...

    def entities(self) -> Sequence[Entity]:
        """Campaign / line-item records: status, ad type, daily budget, segment."""
        ...

    def targeting_units(self) -> Sequence[TargetingUnit]:
        """Targeting-unit dimension: role, search volume, live bid."""
        ...

    def products(self) -> Sequence[Product]:
        """Advertised products and whether they are the advertiser's own."""
        ...

    def market_rows(self) -> Sequence[MarketRow]:
        """Share-of-voice feed: impressions per brand, targeting unit and day."""
        ...

    def offtake_rows(self) -> Sequence[OfftakeRow]:
        """Market sell-out per brand, segment and day."""
        ...

    def shelf_rows(self) -> Sequence[ShelfRow]:
        """Availability and price per product, geo and day."""
        ...

    def budget_days(self) -> Sequence[BudgetDayRow]:
        """Budget pacing per entity and day: cap, spend, exhaustion and served-time telemetry."""
        ...

    def change_events(self) -> Sequence[ChangeEvent]:
        """The change log: who changed which lever on which entity, and when."""
        ...


@dataclass
class Dataset:
    """An in-memory bundle of every feed plus the platform profile."""

    profile: PlatformProfile = field(default_factory=PlatformProfile)
    perf: list[PerfRow] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    targeting_units: list[TargetingUnit] = field(default_factory=list)
    products: list[Product] = field(default_factory=list)
    market: list[MarketRow] = field(default_factory=list)
    offtake: list[OfftakeRow] = field(default_factory=list)
    shelf: list[ShelfRow] = field(default_factory=list)
    budget_days: list[BudgetDayRow] = field(default_factory=list)
    change_events: list[ChangeEvent] = field(default_factory=list)

    @property
    def latest_date(self) -> date | None:
        """Latest date in the performance feed."""
        return max((r.date for r in self.perf), default=None)

    def entity(self, entity_id: str) -> Entity | None:
        """The entity record with this id, or ``None``."""
        for e in self.entities:
            if e.entity_id == entity_id:
                return e
        return None

    def perf_for(self, entity_id: str) -> list[PerfRow]:
        """The delivery rows of one entity."""
        return [r for r in self.perf if r.entity_id == entity_id]


class BaseAdapter:
    """In-memory adapter over a ``Dataset``; satisfies ``PlatformAdapter``.

    Subclass it and fill ``self.dataset`` (the CSV, DataFrame and synthetic
    adapters do exactly that), or wrap an existing ``Dataset``.
    """

    def __init__(self, dataset: Dataset | None = None, profile: PlatformProfile | None = None) -> None:
        self.dataset = dataset or Dataset(profile=profile or PlatformProfile())
        if profile is not None:
            self.dataset.profile = profile

    @property
    def profile(self) -> PlatformProfile:
        """The dataset's platform profile."""
        return self.dataset.profile

    def perf_rows(self) -> Sequence[PerfRow]:
        """Delivery facts from the wrapped dataset."""
        return self.dataset.perf

    def entities(self) -> Sequence[Entity]:
        """Entity records from the wrapped dataset."""
        return self.dataset.entities

    def targeting_units(self) -> Sequence[TargetingUnit]:
        """Targeting-unit dimension from the wrapped dataset."""
        return self.dataset.targeting_units

    def products(self) -> Sequence[Product]:
        """Product records from the wrapped dataset."""
        return self.dataset.products

    def market_rows(self) -> Sequence[MarketRow]:
        """Share-of-voice rows from the wrapped dataset."""
        return self.dataset.market

    def offtake_rows(self) -> Sequence[OfftakeRow]:
        """Market sell-out rows from the wrapped dataset."""
        return self.dataset.offtake

    def shelf_rows(self) -> Sequence[ShelfRow]:
        """Shelf (availability and price) rows from the wrapped dataset."""
        return self.dataset.shelf

    def budget_days(self) -> Sequence[BudgetDayRow]:
        """Budget pacing rows from the wrapped dataset."""
        return self.dataset.budget_days

    def change_events(self) -> Sequence[ChangeEvent]:
        """Change-log rows from the wrapped dataset."""
        return self.dataset.change_events


# --------------------------------------------------------------------------
# Column mapping: loose records in, typed dataclasses out
# --------------------------------------------------------------------------

_T = TypeVar("_T")
_TRUE = {"1", "true", "t", "yes", "y"}
_FALSE = {"0", "false", "f", "no", "n"}


def _is_missing(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and v != v:
        return True
    if isinstance(v, str) and v.strip().lower() in ("", "nan", "none", "null", "nat"):
        return True
    try:  # pandas NaT / NA compare unequal to themselves
        return bool(v != v)
    except Exception:
        return False


def coerce_value(value: Any, type_name: str) -> Any:
    """Coerce a loose value to the base type named in a dataclass annotation.

    ``type_name`` is the annotation text (for example ``"float | None"``).
    Missing values (``None``, NaN, empty string) return ``None``.  Dates
    accept ``date`` / ``datetime`` objects, anything with ``.date()``, and
    ISO strings (a time part is dropped).
    """
    if _is_missing(value):
        return None
    base = type_name.replace("Optional[", "").replace("]", "").split("|")[0].strip()
    if base == "float":
        return float(str(value).replace(",", "")) if isinstance(value, str) else float(value)
    if base == "int":
        return int(float(str(value).replace(",", ""))) if isinstance(value, str) else int(value)
    if base == "bool":
        if isinstance(value, str):
            s = value.strip().lower()
            if s in _TRUE:
                return True
            if s in _FALSE:
                return False
            raise ValueError(f"cannot read {value!r} as a boolean")
        return bool(value)
    if base == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if hasattr(value, "date") and callable(value.date):
            return value.date()
        return date.fromisoformat(str(value).strip()[:10])
    if base == "datetime":
        if isinstance(value, datetime):
            return value
        if hasattr(value, "to_pydatetime"):
            return value.to_pydatetime()
        return datetime.fromisoformat(str(value).strip())
    if base == "str":
        return str(value).strip() if isinstance(value, str) else str(value)
    return value


def build_records(
    cls: type[_T],
    records: Iterable[Mapping[str, Any]],
    column_map: Mapping[str, str] | None = None,
    defaults: Mapping[str, Any] | None = None,
    skip_invalid: bool = False,
) -> list[_T]:
    """Turn loose mappings into typed schema records.

    ``column_map`` maps **schema field name -> source column name**; fields
    not in the map are read from a column of the same name when present.
    ``defaults`` fills fields that are absent or missing in a record.
    Values are coerced by the field's annotation.  A record missing a
    required field raises ``ValueError`` (or is dropped with
    ``skip_invalid=True``).  Returns a list of ``cls`` instances.
    """
    column_map = dict(column_map or {})
    defaults = dict(defaults or {})
    flds = dataclasses.fields(cls)  # type: ignore[arg-type]
    unknown = set(column_map) - {f.name for f in flds}
    if unknown:
        raise ValueError(f"column_map names unknown {cls.__name__} fields: {sorted(unknown)}")
    out: list[_T] = []
    for i, rec in enumerate(records):
        kwargs: dict[str, Any] = {}
        ok = True
        for f in flds:
            col = column_map.get(f.name, f.name)
            raw = rec.get(col) if col in rec else None
            try:
                val = coerce_value(raw, str(f.type))
            except (TypeError, ValueError) as exc:
                if skip_invalid:
                    ok = False
                    break
                raise ValueError(f"record {i}: field {f.name!r} (column {col!r}): {exc}") from exc
            if val is None and f.name in defaults:
                val = defaults[f.name]
            required = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
            if val is None:
                if required and "None" not in str(f.type):
                    if skip_invalid:
                        ok = False
                        break
                    raise ValueError(f"record {i}: required field {f.name!r} (column {col!r}) is missing")
                if not required:
                    continue  # keep the dataclass default
            kwargs[f.name] = val
        if ok:
            out.append(cls(**kwargs))
    return out
