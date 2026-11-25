"""CPM tracking views: what is paid per thousand impressions, at which
position, and how that compares with a fixed reference.

Every ratio is recomputed from sums (``CPM = 1000 * sum(spend) /
sum(impressions)``; position = impression-weighted mean over rows with a
valid position).  Ratios are never averaged, ``days`` of a combined cell is
the max (groups share days), and shares always divide by ALL spend of the
advertiser (every ad type), not keyword-ad spend.

All functions are pure: records in, plain dicts out.  Apply
``ownership.apply_ownership_guard`` to the rows first when the feed can
misfile campaigns.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from ..adapters.base import PlatformProfile
from ..config import Economics, PositionConfig
from ..metrics import cell_from_rows, empty_cell, position_is_valid, sum_cells
from ..schema import BidRecord, Entity, MarketRow, MetricCell, PerfRow, Window
from ._util import cell_dict, in_window, is_keyword_row, norm_kw, pct, position_config
from .config import CpmConfig, money_of
from .restatement import TOTAL_KEY, first_seen
from .sov import rivals as _rivals
from .sov import tracked_keywords

__all__ = [
    "KW_REST", "OTHER", "top_keywords", "overview", "daily_tracker", "window_compare",
    "position_slot", "cpm_by_position", "keyword_view", "keyword_category_from_market",
    "category_matrix",
]

KW_REST = "~kw"      # every keyword outside the top set
OTHER = "~other"     # every non-keyword ad type


_pos_cfg = position_config


def _month(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _sum(cells: Iterable[MetricCell]) -> MetricCell:
    cells = list(cells)
    return sum_cells(cells) if cells else empty_cell()


def top_keywords(
    rows: Iterable[PerfRow], n: int, *, window: Window | None = None,
    profile: PlatformProfile | None = None, cfg: CpmConfig | None = None,
) -> list[str]:
    """The ``n`` keywords with the largest spend among keyword-targeted rows
    (optionally inside ``window``), spend descending then name."""
    cfg = cfg or CpmConfig()
    spend: dict[str, float] = defaultdict(float)
    for r in rows:
        if window is not None and not in_window(r.date, window):
            continue
        if is_keyword_row(r, profile, cfg.keyword_targeting_type):
            spend[norm_kw(r.targeting_unit)] += r.spend
    ranked = sorted(spend.items(), key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in ranked[: max(n, 0)]]


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------

def overview(
    rows: Sequence[PerfRow],
    *,
    start: date | None = None,
    end: date | None = None,
    registered: Iterable[str] | None = None,
    profile: PlatformProfile | None = None,
    position: PositionConfig | None = None,
    cfg: CpmConfig | None = None,
) -> dict:
    """Top-N keywords per advertiser by month.

    1. Rows from ``start`` to ``end`` (every ad type).
    2. Per advertiser the top set = ``cfg.top_n`` keywords by total spend
       over the whole range among keyword-targeted rows.
    3. A cell per advertiser x month (all ad types) and per advertiser x
       month x top keyword.
    4. Shares: keyword-month ``= keyword spend / advertiser ALL-ad-type spend
       that month``; keyword total ``= keyword spend / advertiser spend``;
       advertiser(-month) ``= top-set spend / advertiser spend``; the grand
       total uses the same definition summed across advertisers.
    5. Advertisers with zero spend are dropped and, with any ``registered``
       advertiser that has no rows, listed in ``empty``.

    Returns ``{months, last, advertisers: [{advertiser_id, total, months,
    keywords: [{keyword, total, share, months}], share}], total: {months,
    total, share}, empty}``; advertisers and keywords are spend-descending.
    """
    cfg = cfg or CpmConfig()
    pos = _pos_cfg(profile, position)
    rows = [r for r in rows if (start is None or r.date >= start) and (end is None or r.date <= end)]
    by_adv: dict[str | None, list[PerfRow]] = defaultdict(list)
    for r in rows:
        by_adv[r.advertiser_id].append(r)
    months = sorted({_month(r.date) for r in rows})
    advertisers: list[dict] = []
    empty: list[Any] = []
    top_spend_m: dict[str, float] = defaultdict(float)
    for adv, arows in by_adv.items():
        m_rows: dict[str, list[PerfRow]] = defaultdict(list)
        for r in arows:
            m_rows[_month(r.date)].append(r)
        m_cells = {m: cell_from_rows(rs, pos) for m, rs in m_rows.items()}
        total = _sum(m_cells.values())
        if total.spend <= 0:
            empty.append(adv)
            continue
        tops = top_keywords(arows, cfg.top_n, profile=profile, cfg=cfg)
        kws: list[dict] = []
        adv_top_m: dict[str, float] = defaultdict(float)
        for k in tops:
            km: dict[str, list[PerfRow]] = defaultdict(list)
            for r in arows:
                if is_keyword_row(r, profile, cfg.keyword_targeting_type) and norm_kw(r.targeting_unit) == k:
                    km[_month(r.date)].append(r)
            kc = {m: cell_from_rows(rs, pos) for m, rs in km.items()}
            ktot = _sum(kc.values())
            for m, c in kc.items():
                adv_top_m[m] += c.spend
            kws.append({
                "keyword": k,
                "total": cell_dict(ktot),
                "share": pct(ktot.spend, total.spend),
                "months": {m: cell_dict(c, share=pct(c.spend, m_cells[m].spend)) for m, c in sorted(kc.items())},
            })
        kws.sort(key=lambda d: (-d["total"]["spend"], d["keyword"]))
        for m, v in adv_top_m.items():
            top_spend_m[m] += v
        advertisers.append({
            "advertiser_id": adv,
            "total": cell_dict(total),
            "months": {m: cell_dict(c, share=pct(adv_top_m.get(m, 0.0), c.spend)) for m, c in sorted(m_cells.items())},
            "keywords": kws,
            "share": pct(sum(adv_top_m.values()), total.spend),
            "_cells": m_cells,
        })
    advertisers.sort(key=lambda d: (-d["total"]["spend"], str(d["advertiser_id"])))
    tot_m = {
        m: _sum(a["_cells"][m] for a in advertisers if m in a["_cells"]) for m in months
    }
    grand = _sum(tot_m.values())
    for a in advertisers:
        del a["_cells"]
    seen = {a["advertiser_id"] for a in advertisers}
    for adv in registered or ():
        if adv not in seen and adv not in empty:
            empty.append(adv)
    return {
        "months": months,
        "last": max((r.date for r in rows), default=None),
        "advertisers": advertisers,
        "total": {
            "months": {m: cell_dict(c, share=pct(top_spend_m.get(m, 0.0), c.spend)) for m, c in tot_m.items()},
            "total": cell_dict(grand),
            "share": pct(sum(top_spend_m.values()), grand.spend),
        },
        "empty": empty,
    }


# ---------------------------------------------------------------------------
# daily tracker
# ---------------------------------------------------------------------------

def _sov_sums(market: Sequence[MarketRow], lo: date, hi: date, kws: set[str] | None) -> tuple[float, float]:
    own = tot = 0.0
    for m in market:
        if not (lo <= m.date <= hi):
            continue
        if kws is not None and norm_kw(m.targeting_unit) not in kws:
            continue
        v = m.ad_impressions or 0.0
        tot += v
        if m.is_own:
            own += v
    return own, tot


def daily_tracker(
    rows: Sequence[PerfRow],
    *,
    tracker_start: date,
    references: Mapping[str, Window] | None = None,
    anchor: date | None = None,
    market_rows: Sequence[MarketRow] | Mapping[Any, Sequence[MarketRow]] | None = None,
    only: Iterable[str] | None = None,
    snapshots: Sequence[Mapping[str, Any]] | None = None,
    profile: PlatformProfile | None = None,
    position: PositionConfig | None = None,
    cfg: CpmConfig | None = None,
) -> dict:
    """Daily CPM tracker against a baseline week and fixed reference periods.

    Periods: each label of ``references`` (summed as one cell; its single
    days are not kept), ``"base"`` = the ``cfg.baseline_days`` days right
    before ``tracker_start``, every ISO day from the baseline start, and
    ``"mtd"`` = from the first day of the month containing ``anchor``
    (default: latest row date, which already absorbs any feed lag) through
    ``anchor``.

    Series per advertiser: ``"all"`` (every ad type), ``"kw"``
    (keyword-targeted rows), ``"top"`` (the advertiser's ``cfg.top_n``
    keywords by month-to-date spend, a set fixed for the whole tracker so a
    day's move is price and not a reshuffled list; reference periods are
    read on the same set) and ``"k:<keyword>"`` for each top keyword.

    Share of voice (optional ``market_rows``, one sequence for a single
    advertiser or a mapping per advertiser) is attached per cell as ``sov``
    = own / all ad impressions summed over the period; ``all`` and ``kw``
    read every tracked keyword, ``top`` and ``k:`` their own keywords.
    Reference periods count only keywords still tracked month-to-date
    (like-for-like guard).

    Column span = ``max(cfg.track_days, days from start to the last tracked
    day)``.  Advertisers with no ``all`` spend are dropped; the rest are
    ordered by baseline spend descending; ``only`` keeps a named subset and
    the total follows that filter.

    Returns ``{days, base_days, base: (first, last), month_start,
    advertisers: [{advertiser_id, series: {series: {period: cell}}, tops,
    first}], total: {series: {period: cell}}, last, day_n, first_total,
    rivals}``.  Cells are dicts with ``spend, impressions, sales, cpm,
    position, roas, days`` (+ ``sov`` when available).
    """
    cfg = cfg or CpmConfig()
    pos = _pos_cfg(profile, position)
    refs = dict(references or {})
    rows = list(rows)
    if anchor is None:
        anchor = max((r.date for r in rows), default=tracker_start)
    rows = [r for r in rows if r.date <= anchor]
    b0 = tracker_start - timedelta(days=cfg.baseline_days)
    b1 = tracker_start - timedelta(days=1)
    base_days = [b0 + timedelta(days=i) for i in range(cfg.baseline_days)]
    month_start = anchor.replace(day=1)
    mtd_w = Window(month_start, anchor)
    tracked = sorted({r.date for r in rows if r.date >= tracker_start})
    last = tracked[-1] if tracked else None
    span = max(cfg.track_days, ((last - tracker_start).days + 1) if last else 0)
    cols = [tracker_start + timedelta(days=i) for i in range(span)]
    day_n = sum(1 for d in cols if last is not None and d <= last)
    day_periods = base_days + [d for d in cols if d not in base_days]

    def market_for(adv: Any) -> Sequence[MarketRow]:
        if market_rows is None:
            return ()
        if isinstance(market_rows, Mapping):
            return market_rows.get(adv, ())
        return market_rows

    by_adv: dict[Any, list[PerfRow]] = defaultdict(list)
    for r in rows:
        by_adv[r.advertiser_id].append(r)
    keep = None if only is None else set(only)

    period_windows: dict[str, tuple[date, date]] = {lbl: (w.start, w.end) for lbl, w in refs.items()}
    period_windows["base"] = (b0, b1)
    period_windows["mtd"] = (month_start, anchor)
    for d in day_periods:
        period_windows[d.isoformat()] = (d, d)

    advertisers: list[dict] = []
    raw_cells: dict[Any, dict[str, dict[str, MetricCell]]] = {}
    sov_parts: dict[str, dict[str, list[tuple[float, float]]]] = defaultdict(lambda: defaultdict(list))
    rivals_out: dict[tuple[Any, str], dict] = {}
    for adv, arows in by_adv.items():
        if keep is not None and adv not in keep:
            continue
        tops = top_keywords(arows, cfg.top_n, window=mtd_w, profile=profile, cfg=cfg)
        topset = set(tops)
        series_rows: dict[str, list[PerfRow]] = defaultdict(list)
        for r in arows:
            series_rows["all"].append(r)
            if is_keyword_row(r, profile, cfg.keyword_targeting_type):
                series_rows["kw"].append(r)
                k = norm_kw(r.targeting_unit)
                if k in topset:
                    series_rows["top"].append(r)
                    series_rows[f"k:{k}"].append(r)
        cells: dict[str, dict[str, MetricCell]] = {}
        for s in ["all", "kw", "top"] + [f"k:{k}" for k in tops]:
            srows = series_rows.get(s, [])
            per: dict[str, MetricCell] = {}
            for label, (lo, hi) in period_windows.items():
                sel = [r for r in srows if lo <= r.date <= hi]
                if sel:
                    per[label] = cell_from_rows(sel, pos)
            cells[s] = per
        if sum(c.spend for c in cells["all"].values()) <= 0 and not any(r.spend > 0 for r in arows):
            continue
        if not any(r.spend > 0 for r in arows):
            continue
        market = market_for(adv)
        sov: dict[str, dict[str, float | None]] = {}
        if market:
            still = tracked_keywords(market, mtd_w)
            for s in cells:
                if s in ("all", "kw"):
                    kws: set[str] | None = None
                elif s == "top":
                    kws = topset
                else:
                    kws = {s[2:]}
                sov[s] = {}
                for label, (lo, hi) in period_windows.items():
                    scope = kws
                    if label in refs:
                        scope = still if kws is None else (kws & still)
                    own, tot = _sov_sums(market, lo, hi, scope)
                    sov[s][label] = pct(own, tot)
                    if s in ("all", "kw", "top"):
                        sov_parts[s][label].append((own, tot))
            pw = {lbl: Window(lo, hi) for lbl, (lo, hi) in period_windows.items()}
            for k, per in _rivals(market, pw, tops, cfg).items():
                rivals_out[(adv, k)] = per
        mtd_spend = {k: (cells[f"k:{k}"].get("mtd") or empty_cell()).spend for k in tops}
        tops = sorted(tops, key=lambda k: (-mtd_spend[k], k))
        raw_cells[adv] = cells
        advertisers.append({
            "advertiser_id": adv,
            "series": {
                s: {
                    label: cell_dict(c, **({"sov": sov[s].get(label)} if s in sov else {}))
                    for label, c in per.items()
                }
                for s, per in cells.items()
            },
            "tops": tops,
            "first": first_seen(snapshots, str(adv)) if snapshots else {},
        })
    advertisers.sort(
        key=lambda a: (-(raw_cells[a["advertiser_id"]]["all"].get("base") or empty_cell()).spend, str(a["advertiser_id"]))
    )
    total: dict[str, dict[str, dict]] = {}
    for s in ("all", "kw", "top"):
        total[s] = {}
        for label in period_windows:
            cs = [raw_cells[a["advertiser_id"]][s][label] for a in advertisers if label in raw_cells[a["advertiser_id"]][s]]
            if not cs:
                continue
            extra = {}
            if label in sov_parts.get(s, {}):
                parts = sov_parts[s][label]
                extra["sov"] = pct(sum(p[0] for p in parts), sum(p[1] for p in parts))
            total[s][label] = cell_dict(_sum(cs), **extra)
    return {
        "days": cols,
        "base_days": base_days,
        "base": (b0, b1),
        "month_start": month_start,
        "advertisers": advertisers,
        "total": total,
        "last": last,
        "day_n": day_n,
        "first_total": first_seen(snapshots, TOTAL_KEY) if snapshots else {},
        "rivals": rivals_out,
    }


# ---------------------------------------------------------------------------
# window A vs window B
# ---------------------------------------------------------------------------

def window_compare(
    rows: Sequence[PerfRow],
    a: Window,
    b: Window,
    n: int | None = None,
    *,
    profile: PlatformProfile | None = None,
    position: PositionConfig | None = None,
    cfg: CpmConfig | None = None,
) -> dict:
    """Compare two windows for one advertiser, keyword by keyword.

    1. ``n`` is clamped to ``cfg.window_top_min..cfg.window_top_max`` (1..50,
       default ``cfg.top_n``); the top set = top-n keywords by spend in
       window A.
    2. Each row goes to one key: non-keyword ad types -> ``"~other"``, a top
       keyword -> itself, any other keyword -> ``"~kw"``.
    3. ``A`` / ``B`` = sum over every key (so totals include all ad types).
    4. Per top keyword ``share_a = a.spend / A.spend`` (same for B), ordered
       by A spend descending; ``top_a`` / ``top_b`` = sum of the top set and
       ``share_a`` / ``share_b`` its share of the total.

    Returns ``{A, B, top: [{keyword, a, b, share_a, share_b}], top_a, top_b,
    share_a, share_b, rest: {"~kw": {a, b}, "~other": {a, b}}}``.
    """
    cfg = cfg or CpmConfig()
    pos = _pos_cfg(profile, position)
    n = cfg.top_n if n is None else n
    n = max(cfg.window_top_min, min(cfg.window_top_max, int(n)))
    tops = top_keywords(rows, n, window=a, profile=profile, cfg=cfg)
    topset = set(tops)
    buckets: dict[str, dict[str, list[PerfRow]]] = defaultdict(lambda: {"a": [], "b": []})
    for r in rows:
        ina, inb = in_window(r.date, a), in_window(r.date, b)
        if not (ina or inb):
            continue
        if not is_keyword_row(r, profile, cfg.keyword_targeting_type):
            key = OTHER
        else:
            k = norm_kw(r.targeting_unit)
            key = k if k in topset else KW_REST
        if ina:
            buckets[key]["a"].append(r)
        if inb:
            buckets[key]["b"].append(r)
    cells = {k: (cell_from_rows(v["a"], pos), cell_from_rows(v["b"], pos)) for k, v in buckets.items()}
    A = _sum(c[0] for c in cells.values())
    B = _sum(c[1] for c in cells.values())
    top = []
    for k in tops:
        ca, cb = cells.get(k, (empty_cell(), empty_cell()))
        top.append({
            "keyword": k, "a": cell_dict(ca), "b": cell_dict(cb),
            "share_a": pct(ca.spend, A.spend), "share_b": pct(cb.spend, B.spend),
        })
    top.sort(key=lambda d: (-d["a"]["spend"], d["keyword"]))
    top_a = _sum(cells[k][0] for k in tops if k in cells)
    top_b = _sum(cells[k][1] for k in tops if k in cells)
    return {
        "A": cell_dict(A), "B": cell_dict(B), "top": top,
        "top_a": cell_dict(top_a), "top_b": cell_dict(top_b),
        "share_a": pct(top_a.spend, A.spend), "share_b": pct(top_b.spend, B.spend),
        "rest": {
            k: {"a": cell_dict(cells[k][0]), "b": cell_dict(cells[k][1])}
            for k in (KW_REST, OTHER) if k in cells
        },
    }


# ---------------------------------------------------------------------------
# CPM per position
# ---------------------------------------------------------------------------

def position_slot(position: float | None, cfg: PositionConfig | None = None) -> int | None:
    """Position bucket of a row: ``min(floor(position), cap)`` (the cap, 21
    by default, means "21+"); ``None`` for a missing or invalid position."""
    cfg = cfg or PositionConfig()
    if not position_is_valid(position, cfg):
        return None
    return int(min(math.floor(float(position)), cfg.cap))  # type: ignore[arg-type]


def cpm_by_position(
    rows: Iterable[PerfRow],
    a: Window,
    b: Window | None = None,
    *,
    profile: PlatformProfile | None = None,
    position: PositionConfig | None = None,
) -> list[dict]:
    """CPM actually paid at each observed position, window A vs window B.

    Rows with a valid position are bucketed with ``position_slot``; each
    bucket's CPM comes from its own spend and impressions.  Only positions
    with impressions > 0 in A or B are returned: a position the ad was never
    seen at is never shown and nothing is interpolated between observed
    positions.  ``share_a`` / ``share_b`` = bucket impressions / total
    impressions across buckets in that window (``None`` when the total is 0).

    Returns ``[{position, label, a, b, share_a, share_b}]`` best position
    first (ascending when lower is better).
    """
    pos = _pos_cfg(profile, position)
    buckets: dict[int, dict[str, list[PerfRow]]] = defaultdict(lambda: {"a": [], "b": []})
    for r in rows:
        slot = position_slot(r.position, pos)
        if slot is None:
            continue
        if in_window(r.date, a):
            buckets[slot]["a"].append(r)
        if b is not None and in_window(r.date, b):
            buckets[slot]["b"].append(r)
    cells = {s: (cell_from_rows(v["a"], pos), cell_from_rows(v["b"], pos)) for s, v in buckets.items()}
    cells = {s: c for s, c in cells.items() if c[0].impressions > 0 or c[1].impressions > 0}
    tot_a = sum(c[0].impressions for c in cells.values())
    tot_b = sum(c[1].impressions for c in cells.values())
    out = []
    for s in sorted(cells, reverse=not pos.lower_is_better):
        ca, cb = cells[s]
        out.append({
            "position": s,
            "label": f"{s}+" if s >= pos.cap else str(s),
            "a": cell_dict(ca), "b": cell_dict(cb),
            "share_a": pct(ca.impressions, tot_a), "share_b": pct(cb.impressions, tot_b),
        })
    return out


# ---------------------------------------------------------------------------
# single keyword
# ---------------------------------------------------------------------------

def keyword_view(
    rows: Sequence[PerfRow],
    keyword: str,
    a: Window,
    b: Window,
    *,
    bids: Sequence[BidRecord] | None = None,
    entities: Sequence[Entity] | None = None,
    rulesets: Sequence[Mapping[str, Any]] | None = None,
    foreign_entity_ids: Iterable[str] | None = None,
    profile: PlatformProfile | None = None,
    position: PositionConfig | None = None,
    cfg: CpmConfig | None = None,
) -> dict:
    """Everything about one keyword of one advertiser.

    * Campaign list = campaigns with spend rows on the keyword merged with
      every campaign carrying it in the bid store (``bids``).  A campaign in
      ``foreign_entity_ids`` (campaigns of a multi-brand umbrella account
      that may belong to a sibling) is kept only if it has rows here.
      Campaigns with no spend get zero cells.  Per campaign: cells for A and
      B, last day seen, match types, ``bid_a`` = mean bid over window-A rows
      with bid > 0, its bids and the rule sets (``rules.assemble`` output)
      that hold one of its bids or the campaign itself.
    * Order: A spend desc, B spend desc, live status first, name.
    * Period aggregates ``A``, ``B`` and one per calendar month, each with
      ``days`` = number of days that had rows; one cell per day.
    * ``positions`` = ``cpm_by_position`` (observed positions only).

    Returns ``{keyword, campaigns: [...], A, B, months, days, rulesets,
    positions}``.
    """
    cfg = cfg or CpmConfig()
    pos = _pos_cfg(profile, position)
    k = norm_kw(keyword)
    krows = [
        r for r in rows
        if is_keyword_row(r, profile, cfg.keyword_targeting_type) and norm_kw(r.targeting_unit) == k
    ]
    by_ent: dict[str, list[PerfRow]] = defaultdict(list)
    for r in krows:
        by_ent[r.entity_id].append(r)
    ent_by_id = {e.entity_id: e for e in (entities or ())}
    foreign = set(foreign_entity_ids or ())
    kbids: dict[str, list[BidRecord]] = defaultdict(list)
    for bd in bids or ():
        if norm_kw(bd.targeting_unit) == k:
            kbids[bd.entity_id].append(bd)
    ids = list(by_ent)
    for eid in kbids:
        if eid not in by_ent and eid not in foreign:
            ids.append(eid)
    bid_ids = {bd.bid_id for bl in kbids.values() for bd in bl if bd.bid_id is not None}
    attached: list[Mapping[str, Any]] = []
    rs_for: dict[str, list[tuple[Any, Any, Any]]] = defaultdict(list)
    for rs in rulesets or ():
        hit_entities: set[str] = set()
        for m in rs.get("keywords", ()) or ():
            if m.get("bid_id") in bid_ids and m.get("entity_id") in ids:
                hit_entities.add(m["entity_id"])
        for m in rs.get("campaigns", ()) or ():
            if m.get("entity_id") in ids:
                hit_entities.add(m["entity_id"])
        if hit_entities:
            attached.append(rs)
            for eid in hit_entities:
                rs_for[eid].append((rs.get("name"), rs.get("type"), rs.get("state")))
    camps: list[dict] = []
    for eid in ids:
        ers = by_ent.get(eid, [])
        ra = [r for r in ers if in_window(r.date, a)]
        rb = [r for r in ers if in_window(r.date, b)]
        abids = [r.bid for r in ra if r.bid is not None and r.bid > 0]
        ent = ent_by_id.get(eid)
        status = (ent.status if ent else None) or next((r.status for r in reversed(ers) if r.status), None)
        live = True
        if profile is not None and status is not None:
            live = profile.liveness_class(status).value == "live"
        camps.append({
            "entity_id": eid,
            "name": (ent.name if ent else None) or next((r.entity_name for r in ers if r.entity_name), None),
            "ad_type": (ent.ad_type if ent else None) or next((r.ad_type for r in ers if r.ad_type), None),
            "status": status,
            "live": live,
            "bids": [
                {"bid_id": bd.bid_id, "match_type": bd.match_type, "live_bid": bd.live_bid, "base_bid": bd.base_bid,
                 "status": bd.status, "is_negative": bd.is_negative, "is_archived": bd.is_archived}
                for bd in kbids.get(eid, [])
            ],
            "a": cell_dict(cell_from_rows(ra, pos)),
            "b": cell_dict(cell_from_rows(rb, pos)),
            "match_types": sorted({r.match_type for r in ers if r.match_type}),
            "bid_a": (sum(abids) / len(abids)) if abids else None,
            "last": max((r.date for r in ers), default=None),
            "rulesets": rs_for.get(eid, []),
        })
    camps.sort(key=lambda c: (-c["a"]["spend"], -c["b"]["spend"], not c["live"], str(c["name"] or c["entity_id"])))
    m_rows: dict[str, list[PerfRow]] = defaultdict(list)
    d_rows: dict[date, list[PerfRow]] = defaultdict(list)
    for r in krows:
        m_rows[_month(r.date)].append(r)
        d_rows[r.date].append(r)
    return {
        "keyword": k,
        "campaigns": camps,
        "A": cell_dict(cell_from_rows([r for r in krows if in_window(r.date, a)], pos)),
        "B": cell_dict(cell_from_rows([r for r in krows if in_window(r.date, b)], pos)),
        "months": {m: cell_dict(cell_from_rows(rs, pos)) for m, rs in sorted(m_rows.items())},
        "days": [{"date": d, **cell_dict(cell_from_rows(rs, pos))} for d, rs in sorted(d_rows.items())],
        "rulesets": list(attached),
        "positions": cpm_by_position(krows, a, b, position=pos),
    }


# ---------------------------------------------------------------------------
# category x day matrix
# ---------------------------------------------------------------------------

def keyword_category_from_market(market_rows: Iterable[MarketRow], since: date | None = None) -> dict[str, str]:
    """Map each keyword to one category: the segment under which the keyword
    has the most total impressions in the market feed (from ``since`` on)."""
    tot: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for m in market_rows:
        if m.segment is None or (since is not None and m.date < since):
            continue
        tot[norm_kw(m.targeting_unit)][m.segment] += m.impressions or 0.0
    return {k: sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for k, v in tot.items()}


def category_matrix(
    rows: Sequence[PerfRow],
    *,
    start: date,
    reference: Window | None = None,
    keyword_category: Mapping[str, str] | None = None,
    economics: Economics | None = None,
    profile: PlatformProfile | None = None,
    position: PositionConfig | None = None,
    cfg: CpmConfig | None = None,
) -> dict:
    """Category x day CPM matrix over keyword-targeted rows.

    1. Each keyword maps to one category through ``keyword_category``;
       unmapped keywords go to ``cfg.other_category_label``.
    2. Cells per advertiser x category for the ``reference`` window, each
       day from ``start`` and ``mtd`` = sum of all days from ``start``.
    3. A category whose ``mtd`` spend is below ``cfg.min_category_spend``
       (scaled by ``economics``) folds into the remainder row; rows with
       zero ``mtd`` spend are dropped.
    4. Advertisers by ``mtd`` spend descending; categories likewise with the
       remainder row always last.  Advertiser and grand totals are sums.

    Returns ``{days, last, advertisers: [{advertiser_id, total: {reference,
    mtd, days}, categories: [{category, reference, mtd, days}]}], total}``.
    """
    cfg = cfg or CpmConfig()
    pos = _pos_cfg(profile, position)
    floor = money_of(cfg, "min_category_spend", economics)
    other = cfg.other_category_label
    mapping = {norm_kw(k): v for k, v in (keyword_category or {}).items()}
    grid: dict[Any, dict[str, dict[Any, list[PerfRow]]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    days: set[date] = set()
    for r in rows:
        if not is_keyword_row(r, profile, cfg.keyword_targeting_type):
            continue
        cat = mapping.get(norm_kw(r.targeting_unit), other)
        if r.date >= start:
            grid[r.advertiser_id][cat][r.date].append(r)
            days.add(r.date)
        if in_window(r.date, reference):
            grid[r.advertiser_id][cat]["reference"].append(r)
    day_list = sorted(days)

    def pack(per: Mapping[Any, MetricCell]) -> dict:
        return {
            "reference": cell_dict(per.get("reference") or empty_cell()),
            "mtd": cell_dict(_sum(c for p, c in per.items() if p != "reference")),
            "days": {d: cell_dict(c) for d, c in sorted((p, c) for p, c in per.items() if p != "reference")},
        }

    advertisers = []
    grand: dict[Any, list[MetricCell]] = defaultdict(list)
    for adv, cats in grid.items():
        cat_cells = {c: {p: cell_from_rows(rs, pos) for p, rs in per.items()} for c, per in cats.items()}
        merged: dict[str, dict[Any, list[MetricCell]]] = defaultdict(lambda: defaultdict(list))
        for c, per in cat_cells.items():
            mtd = sum(cell.spend for p, cell in per.items() if p != "reference")
            target = other if (c != other and mtd < floor) else c
            for p, cell in per.items():
                merged[target][p].append(cell)
        final = {c: {p: _sum(cs) for p, cs in per.items()} for c, per in merged.items()}
        rows_out = []
        for c, per in final.items():
            packed = pack(per)
            if packed["mtd"]["spend"] <= 0:
                continue
            rows_out.append({"category": c, **packed, "_per": per})
        if not rows_out:
            continue
        rows_out.sort(key=lambda d: (d["category"] == other, -d["mtd"]["spend"], d["category"]))
        tot_per: dict[Any, list[MetricCell]] = defaultdict(list)
        for d in rows_out:
            for p, cell in d.pop("_per").items():
                tot_per[p].append(cell)
                grand[p].append(cell)
        advertisers.append({
            "advertiser_id": adv,
            "total": pack({p: _sum(cs) for p, cs in tot_per.items()}),
            "categories": rows_out,
        })
    advertisers.sort(key=lambda d: (-d["total"]["mtd"]["spend"], str(d["advertiser_id"])))
    return {
        "days": day_list,
        "last": day_list[-1] if day_list else None,
        "advertisers": advertisers,
        "total": pack({p: _sum(cs) for p, cs in grand.items()}),
    }
