"""Keyword overpay check: are we paying too much for a keyword's position,
and why?  One advertiser, one day, a keyword list; one verdict row per
campaign x keyword x geo with flags from three independent hypotheses.

**H1 - the bidder is not optimising** (needs the bidder's decision log)

=============  ============================================================
``BLIND``      position found on fewer than 60% of decisions
``OVERSHOOT``  better than the target on >= 50% of decisions and the bid was
               not cut over the day (last bid >= first bid)
``CAP``        pinned at the ceiling on >= 30% of decisions and (in-band hit
               rate unknown or < 50%)
``FLAT``       >= 10 decisions and (max bid - min bid) / median bid < 2%
=============  ============================================================

**H2 - our own bid is our floor** (``SELF_FLOOR``).  In a second-price
auction the next bidder beneath sets the price; if that next bidder is our
own campaign on the same keyword and geo, we set our own price.  Runs only
when the platform's auction type is ``second_price``.  It is an assumption
about the auction that has not been verified against billed prices.

**H3 - CPM is high for the position** (``OVERPAY``): CPM more than 1.25x
the median CPM of the advertiser's own rows in the same position band that
day.  The reference is internal, so it detects inconsistency between the
advertiser's own geos and campaigns, not overpayment against the market;
sparse keywords (< 3 rows per band) get no verdict.

Every threshold is a judgment value (``CpmConfig``).  This is a one-day
diagnostic, not a monitored metric.  Unlike the guardrail audit, ``BLIND``
does not suppress ``CAP`` here, and "hit" means *inside* the target range.

Band edge: the row position is an impression-weighted mean, so 1.2 falls in
band ``2-3``; only exactly 1.0 is band ``1``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..adapters.base import PlatformProfile
from ..config import PositionConfig
from ..metrics import cell_from_rows
from ..schema import BidDecision, BidRecord, Entity, PerfRow
from ._util import is_keyword_row, median, norm_kw, to_local
from .config import CpmConfig
from .guardrails import position_seen

__all__ = [
    "NO_BAND", "HYPOTHESES", "AgentDay", "band_names", "position_band", "geo_from_name",
    "agent_day_log", "h1_flags", "self_floor_flag", "band_reference", "overpay_flag",
    "check_overpay", "render_overpay_markdown",
]

NO_BAND = "—"
HYPOTHESES = {"BLIND": "H1", "OVERSHOOT": "H1", "CAP": "H1", "FLAT": "H1", "SELF_FLOOR": "H2", "OVERPAY": "H3"}


@dataclass(frozen=True)
class AgentDay:
    """One bid's decisions on one local day (optionally one geo).  ``None``
    = the log lacks the field; the flags depending on it are skipped."""

    n: int = 0
    target_min: float | None = None
    target_max: float | None = None
    position_median: float | None = None
    seen: int | None = None
    over: int | None = None        # found and better than the target
    hit: int | None = None         # found and inside the target range
    cap: int | None = None         # pinned at the ceiling
    bid_median: float | None = None
    bid_min: float | None = None
    bid_max: float | None = None
    bid_first: float | None = None
    bid_last: float | None = None
    geo: str | None = None


def band_names(cfg: CpmConfig | None = None) -> list[str]:
    """Labels of the position bands from their upper bounds: ``1 | 2-3 |
    4-6 | 7-10 | 11+`` for the default ``(1, 3, 6, 10, 100)``."""
    cfg = cfg or CpmConfig()
    out, prev = [], None
    bounds = cfg.overpay_bands
    for i, ub in enumerate(bounds):
        lo = 1.0 if prev is None else prev + 1
        if i == len(bounds) - 1 and len(bounds) > 1:
            out.append(f"{lo:g}+")
        elif lo == ub:
            out.append(f"{ub:g}")
        else:
            out.append(f"{lo:g}-{ub:g}")
        prev = ub
    return out


def position_band(position: float | None, cfg: CpmConfig | None = None) -> str:
    """Position band of a row: the first band whose upper bound is >= the
    position; ``"—"`` when the position is missing or beyond the last bound."""
    cfg = cfg or CpmConfig()
    if position is None:
        return NO_BAND
    for ub, label in zip(cfg.overpay_bands, band_names(cfg)):
        if position <= ub:
            return label
    return NO_BAND


def geo_from_name(name: str | None, gazetteer: Iterable[str]) -> str | None:
    """Parse a geo out of a campaign name: whole-word, case-insensitive
    match against a caller-supplied geo list, longest match wins; ``None``
    when nothing matches.  No geo list is bundled."""
    if not name:
        return None
    best: str | None = None
    for g in gazetteer:
        if g and re.search(rf"(?<!\w){re.escape(g)}(?!\w)", name, flags=re.IGNORECASE):
            if best is None or len(g) > len(best):
                best = g
    return best


def _bid_key(bid_id: Any, entity_id: Any, keyword: str | None) -> Any:
    return ("id", str(bid_id)) if bid_id is not None else ("pair", entity_id, norm_kw(keyword))


def agent_day_log(
    decisions: Iterable[BidDecision],
    day: date,
    *,
    tz: Any = None,
    position_reported: bool = True,
    lower_is_better: bool = True,
    cfg: CpmConfig | None = None,
) -> dict[Any, list[AgentDay]]:
    """Summarise the bidder's decisions of one local day per bid x geo.

    Scope: decisions with ``new_bid > 0``, not failed, whose local date
    (``tz``) is ``day``.  Per group: ``n``; target range; median found
    position; ``seen`` = found (1..60); ``over`` = found and better than
    the target; ``hit`` = found and inside the target range; ``cap`` =
    at the ceiling; median / min / max bid; first and last bid of the day.
    ``seen/over/hit`` are ``None`` when ``position_reported`` is False,
    ``hit`` also without a target max, ``cap`` without a ceiling flag.

    Returns ``{bid key: [AgentDay per geo]}`` where the key is
    ``("id", bid_id)`` or, for a log without bid ids, ``("pair", entity_id,
    keyword)``.
    """
    cfg = cfg or CpmConfig()
    groups: dict[tuple[Any, Any], list[BidDecision]] = defaultdict(list)
    for d in decisions:
        if d.new_bid is None or d.new_bid <= 0 or d.success is False:
            continue
        if to_local(d.timestamp, tz).date() != day:
            continue
        groups[(_bid_key(d.bid_id, d.entity_id, d.targeting_unit), d.geo)].append(d)
    out: dict[Any, list[AgentDay]] = defaultdict(list)
    for (key, geo), ds in groups.items():
        ds.sort(key=lambda d: to_local(d.timestamp))
        bids = [float(d.new_bid) for d in ds]
        tmins = [d.target_position_min for d in ds if d.target_position_min is not None]
        tmaxs = [d.target_position_max for d in ds if d.target_position_max is not None]
        seen = over = hit = None
        pos_med = None
        if position_reported:
            found = [d for d in ds if position_seen(d.observed_position, cfg)]
            seen = len(found)
            pos_med = median(d.observed_position for d in found)  # type: ignore[misc]
            if tmins or tmaxs:
                over = 0
                hit = 0 if (tmins and tmaxs) else None
                for d in found:
                    p, lo, hi = d.observed_position, d.target_position_min, d.target_position_max
                    best, worst = (lo, hi) if lower_is_better else (hi, lo)
                    if best is not None and (p < best if lower_is_better else p > best):  # type: ignore[operator]
                        over += 1
                    if hit is not None and lo is not None and hi is not None and lo <= p <= hi:  # type: ignore[operator]
                        hit += 1
                if not (tmins if lower_is_better else tmaxs):
                    over = None
        cap = sum(1 for d in ds if d.at_ceiling) if any(d.at_ceiling is not None for d in ds) else None
        out[key].append(AgentDay(
            n=len(ds), target_min=min(tmins) if tmins else None, target_max=max(tmaxs) if tmaxs else None,
            position_median=pos_med, seen=seen, over=over, hit=hit, cap=cap,
            bid_median=median(bids), bid_min=min(bids), bid_max=max(bids),
            bid_first=bids[0], bid_last=bids[-1], geo=geo,
        ))
    return dict(out)


def _flag(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "hypothesis": HYPOTHESES[code], "detail": detail}


def h1_flags(agent: AgentDay | None, cfg: CpmConfig | None = None) -> list[dict[str, str]]:
    """H1: is the bidder failing to optimise this bid today?

    No flags without decisions.  Checked independently, in this order:

    * ``BLIND``: ``seen / n < cfg.blind_seen`` (0.60)
    * ``OVERSHOOT``: ``over / n >= cfg.overshoot_share`` (0.5) and
      ``bid_last >= bid_first``
    * ``CAP``: ``cap / n >= cfg.hot_cap`` (0.30) and (``hit`` unknown or
      ``hit / n < cfg.low_hit`` (0.50))
    * ``FLAT``: ``n >= cfg.flat_min_decisions`` (10), ``bid_median > 0`` and
      ``(bid_max - bid_min) / bid_median < cfg.flat_pct`` (0.02)

    A flag whose counter is ``None`` is skipped.  Returns
    ``[{code, hypothesis: "H1", detail}]``.
    """
    cfg = cfg or CpmConfig()
    if agent is None or agent.n == 0:
        return []
    n = agent.n
    out = []
    if agent.seen is not None and agent.seen / n < cfg.blind_seen:
        out.append(_flag("BLIND", f"position found on {agent.seen}/{n} decisions"))
    if (agent.over is not None and agent.over / n >= cfg.overshoot_share
            and agent.bid_first is not None and agent.bid_last is not None
            and agent.bid_last >= agent.bid_first):
        out.append(_flag("OVERSHOOT", f"better than target on {agent.over}/{n} decisions and the bid was not cut"))
    if agent.cap is not None and agent.cap / n >= cfg.hot_cap and (agent.hit is None or agent.hit / n < cfg.low_hit):
        hit_txt = "unknown" if agent.hit is None else f"{agent.hit}/{n}"
        out.append(_flag("CAP", f"at the ceiling on {agent.cap}/{n} decisions, target hit {hit_txt}"))
    if (n >= cfg.flat_min_decisions and agent.bid_median and agent.bid_median > 0
            and agent.bid_max is not None and agent.bid_min is not None
            and (agent.bid_max - agent.bid_min) / agent.bid_median < cfg.flat_pct):
        out.append(_flag("FLAT", f"bid moved {agent.bid_min:g}-{agent.bid_max:g} over {n} decisions"))
    return out


def self_floor_flag(cpm: float | None, next_own_bid: float | None, cfg: CpmConfig | None = None) -> bool:
    """H2 test: our next campaign beneath bids at least
    ``cfg.self_floor_pct`` (0.80) of this row's CPM:
    ``next_own_bid > 0 and next_own_bid >= 0.80 x cpm``.  Assumes the bid is
    expressed per thousand impressions like the CPM."""
    cfg = cfg or CpmConfig()
    if cpm is None or next_own_bid is None or next_own_bid <= 0:
        return False
    return next_own_bid >= cfg.self_floor_pct * cpm


def band_reference(rows: Iterable[Mapping[str, Any]], cfg: CpmConfig | None = None) -> dict[tuple[str, str], float]:
    """H3 reference: median CPM per (keyword, position band) across all the
    given rows that have both a CPM and a position; a group needs at least
    ``cfg.overpay_ref_min_rows`` (3) rows.  Returns ``{(keyword, band): cpm}``."""
    cfg = cfg or CpmConfig()
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in rows:
        if r.get("cpm") is None or r.get("position") is None:
            continue
        band = position_band(r["position"], cfg)
        if band != NO_BAND:
            groups[(r["keyword"], band)].append(r["cpm"])
    return {k: median(v) for k, v in groups.items() if len(v) >= cfg.overpay_ref_min_rows}  # type: ignore[misc]


def overpay_flag(cpm: float | None, ref_cpm: float | None, cfg: CpmConfig | None = None) -> bool:
    """H3 test: ``cpm / ref_cpm > cfg.overpay_ratio`` (strictly above 1.25)."""
    cfg = cfg or CpmConfig()
    if cpm is None or not ref_cpm:
        return False
    return cpm / ref_cpm > cfg.overpay_ratio


def check_overpay(
    perf_rows: Iterable[PerfRow],
    day: date,
    keywords: Iterable[str],
    *,
    bids: Sequence[BidRecord] = (),
    decisions: Sequence[BidDecision] = (),
    entities: Sequence[Entity] = (),
    auction_type: str | None = None,
    tz: Any = None,
    position_reported: bool = True,
    geo_resolver: Callable[[str | None], str | None] | None = None,
    top_geos: int | None = None,
    profile: PlatformProfile | None = None,
    position: PositionConfig | None = None,
    cfg: CpmConfig | None = None,
) -> dict:
    """Run the three-hypothesis overpay check for one advertiser and day.

    Rows: keyword-targeted perf rows of ``day`` on ``keywords`` with spend >
    0, summed per campaign x keyword x geo.  ``cpm = 1000 x spend /
    impressions``; ``position`` = impression-weighted mean over valid
    positions; ``bid`` = max bid seen.  Geo comes from the row, else from
    ``geo_resolver(campaign name)`` (see ``geo_from_name``), else
    ``cfg.unknown_geo_label``.

    Each row gets its bid-store record (negative and archived bids
    excluded) and the bidder's day record for the same geo if one exists,
    else the bid's busiest record.  ``mode`` = ``"agent"`` when the bid has
    any decision that day, else ``"static"``.

    * H1: ``h1_flags`` on the bidder record.
    * H2 (only when the auction type, given or from ``profile``, is
      ``second_price`` and the profile's ``bid_unit`` is ``"cpm"``; a bid in
      another unit cannot be compared with a CPM, so the check is skipped
      and ``h2_skip_reason`` says why): inside one (geo, keyword) rows are ordered best
      position first, rows without a position last.  For each row with a
      CPM the next own row beneath that has a position supplies
      ``next bid`` = its last bidder bid of the day if bidder-driven, else
      its live bid; flag ``SELF_FLOOR`` per ``self_floor_flag``.
    * H3: ``band_reference`` over **all** rows of the day (every geo and
      campaign, not only the displayed geos); flag ``OVERPAY`` per
      ``overpay_flag``.

    Display scope: the ``top_geos`` (10) geos by spend; rows ordered by geo
    spend, keyword, position.

    Returns ``{day, rows: [{geo, keyword, entity_id, entity_name, mode,
    spend, impressions, cpm, position, band, ref_cpm, live_bid, base_bid,
    row_bid, agent: AgentDay | None, flags: [{code, hypothesis, detail}]}],
    tally: {H1, H2, H3}, geos, h2_checked, h2_skip_reason}``.

    Position bands are rank bands (band ``1`` is the best slot), so H3 pools
    sensibly only where a lower position number is better; on a platform
    with ``lower_is_better=False`` set ``cfg.overpay_bands`` to that
    platform's scale.
    """
    cfg = cfg or CpmConfig()
    if auction_type is None and profile is not None:
        auction_type = profile.auction_type
    if tz is None and profile is not None:
        tz = profile.timezone
    if position is None and profile is not None:
        p = profile.position
        position = PositionConfig(valid_min=p.valid_min, valid_max=p.valid_max, cap=p.cap,
                                  lower_is_better=p.lower_is_better)
    lower = position.lower_is_better if position is not None else True
    top_n = cfg.overpay_top_geos if top_geos is None else top_geos
    wanted = {norm_kw(k) for k in keywords}
    names = {e.entity_id: e.name for e in entities}

    grouped: dict[tuple[str, str, str], list[PerfRow]] = defaultdict(list)
    for r in perf_rows:
        if r.date != day or r.spend <= 0 or not is_keyword_row(r, profile, cfg.keyword_targeting_type):
            continue
        k = norm_kw(r.targeting_unit)
        if k not in wanted:
            continue
        name = r.entity_name or names.get(r.entity_id)
        geo = r.geo or (geo_resolver(name) if geo_resolver else None) or cfg.unknown_geo_label
        grouped[(r.entity_id, k, geo)].append(r)

    bid_idx: dict[tuple[str, str], list[BidRecord]] = defaultdict(list)
    for bd in bids:
        if not bd.is_negative and not bd.is_archived:
            bid_idx[(bd.entity_id, norm_kw(bd.targeting_unit))].append(bd)
    log = agent_day_log(decisions, day, tz=tz, position_reported=position_reported,
                        lower_is_better=lower, cfg=cfg) if decisions else {}

    rows: list[dict] = []
    for (eid, k, geo), rs in grouped.items():
        cell = cell_from_rows(rs, position)
        cands = bid_idx.get((eid, k), [])
        bid = next((bd for bd in cands if bd.geo == geo), cands[0] if cands else None)
        recs: list[AgentDay] = []
        if bid is not None and bid.bid_id is not None:
            recs = log.get(_bid_key(bid.bid_id, None, None), [])
        if not recs:
            recs = log.get(_bid_key(None, eid, k), [])
        same = [a for a in recs if a.geo == geo]
        agent = max(same or recs, key=lambda a: a.n) if recs else None
        row_bids = [r.bid for r in rs if r.bid is not None]
        rows.append({
            "geo": geo, "keyword": k, "entity_id": eid,
            "entity_name": next((r.entity_name for r in rs if r.entity_name), names.get(eid)),
            "mode": "agent" if recs else "static",
            "spend": cell.spend, "impressions": cell.impressions, "sales": cell.sales,
            "cpm": cell.cpm, "position": cell.position,
            "band": position_band(cell.position, cfg), "ref_cpm": None,
            "live_bid": bid.live_bid if bid else None, "base_bid": bid.base_bid if bid else None,
            "row_bid": max(row_bids) if row_bids else None,
            "agent": agent, "flags": h1_flags(agent, cfg),
        })

    # H2: our own next campaign beneath sets the price (second-price auctions only)
    # H2 compares a bid with a CPM, so it needs the bid quoted per thousand impressions.
    bid_unit = profile.bid_unit if profile is not None else "cpm"
    h2 = auction_type == "second_price" and bid_unit == "cpm"
    h2_skip = (None if h2 else
               "bids are not quoted per thousand impressions" if auction_type == "second_price"
               else "the auction is not declared second-price")
    sentinel = 999.0 if lower else -999.0

    def pos_key(r: dict) -> float:
        p = r["position"] if r["position"] is not None else sentinel
        return p if lower else -p

    by_gk: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        by_gk[(r["geo"], r["keyword"])].append(r)
    for group in by_gk.values():
        group.sort(key=lambda r: (pos_key(r), r["entity_id"]))
        if not h2:
            continue
        for i, r in enumerate(group):
            if r["cpm"] is None:
                continue
            below = [x for x in group[i + 1:] if x["position"] is not None]
            if not below:
                continue
            nb = below[0]
            nb_bid = nb["agent"].bid_last if (nb["mode"] == "agent" and nb["agent"] is not None) else nb["live_bid"]
            if self_floor_flag(r["cpm"], nb_bid, cfg):
                r["flags"].append(_flag(
                    "SELF_FLOOR",
                    f"own campaign {nb['entity_name'] or nb['entity_id']} beneath bids {nb_bid:g}, "
                    f"{100 * nb_bid / r['cpm']:.0f}% of this CPM",
                ))

    # H3: CPM high for the position band (reference across every row of the day)
    ref = band_reference(rows, cfg)
    for r in rows:
        r["ref_cpm"] = ref.get((r["keyword"], r["band"]))
        if overpay_flag(r["cpm"], r["ref_cpm"], cfg):
            r["flags"].append(_flag("OVERPAY", f"CPM {r['cpm'] / r['ref_cpm']:.2f}x the band median"))

    geo_spend: dict[str, float] = defaultdict(float)
    for r in rows:
        geo_spend[r["geo"]] += r["spend"]
    geos = [g for g, _ in sorted(geo_spend.items(), key=lambda kv: (-kv[1], kv[0]))][: max(top_n, 0)]
    rank = {g: i for i, g in enumerate(geos)}
    shown = [r for r in rows if r["geo"] in rank]
    shown.sort(key=lambda r: (rank[r["geo"]], r["keyword"], pos_key(r), r["entity_id"]))
    tally = {"H1": 0, "H2": 0, "H3": 0}
    for r in shown:
        for f in r["flags"]:
            tally[f["hypothesis"]] += 1
    return {"day": day, "rows": shown, "tally": tally, "geos": geos, "h2_checked": h2, "h2_skip_reason": h2_skip}


def _n(x: float | None, digits: int = 0) -> str:
    return "" if x is None else f"{x:.{digits}f}"


def render_overpay_markdown(result: Mapping[str, Any]) -> str:
    """Render a ``check_overpay`` result as a markdown table followed by the
    tally line ``H1 n | H2 n | H3 n`` (H2 is marked "not checked" when the
    auction is not second-price)."""
    head = ("| geo | keyword | campaign | mode | spend | CPM | position | ref CPM | live/base bid | "
            "bidder (target, median position, first->last bid, n) | flags |")
    lines = [head, "|" + "---|" * 11]
    for r in result["rows"]:
        a: AgentDay | None = r["agent"]
        agent = ""
        if a is not None:
            agent = (f"{_n(a.target_min)}-{_n(a.target_max)}, {_n(a.position_median, 1)}, "
                     f"{_n(a.bid_first)}->{_n(a.bid_last)} ({a.n})")
        lines.append(
            f"| {r['geo']} | {r['keyword']} | {r['entity_name'] or r['entity_id']} | {r['mode']} | "
            f"{_n(r['spend'])} | {_n(r['cpm'], 1)} | {_n(r['position'], 1)} | {_n(r['ref_cpm'], 1)} | "
            f"{_n(r['live_bid'])}/{_n(r['base_bid'])} | {agent} | "
            f"{', '.join(f['code'] for f in r['flags'])} |"
        )
    t = result["tally"]
    why = result.get("h2_skip_reason")
    h2 = str(t["H2"]) if result.get("h2_checked", True) else ("not checked" + (f" ({why})" if why else ""))
    lines.append("")
    lines.append(f"H1 {t['H1']} | H2 {h2} | H3 {t['H3']}")
    return "\n".join(lines)
