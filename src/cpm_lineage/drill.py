"""Keyword, product and auction drilldowns inside one scope.

A scope is one entity, a collection of entities, or the whole account.
Each drilldown asks "which item drove the ROAS move?" with the same
exact midpoint shift-share used at account level
(:func:`cpm_lineage.decompose.shift_share`): the items' ``total`` values
sum to the scope's ROAS change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Collection, Hashable, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from .adapters.base import PlatformProfile
from .config import Economics
from .decompose import ShiftShareInput, shift_share
from .keywords.aggregate import scope_filter
from .schema import PerfRow, ShelfRow, WindowPair
from .windows import active_days, drill_buckets

__all__ = [
    "DrillConfig", "DrillItem", "DrillResult", "AuctionRow", "AuctionProvider",
    "scope_rows", "scope_buckets", "item_shift_share", "keyword_drill", "product_drill", "auction_drill",
]


@dataclass(frozen=True)
class DrillConfig:
    """Named thresholds with their defaults."""

    days: int = 30                        # current and baseline bucket length (matches the diagnosis windows)
    keyword_top_n: int = 15               # keyword rows: losers plus gainers
    keyword_gainers: int = 5              # winners are half the story
    product_top_n: int = 12               # product rows
    product_gainers: int = 4  # of the product rows, this many are gainers; the rest are losers
    auction_top_n: int = 8                # keywords sent to the auction provider
    auction_min_spend_ref: float = 1000.0  # reference money: keywords below this spend are not worth an auction read
    position_max: float = 500.0           # positions outside 1..500 are discarded


_DC = DrillConfig()


@dataclass(frozen=True)
class DrillItem:
    """One item of a drilldown (``h`` = baseline bucket, ``w`` = current)."""

    key: Hashable
    h_spend: float
    w_spend: float
    h_sales: float
    w_sales: float
    h_roas: float | None
    w_roas: float | None
    h_share: float           # spend share of the scope, 0..1
    w_share: float
    mix: float
    rate: float
    total: float             # exact contribution to the scope's ROAS change
    h_bid: float | None = None
    w_bid: float | None = None
    h_position: float | None = None
    w_position: float | None = None
    h_impressions: float | None = None
    w_impressions: float | None = None
    h_availability: float | None = None
    w_availability: float | None = None


@dataclass(frozen=True)
class DrillResult:
    """Listed items plus the exact scope-level headline."""

    items: tuple[DrillItem, ...] = ()
    buckets: WindowPair | None = None
    roas_baseline: float | None = None
    roas_current: float | None = None
    mix_total: float = 0.0
    rate_total: float = 0.0
    n_items: int = 0                  # items before the top-n cut
    reason: str | None = None

    @property
    def delta(self) -> float | None:
        if self.roas_baseline is None or self.roas_current is None:
            return None
        return self.roas_current - self.roas_baseline


def scope_rows(perf_rows: Iterable[PerfRow], entity_ids: str | Collection[str] | None = None) -> list[PerfRow]:
    """Rows of a scope: one entity id, a collection of ids (an empty
    collection matches nothing), or ``None`` / ``"*"`` for the account."""
    scope = scope_filter(entity_ids)
    return [r for r in perf_rows if scope is None or r.entity_id in scope]


def scope_buckets(rows: Iterable[PerfRow], days: int = _DC.days) -> WindowPair | None:
    """Drill buckets anchored to the scope's own last active day.

    An active day has ``sum(spend) > 0 and sum(impressions) > 0`` for the
    scope.  Current = the last ``days`` days up to that day, baseline = the
    ``days`` before.  ``None`` when the scope has no active day.
    """
    return drill_buckets(active_days(rows, require_units=False), days)


def item_shift_share(sums: Mapping[Hashable, Sequence[float]]) -> tuple[list[DrillItem], Any]:
    """Shift-share over ``{key: (h_spend, h_sales, w_spend, w_sales)}``.

    Returns ``(items sorted ascending by total, the full shift-share
    result)``.  ROAS is ``None`` for a bucket without spend (the effects
    treat it as 0).
    """
    res = shift_share([ShiftShareInput(k, v[0], v[1], v[2], v[3]) for k, v in sums.items()])
    items = [DrillItem(i.key, i.weight_prior, i.weight_current, i.value_prior, i.value_current,
                       i.rate_prior if i.weight_prior > 0 else None,
                       i.rate_current if i.weight_current > 0 else None,
                       i.share_prior, i.share_current, i.mix, i.rate, i.total) for i in res.items]
    items.sort(key=lambda d: (d.total, str(d.key)))
    return items, res


def _select(items: list[DrillItem], top_n: int, gainers: int) -> list[DrillItem]:
    losers = [d for d in items if d.total < 0][: max(top_n - gainers, 0)]
    listed = {d.key for d in losers}
    rest = [d for d in items if d.key not in listed and d.total > 0]
    return losers + rest[-gainers:] if gainers > 0 else losers


def _result(items: list[DrillItem], res: Any, buckets: WindowPair, top_n: int, gainers: int,
            enrich: Any = None) -> DrillResult:
    sel = _select(items, top_n, gainers)
    if enrich is not None:
        sel = [enrich(d) for d in sel]
    return DrillResult(tuple(sel), buckets,
                       res.rate_prior if any(d.h_spend > 0 for d in items) else None,
                       res.rate_current if any(d.w_spend > 0 for d in items) else None,
                       res.mix_total, res.rate_total, len(items))


def keyword_drill(
    perf_rows: Iterable[PerfRow],
    entity_ids: str | Collection[str] | None = None,
    *,
    buckets: WindowPair | None = None,
    profile: PlatformProfile | None = None,
    cfg: DrillConfig | None = None,
) -> DrillResult:
    """Which keywords drove the scope's ROAS move.

    Per keyword and bucket: spend, sales, impressions, spend-weighted bid
    ``sum(spend x bid) / sum(spend)`` and impression-weighted position
    ``sum(impressions x position) / sum(impressions)``; then the exact
    shift-share.  Returns up to ``keyword_top_n - keyword_gainers`` losers
    (``total < 0``, worst first) followed by the ``keyword_gainers``
    largest gainers.  Placement pseudo-targets are excluded through the
    profile (applied uniformly across drills).
    The headline totals cover every keyword, not only the listed ones.
    """
    from dataclasses import replace

    cfg = cfg or _DC
    rows = [r for r in scope_rows(perf_rows, entity_ids) if r.targeting_unit
            and (profile is None or profile.is_search_keyword(r.targeting_unit))]
    buckets = buckets or scope_buckets(rows, cfg.days)
    if buckets is None:
        return DrillResult(reason="the scope has no active day")
    sums: dict[Hashable, list[float]] = {}
    extra: dict[Hashable, list[float]] = {}   # per bucket: bid_w, bid_spend, pos_w, pos_impr, impressions
    for r in rows:
        i = 1 if buckets.current.contains(r.date) else 0 if buckets.prior.contains(r.date) else None
        if i is None:
            continue
        k = " ".join(r.targeting_unit.lower().split())  # type: ignore[union-attr]
        s = sums.setdefault(k, [0.0, 0.0, 0.0, 0.0])
        s[2 * i] += r.spend or 0.0
        s[2 * i + 1] += r.sales or 0.0
        e = extra.setdefault(k, [0.0] * 10)
        if r.bid is not None and (r.spend or 0) > 0:
            e[5 * i] += r.bid * r.spend
            e[5 * i + 1] += r.spend
        if r.position is not None and 1.0 <= r.position <= cfg.position_max and (r.impressions or 0) > 0:
            e[5 * i + 2] += r.position * r.impressions
            e[5 * i + 3] += r.impressions
        e[5 * i + 4] += r.impressions or 0.0
    if not sums:
        return DrillResult(buckets=buckets, reason="no keyword rows in the drill buckets")
    items, res = item_shift_share(sums)

    def enrich(d: DrillItem) -> DrillItem:
        e = extra[d.key]
        return replace(d, h_bid=e[0] / e[1] if e[1] > 0 else None, w_bid=e[5] / e[6] if e[6] > 0 else None,
                       h_position=e[2] / e[3] if e[3] > 0 else None, w_position=e[7] / e[8] if e[8] > 0 else None,
                       h_impressions=e[4], w_impressions=e[9])

    return _result(items, res, buckets, cfg.keyword_top_n, cfg.keyword_gainers, enrich)


def product_drill(
    perf_rows: Iterable[PerfRow],
    entity_ids: str | Collection[str] | None = None,
    *,
    shelf_rows: Iterable[ShelfRow] | None = None,
    buckets: WindowPair | None = None,
    cfg: DrillConfig | None = None,
) -> DrillResult:
    """Which products drove the scope's ROAS move.

    Per product and bucket: spend and sales from product-grain rows, the
    exact shift-share, then availability per product and bucket
    ``sum(available) / sum(checks)`` over own shelf rows with checks (left
    ``None`` without a shelf feed).  Returns up to ``product_top_n -
    product_gainers`` losers and ``product_gainers`` gainers.
    """
    from dataclasses import replace

    cfg = cfg or _DC
    rows = [r for r in scope_rows(perf_rows, entity_ids) if r.product_id]
    buckets = buckets or scope_buckets(rows, cfg.days)
    if buckets is None:
        return DrillResult(reason="the scope has no active day with product-grain rows")
    sums: dict[Hashable, list[float]] = {}
    for r in rows:
        i = 1 if buckets.current.contains(r.date) else 0 if buckets.prior.contains(r.date) else None
        if i is None:
            continue
        s = sums.setdefault(r.product_id, [0.0, 0.0, 0.0, 0.0])
        s[2 * i] += r.spend or 0.0
        s[2 * i + 1] += r.sales or 0.0
    if not sums:
        return DrillResult(buckets=buckets, reason="no product rows in the drill buckets")
    av: dict[str, list[float]] = {}
    for s_ in shelf_rows or ():
        if not s_.is_own or not s_.check_count or s_.check_count <= 0 or s_.available_count is None:
            continue
        i = 1 if buckets.current.contains(s_.date) else 0 if buckets.prior.contains(s_.date) else None
        if i is None:
            continue
        a = av.setdefault(s_.product_id, [0.0, 0.0, 0.0, 0.0])
        a[2 * i] += s_.available_count
        a[2 * i + 1] += s_.check_count
    items, res = item_shift_share(sums)

    def enrich(d: DrillItem) -> DrillItem:
        a = av.get(str(d.key))
        if a is None:
            return d
        return replace(d, h_availability=a[0] / a[1] if a[1] > 0 else None,
                       w_availability=a[2] / a[3] if a[3] > 0 else None)

    return _result(items, res, buckets, cfg.product_top_n, cfg.product_gainers, enrich)


# --------------------------------------------------------------------------
# auction drilldown (optional capability)
# --------------------------------------------------------------------------

@runtime_checkable
class AuctionProvider(Protocol):
    """Optional auction intelligence for a keyword.

    ``keyword_intel`` returns a mapping with any of: ``top_slot_p50``,
    ``top_slot_p75`` (clearing prices of the top slot), ``n``
    (observations), ``top_competitor``, ``threat``, ``cheap_window``,
    ``dear_window``, ``rank_trend``; or ``None`` when it knows nothing.
    """

    def keyword_intel(self, keyword: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True)
class AuctionRow:
    """Own bid against the top slot's clearing prices on one keyword."""

    keyword: str
    spend: float
    own_bid: float | None
    top_slot_p50: float | None = None
    top_slot_p75: float | None = None
    n: int | None = None
    gap: float | None = None             # round(top_slot_p75 - own_bid); positive = bidding under the slot
    top_competitor: str | None = None
    threat: str | None = None
    cheap_window: str | None = None
    dear_window: str | None = None
    rank_trend: Any = None
    extras: Mapping[str, Any] = field(default_factory=dict)


def auction_drill(
    perf_rows: Iterable[PerfRow],
    entity_ids: str | Collection[str] | None = None,
    provider: AuctionProvider | None = None,
    *,
    buckets: WindowPair | None = None,
    profile: PlatformProfile | None = None,
    economics: Economics | None = None,
    cfg: DrillConfig | None = None,
) -> tuple[list[AuctionRow], str | None]:
    """Compare own bids with auction clearing prices on the scope's top keywords.

    Takes the scope's top ``auction_top_n`` search keywords by
    current-bucket spend above ``money(auction_min_spend_ref)``, computes
    the spend-weighted own bid and asks ``provider`` for the top slot's
    clearing prices, the main competitor and its threat class, cheap / dear
    time windows and the rank trend.  ``gap = round(top_slot_p75 -
    own_bid)``.  The whole read is optional: without a provider it returns
    ``([], reason)``.  Returns ``(rows, reason)``.
    """
    cfg, economics = cfg or _DC, economics or Economics()
    if provider is None:
        return [], "no auction provider: clearing prices are an optional adapter capability"
    rows = [r for r in scope_rows(perf_rows, entity_ids) if r.targeting_unit
            and (profile is None or profile.is_search_keyword(r.targeting_unit))]
    buckets = buckets or scope_buckets(rows, cfg.days)
    if buckets is None:
        return [], "the scope has no active day"
    acc: dict[str, list[float]] = {}
    for r in rows:
        if not buckets.current.contains(r.date):
            continue
        a = acc.setdefault(" ".join(r.targeting_unit.lower().split()), [0.0, 0.0, 0.0])  # type: ignore[union-attr]
        a[0] += r.spend or 0.0
        if r.bid is not None and (r.spend or 0) > 0:
            a[1] += r.bid * r.spend
            a[2] += r.spend
    floor = economics.money(cfg.auction_min_spend_ref)
    top = sorted(((k, a) for k, a in acc.items() if a[0] > floor), key=lambda t: (-t[1][0], t[0]))[: cfg.auction_top_n]
    known = {"top_slot_p50", "top_slot_p75", "n", "top_competitor", "threat", "cheap_window", "dear_window",
             "rank_trend"}
    out: list[AuctionRow] = []
    for k, a in top:
        bid = a[1] / a[2] if a[2] > 0 else None
        intel = dict(provider.keyword_intel(k) or {})
        p75 = intel.get("top_slot_p75")
        out.append(AuctionRow(
            keyword=k, spend=a[0], own_bid=bid, top_slot_p50=intel.get("top_slot_p50"), top_slot_p75=p75,
            n=intel.get("n"), gap=float(round(p75 - bid)) if p75 is not None and bid is not None else None,
            top_competitor=intel.get("top_competitor"), threat=intel.get("threat"),
            cheap_window=intel.get("cheap_window"), dear_window=intel.get("dear_window"),
            rank_trend=intel.get("rank_trend"), extras={x: v for x, v in intel.items() if x not in known}))
    return out, None if out else "no keyword above the spend floor in the current bucket"
