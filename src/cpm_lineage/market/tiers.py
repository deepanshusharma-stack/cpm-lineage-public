"""Price-band tiers, tier position, pricing power, tier whitespace and
tier share trend.

When a catalogue has no usable sub-segment, tiers are derived as
sales-weighted price terciles: inspectable, not a clustering.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from ..adapters.base import PlatformProfile
from ..schema import OfftakeRow, Window, WindowPair
from ._util import diff, in_window, pct, pct_growth
from .config import MarketConfig

__all__ = [
    "TIER_NAMES", "PriceTier", "PriceTiers", "TierPower", "WhitespaceGap", "TierTrend",
    "price_tiers", "tier_of", "tier_position", "elasticity", "tier_pricing_power", "tier_whitespace",
    "tier_share_trend",
]

_MC = MarketConfig()
TIER_NAMES: tuple[str, str, str] = ("value", "mid", "premium")


@dataclass(frozen=True)
class PriceTier:
    """One price band of the segment."""

    name: str
    price_min: float
    price_max: float
    n_products: int
    sales: float
    contribution_pct: float | None       # tier sales / segment sales
    own_share_pct: float | None
    own_share_delta_pp: float | None
    growth_pct: float | None             # the tier's own period-over-period growth
    pp_worth: float                      # value of one share point per window = tier sales / 100
    band: str = "absent"
    trend: str = "flat"


@dataclass(frozen=True)
class PriceTiers:
    """Tercile tiers plus fixed product membership."""

    tiers: tuple[PriceTier, ...] = ()
    cuts: tuple[float, float] | None = None
    membership: Mapping[str, str] = field(default_factory=dict)   # product id -> tier name
    reason: str | None = None


def _product_prices(rows: Iterable[OfftakeRow], window: Window | None) -> dict[str, tuple[float, float]]:
    """``{product: (price, sales)}`` with price = list value / units (selling value when no list value)."""
    acc: dict[str, list[float]] = {}
    for o in rows:
        if not o.product_id or not in_window(o.date, window):
            continue
        a = acc.setdefault(o.product_id, [0.0, 0.0, 0.0])
        a[0] += o.list_value if o.list_value is not None else (o.sales or 0.0)
        a[1] += o.units or 0.0
        a[2] += o.sales or 0.0
    return {p: (a[0] / a[1], a[2]) for p, a in acc.items() if a[1] > 0 and a[0] / a[1] > 0 and a[2] != 0}


def _cuts(prices: Mapping[str, tuple[float, float]], cfg: MarketConfig) -> tuple[float, float] | None:
    if len(prices) < cfg.tier_min_products:
        return None
    order = sorted(prices.values())
    total = sum(s for _, s in order)
    if total <= 0:
        return None
    cum, c0, c1 = 0.0, None, None
    for price, sales in order:
        cum += sales
        if c0 is None and cum >= total / 3.0:
            c0 = price
        if c1 is None and cum >= 2.0 * total / 3.0:
            c1 = price
            break
    if c0 is None or c1 is None or not c0 < c1:
        return None
    return c0, c1


def tier_of(price: float, cuts: tuple[float, float]) -> str:
    """Tier name for a price: at or below the first cut ``value``, at or
    below the second ``mid``, else ``premium``."""
    return TIER_NAMES[0] if price <= cuts[0] else TIER_NAMES[1] if price <= cuts[1] else TIER_NAMES[2]


def tier_position(share_pct: float | None, share_delta_pp: float | None,
                  cfg: MarketConfig | None = None) -> tuple[str, str]:
    """Band and trend of the own position in a tier.

    Band: ``owned >= 85``, ``led >= 55``, ``contested >= 25``,
    ``minority >= 1``, else ``absent``.  Trend: ``rising`` when the share
    moved more than ``tier_trend_pp``, ``falling`` below minus that, else
    ``flat``.  Returns ``(band, trend)``.
    """
    cfg = cfg or _MC
    s = share_pct or 0.0
    band = ("owned" if s >= cfg.tier_owned_pct else "led" if s >= cfg.tier_led_pct
            else "contested" if s >= cfg.tier_contested_pct else "minority" if s >= cfg.tier_minority_pct
            else "absent")
    d = share_delta_pp or 0.0
    trend = "rising" if d > cfg.tier_trend_pp else "falling" if d < -cfg.tier_trend_pp else "flat"
    return band, trend


def price_tiers(offtake_rows: Iterable[OfftakeRow] | None, windows: WindowPair,
                cfg: MarketConfig | None = None) -> PriceTiers:
    """Derive value / mid / premium tiers and the own position in each.

    1. per product ``price = sum(list value) / sum(units)`` over the
       current window; keep ``price > 0`` and non-zero sales;
    2. sort by price and walk cumulative current sales: the cut points are
       the prices at which the cumulative first reaches 1/3 and 2/3 of the
       segment (sales-weighted terciles);
    3. require ``tier_min_products`` products and ``cut0 < cut1``;
    4. per tier: contribution, own share and its change, the tier's
       growth, ``pp_worth = tier sales / 100`` and :func:`tier_position`.

    Returns :class:`PriceTiers`; ``reason`` is set (and ``tiers`` empty)
    when the tiers cannot be derived.
    """
    cfg = cfg or _MC
    rows = [o for o in (offtake_rows or ()) if o.product_id]
    prices = _product_prices(rows, windows.current)
    cuts = _cuts(prices, cfg)
    if cuts is None:
        return PriceTiers(reason=f"needs at least {cfg.tier_min_products} priced products with distinct tercile cuts")
    member = {p: tier_of(v[0], cuts) for p, v in prices.items()}
    acc = {n: [0.0, 0.0, 0.0, 0.0] for n in TIER_NAMES}  # sales cur, prev, own cur, own prev
    for o in rows:
        t = member.get(o.product_id or "")
        if t is None:
            continue
        i = 0 if in_window(o.date, windows.current) else 1 if in_window(o.date, windows.prior) else None
        if i is None:
            continue
        acc[t][i] += o.sales or 0.0
        if o.is_own:
            acc[t][2 + i] += o.sales or 0.0
    seg = sum(a[0] for a in acc.values())
    tiers = []
    for n in TIER_NAMES:
        a = acc[n]
        ps = [prices[p][0] for p, t in member.items() if t == n]
        sh, shp = pct(a[2], a[0]), pct(a[3], a[1])
        d = diff(sh, shp)
        band, trend = tier_position(sh, d, cfg)
        tiers.append(PriceTier(n, min(ps) if ps else 0.0, max(ps) if ps else 0.0, len(ps), a[0], pct(a[0], seg),
                               sh, d, pct_growth(a[0], a[1]), a[0] / 100.0, band, trend))
    return PriceTiers(tuple(tiers), cuts, member)


def elasticity(pairs: Sequence[tuple[float, float]], cfg: MarketConfig | None = None
               ) -> tuple[float, float, int] | None:
    """Plain OLS of ``y`` on ``x``.

    Needs ``elasticity_min_points`` points and non-zero variance on both
    axes.  Returns ``(slope = cov / var_x, r = cov / (sd_x sd_y), n)`` or
    ``None``.  Correlation, not causation.
    """
    cfg = cfg or _MC
    n = len(pairs)
    if n < cfg.elasticity_min_points:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    vx = sum((p[0] - mx) ** 2 for p in pairs) / n
    vy = sum((p[1] - my) ** 2 for p in pairs) / n
    if vx <= 0 or vy <= 0:
        return None
    cov = sum((p[0] - mx) * (p[1] - my) for p in pairs) / n
    return cov / vx, cov / (vx ** 0.5 * vy ** 0.5), n


@dataclass(frozen=True)
class TierPower:
    """Weekly own-discount vs own-share relation inside one tier."""

    tier: str
    slope: float | None      # share points per discount point
    r: float | None
    n: int


def tier_pricing_power(offtake_rows: Iterable[OfftakeRow], end: date, membership: Mapping[str, str],
                       cfg: MarketConfig | None = None) -> list[TierPower]:
    """Per tier, regress weekly own share (%) on weekly own discount (%).

    Weekly points over the ``elasticity_weeks`` weeks ending at ``end``:
    ``x = 100 x (1 - own selling value / own list value)``, ``y = 100 x own
    sales / tier sales``; tier membership is fixed (from
    :func:`price_tiers`).  Reports ``slope, r, n`` via :func:`elasticity`
    (``None`` values when the fit is not possible).  Promo weeks dominate:
    read as correlation.
    """
    cfg = cfg or _MC
    start = end - timedelta(days=7 * cfg.elasticity_weeks - 1)
    acc: dict[tuple[str, int], list[float]] = {}
    for o in offtake_rows:
        t = membership.get(o.product_id or "")
        if t is None or not (start <= o.date <= end):
            continue
        a = acc.setdefault((t, (o.date - start).days // 7), [0.0, 0.0, 0.0, 0.0])
        a[0] += o.sales or 0.0
        if o.is_own:
            a[1] += o.sales or 0.0
            if o.list_value:
                a[2] += o.sales or 0.0
                a[3] += o.list_value
    out = []
    for t in TIER_NAMES:
        pts = [(100.0 * (1.0 - a[2] / a[3]), 100.0 * a[1] / a[0])
               for (tt, _), a in sorted(acc.items()) if tt == t and a[0] > 0 and a[3] > 0]
        fit = elasticity(pts, cfg)
        out.append(TierPower(t, fit[0] if fit else None, fit[1] if fit else None, len(pts)))
    return out


@dataclass(frozen=True)
class WhitespaceGap:
    """A price tier in a geo where the segment sells and the account barely lists."""

    geo: str
    tier: str
    pool: float              # tier sales in that geo
    own_share_pct: float


def tier_whitespace(offtake_rows: Iterable[OfftakeRow], window: Window | None = None, *,
                    profile: PlatformProfile | None = None, cfg: MarketConfig | None = None) -> list[WhitespaceGap]:
    """Find tier x geo listing gaps.

    For the top ``whitespace_top_geos`` geos by segment sales (aggregate
    buckets excluded), tiers are re-derived per geo; a gap is a tier with a
    positive pool where the own share is below
    ``whitespace_max_own_share_pct``.  Top ``whitespace_top_n`` by pool.
    A listing decision: no media needed.
    """
    cfg = cfg or _MC
    rows = [o for o in offtake_rows if o.geo and o.product_id and in_window(o.date, window)
            and not (profile is not None and profile.is_aggregate_geo(o.geo))]
    by_geo: dict[str, float] = {}
    for o in rows:
        by_geo[o.geo] = by_geo.get(o.geo, 0.0) + (o.sales or 0.0)  # type: ignore[index]
    gaps: list[WhitespaceGap] = []
    for g in sorted(by_geo, key=lambda k: (-by_geo[k], k))[: cfg.whitespace_top_geos]:
        grows = [o for o in rows if o.geo == g]
        prices = _product_prices(grows, None)
        cuts = _cuts(prices, cfg)
        if cuts is None:
            continue
        pool = {n: [0.0, 0.0] for n in TIER_NAMES}
        for o in grows:
            if o.product_id in prices:
                a = pool[tier_of(prices[o.product_id][0], cuts)]
                a[0] += o.sales or 0.0
                if o.is_own:
                    a[1] += o.sales or 0.0
        for n, a in pool.items():
            if a[0] > 0 and 100.0 * a[1] / a[0] < cfg.whitespace_max_own_share_pct:
                gaps.append(WhitespaceGap(g, n, a[0], 100.0 * a[1] / a[0]))
    gaps.sort(key=lambda x: (-x.pool, x.geo, x.tier))
    return gaps[: cfg.whitespace_top_n]


@dataclass(frozen=True)
class TierTrend:
    """Monthly own share of one tier (membership fixed from recent price)."""

    tier: str
    months: tuple[str, ...]               # "YYYY-MM"
    own_share_pct: tuple[float, ...]


def tier_share_trend(offtake_rows: Iterable[OfftakeRow], membership: Mapping[str, str],
                     cfg: MarketConfig | None = None) -> list[TierTrend]:
    """Monthly own share per tier with band membership fixed.

    Months whose segment volume is under ``trend_min_month_volume`` of the
    mean monthly volume are dropped (partial months); a tier needs
    ``trend_min_months`` usable months, otherwise it is omitted.
    """
    cfg = cfg or _MC
    acc: dict[tuple[str, str], list[float]] = {}
    vol: dict[str, float] = {}
    for o in offtake_rows:
        t = membership.get(o.product_id or "")
        if t is None:
            continue
        mth = f"{o.date.year:04d}-{o.date.month:02d}"
        vol[mth] = vol.get(mth, 0.0) + (o.sales or 0.0)
        a = acc.setdefault((t, mth), [0.0, 0.0])
        a[0] += o.sales or 0.0
        if o.is_own:
            a[1] += o.sales or 0.0
    if not vol:
        return []
    mean = sum(vol.values()) / len(vol)
    keep = sorted(m for m, v in vol.items() if v >= cfg.trend_min_month_volume * mean)
    out = []
    for t in TIER_NAMES:
        ms = [m for m in keep if acc.get((t, m), [0.0])[0] > 0]
        if len(ms) >= cfg.trend_min_months:
            out.append(TierTrend(t, tuple(ms), tuple(100.0 * acc[(t, m)][1] / acc[(t, m)][0] for m in ms)))
    return out
