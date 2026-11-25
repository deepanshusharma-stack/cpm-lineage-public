"""Small descriptive reads of the segment (no decisions)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from ..adapters.base import PlatformProfile
from ..schema import MarketRow, OfftakeRow, PerfRow, Window, WindowPair
from ._util import diff, in_window, norm, pct
from .config import MarketConfig

__all__ = ["RivalSlot", "GeoRoleSov", "SkuFootprint", "keyword_top_rivals", "geo_role_sov", "spend_by_segment",
           "top_search_terms", "sku_footprint"]

_MC = MarketConfig()


@dataclass(frozen=True)
class RivalSlot:
    """The largest rival advertiser on one keyword."""

    keyword: str
    brand: str
    ad_impressions: float
    ad_share_pct: float | None
    organic_position: float | None   # the feed carries organic position only; ad slot is not reported


def keyword_top_rivals(market_rows: Iterable[MarketRow], window: Window | None = None,
                       cfg: MarketConfig | None = None) -> dict[str, RivalSlot]:
    """Per keyword, the top rival by ad impressions (above
    ``rival_slot_min_ad_impressions``) with its ad share and
    impression-weighted organic position."""
    cfg = cfg or _MC
    acc: dict[tuple[str, str], list[float]] = {}
    tot: dict[str, float] = {}
    for m in market_rows:
        if not in_window(m.date, window):
            continue
        k = norm(m.targeting_unit)
        ad = m.ad_impressions or 0.0
        tot[k] = tot.get(k, 0.0) + ad
        if m.is_own:
            continue
        a = acc.setdefault((k, m.brand), [0.0, 0.0, 0.0])
        a[0] += ad
        if m.organic_position is not None and m.impressions > 0:
            a[1] += m.organic_position * m.impressions
            a[2] += m.impressions
    out: dict[str, RivalSlot] = {}
    for (k, b), a in sorted(acc.items(), key=lambda kv: (-kv[1][0], kv[0])):
        if k not in out and a[0] > cfg.rival_slot_min_ad_impressions:
            out[k] = RivalSlot(k, b, a[0], pct(a[0], tot[k]), a[1] / a[2] if a[2] > 0 else None)
    return out


@dataclass(frozen=True)
class GeoRoleSov:
    """Own ad and organic share for one geo x keyword type, percent."""

    geo: str
    role: str
    ad_sov_pct: float | None
    organic_sov_pct: float | None
    ad_sov_delta_pp: float | None
    organic_sov_delta_pp: float | None


def geo_role_sov(market_rows: Iterable[MarketRow], windows: WindowPair, roles: Mapping[str, str] | None = None, *,
                 profile: PlatformProfile | None = None, cfg: MarketConfig | None = None) -> list[GeoRoleSov]:
    """Own ad / organic share of voice by geo and keyword type, with deltas,
    for the top ``geo_role_top_geos`` geos by impressions (aggregate
    buckets excluded).  Needs a geo-grain impression feed; empty otherwise."""
    cfg = cfg or _MC
    acc: dict[tuple[str, str], list[float]] = {}
    size: dict[str, float] = {}
    for m in market_rows:
        if not m.geo or (profile is not None and profile.is_aggregate_geo(m.geo)):
            continue
        i = 0 if in_window(m.date, windows.current) else 1 if in_window(m.date, windows.prior) else None
        if i is None:
            continue
        role = (roles or {}).get(norm(m.targeting_unit)) or m.unit_role or "generic"
        a = acc.setdefault((m.geo, role), [0.0] * 8)  # per period: own ad, all ad, own org, all org
        ad, og = m.ad_impressions or 0.0, m.organic_impressions or 0.0
        a[4 * i + 1] += ad
        a[4 * i + 3] += og
        if m.is_own:
            a[4 * i] += ad
            a[4 * i + 2] += og
        if i == 0:
            size[m.geo] = size.get(m.geo, 0.0) + ad + og
    top = sorted(size, key=lambda g: (-size[g], g))[: cfg.geo_role_top_geos]
    out = []
    for (g, role), a in sorted(acc.items()):
        if g in top:
            ad_c, og_c, ad_p, og_p = pct(a[0], a[1]), pct(a[2], a[3]), pct(a[4], a[5]), pct(a[6], a[7])
            out.append(GeoRoleSov(g, role, ad_c, og_c, diff(ad_c, ad_p), diff(og_c, og_p)))
    return out


def spend_by_segment(perf_rows: Iterable[PerfRow], window: Window | None = None) -> dict[str | None, float]:
    """Own ad spend per segment (``None`` key = rows with no segment)."""
    out: dict[str | None, float] = {}
    for r in perf_rows:
        if in_window(r.date, window):
            out[r.segment] = out.get(r.segment, 0.0) + (r.spend or 0.0)
    return out


def top_search_terms(market_rows: Iterable[MarketRow], window: Window | None = None,
                     top: int = 20) -> list[tuple[str, float]]:
    """Largest keywords by total impressions (all brands)."""
    vol: dict[str, float] = {}
    for m in market_rows:
        if in_window(m.date, window):
            k = norm(m.targeting_unit)
            vol[k] = vol.get(k, 0.0) + (m.impressions or 0.0)
    return sorted(vol.items(), key=lambda kv: (-kv[1], kv[0]))[:top]


@dataclass(frozen=True)
class SkuFootprint:
    """Range breadth of one brand in the segment."""

    brand: str
    is_own: bool
    n_products: int
    sales: float
    sales_per_product: float | None


def sku_footprint(offtake_rows: Iterable[OfftakeRow], window: Window | None = None) -> list[SkuFootprint]:
    """Per brand: distinct selling products, sales and sales per product."""
    acc: dict[tuple[str, bool], tuple[set[str], list[float]]] = {}
    for o in offtake_rows:
        if in_window(o.date, window) and o.brand:
            ids, s = acc.setdefault((o.brand, bool(o.is_own)), (set(), [0.0]))
            s[0] += o.sales or 0.0
            if o.product_id and (o.sales or 0) > 0:
                ids.add(o.product_id)
    out = [SkuFootprint(b, own, len(ids), s[0], s[0] / len(ids) if ids else None)
           for (b, own), (ids, s) in acc.items()]
    return sorted(out, key=lambda f: (-f.sales, f.brand))
