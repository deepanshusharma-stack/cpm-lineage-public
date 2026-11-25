"""Keyword CUT and ADD recommenders.

CUT: loss-making keywords with material spend, unless they buy new users
cheaply.  ADD: keywords the scope does not bid but its products already
appear on (or the platform calls relevant), scored on relevance, demand,
expected payoff and organic position.

Both degrade instead of raising: a missing optional input yields an empty
list plus a ``reason`` string.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from dataclasses import dataclass, field
from datetime import date
from typing import Collection, Iterable, Mapping, Sequence

from ..adapters.base import PlatformProfile
from ..config import Economics
from ..market._util import in_window, norm
from ..schema import MarketRow, PerfRow, SuggestedBid, Window
from .aggregate import KeywordStat, keyword_stats
from .config import DEFAULT_NATIVE_ROLE_MAP, KeywordConfig
from .roles import (COMPETITOR, GENERIC, OWN, SIBLING, Lexicon, RoleCohort, RoleLexicon, cohort_value)

__all__ = [
    "CutRec", "CutResult", "AddRec", "AddResult", "ShelfPresence", "RelevanceRow", "KeywordRecs",
    "acquisition_spare", "cut_recs", "shelf_presence", "relevance_pool", "role_from_native",
    "is_fragment", "score_add", "add_recs", "keyword_recs",
]

_KC = KeywordConfig()
_ECON = Economics()
Cohorts = Mapping[str, "RoleCohort | Mapping[str, float]"]


# --------------------------------------------------------------------------
# CUT
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CutRec:
    """One loss-making keyword."""

    keyword: str
    role: str
    spend: float
    roas: float
    bleed: float                     # spend - sales, money lost in the window
    new_users: float | None
    cost_per_new_user: float | None
    verdict: str                     # "cut" | "review_acquisition"
    why: str


@dataclass(frozen=True)
class CutResult:
    """CUT list plus the materiality floor that was applied."""

    rows: tuple[CutRec, ...] = ()
    floor: float = 0.0
    scope_spend: float = 0.0
    reason: str | None = None        # set when the list is empty for a structural reason


def acquisition_spare(
    spend: float,
    new_users: float | None,
    reference_cpnu: float | None,
    cfg: KeywordConfig | None = None,
) -> bool:
    """Decide whether a loss-making keyword is spared as cheap acquisition.

    Rule: ``new_users >= spare_min_new_users`` and
    ``spend / new_users <= spare_cpnu_mult x reference_cpnu`` (the role's
    median cost per new user).  ``False`` whenever new users or the
    reference are not available: without the signal nothing is spared.
    """
    cfg = cfg or _KC
    if new_users is None or new_users < 1 or not reference_cpnu:
        return False
    return new_users >= cfg.spare_min_new_users and spend / new_users <= cfg.spare_cpnu_mult * reference_cpnu


def cut_recs(
    stats: Iterable[KeywordStat] | Mapping[str, KeywordStat],
    roles: Mapping[str, str] | None = None,
    cohorts: Cohorts | None = None,
    economics: Economics | None = None,
    cfg: KeywordConfig | None = None,
) -> CutResult:
    """Decide which keywords in scope to cut.

    For each keyword with spend > 0 (pass window stats that already exclude
    placement pseudo-targets, e.g. from :func:`keyword_stats`):

    1. ``floor = max(money(cut_floor_ref), cut_floor_share x scope spend)``;
       skip spend at or below it (scope-relative materiality).
    2. ``roas = sales / spend``; skip when ``roas >= cut_roas_be_mult x be``.
    3. Acquisition spare (:func:`acquisition_spare` against the role
       cohort's median cost per new user): spared rows get verdict
       ``"review_acquisition"``, the rest ``"cut"``.

    Rows are sorted by ``bleed = spend - sales`` descending and capped at
    ``cut_top_n``.  Missing roles default to ``generic``; missing or thin
    cohorts disable the spare.  Returns :class:`CutResult`.
    """
    cfg, economics = cfg or _KC, economics or _ECON
    roles = roles or {}
    items = [s for s in (stats.values() if isinstance(stats, Mapping) else stats) if s.spend > 0]
    if not items:
        return CutResult(reason="no keyword spend in scope")
    tot = sum(s.spend for s in items) or 1.0
    floor = max(economics.money(cfg.cut_floor_ref), cfg.cut_floor_share * tot)
    bar = cfg.cut_roas_be_mult * economics.be
    rows: list[CutRec] = []
    for s in items:
        if s.spend <= floor:
            continue
        roas = s.sales / s.spend
        if roas >= bar:
            continue
        role = roles.get(s.keyword, GENERIC)
        cpnu = s.cost_per_new_user
        ref = cohort_value(cohorts, role, "cpnu_p50", cfg.cohort_min_n)
        spared = acquisition_spare(s.spend, s.new_users, ref, cfg)
        if spared:
            why = (f"ROAS {roas:.2f} is below {bar:.2f}, but {s.new_users:.0f} new users at {cpnu:.0f} each "
                   f"is within {cfg.spare_cpnu_mult:g}x the {role} median ({ref:.0f}): review, do not cut blind")
        else:
            why = f"ROAS {roas:.2f} is below {bar:.2f} on material spend ({s.spend:.0f} > floor {floor:.0f})"
        rows.append(CutRec(s.keyword, role, s.spend, roas, float(round(s.spend - s.sales)), s.new_users, cpnu,
                           "review_acquisition" if spared else "cut", why))
    rows.sort(key=lambda r: (-r.bleed, r.keyword))
    return CutResult(tuple(rows[: cfg.cut_top_n]), floor, tot,
                     None if rows else "no loss-making keyword above the materiality floor")


# --------------------------------------------------------------------------
# ADD: candidate pools
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ShelfPresence:
    """Own-product presence on one keyword (proven-presence candidate)."""

    keyword: str
    own_impressions: float = 0.0
    own_ad_impressions: float = 0.0
    own_organic_impressions: float = 0.0
    n_products: int = 0
    native_role: str | None = None        # the feed's own keyword type, if it has one
    organic_position: float | None = None  # impression-weighted, valid positions only


@dataclass(frozen=True)
class RelevanceRow:
    """One row of an optional platform keyword-to-product relevance feed."""

    keyword: str
    relevance: float | None = None
    volume: float | None = None           # searches per window
    product_id: str | None = None
    as_of: date | None = None


def shelf_presence(
    market_rows: Iterable[MarketRow],
    window: Window | None = None,
    *,
    own_product_ids: Collection[str] | None = None,
    profile: PlatformProfile | None = None,
    cfg: KeywordConfig | None = None,
) -> dict[str, ShelfPresence]:
    """Pool A: keywords on which the scope's own products appeared.

    Sums own rows of the market feed in ``window`` (restricted to
    ``own_product_ids`` when given and the feed is product-grain): total /
    ad / organic impressions, distinct own products, the native keyword
    type, and the organic position weighted by organic impressions over
    positions ``1..organic_position_max``.  Placement pseudo-targets are
    dropped.  Keeps the top ``add_pool_presence`` keywords by own
    impressions.
    """
    cfg = cfg or _KC
    acc: dict[str, list[float]] = {}
    prods: dict[str, set[str]] = {}
    native: dict[str, str] = {}
    for m in market_rows:
        if not m.is_own or not in_window(m.date, window):
            continue
        if own_product_ids is not None and m.product_id is not None and m.product_id not in own_product_ids:
            continue
        k = norm(m.targeting_unit)
        if not k or (profile is not None and not profile.is_search_keyword(k)):
            continue
        a = acc.setdefault(k, [0.0, 0.0, 0.0, 0.0, 0.0])
        org = m.organic_impressions or 0.0
        a[0] += m.impressions or 0.0
        a[1] += m.ad_impressions or 0.0
        a[2] += org
        if m.organic_position is not None and 1.0 <= m.organic_position <= cfg.organic_position_max:
            w = org if org > 0 else (m.impressions or 0.0)
            a[3] += m.organic_position * w
            a[4] += w
        if m.product_id:
            prods.setdefault(k, set()).add(m.product_id)
        if m.unit_role and k not in native:
            native[k] = m.unit_role
    out = {k: ShelfPresence(k, a[0], a[1], a[2], len(prods.get(k, ())), native.get(k),
                            a[3] / a[4] if a[4] > 0 else None) for k, a in acc.items()}
    top = sorted(out.values(), key=lambda p: (-p.own_impressions, p.keyword))[: cfg.add_pool_presence]
    return {p.keyword: p for p in top}


def relevance_pool(
    rows: Iterable[RelevanceRow | Mapping[str, object]],
    product_ids: Collection[str] | None = None,
    cfg: KeywordConfig | None = None,
) -> dict[str, RelevanceRow]:
    """Pool B: the platform's relevance feed reduced to one row per keyword.

    Per keyword ``relevance = max(score)``, ``volume = max(volume)`` over
    the scope's products; keep ``relevance >= explorer_min_relevance``; top
    ``add_pool_relevance`` by relevance then volume.  Empty on no input.
    """
    cfg = cfg or _KC
    best: dict[str, list] = {}
    for r in rows:
        if isinstance(r, Mapping):
            r = RelevanceRow(**{k: r.get(k) for k in ("keyword", "relevance", "volume", "product_id", "as_of")})  # type: ignore[arg-type]
        k = norm(r.keyword)
        if not k or r.relevance is None:
            continue
        if product_ids is not None and r.product_id is not None and r.product_id not in product_ids:
            continue
        b = best.setdefault(k, [r.relevance, r.volume, r.as_of])
        b[0] = max(b[0], r.relevance)
        if r.volume is not None:
            b[1] = r.volume if b[1] is None else max(b[1], r.volume)
        if r.as_of is not None:
            b[2] = r.as_of if b[2] is None else max(b[2], r.as_of)
    kept = [RelevanceRow(k, b[0], b[1], None, b[2]) for k, b in best.items() if b[0] >= cfg.explorer_min_relevance]
    kept.sort(key=lambda r: (-(r.relevance or 0.0), -(r.volume or 0.0), r.keyword))
    return {r.keyword: r for r in kept[: cfg.add_pool_relevance]}


def role_from_native(native: str | None,
                     native_map: Sequence[tuple[str, str]] = DEFAULT_NATIVE_ROLE_MAP) -> str | None:
    """Map a feed's native keyword-type label to a neutral role (``None`` if unknown)."""
    if not native:
        return None
    return dict(native_map).get(str(native).strip().lower())


def is_fragment(keyword: str, universe: Sequence[str], cfg: KeywordConfig | None = None,
                *, _sorted: bool = False) -> bool:
    """Decide whether a candidate is a typed fragment of a longer term.

    A keyword containing a space is never a fragment.  A one-word keyword
    shorter than ``fragment_min_len`` always is.  Otherwise it is a
    fragment when another universe term starts with it and the next
    character of that term is not a space (a truncated prefix of a longer
    word; a full word followed by more words survives).

    ``universe`` may be pre-sorted (pass ``_sorted=True``) for a binary
    search instead of a scan.
    """
    cfg = cfg or _KC
    kw = norm(keyword)
    if " " in kw:
        return False
    if len(kw) < cfg.fragment_min_len:
        return True
    terms = universe if _sorted else sorted(universe)
    i = bisect_left(terms, kw)
    while i < len(terms) and terms[i].startswith(kw):
        t = terms[i]
        if len(t) > len(kw) and t[len(kw)] != " ":
            return True
        i += 1
    return False


def score_add(
    relevance: float | None,
    volume: float | None,
    own_impressions: float | None,
    expected_roas: float | None,
    organic_position: float | None,
    cfg: KeywordConfig | None = None,
) -> float:
    """Score an ADD candidate (nominally 0-100).

    Formula::

        rel = relevance, or unknown_relevance when missing
        dem = 0.6 * log10(1 + volume) / 6 + 0.4 * log10(1 + own_impressions) / 6
        pay = min(expected_roas or 1.0, payoff_cap) / payoff_cap
        org = organic_bonus when organic_position <= organic_bonus_position else 1
        score = round(100 * rel * dem * pay * org, 1)

    Multiplicative, but a missing signal is neutral, never a veto.  The
    demand term is not capped, so very large volumes can push the score
    above 100: the scale is nominal.
    """
    cfg = cfg or _KC
    rel_f = relevance if relevance is not None else cfg.unknown_relevance
    dem = (cfg.demand_volume_weight * math.log10(1.0 + max(volume or 0.0, 0.0)) / cfg.demand_log_scale
           + cfg.demand_presence_weight * math.log10(1.0 + max(own_impressions or 0.0, 0.0)) / cfg.demand_log_scale)
    pay = min(expected_roas or cfg.payoff_unknown, cfg.payoff_cap) / cfg.payoff_cap
    org = cfg.organic_bonus if organic_position is not None and organic_position <= cfg.organic_bonus_position else 1.0
    return round(100.0 * rel_f * dem * pay * org, 1)


# --------------------------------------------------------------------------
# ADD
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AddRec:
    """One keyword worth adding."""

    keyword: str
    role: str
    intent: str                      # "growth" | "defense"
    demand: float                    # own impressions in the window
    own_organic_impressions: float
    own_ad_impressions: float
    n_products: int
    exp_roas: float | None
    exp_lo: float | None
    exp_hi: float | None
    payoff_source: str | None        # which cohort priced it: the role itself, "generic" fallback, or None
    organic_position: float | None
    relevance: float | None
    volume: float | None
    conquest: bool
    score: float
    why: str
    source: str = "presence"         # "presence" | "relevance"
    suggested_low: float | None = None
    suggested_high: float | None = None
    suggested_as_of: date | None = None
    searches: float | None = None
    relevance_as_of: date | None = None


@dataclass(frozen=True)
class AddResult:
    """ADD list plus what was filtered on the way."""

    rows: tuple[AddRec, ...] = ()
    n_candidates: int = 0
    n_fragments: int = 0
    n_gated: int = 0
    reason: str | None = None


def _payoff(cohorts: Cohorts | None, role: str, min_n: int) -> tuple[float | None, float | None, float | None, str | None]:
    for src in (role, GENERIC):
        exp = cohort_value(cohorts, src, "roas_p50", min_n)
        if exp is not None:
            return (exp, cohort_value(cohorts, src, "roas_p25", min_n),
                    cohort_value(cohorts, src, "roas_p75", min_n), src)
    return None, None, None, None


def add_recs(
    presence: Mapping[str, ShelfPresence] | Iterable[ShelfPresence] | None,
    bid_keywords: Collection[str],
    cohorts: Cohorts | None = None,
    *,
    relevance: Mapping[str, RelevanceRow] | None = None,
    lexicon: RoleLexicon | None = None,
    suggested_bids: Mapping[str, SuggestedBid] | None = None,
    profile: PlatformProfile | None = None,
    economics: Economics | None = None,
    cfg: KeywordConfig | None = None,
) -> AddResult:
    """Decide which unbid keywords to add.

    Candidates: pool A ``presence`` (keywords the scope's own products
    already appear on, see :func:`shelf_presence`) and optional pool B
    ``relevance`` (see :func:`relevance_pool`), minus anything already bid
    and placement pseudo-targets.

    1. Role: the feed's native keyword type when present, else the
       lexicon, else ``generic``.  A competitor term matching a sibling
       signature is re-labelled ``sibling_brand``.
    2. Fragment filter (:func:`is_fragment`) over pool A union pool B.
    3. Expected payoff = the role cohort's ROAS p50 / p25 / p75, falling
       back to the ``generic`` cohort (``payoff_source`` says which).
    4. Gate: drop when the expected ROAS is known and below
       ``add_min_exp_roas_be_mult x be``.  Unknown passes.
    5. ``intent = "defense"`` for own-brand terms (bidding the own name
       re-buys free organic traffic), else ``"growth"``.
    6. Score with :func:`score_add`.
    7. Sort growth first, then score descending; keep ``add_top_n``.
    8. Attach the platform's suggested bid range when supplied.

    With neither pool the result is empty with a reason.  Returns
    :class:`AddResult`.
    """
    cfg, economics = cfg or _KC, economics or _ECON
    pool_a: dict[str, ShelfPresence] = {}
    if presence is not None:
        vals = presence.values() if isinstance(presence, Mapping) else presence
        pool_a = {norm(p.keyword): p for p in vals}
    pool_b = {norm(k): v for k, v in (relevance or {}).items()}
    if not pool_a and not pool_b:
        return AddResult(reason="no shelf-presence feed and no relevance feed: nothing to propose")
    bid = {norm(k) for k in bid_keywords}
    universe = sorted(set(pool_a) | set(pool_b))
    bar = cfg.add_min_exp_roas_be_mult * economics.be
    sibling_only = Lexicon(sibling_specs=lexicon.sibling_specs, cfg=lexicon.cfg) if isinstance(lexicon, Lexicon) \
        and lexicon.sibling_specs else None

    rows: list[AddRec] = []
    n_cand = n_frag = n_gate = 0

    def consider(kw: str, p: ShelfPresence | None, source: str) -> None:
        nonlocal n_cand, n_frag, n_gate
        if not kw or kw in bid or (profile is not None and not profile.is_search_keyword(kw)):
            return
        n_cand += 1
        if is_fragment(kw, universe, cfg, _sorted=True):
            n_frag += 1
            return
        role = role_from_native(p.native_role) if p is not None else None
        if role is None:
            role = lexicon.classify(kw).role if lexicon is not None else GENERIC
        elif role == COMPETITOR and sibling_only is not None and sibling_only.classify(kw).role == SIBLING:
            role = SIBLING
        exp, lo, hi, src = _payoff(cohorts, role, cfg.cohort_min_n)
        if exp is not None and exp < bar:
            n_gate += 1
            return
        rel_row = pool_b.get(kw)
        rel = rel_row.relevance if rel_row else None
        vol = rel_row.volume if rel_row else None
        own_impr = p.own_impressions if p else 0.0
        pos = p.organic_position if p else None
        score = score_add(rel, vol, own_impr, exp, pos, cfg)
        bits = []
        if p is not None and own_impr > 0:
            bits.append(f"own products already drew {own_impr:.0f} impressions here unbid")
        if rel is not None:
            bits.append(f"platform relevance {rel:.2f}")
        if exp is not None:
            bits.append(f"{src} keywords earn a median ROAS of {exp:.2f}")
        if pos is not None and pos <= cfg.organic_bonus_position:
            bits.append(f"organic position {pos:.1f} proves conversion relevance")
        if role == OWN:
            bits.append("own-brand term: bid only to defend, it re-buys organic traffic")
        sb = (suggested_bids or {}).get(kw)
        rows.append(AddRec(
            keyword=kw, role=role, intent="defense" if role == OWN else "growth", demand=own_impr,
            own_organic_impressions=p.own_organic_impressions if p else 0.0,
            own_ad_impressions=p.own_ad_impressions if p else 0.0,
            n_products=p.n_products if p else 0, exp_roas=exp, exp_lo=lo, exp_hi=hi, payoff_source=src,
            organic_position=pos, relevance=rel, volume=vol, conquest=role == COMPETITOR, score=score,
            why="; ".join(bits) or "candidate with no supporting signal yet", source=source,
            suggested_low=sb.low if sb else None, suggested_high=sb.high if sb else None,
            suggested_as_of=sb.as_of if sb else None, searches=sb.searches if sb else None,
            relevance_as_of=rel_row.as_of if rel_row else None))

    for kw, p in pool_a.items():
        consider(kw, p, "presence")
    for kw in pool_b:
        if kw not in pool_a:
            consider(kw, None, "relevance")
    rows.sort(key=lambda r: (r.intent != "growth", -r.score, r.keyword))
    return AddResult(tuple(rows[: cfg.add_top_n]), n_cand, n_frag, n_gate,
                     None if rows else "every candidate was already bid, a fragment, or priced below breakeven")


@dataclass(frozen=True)
class KeywordRecs:
    """ADD and CUT lists for one scope, sharing one cohort read."""

    add: AddResult
    cut: CutResult
    n_entities: int | None = None
    cohorts: Mapping[str, RoleCohort] = field(default_factory=dict)


def keyword_recs(
    perf_rows: Iterable[PerfRow],
    window: Window,
    *,
    entity_ids: str | Collection[str] | None = None,
    roles: Mapping[str, str] | None = None,
    cohorts: Cohorts | None = None,
    market_rows: Iterable[MarketRow] | None = None,
    own_product_ids: Collection[str] | None = None,
    relevance: Mapping[str, RelevanceRow] | None = None,
    lexicon: RoleLexicon | None = None,
    suggested_bids: Mapping[str, SuggestedBid] | None = None,
    profile: PlatformProfile | None = None,
    economics: Economics | None = None,
    cfg: KeywordConfig | None = None,
) -> KeywordRecs:
    """Run CUT and ADD for one scope over ``window``.

    ``entity_ids`` is one id, a collection (for instance the pure campaigns
    of one segment; an empty collection matches nothing) or ``None`` for
    the whole account.  ``cohorts`` should come from
    :func:`cpm_lineage.keywords.roles.role_cohorts` over the account.  The
    bid set for ADD is every keyword with a row in scope inside the window.
    """
    cfg = cfg or _KC
    rows = list(perf_rows)
    stats = keyword_stats(rows, window, entity_ids=entity_ids, profile=profile,
                          position_max=cfg.position_sanity_max)
    cut = cut_recs(stats, roles, cohorts, economics, cfg)
    if market_rows is None and not relevance:
        add = AddResult(reason="no shelf-presence feed and no relevance feed: nothing to propose")
    else:
        pres = shelf_presence(market_rows or (), window, own_product_ids=own_product_ids, profile=profile, cfg=cfg)
        add = add_recs(pres, set(stats), cohorts, relevance=relevance, lexicon=lexicon,
                       suggested_bids=suggested_bids, profile=profile, economics=economics, cfg=cfg)
    n_ent = None if entity_ids is None or isinstance(entity_ids, str) else len(set(entity_ids))
    return KeywordRecs(add=add, cut=cut, n_entities=n_ent, cohorts=dict(cohorts or {}))  # type: ignore[arg-type]
