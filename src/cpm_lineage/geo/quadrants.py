"""Geo quadrants, investment tiers and account posture.

Each geo is placed on two axes: is the account's market share there at
or above its national share, and is its paid share of voice there at or
above its own market share there.  The cell gives a posture and a lever
order; a tier says whether to fix, build or scale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from ..adapters.base import PlatformProfile
from ..config import AvailabilityPolicy, Economics
from ..market._util import diff, in_window, pct, pct_growth, percentile, ratio
from ..schema import MarketRow, OfftakeRow, PerfRow, ShelfRow, WindowPair
from ..windows import latest_date, trailing_windows
from .config import GeoConfig

__all__ = [
    "QUADRANTS", "GeoRow", "AvailabilityStanding", "Classification", "GeoQuadrantResult",
    "quadrant", "availability_standing", "geo_tier", "account_posture", "rank_label", "geo_quadrants",
]

_GC = GeoConfig()

# (share_high, voice_high) -> (label, posture, lever order)
QUADRANTS: Mapping[tuple[bool, bool], tuple[str, str, tuple[str, ...]]] = {
    (True, True): ("Q1 Market Leader", "Defend",
                   ("Availability (protect)", "Pricing (maintain)", "Visibility (optimise down)",
                    "Assortment (extend)")),
    (True, False): ("Q2 Organic Champion", "Harvest",
                    ("Visibility (selective push)", "Availability", "Assortment (defend)", "Pricing (hold)")),
    (False, True): ("Q3 Ad-Dependent", "Convert",
                    ("Pricing (investigate gap)", "Assortment (fit check)", "Visibility (restructure)",
                     "Availability")),
    (False, False): ("Q4 Under-Invested", "Build",
                     ("Assortment", "Pricing (competitive entry)", "Visibility (targeted)", "Availability")),
}
_EXIT = ("Q4 Under-Invested (Exit)", "Exit", ("Deprioritise spend",))


def quadrant(
    share_pct: float,
    national_share_pct: float,
    ad_sov_pct: float | None,
    segment_growth_pct: float | None = None,
    cfg: GeoConfig | None = None,
) -> tuple[str, str, str, tuple[str, ...]]:
    """Classify one geo.

    ``share_high = share >= national share``;
    ``voice_high = ad_sov is known and share > 0 and ad_sov >= share``
    (voice against the geo's OWN share, not national voice; unknown voice
    counts as low).  An under-invested geo whose segment is shrinking
    faster than ``exit_segment_growth_pct`` becomes the Exit variant.
    Returns ``(key, label, posture, lever order)`` with key ``"Q1".."Q4"``.
    """
    cfg = cfg or _GC
    hi = share_pct >= national_share_pct
    voice = ad_sov_pct is not None and share_pct > 0 and ad_sov_pct >= share_pct
    label, posture, levers = QUADRANTS[(hi, voice)]
    key = label[:2]
    if key == "Q4" and segment_growth_pct is not None and segment_growth_pct < cfg.exit_segment_growth_pct:
        label, posture, levers = _EXIT
    return key, label, posture, levers


@dataclass(frozen=True)
class AvailabilityStanding:
    """Where the account's availability ranks among brands in one geo."""

    rank: int            # 1 = best
    n_brands: int
    p90: float           # availability of the brand at the top decile


def availability_standing(pairs: Sequence[tuple[float, bool]], cfg: GeoConfig | None = None
                          ) -> AvailabilityStanding | None:
    """Relative availability standing from ``(availability, is_own)`` per brand.

    Pass only brands with enough shelf checks.  ``None`` with fewer than
    ``standing_min_brands`` brands or no own row.  Sorted descending:
    ``rank`` = the own position (1-based), ``p90 = sorted[max(0, (n - 1) //
    10)]``.  Additive context only: the absolute floor still gates.
    """
    cfg = cfg or _GC
    if len(pairs) < cfg.standing_min_brands or not any(own for _, own in pairs):
        return None
    order = sorted(pairs, key=lambda p: -p[0])
    rank = next(i + 1 for i, p in enumerate(order) if p[1])
    return AvailabilityStanding(rank, len(order), order[max(0, (len(order) - 1) // 10)][0])


def geo_tier(
    availability: float | None,
    roas: float | None,
    share_high: bool,
    roas_p25: float | None,
    roas_p50: float | None,
    availability_floor: float | None,
    standing: AvailabilityStanding | None = None,
) -> tuple[str, str]:
    """Investment tier of one geo, first match:

    1. availability below the floor -> ``Fix first`` (when the account is
       already rank 1 on availability there the gap is geo-wide: escalate
       and restrict ads to in-stock products; otherwise fix supply first);
    2. ROAS below the geo-ROAS first quartile -> ``Fix first`` (clean up
       ROAS before investing);
    3. share high and ROAS above the geo-ROAS median -> ``Scale`` (raise
       budget, after the utilisation check);
    4. otherwise -> ``Build`` (lift bids and add keywords).

    A gate whose input is ``None`` is skipped.  Returns ``(tier, why)``.
    A low share has the same outcome as the default, so it has no branch
    of its own (rule 4 covers it).
    """
    if availability is not None and availability_floor is not None and availability < availability_floor:
        if standing is not None and standing.rank == 1:
            return "Fix first", ("geo-wide serviceability gap: escalate, and restrict ads to in-stock products")
        return "Fix first", "fix supply first: availability is below the floor"
    if roas is not None and roas_p25 is not None and roas < roas_p25:
        return "Fix first", "clean up ROAS before investing: bottom quartile of geos"
    if share_high and roas is not None and roas_p50 is not None and roas > roas_p50:
        return "Scale", "raise budget, after the utilisation check"
    return "Build", "lift bids and add keywords"


def rank_label(national_share_pct: float | None, cfg: GeoConfig | None = None) -> str:
    """Approximate rank label from national share: ``> 25`` Category
    Leader, ``>= 10`` Contender, ``>= 3`` Mid-Tier, else Niche/Emerging.
    A true cross-brand rank is not computed."""
    cfg = cfg or _GC
    s = national_share_pct or 0.0
    if s > cfg.rank_leader_pct:
        return "Category Leader"
    if s >= cfg.rank_contender_pct:
        return "Contender"
    if s >= cfg.rank_mid_pct:
        return "Mid-Tier"
    return "Niche/Emerging"


def account_posture(
    quadrant_sales_pct: Mapping[str, float],
    gaining_pct: float,
    national_segment_growth_pct: float | None = None,
    cfg: GeoConfig | None = None,
) -> str:
    """Account posture from the quadrant mix, first match.

    ``q1..q4`` = percent of OWN SALES (not geo count) in each quadrant;
    ``pg`` = percent of geos gaining share.

    1. ``q1 > 60 and pg > 50`` -> Defending
    2. ``pg >= 75`` -> Gaining
    3. ``q4 > 40 and segment growth > 15 and pg > 50`` -> Building
    4. ``q1 + q3 > 50 and pg < 50`` -> Stalling
    5. ``q2 + q4 > 50 or pg < 25`` -> Under Pressure
    6. else Unclassified
    """
    cfg = cfg or _GC
    q1, q2, q3, q4 = (quadrant_sales_pct.get(k, 0.0) for k in ("Q1", "Q2", "Q3", "Q4"))
    pg = gaining_pct
    if q1 > cfg.posture_defend_q1_pct and pg > cfg.posture_defend_gaining_pct:
        return "Defending"
    if pg >= cfg.posture_gaining_pct:
        return "Gaining"
    if (q4 > cfg.posture_build_q4_pct and national_segment_growth_pct is not None
            and national_segment_growth_pct > cfg.posture_build_growth_pct and pg > cfg.posture_build_gaining_pct):
        return "Building"
    if q1 + q3 > cfg.posture_stall_pct and pg < cfg.posture_stall_gaining_pct:
        return "Stalling"
    if q2 + q4 > cfg.posture_pressure_pct or pg < cfg.posture_pressure_gaining_pct:
        return "Under Pressure"
    return "Unclassified"


@dataclass(frozen=True)
class GeoRow:
    """One geo's market position (shares percent, availability 0..1)."""

    geo: str
    share_pct: float
    share_prev_pct: float | None
    share_trend_pp: float | None
    segment_sales: float
    own_sales: float
    segment_growth_pct: float | None
    availability: float | None = None
    availability_delta: float | None = None
    availability_rank: int | None = None
    availability_n_brands: int | None = None
    availability_p90: float | None = None
    ad_sov_pct: float | None = None
    ad_sov_delta_pp: float | None = None
    organic_sov_pct: float | None = None      # own organic / all impressions
    overall_sov_pct: float | None = None
    roas: float | None = None
    quadrant: str = ""                        # "Q1".."Q4"
    quadrant_label: str = ""
    posture: str = ""
    lever_priority: tuple[str, ...] = ()
    primary_lever: str = ""
    tier: str = ""
    tier_reason: str = ""


@dataclass(frozen=True)
class Classification:
    """Account-level read of the geo map."""

    rank: str
    posture: str
    combined: str
    rank_note: str = "approximated from own share; a true cross-brand rank is not computed"


@dataclass(frozen=True)
class GeoQuadrantResult:
    """Geo quadrant map plus the account posture."""

    national_share_pct: float | None = None
    national_ad_sov_pct: float | None = None
    national_segment_growth_pct: float | None = None
    availability_floor: float | None = None
    geos: tuple[GeoRow, ...] = ()
    quadrant_dist: Mapping[str, float] = field(default_factory=dict)   # % of own sales per quadrant
    gaining_pct: float | None = None                                   # % of geos gaining share
    classification: Classification | None = None
    roas_p25: float | None = None
    roas_p50: float | None = None
    reason: str | None = None


def _pair(rows: Sequence, windows: WindowPair | None, days: int) -> WindowPair | None:
    if windows is not None:
        return windows
    end = latest_date(rows)
    return trailing_windows(end, days) if end is not None else None


def geo_quadrants(
    offtake_rows: Iterable[OfftakeRow] | None,
    windows: WindowPair | None = None,
    *,
    shelf_rows: Iterable[ShelfRow] | None = None,
    market_rows: Iterable[MarketRow] | None = None,
    perf_rows: Iterable[PerfRow] | None = None,
    profile: PlatformProfile | None = None,
    availability: AvailabilityPolicy | None = None,
    economics: Economics | None = None,
    cfg: GeoConfig | None = None,
) -> GeoQuadrantResult:
    """Build the geo quadrant map, tiers and account posture.

    Per geo, current window versus the prior equal window (when
    ``windows`` is ``None`` each feed is anchored to ITS OWN latest date
    over ``window_days``; pass a ``WindowPair`` to align
    them):

    * ``share = 100 x own sales / segment sales``, its prior value and
      trend, segment growth; geos with negligible segment sales are dropped;
    * availability ``sum(available x w) / sum(checks x w)`` over own
      products, ``w`` = the row's velocity weight (floored for active
      products, 1 for inactive ones), and its change;
    * ad, organic (``own organic / all impressions``) and overall share of
      voice; ROAS = own direct sales / own spend.

    National share and voice include the aggregate ("others") bucket;
    geo rankings exclude it.  ``profile.rollup_geo`` is applied before
    grouping.  Classification per geo by :func:`quadrant`; tier by
    :func:`geo_tier` with the availability floor
    ``availability.floor_for_share(national share)`` and the first quartile
    and median of geo ROAS (both the median with fewer than
    ``tier_min_geos_for_quartile`` geos).  Posture by
    :func:`account_posture`.

    Degrades: without the sell-out feed the result carries ``reason``;
    without impressions ``voice_high`` is false; without spend the ROAS
    tier rules are skipped; without shelf data (or with the availability
    policy disabled) the supply rule is skipped.
    """
    cfg, economics = cfg or _GC, economics or Economics()
    availability = availability or AvailabilityPolicy()
    off = list(offtake_rows or ())
    wp = _pair(off, windows, cfg.window_days)
    if wp is None or not off:
        return GeoQuadrantResult(reason="no market sell-out feed: geo quadrants unavailable")

    def g(name: str | None) -> str | None:
        return profile.rollup_geo(name) if profile is not None else name

    def agg(name: str | None) -> bool:
        return profile.is_aggregate_geo(name) if profile is not None else False

    # --- sales: own cur, seg cur, own prev, seg prev
    sales: dict[str, list[float]] = {}
    nat = [0.0, 0.0, 0.0, 0.0]
    for o in off:
        i = 0 if in_window(o.date, wp.current) else 2 if in_window(o.date, wp.prior) else None
        if i is None:
            continue
        v = o.sales or 0.0
        nat[i + 1] += v
        if o.is_own:
            nat[i] += v
        geo = g(o.geo)
        if geo is None:
            continue
        a = sales.setdefault(geo, [0.0, 0.0, 0.0, 0.0])
        a[i + 1] += v
        if o.is_own:
            a[i] += v
    if nat[1] <= 0:
        return GeoQuadrantResult(reason="no segment sales in the current window")
    nat_share = pct(nat[0], nat[1])
    nat_growth = pct_growth(nat[1], nat[3])

    # --- availability: own weighted (cur / prev) and per-brand unweighted (cur)
    shelf = list(shelf_rows or ())
    sp = _pair(shelf, windows, cfg.window_days)
    own_av: dict[str, list[float]] = {}
    brand_av: dict[str, dict[tuple[str, bool], list[float]]] = {}
    for s in shelf:
        if sp is None or not s.check_count or s.available_count is None:
            continue
        i = 0 if in_window(s.date, sp.current) else 2 if in_window(s.date, sp.prior) else None
        geo = g(s.geo)
        if i is None or geo is None:
            continue
        if s.is_own:
            w = max(s.weight, cfg.availability_weight_floor) if s.is_active else 1.0
            a = own_av.setdefault(geo, [0.0, 0.0, 0.0, 0.0])
            a[i] += s.available_count * w
            a[i + 1] += s.check_count * w
        if i == 0:
            bkey = ("~own" if s.is_own else (s.brand or ""), bool(s.is_own))
            b = brand_av.setdefault(geo, {}).setdefault(bkey, [0.0, 0.0])
            b[0] += s.available_count
            b[1] += s.check_count

    # --- impressions: own ad, all ad, own organic, all total (cur) + own ad, all ad (prev)
    mkt = list(market_rows or ())
    mp = _pair(mkt, windows, cfg.window_days)
    imp: dict[str, list[float]] = {}
    nimp = [0.0, 0.0]
    for m in mkt:
        if mp is None:
            break
        cur = in_window(m.date, mp.current)
        if not cur and not in_window(m.date, mp.prior):
            continue
        ad = m.ad_impressions or 0.0
        if cur:
            nimp[1] += ad
            if m.is_own:
                nimp[0] += ad
        geo = g(m.geo)
        if geo is None:
            continue
        a = imp.setdefault(geo, [0.0] * 7)
        if cur:
            a[1] += ad
            a[3] += m.impressions or 0.0
            if m.is_own:
                a[0] += ad
                a[2] += m.organic_impressions or 0.0
                a[6] += m.impressions or 0.0
        else:
            a[5] += ad
            if m.is_own:
                a[4] += ad
    nat_sov = pct(nimp[0], nimp[1])

    # --- own spend / direct sales per geo (cur)
    perf = list(perf_rows or ())
    pp = _pair(perf, windows, cfg.window_days)
    money: dict[str, list[float]] = {}
    for r in perf:
        geo = g(r.geo)
        if pp is None or geo is None or not in_window(r.date, pp.current):
            continue
        a = money.setdefault(geo, [0.0, 0.0])
        a[0] += r.spend or 0.0
        a[1] += r.sales or 0.0

    min_sales = economics.money(cfg.min_segment_sales_ref)
    ranked = [geo for geo, a in sales.items() if a[1] >= min_sales and not agg(geo)]
    roas_by = {geo: ratio(money[geo][1], money[geo][0]) for geo in ranked if geo in money}
    vals = [v for v in roas_by.values() if v is not None]
    p50 = percentile(vals, 0.5)
    p25 = percentile(vals, 0.25) if len(vals) >= cfg.tier_min_geos_for_quartile else p50
    floor = availability.floor_for_share(nat_share) if availability.enabled else None

    rows: list[GeoRow] = []
    for geo in ranked:
        a = sales[geo]
        share = 100.0 * a[0] / a[1]
        share_prev = pct(a[2], a[3])
        growth = pct_growth(a[1], a[3])
        av = own_av.get(geo)
        av_c = av[0] / av[1] if av and av[1] > 0 else None
        av_p = av[2] / av[3] if av and av[3] > 0 else None
        pairs = [(b[0] / b[1], own) for (_, own), b in brand_av.get(geo, {}).items()
                 if b[1] >= cfg.standing_min_checks]
        standing = availability_standing(pairs, cfg)
        m = imp.get(geo)
        sov = pct(m[0], m[1]) if m else None
        sov_prev = pct(m[4], m[5]) if m else None
        key, label, posture, levers = quadrant(share, nat_share or 0.0, sov, growth, cfg)
        roas = roas_by.get(geo)
        tier, why = geo_tier(av_c, roas, share >= (nat_share or 0.0), p25, p50, floor, standing)
        rows.append(GeoRow(
            geo=geo, share_pct=share, share_prev_pct=share_prev, share_trend_pp=diff(share, share_prev),
            segment_sales=a[1], own_sales=a[0], segment_growth_pct=growth,
            availability=av_c, availability_delta=diff(av_c, av_p),
            availability_rank=standing.rank if standing else None,
            availability_n_brands=standing.n_brands if standing else None,
            availability_p90=standing.p90 if standing else None,
            ad_sov_pct=sov, ad_sov_delta_pp=diff(sov, sov_prev),
            organic_sov_pct=pct(m[2], m[3]) if m else None, overall_sov_pct=pct(m[6], m[3]) if m else None,
            roas=roas, quadrant=key, quadrant_label=label, posture=posture, lever_priority=levers,
            primary_lever=levers[0], tier=tier, tier_reason=why))
    rows.sort(key=lambda r: (-r.own_sales, r.geo))
    if not rows:
        return GeoQuadrantResult(national_share_pct=nat_share, national_ad_sov_pct=nat_sov,
                                 national_segment_growth_pct=nat_growth, availability_floor=floor,
                                 reason="no geo-grain sell-out rows (aggregate buckets are not ranked)")
    own_total = sum(r.own_sales for r in rows)
    dist = {k: (100.0 * sum(r.own_sales for r in rows if r.quadrant == k) / own_total if own_total > 0 else 0.0)
            for k in ("Q1", "Q2", "Q3", "Q4")}
    gaining = 100.0 * sum(1 for r in rows if (r.share_trend_pp or 0.0) > 0) / len(rows)
    rank = rank_label(nat_share, cfg)
    posture = account_posture(dist, gaining, nat_growth, cfg)
    return GeoQuadrantResult(nat_share, nat_sov, nat_growth, floor, tuple(rows), dist, gaining,
                             Classification(rank, posture, f"{rank} / {posture}"), p25, p50)
