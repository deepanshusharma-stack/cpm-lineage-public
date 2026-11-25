"""Auction intelligence from a position-targeting bidder's decision log.

What bid wins a slot for a keyword, who else is bidding and how threatening
they are, which part of the day is cheap or dear, whether organic position
is improving, and the platform's own suggested bid range.

Read this before trusting the numbers:

* "Clearing bids" are quantiles of **bids set** by advertisers that run the
  automation whose log is supplied.  They are not prices paid, and manual
  bidders are invisible.  Always show them with the observation count and
  ``CLEARING_CAVEAT``.  Interpretation: p75 = safe clearing bid, p50 =
  competitive floor.
* They only approximate a market price when the log is **pooled across
  advertisers**.  With one advertiser's log the same quantiles are merely
  that advertiser's own winning-bid distribution (``OWN_LOG_CAVEAT``); the
  platform's suggested bids are then the fallback.
* The organic trend is a two-point slope, not a regression.

Every function returns an empty result on missing input and callers should
treat all of it as optional context.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from ..config import Economics
from ..schema import BidDecision, PerfRow, SuggestedBid, Window
from ._util import in_window, norm_kw, quantile, to_local
from .config import CpmConfig, money_of

__all__ = [
    "CLEARING_CAVEAT", "OWN_LOG_CAVEAT", "THREAT_SENTENCES", "clearing_bids", "competitors",
    "threat_class", "classify_threat", "daypart_of", "timing", "organic_trend", "suggested_bids",
    "suggested_bids_by_geo", "auction_hints",
]

CLEARING_CAVEAT = "bids set by automated advertisers only; not prices paid, manual bidders are invisible"
OWN_LOG_CAVEAT = "single-advertiser log: this is your own winning-bid distribution, not a clearing price"

THREAT_SENTENCES: dict[str, str] = {
    "ceiling-constrained": "ceiling-constrained: a modest lift past their cap wins",
    "active threat": "active threat: targeting the top slot above your bid",
    "background": "background: competing for deeper slots",
    "present": "present",
}


def _kwset(keywords: Iterable[str] | None) -> set[str] | None:
    return None if keywords is None else {norm_kw(k) for k in keywords}


def _round(x: float, cfg: CpmConfig) -> float:
    return float(round(x, cfg.bid_round_digits))


def _recent(
    decisions: Iterable[BidDecision], keywords: Iterable[str] | None, days: int, as_of: datetime | None,
) -> list[BidDecision]:
    """Applied decisions with a positive bid on the wanted keywords within
    the last ``days`` before ``as_of`` (default: the log's latest decision)."""
    kws = _kwset(keywords)
    ds = [
        d for d in decisions
        if d.success is not False and d.new_bid is not None and d.new_bid > 0
        and (kws is None or norm_kw(d.targeting_unit) in kws)
    ]
    if not ds:
        return []
    naive = [to_local(d.timestamp) for d in ds]
    end = to_local(as_of) if as_of is not None else max(naive)
    start = end - timedelta(days=days)
    return [d for d, t in zip(ds, naive) if start <= t <= end]


def clearing_bids(
    decisions: Iterable[BidDecision],
    keywords: Iterable[str] | None = None,
    *,
    days: int | None = None,
    as_of: datetime | None = None,
    cfg: CpmConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Estimate the bid that wins a slot, per keyword.

    1. Keep applied decisions (``success`` not False; unreported = kept)
       with ``new_bid > 0``, a target-min position in
       ``cfg.clearing_target_min..max`` (1..5), within ``days`` (30).
    2. Slot group: ``s1`` when ``target min <= cfg.clearing_slot_split``
       (targets 1-2, the top slot), else ``s3`` (targets 3-5).
    3. Per (keyword, slot): 25th / 50th / 75th percentile of ``new_bid``
       (rounded to ``cfg.bid_round_digits``) and ``obs``.
    4. Groups with ``obs < cfg.min_obs`` (5) are dropped.

    Returns ``{keyword: {s1_p25, s1_p50, s1_p75, s1_obs, s3_p25, s3_p50,
    s3_p75, s3_obs}}`` with only the slot groups that survived; keywords
    with none are absent.
    """
    cfg = cfg or CpmConfig()
    days = cfg.clearing_days if days is None else days
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for d in _recent(decisions, keywords, days, as_of):
        t = d.target_position_min
        if t is None or not (cfg.clearing_target_min <= t <= cfg.clearing_target_max):
            continue
        slot = "s1" if t <= cfg.clearing_slot_split else "s3"
        groups[(norm_kw(d.targeting_unit), slot)].append(float(d.new_bid))
    out: dict[str, dict[str, Any]] = {}
    for (kw, slot), bids in sorted(groups.items()):
        if len(bids) < cfg.min_obs:
            continue
        rec = out.setdefault(kw, {})
        for label, q in (("p25", 0.25), ("p50", 0.50), ("p75", 0.75)):
            rec[f"{slot}_{label}"] = _round(quantile(bids, q), cfg)
        rec[f"{slot}_obs"] = len(bids)
    return out


def _slug(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def competitors(
    decisions: Iterable[BidDecision],
    keywords: Iterable[str] | None = None,
    own_brand: str | None = None,
    *,
    days: int | None = None,
    as_of: datetime | None = None,
    cfg: CpmConfig | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Who else is bidding on each keyword (pooled log only).

    Per (keyword, brand) over recent applied decisions carrying a brand:
    ``obs``; ``avg_bid`` and ``max_bid`` of ``new_bid``; ``slot`` =
    round(mean target-min position); ``ceiling_pct`` = round(100 x share of
    decisions at the ceiling), ``None`` when the log has no ceiling flag.
    Groups with ``obs < cfg.min_obs`` are dropped; ordered by ``avg_bid``
    descending.  The own brand is excluded by a loose match: both names are
    reduced to lower-case alphanumerics and the row is dropped when either
    contains the other.

    Returns ``{keyword: [{brand, avg_bid, max_bid, slot, obs, ceiling_pct}]}``.
    """
    cfg = cfg or CpmConfig()
    days = cfg.clearing_days if days is None else days
    own = _slug(own_brand)
    groups: dict[tuple[str, str], list[BidDecision]] = defaultdict(list)
    for d in _recent(decisions, keywords, days, as_of):
        if not d.brand:
            continue
        s = _slug(d.brand)
        if own and s and (own in s or s in own):
            continue
        groups[(norm_kw(d.targeting_unit), d.brand)].append(d)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (kw, brand), ds in groups.items():
        if len(ds) < cfg.min_obs:
            continue
        bids = [float(d.new_bid) for d in ds]
        targets = [d.target_position_min for d in ds if d.target_position_min is not None]
        flagged = [d for d in ds if d.at_ceiling is not None]
        out[kw].append({
            "brand": brand,
            "avg_bid": _round(sum(bids) / len(bids), cfg),
            "max_bid": _round(max(bids), cfg),
            "slot": float(round(sum(targets) / len(targets))) if targets else None,
            "obs": len(ds),
            "ceiling_pct": float(round(100.0 * sum(1 for d in flagged if d.at_ceiling) / len(ds))) if flagged else None,
        })
    for kw in out:
        out[kw].sort(key=lambda r: (-r["avg_bid"], r["brand"]))
    return dict(out)


def threat_class(
    competitor: Mapping[str, Any] | None, own_bid: float | None, cfg: CpmConfig | None = None,
) -> str | None:
    """Classify one competitor row against our bid.  First match wins:

    1. no competitor -> ``None``
    2. ``ceiling_pct > cfg.threat_ceiling_pct`` (20) -> ``"ceiling-constrained"``
    3. ``slot <= cfg.threat_top_slot`` (2) and our bid known and their
       ``max_bid > own_bid`` -> ``"active threat"``
    4. ``slot >= cfg.threat_deep_slot`` (3) and our bid known and their
       ``avg_bid < own_bid`` -> ``"background"``
    5. otherwise ``"present"``

    Slot thresholds assume position 1 is the best slot.
    """
    cfg = cfg or CpmConfig()
    if competitor is None:
        return None
    ceil = competitor.get("ceiling_pct")
    slot = competitor.get("slot")
    if ceil is not None and ceil > cfg.threat_ceiling_pct:
        return "ceiling-constrained"
    if slot is not None and own_bid:
        if slot <= cfg.threat_top_slot and (competitor.get("max_bid") or 0) > own_bid:
            return "active threat"
        if slot >= cfg.threat_deep_slot and (competitor.get("avg_bid") or 0) < own_bid:
            return "background"
    return "present"


def classify_threat(
    competitor: Mapping[str, Any] | None, own_bid: float | None, cfg: CpmConfig | None = None,
) -> str | None:
    """``threat_class`` rendered as the sentence shown to the operator
    (``THREAT_SENTENCES``); ``None`` without a competitor."""
    code = threat_class(competitor, own_bid, cfg)
    return None if code is None else THREAT_SENTENCES[code]


def daypart_of(hour: float, dayparts: Sequence[tuple[str, int, int]]) -> str | None:
    """Label of the daypart containing a local hour.  A part ``(label,
    start, end)`` covers ``start <= hour < end``; one whose end is not after
    its start wraps past midnight (22-06 covers 22, 23, 0..5)."""
    for label, start, end in dayparts:
        if start < end:
            if start <= hour < end:
                return label
        elif hour >= start or hour < end:
            return label
    return None


def timing(
    decisions: Iterable[BidDecision],
    keywords: Iterable[str] | None = None,
    *,
    tz: Any = None,
    days: int | None = None,
    as_of: datetime | None = None,
    cfg: CpmConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Cheapest and dearest part of the day per keyword.

    Decision times are converted to local time (``tz``: IANA name, tzinfo or
    minutes east of UTC; naive timestamps are UTC) and bucketed into
    ``cfg.dayparts``.  Per (keyword, daypart): ``avg_bid`` = mean
    ``new_bid``, kept at ``obs >= cfg.timing_min_obs`` (10).  A keyword is
    reported only with at least ``cfg.timing_min_parts`` (2) dayparts.

    Returns ``{keyword: {cheap: (label, avg_bid), dear: (label, avg_bid),
    parts: {label: {avg_bid, obs}}}}``.
    """
    cfg = cfg or CpmConfig()
    days = cfg.clearing_days if days is None else days
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for d in _recent(decisions, keywords, days, as_of):
        local = to_local(d.timestamp, tz)
        part = daypart_of(local.hour, cfg.dayparts)
        if part is not None:
            groups[(norm_kw(d.targeting_unit), part)].append(float(d.new_bid))
    per_kw: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for (kw, part), bids in groups.items():
        if len(bids) >= cfg.timing_min_obs:
            per_kw[kw][part] = {"avg_bid": _round(sum(bids) / len(bids), cfg), "obs": len(bids)}
    order = {label: i for i, (label, _, _) in enumerate(cfg.dayparts)}
    out: dict[str, dict[str, Any]] = {}
    for kw, parts in per_kw.items():
        if len(parts) < cfg.timing_min_parts:
            continue
        ranked = sorted(parts.items(), key=lambda kv: (kv[1]["avg_bid"], order[kv[0]]))
        cheap = ranked[0]
        dear = sorted(parts.items(), key=lambda kv: (-kv[1]["avg_bid"], order[kv[0]]))[0]
        out[kw] = {
            "cheap": (cheap[0], cheap[1]["avg_bid"]),
            "dear": (dear[0], dear[1]["avg_bid"]),
            "parts": {p: parts[p] for p in sorted(parts, key=order.get)},  # type: ignore[arg-type]
        }
    return out


def organic_trend(
    decisions: Iterable[BidDecision],
    keywords: Iterable[str] | None = None,
    *,
    tz: Any = None,
    days: int | None = None,
    as_of: datetime | None = None,
    cfg: CpmConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """Is organic position improving?  A two-point weekly slope per keyword.

    Per (keyword, calendar week starting Monday, local time): mean organic
    position over decisions with ``organic_position > 0`` in the last
    ``days`` (42); weeks with fewer than ``cfg.min_obs`` (5) observations
    are dropped; at least ``cfg.organic_min_weeks`` (2) weeks are needed.

    ``position`` = the last week's mean (1 dp);
    ``delta_per_week = (last - first) / max(weeks - 1, 1)`` (2 dp).
    Where a lower position number is better, **negative = improving**.

    Returns ``{keyword: {position, delta_per_week, weeks}}``.
    """
    cfg = cfg or CpmConfig()
    days = cfg.organic_days if days is None else days
    kws = _kwset(keywords)
    ds = [
        d for d in decisions
        if d.organic_position is not None and d.organic_position > 0
        and (kws is None or norm_kw(d.targeting_unit) in kws)
    ]
    if not ds:
        return {}
    end = to_local(as_of) if as_of is not None else max(to_local(d.timestamp) for d in ds)
    start = end - timedelta(days=days)
    groups: dict[tuple[str, date], list[float]] = defaultdict(list)
    for d in ds:
        if not (start <= to_local(d.timestamp) <= end):
            continue
        day = to_local(d.timestamp, tz).date()
        week = day - timedelta(days=day.weekday())
        groups[(norm_kw(d.targeting_unit), week)].append(float(d.organic_position))
    per_kw: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for (kw, week), vals in groups.items():
        if len(vals) >= cfg.min_obs:
            per_kw[kw].append((week, sum(vals) / len(vals)))
    out: dict[str, dict[str, Any]] = {}
    for kw, weeks in per_kw.items():
        if len(weeks) < cfg.organic_min_weeks:
            continue
        weeks.sort()
        first, last = weeks[0][1], weeks[-1][1]
        out[kw] = {
            "position": round(last, 1),
            "delta_per_week": round((last - first) / max(len(weeks) - 1, 1), 2),
            "weeks": len(weeks),
        }
    return out


def _fresh_suggestions(rows: Iterable[SuggestedBid], days: int) -> list[SuggestedBid]:
    rows = [r for r in rows if r.high is not None and r.high > 0]
    dated = [r.as_of for r in rows if r.as_of is not None]
    if not dated:
        return rows
    latest = max(dated)
    start = latest - timedelta(days=days)
    return [r for r in rows if r.as_of is None or start <= r.as_of <= latest]


def suggested_bids(
    rows: Iterable[SuggestedBid],
    keywords: Iterable[str] | None = None,
    *,
    days: int | None = None,
    cfg: CpmConfig | None = None,
) -> dict[str, dict[str, Any]]:
    """The platform's own suggested bid range per keyword.

    Rows with ``high > 0`` inside a window of ``days`` (30) ending at the
    **feed's own latest date** (freshness rule: never anchor to today).  Per
    keyword: ``lo`` = mean of the low suggestions, ``hi`` = mean of the high
    ones (both rounded), ``searches`` = max, ``as_of`` = latest date.  At
    most ``cfg.suggested_max_keywords`` (100) keywords, largest ``searches``
    first when more are present.

    Returns ``{keyword: {lo, hi, searches, as_of}}``.
    """
    cfg = cfg or CpmConfig()
    days = cfg.suggested_days if days is None else days
    kws = _kwset(keywords)
    groups: dict[str, list[SuggestedBid]] = defaultdict(list)
    for r in _fresh_suggestions(rows, days):
        k = norm_kw(r.targeting_unit)
        if kws is None or k in kws:
            groups[k].append(r)
    out: dict[str, dict[str, Any]] = {}
    for k, rs in groups.items():
        lows = [r.low for r in rs if r.low is not None]
        searches = [r.searches for r in rs if r.searches is not None]
        dates = [r.as_of for r in rs if r.as_of is not None]
        out[k] = {
            "lo": _round(sum(lows) / len(lows), cfg) if lows else None,
            "hi": _round(sum(r.high for r in rs) / len(rs), cfg),  # type: ignore[misc]
            "searches": max(searches) if searches else None,
            "as_of": max(dates) if dates else None,
        }
    if len(out) > cfg.suggested_max_keywords:
        keep = sorted(out, key=lambda k: (-(out[k]["searches"] or 0), k))[: cfg.suggested_max_keywords]
        out = {k: out[k] for k in keep}
    return out


def suggested_bids_by_geo(
    rows: Iterable[SuggestedBid],
    keyword: str,
    *,
    days: int | None = None,
    top: int | None = None,
    cfg: CpmConfig | None = None,
) -> list[dict[str, Any]]:
    """Suggested bid range of one keyword per geo (the same keyword can be
    suggested several times higher in one geo than in another).

    Same freshness window as ``suggested_bids``; geos with fewer than
    ``cfg.suggested_geo_min_rows`` (3) captures are dropped; ordered by
    ``hi`` descending; first ``top`` (12).

    Returns ``[{geo, lo, hi, n}]``.
    """
    cfg = cfg or CpmConfig()
    days = cfg.suggested_days if days is None else days
    top = cfg.suggested_geo_top if top is None else top
    k = norm_kw(keyword)
    groups: dict[str, list[SuggestedBid]] = defaultdict(list)
    for r in _fresh_suggestions(rows, days):
        if r.geo is not None and norm_kw(r.targeting_unit) == k:
            groups[r.geo].append(r)
    out = []
    for geo, rs in groups.items():
        if len(rs) < cfg.suggested_geo_min_rows:
            continue
        lows = [r.low for r in rs if r.low is not None]
        out.append({
            "geo": geo,
            "lo": _round(sum(lows) / len(lows), cfg) if lows else None,
            "hi": _round(sum(r.high for r in rs) / len(rs), cfg),  # type: ignore[misc]
            "n": len(rs),
        })
    out.sort(key=lambda d: (-d["hi"], d["geo"]))
    return out[: max(top, 0)]


def auction_hints(
    perf_rows: Iterable[PerfRow],
    window: Window,
    decisions: Sequence[BidDecision],
    *,
    entity_id: str | None = None,
    own_brand: str | None = None,
    pooled: bool = True,
    tz: Any = None,
    economics: Economics | None = None,
    cfg: CpmConfig | None = None,
) -> list[dict[str, Any]]:
    """Auction context for an entity's biggest keywords.

    Keywords: the top ``cfg.intel_top_keywords`` (8) by spend in ``window``
    with spend above ``cfg.intel_min_spend`` (money, scaled by
    ``economics``).  Per keyword: ``own_bid`` = spend-weighted mean bid over
    rows carrying a bid; clearing quantiles; ``gap = s1_p75 - own_bid``
    (how far below the safe top-slot bid we sit); the top competitor
    (highest average bid) and its threat class; timing and organic trend.

    ``pooled=False`` declares a single-advertiser log: competitors and
    threat are omitted and the caveat says the quantiles are our own bid
    distribution.

    Returns ``[{keyword, spend, own_bid, clearing, gap, competitor, threat,
    timing, organic, caveat}]`` ordered by spend descending.
    """
    cfg = cfg or CpmConfig()
    floor = money_of(cfg, "intel_min_spend", economics)
    spend: dict[str, float] = defaultdict(float)
    bw: dict[str, float] = defaultdict(float)
    bs: dict[str, float] = defaultdict(float)
    for r in perf_rows:
        if entity_id is not None and r.entity_id != entity_id:
            continue
        if not r.targeting_unit or not in_window(r.date, window):
            continue
        k = norm_kw(r.targeting_unit)
        spend[k] += r.spend
        if r.bid is not None and r.bid > 0 and r.spend > 0:
            bw[k] += r.bid * r.spend
            bs[k] += r.spend
    top = [k for k, v in sorted(spend.items(), key=lambda kv: (-kv[1], kv[0])) if v > floor][: cfg.intel_top_keywords]
    if not top:
        return []
    clear = clearing_bids(decisions, top, cfg=cfg)
    comps = competitors(decisions, top, own_brand, cfg=cfg) if pooled else {}
    tim = timing(decisions, top, tz=tz, cfg=cfg)
    org = organic_trend(decisions, top, tz=tz, cfg=cfg)
    out = []
    for k in top:
        own_bid = bw[k] / bs[k] if bs[k] > 0 else None
        c = clear.get(k)
        p75 = c.get("s1_p75") if c else None
        comp = comps.get(k, [None])[0] if comps.get(k) else None
        out.append({
            "keyword": k,
            "spend": spend[k],
            "own_bid": own_bid,
            "clearing": c,
            "gap": _round(p75 - own_bid, cfg) if (p75 is not None and own_bid is not None) else None,
            "competitor": comp,
            "threat": classify_threat(comp, own_bid, cfg),
            "timing": tim.get(k),
            "organic": org.get(k),
            "caveat": CLEARING_CAVEAT if pooled else OWN_LOG_CAVEAT,
        })
    return out
