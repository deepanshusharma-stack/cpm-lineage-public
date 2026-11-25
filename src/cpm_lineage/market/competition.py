"""Segment competition table and multi-brand ("house of brands") detection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from ..schema import MarketRow, OfftakeRow, ShelfRow, WindowPair
from ._util import diff, in_window, pct, pct_growth
from .config import OWN_LABEL, MarketConfig

__all__ = ["BrandRow", "CompetitionTable", "house_key", "competition_table"]

_MC = MarketConfig()
_ALNUM = re.compile(r"[^\W_]+", re.UNICODE)


def house_key(name: str | None) -> str:
    """First alphanumeric token of a brand name, lower-cased.

    Two brands sharing a key are treated as one house.  Documented limit:
    umbrella brands with distinct names need a curated map
    (``house_map`` in :func:`competition_table`).
    """
    m = _ALNUM.search((name or "").lower())
    return m.group(0) if m else ""


@dataclass(frozen=True)
class BrandRow:
    """One brand in the segment scoreboard (percent values; deltas vs prior window)."""

    brand: str
    is_own: bool = False
    is_house: bool = False               # rival that belongs to the advertiser's own house: not a threat
    sales: float = 0.0
    share_pct: float | None = None
    share_delta_pp: float | None = None
    discount_pct: float | None = None
    discount_delta_pp: float | None = None
    availability: float | None = None    # 0..1
    availability_delta: float | None = None
    ad_sov_pct: float | None = None
    ad_sov_delta_pp: float | None = None
    organic_sov_pct: float | None = None
    organic_sov_delta_pp: float | None = None
    overall_sov_pct: float | None = None
    overall_sov_delta_pp: float | None = None


@dataclass(frozen=True)
class CompetitionTable:
    """Top rivals plus the single aggregated own row."""

    rows: tuple[BrandRow, ...] = ()          # own row included, sorted by share
    own: BrandRow | None = None
    own_rank: int | None = None              # 1-based rank of the own row among all brands by sales
    n_brands: int = 0
    top_advertisers: tuple[tuple[str, float], ...] = ()   # (brand, ad share of voice %)
    unmapped_pct: float | None = None        # share of segment sales with no brand mapping
    segment_sales: float = 0.0
    segment_growth_pct: float | None = None
    reason: str | None = None


def competition_table(
    offtake_rows: Iterable[OfftakeRow] | None,
    windows: WindowPair,
    *,
    shelf_rows: Iterable[ShelfRow] | None = None,
    market_rows: Iterable[MarketRow] | None = None,
    house_map: Mapping[str, str] | None = None,
    cfg: MarketConfig | None = None,
) -> CompetitionTable:
    """Build the per-brand scoreboard of a segment.

    Per brand: ``share = sales / segment total`` where the denominator
    *includes* rows with no brand (otherwise the scoreboard disagrees with
    the headline share); ``discount = 100 x (1 - sum(sales) /
    sum(list_value))``; availability weighted by units (shelf weight when
    units are absent); ad / organic / overall share of voice.  Every
    metric carries its change versus ``windows.prior``.

    All own brands are folded into ONE own row (share additive,
    availability and discount from summed components).  A rival is flagged
    ``is_house`` when its rows are marked sibling, or its
    :func:`house_key` (or ``house_map`` entry) equals an own brand's: such
    rows are not threats and should be left out of mover / defense reads.

    Returns the top ``competition_top_n`` rivals plus the own row, the own
    rank, the top advertisers by ad share of voice, the unmapped share and
    the segment's growth.
    """
    cfg = cfg or _MC
    rows = [o for o in (offtake_rows or ()) if in_window(o.date, windows.current) or in_window(o.date, windows.prior)]
    if not any(in_window(o.date, windows.current) for o in rows):
        return CompetitionTable(reason="no market sell-out rows in the current window")
    hmap = {k.lower(): v for k, v in (house_map or {}).items()}

    def hk(name: str) -> str:
        return hmap.get(name.lower(), house_key(name))

    own_names = {o.brand for o in rows if o.is_own and o.brand}
    own_keys = {hk(n) for n in own_names} - {""}
    sib = {o.brand for o in rows if o.is_sibling and not o.is_own}

    def label(brand: str | None, is_own: bool) -> str:
        return OWN_LABEL if is_own else (brand or "")

    # sales, list value, selling value with list value: [cur, prev] each
    acc: dict[str, list[float]] = {}
    tot = [0.0, 0.0]
    for o in rows:
        i = 0 if in_window(o.date, windows.current) else 1
        tot[i] += o.sales or 0.0
        a = acc.setdefault(label(o.brand, o.is_own), [0.0] * 6)
        a[i] += o.sales or 0.0
        if o.list_value:
            a[2 + i] += o.list_value
            a[4 + i] += o.sales or 0.0

    av: dict[str, list[float]] = {}
    for s in shelf_rows or ():
        if not s.check_count or s.available_count is None:
            continue
        if in_window(s.date, windows.current):
            i = 0
        elif in_window(s.date, windows.prior):
            i = 1
        else:
            continue
        w = s.units if s.units else s.weight
        a = av.setdefault(label(s.brand, s.is_own), [0.0] * 4)
        a[i] += s.available_count * w
        a[2 + i] += s.check_count * w

    imp: dict[str, list[float]] = {}
    itot = [0.0] * 4  # ad cur, ad prev, organic cur, organic prev
    for m in market_rows or ():
        if in_window(m.date, windows.current):
            i = 0
        elif in_window(m.date, windows.prior):
            i = 1
        else:
            continue
        a = imp.setdefault(label(m.brand, m.is_own), [0.0] * 4)
        ad, og = m.ad_impressions or 0.0, m.organic_impressions or 0.0
        a[i] += ad
        a[2 + i] += og
        itot[i] += ad
        itot[2 + i] += og

    def build(name: str) -> BrandRow:
        a = acc.get(name, [0.0] * 6)
        sh, shp = pct(a[0], tot[0]), pct(a[1], tot[1])
        dc = None if a[2] <= 0 else 100.0 * (1.0 - a[4] / a[2])
        dp = None if a[3] <= 0 else 100.0 * (1.0 - a[5] / a[3])
        v = av.get(name)
        ac = v[0] / v[2] if v and v[2] > 0 else None
        ap = v[1] / v[3] if v and v[3] > 0 else None
        m = imp.get(name)
        if m:
            sa, sap = pct(m[0], itot[0]), pct(m[1], itot[1])
            so, sop = pct(m[2], itot[2]), pct(m[3], itot[3])
            sv, svp = pct(m[0] + m[2], itot[0] + itot[2]), pct(m[1] + m[3], itot[1] + itot[3])
        else:
            sa = sap = so = sop = sv = svp = None
        is_own = name == OWN_LABEL
        return BrandRow(
            brand=name, is_own=is_own,
            is_house=(not is_own) and (name in sib or (hk(name) in own_keys and hk(name) != "")),
            sales=a[0], share_pct=sh, share_delta_pp=diff(sh, shp),
            discount_pct=dc, discount_delta_pp=diff(dc, dp),
            availability=ac, availability_delta=diff(ac, ap),
            ad_sov_pct=sa, ad_sov_delta_pp=diff(sa, sap),
            organic_sov_pct=so, organic_sov_delta_pp=diff(so, sop),
            overall_sov_pct=sv, overall_sov_delta_pp=diff(sv, svp))

    names = [n for n in acc if n]  # the unmapped bucket "" stays in the denominator only
    built = sorted((build(n) for n in names), key=lambda r: (-r.sales, r.brand))
    own = next((r for r in built if r.is_own), None)
    rank = next((i + 1 for i, r in enumerate(built) if r.is_own), None)
    rivals = [r for r in built if not r.is_own][: cfg.competition_top_n]
    out = sorted(rivals + ([own] if own else []), key=lambda r: (-r.sales, r.brand))
    adv = sorted(((n, pct(m[0], itot[0])) for n, m in imp.items() if n and m[0] > 0),
                 key=lambda kv: (-(kv[1] or 0.0), kv[0]))[: cfg.advertisers_top_n]
    unmapped = acc.get("", [0.0])[0]
    return CompetitionTable(rows=tuple(out), own=own, own_rank=rank, n_brands=len(built),
                            top_advertisers=tuple((n, v) for n, v in adv if v is not None),
                            unmapped_pct=pct(unmapped, tot[0]), segment_sales=tot[0],
                            segment_growth_pct=pct_growth(tot[0], tot[1]))
