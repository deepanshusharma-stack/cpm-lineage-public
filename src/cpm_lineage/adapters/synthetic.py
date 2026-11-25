"""Seeded synthetic dataset with known, injected ground-truth events.

``generate(seed)`` returns a deterministic 90-day, multi-campaign,
multi-keyword, multi-geo dataset for one advertiser on an imaginary
first-price CPM platform with no click data.  Tests and examples use it to
assert that the engine *finds* what was planted.

The world
---------
* Advertiser ``adv-001``, brand ``own-brand``, segment ``snack-bars``;
  three rivals (``rival-b``, ``rival-c``, ``rival-d``).
* Geos ``geo-north, geo-south, geo-east, geo-west`` plus the long-tail
  aggregate bucket ``others`` (never a valid target).
* Four own products ``sku-a1 .. sku-a4`` and five rival products.
* Nine campaigns (see ``CAMPAIGNS``): seven keyword-listing campaigns, one
  auto-targeted recommendation campaign (no targeting units) and one
  reach-class featured-listing campaign (no attributed sales).
* Fact grain: campaign x targeting unit x geo x day, each row tied to one
  product.  Feeds: performance, entities, targeting units, products,
  share-of-voice market view, market offtake, shelf (availability and
  price), budget pacing, change log.

Injected events (``SyntheticDataset.events``)
---------------------------------------------
``cpm_spike``
    Campaign ``cmp-generic``, keyword ``protein bar``: for the last 30 days
    CPM (and the bid behind it) is 1.6x its earlier level while position
    is unchanged and ``rival-b`` ad impressions on the keyword are 2.5x.
    Conversion and price do not move, so the keyword's ROAS falls by
    1 / 1.6.  Expected read: CPM-driven ROAS decline, rival pressure.
``availability_drop``
    Product ``sku-a2`` in ``geo-south``: for the last 21 days availability
    falls from about 0.95 to about 0.45.  Conversion on every fact row for
    that product and geo scales with availability (campaigns ``cmp-brand``
    and ``cmp-reco``).  Expected read: conversion-driven drop localised to
    one product and geo, availability as the cause.
``budget_capped_winner``
    Campaign ``cmp-winner``: ROAS above 4 every day, but its daily budget
    covers only about a third of available demand, so it exhausts around
    11:15 local time every day (status ``BUDGET_HELD``, yet spending).
    Expected read: budget-bound, live, raise the budget.
``bleeding_keyword``
    Campaign ``cmp-conquest``, keyword ``rivalb bar``: ROAS about 0.35 for
    all 90 days on material spend, almost no new customers, negligible
    halo.  Expected read: structural bleed; cut the keyword.
``own_overlap``
    Keyword ``energy bar`` is bid by two own campaigns (``cmp-generic``
    and ``cmp-overlap``) in every geo on every day for the same product
    (``sku-a1``); ``cmp-overlap`` has the worse ROAS on it.  Expected
    read: own-campaign collision; consolidate into the better campaign.

Background facts that are *not* events but exercise edge paths:
``cmp-paused`` was operator-paused after day 40 (hard dead, no recent
spend); ``cmp-young`` launched 10 days before the end (cold-start tier);
``cmp-reach`` has zero attributed sales by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

import numpy as np

from ..schema import (
    AuctionType,
    BudgetDayRow,
    CanonicalStatus,
    CapabilityClass,
    ChangeEvent,
    Entity,
    MarketRow,
    OfftakeRow,
    PerfRow,
    Product,
    ShelfRow,
    TargetingRole,
    TargetingUnit,
)
from .base import AdTypeCapability, BaseAdapter, Dataset, PlatformProfile, PositionSemantics

__all__ = [
    "generate", "SyntheticAdapter", "SyntheticDataset", "InjectedEvent",
    "synthetic_profile", "CAMPAIGNS", "GEOS", "DEFAULT_SEED", "DEFAULT_END_DATE",
    "ADVERTISER_ID", "OWN_BRAND", "SEGMENT",
]

DEFAULT_SEED = 7
DEFAULT_END_DATE = date(2026, 3, 31)
ADVERTISER_ID = "adv-001"
OWN_BRAND = "own-brand"
SEGMENT = "snack-bars"

GEOS: tuple[tuple[str, float], ...] = (
    ("geo-north", 0.30), ("geo-south", 0.25), ("geo-east", 0.20), ("geo-west", 0.17), ("others", 0.08),
)
RIVALS: tuple[tuple[str, float], ...] = (("rival-b", 0.5), ("rival-c", 0.3), ("rival-d", 0.2))

# product_id: (brand, price, list_price)
_PRODUCTS: dict[str, tuple[str, float, float]] = {
    "sku-a1": (OWN_BRAND, 240.0, 300.0),
    "sku-a2": (OWN_BRAND, 180.0, 200.0),
    "sku-a3": (OWN_BRAND, 405.0, 450.0),
    "sku-a4": (OWN_BRAND, 120.0, 150.0),
    "sku-b1": ("rival-b", 230.0, 280.0),
    "sku-b2": ("rival-b", 390.0, 450.0),
    "sku-c1": ("rival-c", 200.0, 220.0),
    "sku-c2": ("rival-c", 150.0, 160.0),
    "sku-d1": ("rival-d", 260.0, 320.0),
}

_OWN, _GEN, _COMP = (
    TargetingRole.OWN_BRAND.value, TargetingRole.GENERIC.value, TargetingRole.COMPETITOR.value,
)

# Each keyword: (value, role, impressions/day before scaling, cpm, icr, position, searches/day)
# Each campaign: id, name, ad_type, raw status, products, keywords, budget factor
# (daily cap as a multiple of mean daily demand spend), first and last active
# day index (None = run to the end).
CAMPAIGNS: tuple[dict[str, Any], ...] = (
    dict(id="cmp-brand", name="Brand defence", ad_type="keyword_listing", status="ACTIVE",
         products=("sku-a1", "sku-a2"), budget_factor=1.8, first=0, last=None,
         keywords=(("ownbrand bar", _OWN, 9000, 120.0, 0.0050, 1.3, 900),
                   ("ownbrand protein", _OWN, 6000, 110.0, 0.0048, 1.5, 500))),
    dict(id="cmp-generic", name="Generic search", ad_type="keyword_listing", status="ACTIVE",
         products=("sku-a3", "sku-a1"), budget_factor=1.8, first=0, last=None,
         keywords=(("protein bar", _GEN, 20000, 150.0, 0.0016, 2.6, 4000),
                   ("energy bar", _GEN, 12000, 140.0, 0.0019, 3.1, 2500),
                   ("healthy snack", _GEN, 15000, 100.0, 0.0007, 4.2, 3000),
                   ("granola", _GEN, 8000, 90.0, 0.0010, 5.0, 1500))),
    dict(id="cmp-winner", name="High-protein push", ad_type="keyword_listing", status="BUDGET_HELD",
         products=("sku-a3",), budget_factor=0.33, first=0, last=None,
         keywords=(("high protein snack", _GEN, 30000, 130.0, 0.0018, 2.2, 5000),
                   ("protein bar box", _GEN, 18000, 125.0, 0.0016, 2.4, 2000))),
    dict(id="cmp-conquest", name="Conquest", ad_type="keyword_listing", status="ACTIVE",
         products=("sku-a1", "sku-a4"), budget_factor=1.8, first=0, last=None,
         keywords=(("rivalb bar", _COMP, 9000, 170.0, 0.00025, 3.4, 1200),
                   ("rivalc bites", _COMP, 6000, 160.0, 0.0024, 3.0, 800),
                   ("rivald crunch", _COMP, 4000, 150.0, 0.0011, 2.8, 600))),
    dict(id="cmp-overlap", name="Bars always-on", ad_type="keyword_listing", status="ACTIVE",
         products=("sku-a1",), budget_factor=1.8, first=0, last=None,
         keywords=(("energy bar", _GEN, 9000, 165.0, 0.0009, 3.6, 2500),
                   ("snack bar", _GEN, 7000, 110.0, 0.0012, 3.9, 1800))),
    dict(id="cmp-reco", name="Recommendations", ad_type="recommendation", status="ACTIVE",
         products=("sku-a2",), budget_factor=1.8, first=0, last=None,
         keywords=((None, TargetingRole.PLACEMENT.value, 25000, 80.0, 0.0016, None, None),)),
    dict(id="cmp-reach", name="Featured listing", ad_type="featured_listing", status="ACTIVE",
         products=("sku-a1",), budget_factor=1.8, first=0, last=None,
         keywords=((None, TargetingRole.PLACEMENT.value, 60000, 60.0, 0.0, None, None),)),
    dict(id="cmp-paused", name="Cookies test", ad_type="keyword_listing", status="PAUSED",
         products=("sku-a4",), budget_factor=1.8, first=0, last=39,
         keywords=(("protein cookie", _GEN, 8000, 120.0, 0.0018, 4.5, 700),)),
    dict(id="cmp-young", name="Vegan launch", ad_type="keyword_listing", status="ACTIVE",
         products=("sku-a3",), budget_factor=1.8, first=-10, last=None,
         keywords=(("vegan protein bar", _GEN, 7000, 115.0, 0.0015, 3.0, 900),)),
)

# Own ad share of voice, own organic share, by role (market view).
_SOV = {_OWN: (0.70, 0.80), _GEN: (0.35, 0.30), _COMP: (0.15, 0.05)}
# (halo share of direct sales, new-customer rate) by role.
_HALO_NTB = {_OWN: (0.05, 0.10), _GEN: (0.15, 0.40), _COMP: (0.10, 0.60),
             TargetingRole.PLACEMENT.value: (0.20, 0.30)}

_SPIKE_KW, _SPIKE_ENTITY, _SPIKE_MULT, _SPIKE_DAYS, _SPIKE_RIVAL, _SPIKE_RIVAL_MULT = (
    "protein bar", "cmp-generic", 1.6, 30, "rival-b", 2.5)
_DROP_SKU, _DROP_GEO, _DROP_DAYS, _AVAIL_BASE, _AVAIL_DROP, _AVAIL_RIVAL = (
    "sku-a2", "geo-south", 21, 0.95, 0.45, 0.90)
_BLEED_KW, _BLEED_ENTITY = "rivalb bar", "cmp-conquest"
_OVERLAP_KW, _OVERLAP_ENTITIES = "energy bar", ("cmp-generic", "cmp-overlap")
_WINNER = "cmp-winner"
_DAY_START_MIN, _EXPECTED_LIVE_MIN = 360.0, 960.0   # serving 06:00 -> 22:00 local
_SHELF_CHECKS = 24
_ORGANIC_MULT = 3.0   # total sell-out per unit of ad-attributed sales


@dataclass(frozen=True)
class InjectedEvent:
    """One planted ground-truth event."""

    name: str
    kind: str
    description: str
    entity_ids: tuple[str, ...] = ()
    targeting_unit: str | None = None
    geo: str | None = None
    product_id: str | None = None
    start: date | None = None
    end: date | None = None
    magnitude: float | None = None        # event-specific size (see description)
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class SyntheticDataset(Dataset):
    """``Dataset`` plus the generation parameters and the planted events."""

    seed: int = DEFAULT_SEED
    start_date: date | None = None
    end_date: date | None = None
    events: dict[str, InjectedEvent] = field(default_factory=dict)


def synthetic_profile() -> PlatformProfile:
    """Profile of the imaginary platform: first-price CPM, no clicks, city-grain geo."""
    return PlatformProfile(
        name="synthetic",
        has_clicks=False,
        auction_type=AuctionType.FIRST_PRICE.value,
        position=PositionSemantics(available=True, kind="average", lower_is_better=True),
        ad_types={
            "keyword_listing": AdTypeCapability(
                name="keyword_listing", capability_class=CapabilityClass.KEYWORD_AUCTION.value),
            "recommendation": AdTypeCapability(
                name="recommendation", capability_class=CapabilityClass.RETARGETING.value,
                targeting="auto", keyword_drillable=False, judged_on="deflated_roas", roas_deflator=0.6),
            "featured_listing": AdTypeCapability(
                name="featured_listing", capability_class=CapabilityClass.REACH.value,
                targeting="none", keyword_drillable=False, sales_attributed=False,
                judged_on="reach", has_reach=True),
        },
        ad_type_aliases={"kw listing": "keyword_listing", "reco": "recommendation",
                         "banner": "featured_listing"},
        status_map={"ACTIVE": CanonicalStatus.ACTIVE.value,
                    "BUDGET_HELD": CanonicalStatus.BUDGET_HELD.value,
                    "PAUSED": CanonicalStatus.OPERATOR_PAUSED.value},
        geo_grain="city",
        aggregate_geo_labels=frozenset({"others"}),
        placement_targets=frozenset({"feed"}),
        currency="",
        trading_day_start_hour=6.0,
        trading_day_end_hour=18.0,
        funding_target_hour=22.0,
        feeds=frozenset({"perf", "entities", "targeting_units", "products", "market",
                         "offtake", "shelf", "budget_days", "change_events"}),
        automated_sources=frozenset({"position-bidder"}),
    )


def _availability(sku: str, geo: str, day_idx: int, days: int) -> float:
    """Noise-free availability for a product x geo x day."""
    if _PRODUCTS[sku][0] != OWN_BRAND:
        return _AVAIL_RIVAL
    if sku == _DROP_SKU and geo == _DROP_GEO and day_idx >= days - _DROP_DAYS:
        return _AVAIL_DROP
    return _AVAIL_BASE


def generate(
    seed: int = DEFAULT_SEED,
    days: int = 90,
    end_date: date = DEFAULT_END_DATE,
    scale: float = 3.0,
) -> SyntheticDataset:
    """Generate the synthetic dataset.

    Deterministic for a given ``(seed, days, end_date, scale)``.  ``scale``
    multiplies every impression volume (and so spend and sales) without
    changing any ratio.  ``days`` must be at least 60 so that every event
    window fits.  Returns a ``SyntheticDataset`` whose ``events`` dict
    documents what was planted and where.
    """
    if days < 60:
        raise ValueError("days must be >= 60 so the injected event windows fit")
    rng = np.random.default_rng(seed)
    start = end_date - timedelta(days=days - 1)
    dates = [start + timedelta(days=i) for i in range(days)]
    geo_names = [g for g, _ in GEOS]
    geo_w = np.array([w for _, w in GEOS])
    n_geo = len(GEOS)
    weekday = np.array([1.15 if d.isoweekday() >= 6 else 1.0 for d in dates])
    spike_from = days - _SPIKE_DAYS

    # Availability matrix per own product: (days, geos), observed with binomial noise.
    shelf: list[ShelfRow] = []
    avail_obs: dict[str, np.ndarray] = {}
    for sku, (brand, price, list_price) in _PRODUCTS.items():
        p = np.array([[_availability(sku, g, i, days) for g in geo_names] for i in range(days)])
        hits = rng.binomial(_SHELF_CHECKS, p)
        avail_obs[sku] = hits / _SHELF_CHECKS
        for i, d in enumerate(dates):
            for j, g in enumerate(geo_names):
                shelf.append(ShelfRow(
                    date=d, product_id=sku, geo=g, available_count=float(hits[i, j]),
                    check_count=float(_SHELF_CHECKS), price=price, list_price=list_price,
                    is_own=brand == OWN_BRAND, brand=brand, segment=SEGMENT))

    perf: list[PerfRow] = []
    budget_days: list[BudgetDayRow] = []
    entities: list[Entity] = []
    units_out: list[TargetingUnit] = []
    own_kw_impr: dict[str, np.ndarray] = {}        # keyword -> own ad impressions per day
    kw_expected: dict[str, float] = {}             # keyword -> noise-free own impressions per day
    kw_role: dict[str, str] = {}
    own_sales: dict[str, np.ndarray] = {           # product -> (days, geos) ad-attributed sales
        sku: np.zeros((days, n_geo)) for sku, v in _PRODUCTS.items() if v[0] == OWN_BRAND}

    for camp in CAMPAIGNS:
        first = camp["first"] if camp["first"] >= 0 else days + camp["first"]
        last = days - 1 if camp["last"] is None else min(camp["last"], days - 1)
        live = np.zeros(days, dtype=bool)
        live[first:last + 1] = True
        kws = camp["keywords"]
        impr = np.zeros((len(kws), days, n_geo))
        cpm = np.zeros((len(kws), days, n_geo))
        mean_demand = 0.0
        for k, (kw, role, base_impr, base_cpm, _icr, _pos, _vol) in enumerate(kws):
            impr[k] = (base_impr * scale * weekday[:, None] * geo_w[None, :]
                       * rng.lognormal(0.0, 0.10, size=(days, n_geo)))
            c = base_cpm * rng.lognormal(0.0, 0.04, size=(days, n_geo))
            if camp["id"] == _SPIKE_ENTITY and kw == _SPIKE_KW:
                c[spike_from:] *= _SPIKE_MULT
            cpm[k] = c
            mean_demand += base_impr * scale * base_cpm / 1000.0 * float(weekday.mean())
        cap = float(round(camp["budget_factor"] * mean_demand, -1))
        demand = (impr * cpm / 1000.0).sum(axis=(0, 2))            # per day
        served = np.minimum(1.0, cap / np.maximum(demand, 1e-9))    # fraction of the day served
        impr = np.rint(impr * served[None, :, None])
        impr[:, ~live, :] = 0.0
        spend = np.round(impr * cpm / 1000.0, 2)

        day_spend = np.zeros(days)
        day_sales = np.zeros(days)
        day_impr = np.zeros(days)
        for k, (kw, role, base_impr, base_cpm, base_icr, base_pos, vol) in enumerate(kws):
            sku = camp["products"][k % len(camp["products"])]
            price = _PRODUCTS[sku][1]
            avail = np.array([[_availability(sku, g, i, days) for g in geo_names] for i in range(days)])
            rate = base_icr * avail / _AVAIL_BASE
            units = rng.poisson(impr[k] * rate).astype(float)
            sales = units * price
            halo, ntb = _HALO_NTB[role]
            if camp["id"] == _BLEED_ENTITY and kw == _BLEED_KW:
                halo, ntb = 0.02, 0.05
            new_users = rng.binomial(units.astype(int), ntb).astype(float)
            pos = None
            if base_pos is not None:
                pos = np.clip(base_pos + rng.normal(0.0, 0.25, size=(days, n_geo)), 1.0, None)
            own_sales[sku] += sales
            day_spend += spend[k].sum(axis=1)
            day_sales += sales.sum(axis=1)
            day_impr += impr[k].sum(axis=1)
            if kw is not None:
                own_kw_impr[kw] = own_kw_impr.get(kw, np.zeros(days)) + impr[k].sum(axis=1)
                kw_expected[kw] = kw_expected.get(kw, 0.0) + base_impr * scale * min(1.0, camp["budget_factor"])
                kw_role[kw] = role
                units_out.append(TargetingUnit(
                    value=kw, kind="keyword", role=role, entity_id=camp["id"],
                    search_volume=float(vol), live_bid=round(base_cpm / 0.95, 2),
                    base_bid=round(base_cpm / 0.95, 2), segment=SEGMENT))
            for i in range(first, last + 1):
                for j, g in enumerate(geo_names):
                    if impr[k, i, j] <= 0:
                        continue
                    perf.append(PerfRow(
                        date=dates[i], entity_id=camp["id"], spend=float(spend[k, i, j]),
                        impressions=float(impr[k, i, j]), sales=float(sales[i, j]),
                        units=float(units[i, j]), advertiser_id=ADVERTISER_ID,
                        targeting_unit=kw,
                        targeting_type="keyword" if kw is not None else (
                            "auto" if camp["ad_type"] == "recommendation" else "placement"),
                        geo=g, product_id=sku, clicks=None,
                        bid=round(float(cpm[k, i, j]) / 0.95, 2) if kw is not None else None,
                        position=None if pos is None else round(float(pos[i, j]), 2),
                        ad_type=camp["ad_type"], status=camp["status"],
                        indirect_sales=round(float(sales[i, j]) * halo, 2),
                        new_users=float(new_users[i, j]),
                        entity_name=camp["name"], segment=SEGMENT))

        for i in range(first, last + 1):
            exhausted = served[i] < 1.0
            live_min = float(round(served[i] * _EXPECTED_LIVE_MIN, 1))
            budget_days.append(BudgetDayRow(
                date=dates[i], entity_id=camp["id"], daily_budget=cap,
                spend=float(round(day_spend[i], 2)), sales=float(day_sales[i]),
                impressions=float(day_impr[i]),
                exhausted_at_minute=_DAY_START_MIN + live_min if exhausted else None,
                live_minutes=live_min if exhausted else _EXPECTED_LIVE_MIN,
                expected_live_minutes=_EXPECTED_LIVE_MIN,
                platform_held=bool(exhausted), status=camp["status"]))

        w30 = slice(days - 30, days)
        sp30 = float(day_spend[w30].sum())
        own_share = None
        if kws[0][0] is not None:
            tot = sum(float(spend[k].sum()) for k in range(len(kws)))
            own = sum(float(spend[k].sum()) for k, kwrow in enumerate(kws) if kwrow[1] == _OWN)
            own_share = own / tot if tot > 0 else None
        halo_sales30 = sum(r.indirect_sales or 0.0 for r in perf
                           if r.entity_id == camp["id"] and r.date >= dates[days - 30])
        entities.append(Entity(
            entity_id=camp["id"], name=camp["name"], advertiser_id=ADVERTISER_ID,
            ad_type=camp["ad_type"], status=camp["status"], daily_budget=cap, segment=SEGMENT,
            is_pure=True, auto_targeted=camp["ad_type"] == "recommendation",
            start_date=dates[first], product_ids=tuple(camp["products"]),
            spend_3d=float(day_spend[days - 3:].sum()), spend_7d=float(day_spend[days - 7:].sum()),
            spend_window=sp30,
            roas_primary=(float(day_sales[w30].sum()) + halo_sales30) / sp30 if sp30 > 0 else None,
            roas_direct=float(day_sales[w30].sum()) / sp30 if sp30 > 0 else None,
            own_brand_share=own_share))

    # Market view: keyword x brand x day.
    market: list[MarketRow] = []
    for kw in sorted(own_kw_impr):
        role = kw_role[kw]
        ad_sov, org_share = _SOV[role]
        expected = kw_expected[kw] * float(weekday.mean())
        rival_ad_total = expected * (1.0 - ad_sov) / ad_sov
        organic_pool = expected * 3.0
        for i, d in enumerate(dates):
            wd = float(weekday[i]) / float(weekday.mean())
            market.append(MarketRow(
                date=d, targeting_unit=kw, brand=OWN_BRAND, is_own=True,
                ad_impressions=float(own_kw_impr[kw][i]),
                organic_impressions=float(np.rint(organic_pool * org_share * wd * rng.lognormal(0.0, 0.05))),
                segment=SEGMENT, unit_role=role, organic_position=2.0 if role == _OWN else 8.0))
            for rival, w in RIVALS:
                ad = rival_ad_total * w * wd * rng.lognormal(0.0, 0.08)
                if kw == _SPIKE_KW and rival == _SPIKE_RIVAL and i >= spike_from:
                    ad *= _SPIKE_RIVAL_MULT
                org = organic_pool * (1.0 - org_share) * w * wd * rng.lognormal(0.0, 0.05)
                market.append(MarketRow(
                    date=d, targeting_unit=kw, brand=rival, is_own=False,
                    ad_impressions=float(np.rint(ad)), organic_impressions=float(np.rint(org)),
                    segment=SEGMENT, unit_role=role))

    # Market offtake: own (product x geo x day, ad sales x organic multiple) and rivals (brand x geo x day).
    offtake: list[OfftakeRow] = []
    own_total = sum(float(a.sum()) for a in own_sales.values()) * _ORGANIC_MULT / days
    for sku, arr in own_sales.items():
        list_ratio = _PRODUCTS[sku][2] / _PRODUCTS[sku][1]
        noise = rng.lognormal(0.0, 0.05, size=(days, n_geo))
        for i, d in enumerate(dates):
            for j, g in enumerate(geo_names):
                s = float(round(arr[i, j] * _ORGANIC_MULT * noise[i, j], 2))
                offtake.append(OfftakeRow(
                    date=d, brand=OWN_BRAND, is_own=True, sales=s,
                    units=float(round(s / _PRODUCTS[sku][1], 2)), segment=SEGMENT,
                    product_id=sku, geo=g, list_value=round(s * list_ratio, 2)))
    for (rival, w), mult in zip(RIVALS, (1.2, 0.8, 0.4)):
        sku = next(s for s, v in _PRODUCTS.items() if v[0] == rival)
        noise = rng.lognormal(0.0, 0.06, size=(days, n_geo))
        for i, d in enumerate(dates):
            for j, g in enumerate(geo_names):
                s = float(round(own_total * mult * geo_w[j] * weekday[i] / weekday.mean() * noise[i, j], 2))
                offtake.append(OfftakeRow(
                    date=d, brand=rival, is_own=False, sales=s,
                    units=float(round(s / _PRODUCTS[sku][1], 2)), segment=SEGMENT,
                    product_id=sku, geo=g,
                    list_value=round(s * _PRODUCTS[sku][2] / _PRODUCTS[sku][1], 2)))

    products = [
        Product(product_id=sku, brand=brand, is_own=brand == OWN_BRAND, segment=SEGMENT,
                title=f"{brand} {sku}", list_price=lp, first_sale_date=start - timedelta(days=365))
        for sku, (brand, _price, lp) in _PRODUCTS.items()
    ]

    spike_start = dates[spike_from]
    drop_start = dates[days - _DROP_DAYS]
    changes = [
        ChangeEvent(timestamp=datetime.combine(dates[min(40, days - 1)], time(9, 0)),
                    entity_id="cmp-paused", field="status", old="ACTIVE", new="PAUSED",
                    source="operator", automated=False, verified=True),
        ChangeEvent(timestamp=datetime.combine(spike_start, time(6, 0)), entity_id=_SPIKE_ENTITY,
                    field="bid", old=round(150.0 / 0.95, 2), new=round(150.0 * _SPIKE_MULT / 0.95, 2),
                    source="position-bidder", automated=True, verified=True,
                    targeting_unit=_SPIKE_KW),
    ]

    events = {
        "cpm_spike": InjectedEvent(
            name="cpm_spike", kind="cpm",
            description="CPM and bid 1.6x for the last 30 days on one keyword; position flat; "
                        "one rival's ad impressions 2.5x; conversion and price unchanged",
            entity_ids=(_SPIKE_ENTITY,), targeting_unit=_SPIKE_KW, start=spike_start, end=end_date,
            magnitude=_SPIKE_MULT,
            details={"rival": _SPIKE_RIVAL, "rival_ad_impression_mult": _SPIKE_RIVAL_MULT}),
        "availability_drop": InjectedEvent(
            name="availability_drop", kind="availability",
            description="availability of one product in one geo falls from 0.95 to 0.45 for the "
                        "last 21 days; conversion on its rows scales with availability",
            entity_ids=("cmp-brand", "cmp-reco"), geo=_DROP_GEO, product_id=_DROP_SKU,
            start=drop_start, end=end_date, magnitude=_AVAIL_DROP,
            details={"baseline": _AVAIL_BASE, "affected_keyword": "ownbrand protein"}),
        "budget_capped_winner": InjectedEvent(
            name="budget_capped_winner", kind="budget",
            description="profitable campaign whose daily budget covers about a third of demand; "
                        "exhausts before midday every day while its status reads budget-held",
            entity_ids=(_WINNER,), start=start, end=end_date, magnitude=0.33,
            details={"expected_live_minutes": _EXPECTED_LIVE_MIN}),
        "bleeding_keyword": InjectedEvent(
            name="bleeding_keyword", kind="bleed",
            description="competitor keyword with ROAS about 0.35 on material spend for the whole "
                        "period, few new customers, negligible halo",
            entity_ids=(_BLEED_ENTITY,), targeting_unit=_BLEED_KW, start=start, end=end_date,
            magnitude=0.35),
        "own_overlap": InjectedEvent(
            name="own_overlap", kind="collision",
            description="two own campaigns bid the same keyword for the same product in every "
                        "geo on every day; the second campaign has the worse ROAS on it",
            entity_ids=_OVERLAP_ENTITIES, targeting_unit=_OVERLAP_KW, product_id="sku-a1",
            start=start, end=end_date, details={"better": "cmp-generic", "worse": "cmp-overlap"}),
    }

    return SyntheticDataset(
        profile=synthetic_profile(), perf=perf, entities=entities, targeting_units=units_out,
        products=products, market=market, offtake=offtake, shelf=shelf, budget_days=budget_days,
        change_events=changes, seed=seed, start_date=start, end_date=end_date, events=events)


class SyntheticAdapter(BaseAdapter):
    """``PlatformAdapter`` over the synthetic dataset (``self.dataset.events`` holds the ground truth)."""

    def __init__(self, seed: int = DEFAULT_SEED, days: int = 90,
                 end_date: date = DEFAULT_END_DATE, scale: float = 3.0) -> None:
        super().__init__(generate(seed=seed, days=days, end_date=end_date, scale=scale))

    @property
    def events(self) -> dict[str, InjectedEvent]:
        """The planted events of the synthetic dataset, by name."""
        return self.dataset.events  # type: ignore[attr-defined]
