"""Product x geo push / defend map.

For the account's biggest geos and products: is the product a priority
there, and is its organic position already strong?  The pair decides
whether paid support is incremental (invest), re-buys a free win
(defend), or should be left alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from ..adapters.base import PlatformProfile
from ..config import Economics
from ..market._util import in_window, norm
from ..schema import MarketRow, OfftakeRow, PerfRow, Window
from .config import GeoConfig

__all__ = ["MapCell", "ProductGeoMap", "cell_decision", "product_geo_map"]

_GC = GeoConfig()


@dataclass(frozen=True)
class MapCell:
    """One product x geo cell."""

    product_id: str
    geo: str
    sales: float
    spend: float
    roas: float | None
    organic_position: float | None
    priority: bool
    strong: bool
    decision: str        # "INVEST" | "FIX FIRST" | "DEFEND" | "HARVEST" | "DEPRIORITISE" | ""


@dataclass(frozen=True)
class ProductGeoMap:
    geos: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    cells: tuple[MapCell, ...] = ()
    reason: str | None = None

    def cell(self, product_id: str, geo: str) -> MapCell | None:
        for c in self.cells:
            if c.product_id == product_id and c.geo == geo:
                return c
        return None


def cell_decision(priority: bool, strong: bool, roas: float | None, has_data: bool = True,
                  invest_bar: float = 1.0) -> str:
    """Decide one cell.

    ========  ======  ======================  =============================================
    priority  strong  extra                   decision
    ========  ======  ======================  =============================================
    (no data at all)                          ``""``
    yes       no      ROAS unknown or >= bar  ``INVEST`` (paid is incremental)
    yes       no      ROAS < bar              ``FIX FIRST`` (landing / price, not a slot problem)
    yes       yes                             ``DEFEND`` (bid to floor; paid re-buys the free win)
    no        yes                             ``HARVEST``
    no        no                              ``DEPRIORITISE``
    ========  ======  ======================  =============================================
    """
    if not has_data:
        return ""
    if priority and not strong:
        return "INVEST" if roas is None or roas >= invest_bar else "FIX FIRST"
    if priority:
        return "DEFEND"
    return "HARVEST" if strong else "DEPRIORITISE"


def product_geo_map(
    offtake_rows: Iterable[OfftakeRow] | None,
    window: Window | None = None,
    *,
    market_rows: Iterable[MarketRow] | None = None,
    perf_rows: Iterable[PerfRow] | None = None,
    roles: Mapping[str, str] | None = None,
    profile: PlatformProfile | None = None,
    economics: Economics | None = None,
    cfg: GeoConfig | None = None,
) -> ProductGeoMap:
    """Build the product x geo push / defend map.

    Top ``map_top_geos`` geos by own sales (aggregate buckets excluded) and
    the top ``map_top_products`` own products within them.  Per cell: own
    sales, ad spend and ROAS, and the impression-weighted ORGANIC position
    on generic keywords (positions 1..``map_position_max``; needs a product
    x geo impression feed).  ``priority`` = the product is among the geo's
    top ``map_priority_rank`` own products by sales; ``strong`` = organic
    position at or better than the platform's page-one depth
    (``profile.position.page_one_depth``, default 12).  Decision by
    :func:`cell_decision`.  Both cut-offs are declared tuning knobs.
    """
    cfg, economics = cfg or _GC, economics or Economics()

    def g(name: str | None) -> str | None:
        if name is None:
            return None
        r = profile.rollup_geo(name) if profile is not None else name
        return None if (profile is not None and profile.is_aggregate_geo(r)) else r

    sales: dict[tuple[str, str], float] = {}
    for o in offtake_rows or ():
        geo = g(o.geo)
        if o.is_own and o.product_id and geo and in_window(o.date, window):
            sales[(o.product_id, geo)] = sales.get((o.product_id, geo), 0.0) + (o.sales or 0.0)
    if not sales:
        return ProductGeoMap(reason="no own product x geo sell-out rows in the window")
    by_geo: dict[str, float] = {}
    for (_, geo), v in sales.items():
        by_geo[geo] = by_geo.get(geo, 0.0) + v
    geos = sorted(by_geo, key=lambda k: (-by_geo[k], k))[: cfg.map_top_geos]
    by_prod: dict[str, float] = {}
    for (pid, geo), v in sales.items():
        if geo in geos:
            by_prod[pid] = by_prod.get(pid, 0.0) + v
    prods = sorted(by_prod, key=lambda k: (-by_prod[k], k))[: cfg.map_top_products]

    money: dict[tuple[str, str], list[float]] = {}
    for r in perf_rows or ():
        geo = g(r.geo)
        if r.product_id and geo and in_window(r.date, window):
            a = money.setdefault((r.product_id, geo), [0.0, 0.0])
            a[0] += r.spend or 0.0
            a[1] += r.sales or 0.0
    pos: dict[tuple[str, str], list[float]] = {}
    for m in market_rows or ():
        geo = g(m.geo)
        if not (m.is_own and m.product_id and geo and in_window(m.date, window)):
            continue
        role = (roles or {}).get(norm(m.targeting_unit)) or m.unit_role or "generic"
        if role != "generic" or m.organic_position is None or not 1.0 <= m.organic_position <= cfg.map_position_max:
            continue
        w = (m.organic_impressions or 0.0) or (m.impressions or 0.0)
        if w > 0:
            a = pos.setdefault((m.product_id, geo), [0.0, 0.0])
            a[0] += m.organic_position * w
            a[1] += w
    depth = profile.position.page_one_depth if profile is not None else 12
    bar = cfg.map_invest_roas_be_mult * economics.be
    cells: list[MapCell] = []
    for geo in geos:
        top = sorted(((v, pid) for (pid, gg), v in sales.items() if gg == geo), key=lambda t: (-t[0], t[1]))
        priority_ids = {pid for _, pid in top[: cfg.map_priority_rank]}
        for pid in prods:
            s = sales.get((pid, geo), 0.0)
            mny = money.get((pid, geo))
            p = pos.get((pid, geo))
            op = p[0] / p[1] if p and p[1] > 0 else None
            spend = mny[0] if mny else 0.0
            roas = mny[1] / mny[0] if mny and mny[0] > 0 else None
            has = s > 0 or spend > 0 or op is not None
            pri = pid in priority_ids
            strong = op is not None and op <= depth
            cells.append(MapCell(pid, geo, s, spend, roas, op, pri, strong,
                                 cell_decision(pri, strong, roas, has, bar)))
    return ProductGeoMap(tuple(geos), tuple(prods), tuple(cells))
