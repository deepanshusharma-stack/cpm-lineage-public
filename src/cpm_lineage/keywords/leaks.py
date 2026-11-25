"""Cross-campaign keyword leaks, rank-overpay re-slot, owned niches and the
branded cannibalisation rate.

All screens work on keyword totals across *every* entity of a segment, so
a keyword split over several campaigns is judged on its combined money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Collection, Iterable, Mapping

from ..adapters.base import PlatformProfile
from ..config import Economics
from ..market._util import in_window, norm, pct
from ..schema import BudgetDayRow, MarketRow, PerfRow, Window
from .aggregate import KeywordStat, keyword_stats
from .config import KeywordConfig
from .roles import GENERIC, OWN

__all__ = [
    "LeakRow", "LeakResult", "NicheRow", "NicheResult",
    "keyword_leaks", "niche_class", "exhausted_days_by_keyword", "owned_niches", "cannibalisation_rate",
    "incremental_roas",
]

_KC = KeywordConfig()
_ECON = Economics()


@dataclass(frozen=True)
class LeakRow:
    """One keyword flagged by the leak / re-slot / untracked screens."""

    keyword: str
    kind: str                        # "leak" | "reslot" | "untracked"
    spend: float                     # spend in ROAS-judged entities
    sales: float
    roas: float | None
    n_entities: int
    cpm: float | None
    position: float | None
    untracked_spend: float = 0.0     # spend in entities with no keyword-grain sales attribution
    fix: str = ""


@dataclass(frozen=True)
class LeakResult:
    """Leaks (cut candidates), re-slots (re-bid candidates) and untracked notes."""

    leaks: tuple[LeakRow, ...] = ()
    reslot: tuple[LeakRow, ...] = ()
    untracked: tuple[LeakRow, ...] = ()
    min_spend: float = 0.0
    cpm_p75: float | None = None
    reason: str | None = None
    reslot_reason: str | None = None


def keyword_leaks(
    perf_rows: Iterable[PerfRow],
    window: Window | None = None,
    *,
    judged_entity_ids: Collection[str] | None = None,
    profile: PlatformProfile | None = None,
    economics: Economics | None = None,
    cfg: KeywordConfig | None = None,
) -> LeakResult:
    """Screen a segment's keywords for cross-campaign leaks and rank overpay.

    Rows are summed per keyword across all entities supplied.  When
    ``judged_entity_ids`` (the entities judged on keyword-grain ROAS) is
    given, spend, sales, CPM and position come only from those; spend in
    the other entities is kept apart as ``untracked_spend``.

    * ``leaks``: ``spend >= money(leak_min_spend_ref)`` and
      ``roas < leak_roas_be_mult x be``; by spend, top ``leaks_top_n``.
    * ``reslot`` (rank overpay *above* breakeven, a re-bid not a cut): not
      a leak, material spend, ROAS at or above the bar, position at or
      deeper than ``reslot_min_position`` and ``cpm >= cpm_p75`` where
      ``cpm_p75 = sorted_cpms[int(0.75 * (n - 1))]`` over material keywords
      (needs ``reslot_min_keywords``); top ``reslot_top_n``.  Skipped with
      a reason when the platform reports no position or a higher position
      number is better.
    * ``untracked``: untracked spend is material while judged spend is
      not; top ``untracked_top_n``.  A note, never a leak.

    Returns :class:`LeakResult`.
    """
    cfg, economics = cfg or _KC, economics or _ECON
    rows = list(perf_rows)
    judged = None if judged_entity_ids is None else frozenset(judged_entity_ids)
    stats = keyword_stats(rows, window, entity_ids=judged, profile=profile, position_max=cfg.position_sanity_max)
    other: dict[str, KeywordStat] = {}
    if judged is not None:
        rest = [r for r in rows if r.entity_id not in judged]
        other = keyword_stats(rest, window, profile=profile, position_max=cfg.position_sanity_max)
    if not stats and not other:
        return LeakResult(reason="no keyword rows in scope")
    min_spend = economics.money(cfg.leak_min_spend_ref)
    bar = cfg.leak_roas_be_mult * economics.be

    def row(s: KeywordStat, kind: str, fix: str) -> LeakRow:
        o = other.get(s.keyword)
        return LeakRow(s.keyword, kind, s.spend, s.sales, s.roas, s.n_entities, s.cpm, s.position,
                       o.spend if o else 0.0, fix)

    material = [s for s in stats.values() if s.spend >= min_spend]
    leak_set = [s for s in material if s.roas is not None and s.roas < bar]
    leaks = [row(s, "leak", "cut or negate: material spend below breakeven across campaigns")
             for s in sorted(leak_set, key=lambda s: (-s.spend, s.keyword))[: cfg.leaks_top_n]]

    reslot: list[LeakRow] = []
    cpm_p75: float | None = None
    reslot_reason: str | None = None
    pos_ok = profile is None or (profile.position.available and profile.position.lower_is_better)
    cpms = sorted(s.cpm for s in material if s.cpm is not None)
    if not pos_ok:
        reslot_reason = "position is not reported, or a larger position number is better on this platform"
    elif len(cpms) < cfg.reslot_min_keywords:
        reslot_reason = f"fewer than {cfg.reslot_min_keywords} material keywords: no CPM quartile"
    else:
        cpm_p75 = cpms[int(cfg.reslot_cpm_quantile * (len(cpms) - 1))]
        leak_kw = {s.keyword for s in leak_set}
        cand = [s for s in material
                if s.keyword not in leak_kw and s.roas is not None and s.roas >= bar
                and s.position is not None and s.position >= cfg.reslot_min_position
                and s.cpm is not None and s.cpm >= cpm_p75]
        reslot = [row(s, "reslot", "re-bid to a target slot: top-quartile CPM is buying a deep position")
                  for s in sorted(cand, key=lambda s: (-s.spend, s.keyword))[: cfg.reslot_top_n]]

    untracked: list[LeakRow] = []
    for kw, o in other.items():
        s = stats.get(kw)
        if o.spend >= min_spend and (s.spend if s else 0.0) < min_spend:
            base = s or KeywordStat(kw)
            untracked.append(LeakRow(kw, "untracked", base.spend, base.sales, base.roas, o.n_entities,
                                     o.cpm, o.position, o.spend,
                                     "spend sits in entities with no keyword-grain sales: cannot be judged here"))
    untracked.sort(key=lambda r: (-r.untracked_spend, r.keyword))
    return LeakResult(tuple(leaks), tuple(reslot), tuple(untracked[: cfg.untracked_top_n]),
                      min_spend, cpm_p75, None, reslot_reason)


# --------------------------------------------------------------------------
# owned niches
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class NicheRow:
    """A generic keyword the account dominates in paid share of voice."""

    keyword: str
    own_ad_sov_pct: float
    ad_impressions: float            # all brands
    spend: float
    roas: float | None
    cpm: float | None
    position: float | None
    exhausted_days: int | None       # of the last ``niche_exhaust_window_days`` days
    klass: str                       # "scale" | "topup-budget" | "fix-conversion" | "probe" | "hold" | "hold-top-slot"


@dataclass(frozen=True)
class NicheResult:
    rows: tuple[NicheRow, ...] = ()
    reason: str | None = None


_NICHE_ORDER = {"scale": 0, "topup-budget": 0, "fix-conversion": 0, "probe": 1, "hold": 2, "hold-top-slot": 2}


def niche_class(roas: float | None, economics: Economics | None = None, cfg: KeywordConfig | None = None) -> str:
    """Class a niche by ROAS: ``>= niche_scale_roas_be_mult x be`` scale,
    ``< niche_fix_roas_be_mult x be`` fix-conversion, else hold (also hold
    when ROAS is unknown)."""
    cfg, economics = cfg or _KC, economics or _ECON
    if roas is None:
        return "hold"
    if roas >= cfg.niche_scale_roas_be_mult * economics.be:
        return "scale"
    if roas < cfg.niche_fix_roas_be_mult * economics.be:
        return "fix-conversion"
    return "hold"


def exhausted_days_by_keyword(
    perf_rows: Iterable[PerfRow],
    budget_days: Iterable[BudgetDayRow],
    anchor: date,
    cfg: KeywordConfig | None = None,
) -> dict[str, int]:
    """Per keyword, the number of the last ``niche_exhaust_window_days``
    days (ending at ``anchor``) on which any entity bidding it ran out of
    budget (an exhaust minute was recorded, or the platform held it)."""
    cfg = cfg or _KC
    start = anchor - timedelta(days=cfg.niche_exhaust_window_days - 1)
    hit: dict[str, set[date]] = {}
    for b in budget_days:
        if start <= b.date <= anchor and (b.exhausted_at_minute is not None or b.platform_held):
            hit.setdefault(b.entity_id, set()).add(b.date)
    kw_ent: dict[str, set[str]] = {}
    for r in perf_rows:
        k = norm(r.targeting_unit)
        if k and start <= r.date <= anchor:
            kw_ent.setdefault(k, set()).add(r.entity_id)
    return {k: len(set().union(*(hit.get(e, set()) for e in es))) for k, es in kw_ent.items()}


def owned_niches(
    market_rows: Iterable[MarketRow],
    stats: Mapping[str, KeywordStat],
    roles: Mapping[str, str],
    window: Window | None = None,
    *,
    exhausted_days: Mapping[str, int] | None = None,
    profile: PlatformProfile | None = None,
    economics: Economics | None = None,
    cfg: KeywordConfig | None = None,
) -> NicheResult:
    """Find generic keywords the account already owns and say what to do.

    1. Keep generic keywords with own ad share of voice >=
       ``niche_sov_floor_pct`` and segment ad impressions >=
       ``niche_min_ad_impressions`` (a material denominator).
    2. Join spend, ROAS, CPM, position from ``stats`` and the count of
       recent budget-exhausted days.
    3. Class: spend below ``money(niche_probe_spend_ref)`` -> ``probe``;
       else :func:`niche_class`.  A ``scale`` niche already at the top
       slot cannot buy a better slot: ``topup-budget`` when its budget ran
       out recently, else ``hold-top-slot``.
    4. Sort actionable classes first, then probes, then holds; spend
       descending within a class.

    Roles default to ``generic`` for unknown keywords.  Returns
    :class:`NicheResult` (empty with a reason without a market feed).
    """
    cfg, economics = cfg or _KC, economics or _ECON
    own: dict[str, float] = {}
    tot: dict[str, float] = {}
    for m in market_rows:
        if not in_window(m.date, window):
            continue
        k = norm(m.targeting_unit)
        v = m.ad_impressions or 0.0
        tot[k] = tot.get(k, 0.0) + v
        if m.is_own:
            own[k] = own.get(k, 0.0) + v
    if not tot:
        return NicheResult(reason="no share-of-voice feed in the window")
    top_slot = profile.position.top_value if profile is not None else 1.0
    slot_tol = cfg.niche_top_position - 1.0
    probe = economics.money(cfg.niche_probe_spend_ref)
    rows: list[NicheRow] = []
    for k, t in tot.items():
        if roles.get(k, GENERIC) != GENERIC or t < cfg.niche_min_ad_impressions:
            continue
        sov = pct(own.get(k, 0.0), t)
        if sov is None or sov < cfg.niche_sov_floor_pct:
            continue
        s = stats.get(k) or KeywordStat(k)
        ex = None if exhausted_days is None else int(exhausted_days.get(k, 0))
        if s.spend < probe:
            klass = "probe"
        else:
            klass = niche_class(s.roas, economics, cfg)
            if klass == "scale" and s.position is not None and s.position <= top_slot + slot_tol:
                klass = "topup-budget" if (ex or 0) > 0 else "hold-top-slot"
        rows.append(NicheRow(k, sov, t, s.spend, s.roas, s.cpm, s.position, ex, klass))
    rows.sort(key=lambda r: (_NICHE_ORDER[r.klass], -r.spend, r.keyword))
    return NicheResult(tuple(rows), None if rows else "no generic keyword clears the share-of-voice floor")


# --------------------------------------------------------------------------
# branded cannibalisation
# --------------------------------------------------------------------------

def cannibalisation_rate(
    market_rows: Iterable[MarketRow],
    window: Window | None = None,
    *,
    roles: Mapping[str, str] | None = None,
    own_brand_keywords: Collection[str] | None = None,
) -> float | None:
    """Share of own-brand-intent exposure that was already organic.

    ``rate = own organic impressions / (own organic + own ad impressions)``
    over own-brand keywords (from ``own_brand_keywords``, else ``roles``,
    else the feed's native ``unit_role``), 0..1.  ``None`` when there is no
    such exposure: the caller then skips the gate, never guesses.
    """
    kws = None if own_brand_keywords is None else {norm(k) for k in own_brand_keywords}
    org = ad = 0.0
    for m in market_rows:
        if not m.is_own or not in_window(m.date, window):
            continue
        k = norm(m.targeting_unit)
        if kws is not None:
            ok = k in kws
        elif roles is not None:
            ok = roles.get(k) == OWN
        else:
            ok = (m.unit_role or "").strip().lower().replace("-", "_") in (OWN, "brand")
        if ok:
            org += m.organic_impressions or 0.0
            ad += m.ad_impressions or 0.0
    return org / (org + ad) if org + ad > 0 else None


def incremental_roas(direct_roas: float | None, rate: float | None) -> float | None:
    """``direct_roas x (1 - cannibalisation rate)``; ``None`` if either is unknown."""
    if direct_roas is None or rate is None:
        return None
    return direct_roas * (1.0 - rate)
