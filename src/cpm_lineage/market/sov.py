"""Share of voice: ad, organic and overall, with like-for-like comparison,
per-keyword-type cuts and the rival table.

Share of voice is additive context.  Every function returns ``None`` (or
an empty structure with a reason) when the impression-share feed is
missing; ``None`` means "not reported", never 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Collection, Iterable, Mapping

from ..schema import MarketRow, ShelfRow, Window, WindowPair
from ._util import diff, in_window, norm, pct
from .config import OWN_LABEL, MarketConfig

__all__ = [
    "SovCut", "SovSummary", "tracked_keywords", "ad_impression_sums", "ad_sov", "sov_cut", "sov_summary", "brand_ad_shares",
    "rival_table", "weighted_availability", "organic_share",
]

_MC = MarketConfig()


def _kwset(keywords: Iterable[str] | None) -> set[str] | None:
    return None if keywords is None else {norm(k) for k in keywords}


def tracked_keywords(market_rows: Iterable[MarketRow], window: Window) -> set[str]:
    """Keywords the feed still tracks in ``window`` (summed ad impressions > 0)."""
    tot: dict[str, float] = {}
    for m in market_rows:
        if in_window(m.date, window):
            k = norm(m.targeting_unit)
            tot[k] = tot.get(k, 0.0) + (m.ad_impressions or 0.0)
    return {k for k, v in tot.items() if v > 0}


def ad_impression_sums(
    market_rows: Iterable[MarketRow],
    window: Window | None = None,
    keywords: Iterable[str] | None = None,
    *,
    like_for_like: Window | None = None,
) -> tuple[float, float]:
    """``(own ad impressions, all brands' ad impressions)`` summed over the scope.

    The single aggregation behind every ad share-of-voice figure in the
    library (this module in percent, ``cpm.sov`` as a 0..1 fraction).  With
    ``like_for_like`` only keywords still tracked in that window (ad
    impressions > 0 there) are counted, so a keyword leaving the feed cannot
    fake a fall.
    """
    rows = list(market_rows)
    kws = _kwset(keywords)
    if like_for_like is not None:
        still = tracked_keywords(rows, like_for_like)
        kws = still if kws is None else kws & still
    own = tot = 0.0
    for m in rows:
        if not in_window(m.date, window):
            continue
        if kws is not None and norm(m.targeting_unit) not in kws:
            continue
        v = m.ad_impressions or 0.0
        tot += v
        if m.is_own:
            own += v
    return own, tot


def ad_sov(
    market_rows: Iterable[MarketRow],
    window: Window | None = None,
    keywords: Iterable[str] | None = None,
    *,
    like_for_like: Window | None = None,
) -> float | None:
    """Ad share of voice of the own brand, in percent.

    ``100 * sum(own ad impressions) / sum(all brands' ad impressions)``
    over every day and keyword in scope: sums first, one division, never a
    mean of daily shares.  With ``like_for_like`` only keywords still
    tracked in that window (ad impressions > 0 there) are counted, so a
    keyword leaving the feed cannot fake a fall.  ``None`` when the
    denominator is 0.
    """
    own, tot = ad_impression_sums(market_rows, window, keywords, like_for_like=like_for_like)
    return pct(own, tot)


@dataclass(frozen=True)
class SovCut:
    """Share of voice for one scope, percent, with deltas vs the prior window."""

    ad: float | None = None
    organic: float | None = None           # own organic / all organic
    overall: float | None = None           # own (ad + organic) / all (ad + organic)
    ad_delta: float | None = None
    organic_delta: float | None = None
    overall_delta: float | None = None
    mix: float | None = None               # this scope's share of all impressions in the segment, percent
    own_organic_share: float | None = None  # own organic / (own organic + own ad), 0..1


def _sums(rows: Iterable[MarketRow], window: Window | None, kws: set[str] | None) -> list[float]:
    s = [0.0, 0.0, 0.0, 0.0]  # own ad, own organic, all ad, all organic
    for m in rows:
        if not in_window(m.date, window):
            continue
        if kws is not None and norm(m.targeting_unit) not in kws:
            continue
        a, o = m.ad_impressions or 0.0, m.organic_impressions or 0.0
        s[2] += a
        s[3] += o
        if m.is_own:
            s[0] += a
            s[1] += o
    return s


def sov_cut(
    market_rows: Iterable[MarketRow],
    windows: WindowPair,
    keywords: Iterable[str] | None = None,
    *,
    like_for_like: bool = True,
) -> SovCut:
    """Ad / organic / overall share of voice for a keyword scope, with deltas.

    ``ad = own ad / all ad``; ``organic = own organic / all organic``;
    ``overall = (own ad + own organic) / (all ad + all organic)``; each in
    percent with the change versus ``windows.prior``.  With
    ``like_for_like`` the prior window counts only keywords still tracked
    in the current one.
    """
    rows = list(market_rows)
    kws = _kwset(keywords)
    pkws = kws
    if like_for_like:
        still = tracked_keywords(rows, windows.current)
        pkws = still if kws is None else kws & still
    c = _sums(rows, windows.current, kws)
    p = _sums(rows, windows.prior, pkws)

    def trio(s: list[float]) -> tuple[float | None, float | None, float | None]:
        return pct(s[0], s[2]), pct(s[1], s[3]), pct(s[0] + s[1], s[2] + s[3])

    ca, co, cv = trio(c)
    pa, po, pv = trio(p)
    own_tot = c[0] + c[1]
    return SovCut(ca, co, cv, diff(ca, pa), diff(co, po), diff(cv, pv), None,
                  c[1] / own_tot if own_tot > 0 else None)


@dataclass(frozen=True)
class SovSummary:
    """Segment share of voice: the total cut and one cut per keyword type."""

    total: SovCut = field(default_factory=SovCut)
    by_role: Mapping[str, SovCut] = field(default_factory=dict)
    reason: str | None = None


def sov_summary(
    market_rows: Iterable[MarketRow],
    windows: WindowPair,
    roles: Mapping[str, str] | None = None,
    *,
    like_for_like: bool = True,
) -> SovSummary:
    """Segment share of voice, in total and per keyword type.

    The per-type cut uses ``roles`` (``{keyword: role}``) or, failing
    that, the feed's native ``unit_role``; keywords with neither count as
    ``generic``.  ``mix`` is the type's share of all segment impressions
    (ad + organic, all brands) in the current window.  Returns a
    :class:`SovSummary`; with no rows in the current window the cuts are
    empty and ``reason`` says so.
    """
    rows = [m for m in market_rows if in_window(m.date, windows.current) or in_window(m.date, windows.prior)]
    if not any(in_window(m.date, windows.current) for m in rows):
        return SovSummary(reason="no share-of-voice rows in the current window")
    total = sov_cut(rows, windows, like_for_like=like_for_like)
    by_kw: dict[str, str] = {}
    for m in rows:
        k = norm(m.targeting_unit)
        if k not in by_kw:
            by_kw[k] = (roles or {}).get(k) or (m.unit_role or "generic")
    all_c = _sums(rows, windows.current, None)
    denom = all_c[2] + all_c[3]
    by_role: dict[str, SovCut] = {}
    for role in sorted(set(by_kw.values())):
        kws = {k for k, r in by_kw.items() if r == role}
        cut = sov_cut(rows, windows, kws, like_for_like=like_for_like)
        s = _sums(rows, windows.current, kws)
        by_role[role] = SovCut(cut.ad, cut.organic, cut.overall, cut.ad_delta, cut.organic_delta,
                               cut.overall_delta, pct(s[2] + s[3], denom), cut.own_organic_share)
    return SovSummary(total, by_role)


def brand_ad_shares(
    market_rows: Iterable[MarketRow],
    window: Window | None = None,
    keywords: Iterable[str] | None = None,
    top: int | None = None,
) -> dict[str, float]:
    """Each brand's ad share (0..1) in a period; all own brands are keyed
    ``"~own"``.  ``top`` keeps the largest brands by ad impressions.  Empty
    without ad impressions."""
    kws = _kwset(keywords)
    by: dict[str, float] = {}
    for m in market_rows:
        if not in_window(m.date, window):
            continue
        if kws is not None and norm(m.targeting_unit) not in kws:
            continue
        key = OWN_LABEL if m.is_own else m.brand
        by[key] = by.get(key, 0.0) + (m.ad_impressions or 0.0)
    tot = sum(by.values())
    if tot <= 0:
        return {}
    ranked = sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))
    if top is not None:
        ranked = ranked[:top]
    return {b: v / tot for b, v in ranked}


def rival_table(
    market_rows: Iterable[MarketRow],
    periods: Mapping[str, Window],
    keywords: Iterable[str],
    cfg: MarketConfig | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """Rival table ``{keyword: {period label: {brand: ad share}}}`` keeping
    the ``rivals_top`` largest brands per keyword and period; periods with
    no ad impressions are omitted."""
    cfg = cfg or _MC
    rows = list(market_rows)
    out: dict[str, dict[str, dict[str, float]]] = {}
    for kw in keywords:
        k = norm(kw)
        per: dict[str, dict[str, float]] = {}
        for label, window in periods.items():
            shares = brand_ad_shares(rows, window, [k], top=cfg.rivals_top)
            if shares:
                per[label] = shares
        out[k] = per
    return out


def organic_share(market_rows: Iterable[MarketRow], window: Window | None = None,
                  keywords: Iterable[str] | None = None) -> float | None:
    """Own organic impression share ``own organic / (own organic + own ad)``, 0..1."""
    s = _sums(market_rows, window, _kwset(keywords))
    return s[1] / (s[0] + s[1]) if s[0] + s[1] > 0 else None


def weighted_availability(
    shelf_rows: Iterable[ShelfRow],
    window: Window | None = None,
    *,
    own: bool | None = True,
    product_ids: Collection[str] | None = None,
    geo: str | None = None,
) -> float | None:
    """Availability of a product set, 0..1.

    Velocity-weighted when the rows carry units: ``sum(availability_product
    x units_product) / sum(units_product)``; otherwise check-weighted
    ``sum(available) / sum(checks)``.  ``own`` filters on the own flag
    (``None`` = every brand).  ``None`` without shelf checks.
    """
    per: dict[str, list[float]] = {}
    for s in shelf_rows:
        if not in_window(s.date, window) or not s.check_count or s.available_count is None:
            continue
        if own is not None and bool(s.is_own) != own:
            continue
        if product_ids is not None and s.product_id not in product_ids:
            continue
        if geo is not None and s.geo != geo:
            continue
        a = per.setdefault(s.product_id, [0.0, 0.0, 0.0])
        a[0] += s.available_count
        a[1] += s.check_count
        a[2] += s.units or 0.0
    if not per:
        return None
    units = sum(a[2] for a in per.values())
    if units > 0:
        return sum(a[0] / a[1] * a[2] for a in per.values()) / units
    return sum(a[0] for a in per.values()) / sum(a[1] for a in per.values())
