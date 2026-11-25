"""Geography drilldown: where did availability move, how does it compare
with competitors on the same shelf, and how much should each geo get.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Iterable, Mapping

from ..adapters.base import PlatformProfile
from ..config import AvailabilityPolicy
from ..market._util import in_window, norm, upper_pick
from ..schema import MarketRow, PerfRow, ShelfRow, Window, WindowPair
from .config import GeoConfig

__all__ = ["GeoDrillRow", "GeoDrillResult", "competitor_products", "geo_roas", "investment_tier", "geo_drill"]

_GC = GeoConfig()


@dataclass(frozen=True)
class GeoDrillRow:
    """One geo in the drilldown (availability 0..1; ``h`` = baseline, ``w`` = current)."""

    geo: str
    h_own: float | None
    w_own: float | None
    comp: float | None           # competitor availability, current bucket
    drop: float                  # h_own - w_own (0 when either is missing)
    gap: float | None            # w_own - comp (negative = trailing competitors)
    own_price: float | None
    comp_price: float | None
    checks: float                # own shelf checks across both buckets
    weight: float                # checks / sum(checks)
    impact: float                # weight x |drop|
    roas: float | None = None
    tier: str | None = None      # "FIX-FIRST" | "SCALE" | "BUILD" | "HOLD" | None


@dataclass(frozen=True)
class GeoDrillResult:
    rows: tuple[GeoDrillRow, ...] = ()
    roas_p25: float | None = None
    roas_p50: float | None = None
    reason: str | None = None


def competitor_products(market_rows: Iterable[MarketRow], keywords: Collection[str],
                        own_product_ids: Collection[str] = (), window: Window | None = None) -> set[str]:
    """Competitor products = non-own products appearing on the scope's
    keywords in the impression feed, minus anything in the own set."""
    kws = {norm(k) for k in keywords}
    own = set(own_product_ids)
    return {m.product_id for m in market_rows
            if m.product_id and not m.is_own and m.product_id not in own
            and norm(m.targeting_unit) in kws and in_window(m.date, window)}


def geo_roas(perf_rows: Iterable[PerfRow], window: Window | None = None,
             profile: PlatformProfile | None = None) -> dict[str, float]:
    """ROAS by geo over ``window`` (aggregate buckets excluded, rollup applied)."""
    acc: dict[str, list[float]] = {}
    for r in perf_rows:
        if not r.geo or not in_window(r.date, window):
            continue
        g = profile.rollup_geo(r.geo) if profile is not None else r.geo
        if g is None or (profile is not None and profile.is_aggregate_geo(g)):
            continue
        a = acc.setdefault(g, [0.0, 0.0])
        a[0] += r.spend or 0.0
        a[1] += r.sales or 0.0
    return {g: a[1] / a[0] for g, a in acc.items() if a[0] > 0}


def investment_tier(
    own_availability: float | None,
    roas: float | None,
    roas_p25: float | None,
    roas_p50: float | None,
    availability: AvailabilityPolicy | None = None,
    cfg: GeoConfig | None = None,
) -> str | None:
    """Investment tier of a geo in the drilldown, first match:

    * own availability below ``availability.block`` -> ``FIX-FIRST``
      (overrides everything: supply precedes demand);
    * geo ROAS unknown -> ``None``;
    * ``roas >= p50`` and availability (unknown counts as fine) at least
      ``tier_scale_min_availability`` -> ``SCALE``;
    * ``roas >= p25`` -> ``BUILD``;
    * else ``HOLD``.
    """
    cfg, availability = cfg or _GC, availability or AvailabilityPolicy()
    if availability.enabled and own_availability is not None and own_availability < availability.block:
        return "FIX-FIRST"
    if roas is None or roas_p25 is None or roas_p50 is None:
        return None
    av = 1.0 if own_availability is None else own_availability
    if roas >= roas_p50 and av >= cfg.tier_scale_min_availability:
        return "SCALE"
    if roas >= roas_p25:
        return "BUILD"
    return "HOLD"


def geo_drill(
    shelf_rows: Iterable[ShelfRow] | None,
    buckets: WindowPair,
    own_product_ids: Collection[str] | None = None,
    competitor_product_ids: Collection[str] | None = None,
    *,
    roas_by_geo: Mapping[str, float] | None = None,
    profile: PlatformProfile | None = None,
    availability: AvailabilityPolicy | None = None,
    cfg: GeoConfig | None = None,
) -> GeoDrillResult:
    """Rank geos by availability impact and assign an investment tier.

    For each geo and bucket (``buckets.prior`` = baseline, ``.current``)
    over shelf rows with checks: ``own = sum(available x w) / sum(checks x
    w)`` over own products and ``comp`` the same over competitor products
    (identical definition, matched on geo: like for like); ``w`` is the
    row's velocity weight (1 for inactive products).  Own products default
    to rows flagged own, competitors to the rest.

    Per geo: ``drop = h_own - w_own`` (0 if either is missing, which hides
    geos that only just appeared), ``gap = w_own - comp`` (competitor
    values from the current bucket only), ``weight = own checks / all own
    checks``, ``impact = weight x |drop|``.  Aggregate buckets and geos
    with no own availability are skipped.  Sorted by impact, top
    ``drill_top_n``.

    With ``roas_by_geo`` (see :func:`geo_roas`) each row gets
    :func:`investment_tier` against ``p50 = vals[n // 2]`` and ``p25 =
    vals[n // 4]`` of the sorted geo ROAS (upper-index picks, not
    interpolated; with very few geos they can coincide).
    """
    cfg = cfg or _GC
    own_ids = None if own_product_ids is None else set(own_product_ids)
    comp_ids = None if competitor_product_ids is None else set(competitor_product_ids) - (own_ids or set())
    # per geo: [h_own_av, h_own_ck, w_own_av, w_own_ck, w_comp_av, w_comp_ck, own_price_sum, own_price_n,
    #           comp_price_sum, comp_price_n, own raw checks]
    acc: dict[str, list[float]] = {}
    for s in shelf_rows or ():
        if not s.check_count or s.check_count <= 0 or s.available_count is None or not s.geo:
            continue
        i = 2 if in_window(s.date, buckets.current) else 0 if in_window(s.date, buckets.prior) else None
        if i is None:
            continue
        is_own = (s.product_id in own_ids) if own_ids is not None else bool(s.is_own)
        is_comp = (not is_own) and ((s.product_id in comp_ids) if comp_ids is not None else not s.is_own)
        if not is_own and not is_comp:
            continue
        geo = profile.rollup_geo(s.geo) if profile is not None else s.geo
        if geo is None or (profile is not None and profile.is_aggregate_geo(geo)):
            continue
        w = max(s.weight, 1.0) if s.is_active else 1.0
        a = acc.setdefault(geo, [0.0] * 11)
        if is_own:
            a[i] += s.available_count * w
            a[i + 1] += s.check_count * w
            a[10] += s.check_count
            if i == 2 and s.price is not None:
                a[6] += s.price
                a[7] += 1
        elif i == 2:
            a[4] += s.available_count * w
            a[5] += s.check_count * w
            if s.price is not None:
                a[8] += s.price
                a[9] += 1
    geos = {g: a for g, a in acc.items() if a[1] > 0 or a[3] > 0}
    if not geos:
        return GeoDrillResult(reason="no own shelf checks by geo in the drill buckets")
    total_checks = sum(a[10] for a in geos.values()) or 1.0
    vals = sorted(v for v in (roas_by_geo or {}).values() if v is not None)
    p50, p25 = upper_pick(vals, 2), upper_pick(vals, 4)
    rows: list[GeoDrillRow] = []
    for g, a in geos.items():
        h = a[0] / a[1] if a[1] > 0 else None
        w_ = a[2] / a[3] if a[3] > 0 else None
        comp = a[4] / a[5] if a[5] > 0 else None
        drop = (h - w_) if h is not None and w_ is not None else 0.0
        weight = a[10] / total_checks
        roas = (roas_by_geo or {}).get(g)
        tier = investment_tier(w_, roas, p25, p50, availability, cfg) if roas_by_geo is not None else None
        rows.append(GeoDrillRow(g, h, w_, comp, drop, (w_ - comp) if w_ is not None and comp is not None else None,
                                a[6] / a[7] if a[7] else None, a[8] / a[9] if a[9] else None,
                                a[10], weight, weight * abs(drop), roas, tier))
    rows.sort(key=lambda r: (-r.impact, r.geo))
    return GeoDrillResult(tuple(rows[: cfg.drill_top_n]), p25, p50)
