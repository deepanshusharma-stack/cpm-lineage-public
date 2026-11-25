"""Cold-start pre-flight and state-mode diagnosis.

A campaign younger than the "mature" tier has no period-over-period move to
attribute.  Instead of saying nothing, this module predicts each of its
targeting units, products and geos from what the *account* already knows
about them in its other campaigns, and proposes reversible day-0 actions:
cap known bleeders, fund proven winners, hold rank on own-brand terms.

Pieces, in dependency order:

* :func:`shrink` - empirical-Bayes shrinkage of a unit's account-history
  ROAS towards its role-cohort median, with evidence counted in days *and*
  independent campaigns.
* :func:`blend_local` - blend of the shrunk number with the campaign's own
  early ROAS, weighted by own spend.
* :func:`siblings` - "same shopper intent" test between two terms.
* :func:`preflight_keywords`, :func:`preflight_products`, :func:`geo_signal`
  and the :func:`preflight` wrapper - the pre-flight card.
* :func:`detect_regime` - did the operator change something so recently
  that the numbers describe the previous setup?
* :func:`diagnose_state` - the state-mode diagnosis for a young campaign.
* :func:`history_from_rows` - a pure builder of the account history
  (targeting unit / product / geo grain) from performance rows, for callers
  that do not keep a persistent history store.

Every ROAS bar is expressed in multiples of the account's effective
breakeven (``Economics.be``); with the default economics the bars are
1.0 / 2.0 / 15.0.  Money thresholds are reference-unit amounts
passed through ``Economics.money``.  Availability is on the 0..1 scale.

All functions are pure: plain records in, plain dataclasses out.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from statistics import median
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import AvailabilityPolicy, Economics, TierConfig
from .liveness import liveness_class, stale_stopped
from .schema import ChangeEvent, DataTier, LivenessClass, PerfRow
from .windows import route_tier

__all__ = [
    "ColdStartConfig", "UnitHistory", "ProductHistory", "GeoHistory", "AccountHistory", "OwnUnit",
    "ClearingBid", "KeywordCard", "ProductCard", "GeoSignal", "Preflight", "RegimeChange",
    "StateAction", "StateDiagnosis",
    "shrink", "blend_local", "siblings", "confidence_from_weight", "preflight_keywords",
    "preflight_products", "geo_signal", "preflight", "history_from_rows", "daily_weighted_bid",
    "detect_regime", "diagnose_state",
    "CALL_NO_HISTORY", "CALL_THIN_HISTORY", "CALL_BRAND_DEFENSE", "CALL_PROVEN_WINNER",
    "CALL_THIN_PROMISING", "CALL_KNOWN_BLEEDER", "CALL_NEUTRAL",
    "CALL_PROVEN_PRODUCT", "CALL_BLEEDER_PRODUCT", "CALL_NEUTRAL_PRODUCT",
]

CALL_NO_HISTORY = "no-history"
CALL_THIN_HISTORY = "thin-history"
CALL_BRAND_DEFENSE = "brand-defense"
CALL_PROVEN_WINNER = "proven-winner"
CALL_THIN_PROMISING = "thin-promising"
CALL_KNOWN_BLEEDER = "known-bleeder"
CALL_NEUTRAL = "neutral"

CALL_PROVEN_PRODUCT = "proven-product"
CALL_BLEEDER_PRODUCT = "bleeder-product"
CALL_NEUTRAL_PRODUCT = "neutral-product"

_KEYWORD_ORDER = {CALL_KNOWN_BLEEDER: 0, CALL_PROVEN_WINNER: 1, CALL_THIN_HISTORY: 2,
                  CALL_NO_HISTORY: 3, CALL_NEUTRAL: 4}
_PRODUCT_ORDER = {CALL_BLEEDER_PRODUCT: 0, CALL_PROVEN_PRODUCT: 1, CALL_NEUTRAL_PRODUCT: 2}


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class ColdStartConfig:
    """Thresholds of the cold-start layer.  ROAS bars are breakeven multiples."""

    shrink_k: float = 14.0                  # pseudo-count: evidence gets half weight at 14 evidence-units
    camp_evidence: float = 4.0              # evidence-units per independent campaign (replication is strong evidence)
    blend_tau_ref: float = 2000.0           # pseudo-spend of the local blend: own data gets half weight at this own spend
    blend_min_own_spend_ref: float = 500.0  # below this own spend, own ROAS is a handful of orders: do not blend
    conf_high_weight: float = 0.7           # self-weight at or above -> high confidence (persistence depth, not age)
    conf_med_weight: float = 0.4            # self-weight at or above -> medium confidence
    brand_defense_roas_be: float = 15.0     # own-brand term at or above: absurd ROAS = organic capture, never scale on it
    winner_roas_be: float = 2.0             # absolute winner bar (also met by the cohort p75)
    bleeder_roas_be: float = 1.0            # absolute bleeder bar (also met by falling under the cohort p25)
    proven_min_camps: int = 2               # proven needs replication in two campaigns ...
    proven_min_days: int = 10               # ... or ten active days (a few good days are a lead, not proof)
    halo_spare_share: float = 0.2           # indirect share above this: cap, do not kill
    blend_disclosure: float = 0.15          # |blended - shrunk| at or above this adds an explanatory line
    sibling_prefix_len: int = 5             # typo-prefix head match needs both heads this long
    sibling_remainder_len: int = 4          # substring remainder match needs the shorter remainder this long
    cohort_min_n: int = 12                  # a role cohort smaller than this is not trusted (5 is a looser alternative)
    product_prior_roas_be: float = 1.5      # flat product prior when the history store is empty (no product cohorts)
    product_proven_roas_be: float = 2.0     # product at or above -> proven
    product_bleeder_roas_be: float = 1.0    # product below -> bleeder
    geo_min_count: int = 2                  # a geo signal needs at least two geos
    geo_list_n: int = 3                     # geos listed per side
    geo_strong_roas_be: float = 1.0         # strong at or above, weak below
    coverage_high_pct: float = 80.0         # share of units with history for High state confidence
    coverage_med_pct: float = 40.0          # ... for Medium
    max_keyword_bleed_actions: int = 2      # stop-loss first: two keyword bleeders ...
    max_product_bleed_actions: int = 1      # ... and one product bleeder
    max_defense_actions: int = 1            # one brand-defense action
    max_winner_actions: int = 2             # two keyword winners
    max_product_winner_actions: int = 1     # one product winner
    max_state_actions: int = 3              # an operator acts on a short list
    regime_lookback_days: int = 30          # how far back an operator change is searched
    regime_bid_step: float = 0.25           # day-over-day step in the spend-weighted bid that counts as a change
    regime_post_days_max: int = 2           # at most this many post-change days -> numbers describe the prior regime
    regime_rerun_days: int = 3              # advice: re-run after this many post-change days
    regime_fields: tuple[str, ...] = ("bid", "daily_budget", "budget", "status")  # change-event fields that reset a regime
    regime_bid_fields: tuple[str, ...] = ("bid",)  # fields for which automated changes are ignored


_CC = ColdStartConfig()
_ECON = Economics()
_AVAIL = AvailabilityPolicy()


# --------------------------------------------------------------------------- records

@dataclass(frozen=True)
class UnitHistory:
    """What the account knows about one targeting unit across its campaigns."""

    roas: float | None = None
    n_days: int = 0                      # distinct active days
    n_camps: int = 0                     # distinct campaigns the unit ran in
    role: str | None = None
    halo: float | None = None            # indirect / (direct + indirect) sales
    cost_per_new_user: float | None = None
    spend: float | None = None
    last_seen: date | None = None


@dataclass(frozen=True)
class ProductHistory:
    """Account history of one product."""

    roas: float | None = None
    n_days: int = 0
    n_camps: int = 0
    availability: float | None = None    # 0..1
    spend: float | None = None
    last_seen: date | None = None


@dataclass(frozen=True)
class GeoHistory:
    """Account history of one geo."""

    roas: float | None = None
    n_days: int = 0
    n_camps: int = 0
    spend: float | None = None
    last_seen: date | None = None


@dataclass(frozen=True)
class AccountHistory:
    """The three history maps a pre-flight reads (see :func:`history_from_rows`)."""

    units: Mapping[str, UnitHistory] = field(default_factory=dict)
    products: Mapping[str, ProductHistory] = field(default_factory=dict)
    geos: Mapping[str, GeoHistory] = field(default_factory=dict)
    window_days: int | None = None


@dataclass(frozen=True)
class OwnUnit:
    """The campaign's own early numbers on one targeting unit (absent on true day 0)."""

    value: str
    spend: float | None = None
    sales: float | None = None
    bid: float | None = None             # spend-weighted


@dataclass(frozen=True)
class ClearingBid:
    """Top-slot clearing bid percentiles for one targeting unit (optional feed)."""

    p50: float | None = None
    p75: float | None = None


@dataclass
class KeywordCard:
    """Pre-flight call for one targeting unit."""

    kw: str
    role: str
    call: str
    confidence: str                      # "high" | "med" | "low"
    own_roas: float | None = None
    blended_roas: float | None = None
    hist_roas: float | None = None
    shrunk_roas: float | None = None
    self_weight: float = 0.0
    n_days: int = 0
    n_camps: int = 0
    cost_per_new_user: float | None = None
    halo: float | None = None
    top_bid_p50: float | None = None
    top_bid_p75: float | None = None
    own_bid: float | None = None
    spend: float = 0.0
    actions: list[str] = field(default_factory=list)
    sibling_conflict: str | None = None  # the conflicting sibling term

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProductCard:
    """Pre-flight call for one product."""

    product_id: str
    call: str
    confidence: str
    hist_roas: float | None = None
    shrunk_roas: float | None = None
    self_weight: float = 0.0
    n_days: int = 0
    n_camps: int = 0
    availability: float | None = None
    solo: bool = False
    actions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GeoSignal:
    """Strongest and weakest geos in the account history.  Rows are dicts
    ``{geo, roas, availability?, tier?}``."""

    strong: tuple[dict[str, Any], ...] = ()
    weak: tuple[dict[str, Any], ...] = ()
    note: str = ""


@dataclass
class Preflight:
    """The pre-flight card of one campaign."""

    status: str = "ok"                    # "ok" | "empty"
    reason: str | None = None
    n_keywords: int = 0
    n_with_history: int = 0
    coverage_pct: int = 0
    winners: list[KeywordCard] = field(default_factory=list)
    thin_promising: list[KeywordCard] = field(default_factory=list)
    bleeders: list[KeywordCard] = field(default_factory=list)
    brand_defense: list[KeywordCard] = field(default_factory=list)
    cards: list[KeywordCard] = field(default_factory=list)
    product_winners: list[ProductCard] = field(default_factory=list)
    product_bleeders: list[ProductCard] = field(default_factory=list)
    product_cards: list[ProductCard] = field(default_factory=list)
    geo_signal: GeoSignal | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RegimeChange:
    """The last operator change found by :func:`detect_regime`."""

    changed_on: date
    source: str                           # "change_log" | "declared" | "bid_telemetry"
    detail: str
    post_change_days: int
    prior_regime: bool                    # True -> numbers describe the previous setup
    warning: str | None = None


@dataclass(frozen=True)
class StateAction:
    """One day-0 action of the state-mode diagnosis."""

    verb: str
    lever: str                            # "keyword" | "product" | "budget"
    kind: str                             # "bleed" | "win"
    targets: tuple[str, ...]
    headline: str
    sizing: str


@dataclass
class StateDiagnosis:
    """State-mode result.  Shape-compatible with a full diagnosis: ``mode``
    is ``"state"``, ``verdict`` ``"state-only"``, the move is zero and the
    layer-1 / driver blocks are empty."""

    status: str = "ok"                    # "ok" | "excluded"
    reason: str | None = None
    entity_id: str | None = None
    mode: str = "state"
    verdict: str = "state-only"
    dln_roas: float = 0.0
    layer1: dict[str, Any] = field(default_factory=dict)
    drivers: list[Any] = field(default_factory=list)
    short: bool = True
    tier: str | None = None
    n_active_days: int = 0
    confidence: str = "Low"               # "High" | "Medium" | "Low"
    priority: str = "p2"
    top3: list[StateAction] = field(default_factory=list)
    regime: RegimeChange | None = None
    untracked: list[str] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)
    preflight: Preflight | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- helpers

def _g(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _unit_history(h: Any) -> UnitHistory | None:
    if h is None or isinstance(h, UnitHistory):
        return h
    return UnitHistory(
        roas=_g(h, "roas"), n_days=int(_g(h, "n_days", 0) or 0), n_camps=int(_g(h, "n_camps", 0) or 0),
        role=_g(h, "role"), halo=_g(h, "halo"),
        cost_per_new_user=_g(h, "cost_per_new_user", _g(h, "cpnu")),
        spend=_g(h, "spend"), last_seen=_g(h, "last_seen"))


def _product_history(h: Any) -> ProductHistory | None:
    if h is None or isinstance(h, ProductHistory):
        return h
    return ProductHistory(
        roas=_g(h, "roas"), n_days=int(_g(h, "n_days", 0) or 0), n_camps=int(_g(h, "n_camps", 0) or 0),
        availability=_g(h, "availability"), spend=_g(h, "spend"), last_seen=_g(h, "last_seen"))


def _cohort_bars(cohorts: Mapping[str, Any] | None, role: str,
                 min_n: int) -> tuple[float | None, float | None, float | None]:
    """(p25, p50, p75) of a role cohort; all ``None`` when the cohort is missing or too small."""
    c = cohorts.get(role) if cohorts else None
    if c is None:
        return None, None, None
    n = _g(c, "n")
    if n is not None and n < min_n:
        return None, None, None
    return tuple(_g(c, f"roas_p{q}", _g(c, f"p{q}")) for q in (25, 50, 75))  # type: ignore[return-value]


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def confidence_from_weight(weight: float, cfg: ColdStartConfig | None = None) -> str:
    """Confidence band of a shrunk estimate: ``"high"`` at self-weight >=
    ``conf_high_weight`` (0.7), ``"med"`` at >= ``conf_med_weight`` (0.4),
    else ``"low"``.  It measures persistence depth, not campaign age."""
    cfg = cfg or _CC
    if weight >= cfg.conf_high_weight:
        return "high"
    if weight >= cfg.conf_med_weight:
        return "med"
    return "low"


# --------------------------------------------------------------------------- shrinkage and blend

def shrink(self_value: float | None, prior_value: float | None, n_days: float, n_camps: float = 0,
           cfg: ColdStartConfig | None = None) -> tuple[float | None, float]:
    """Shrink a unit's own history towards its cohort prior.

    Decision: how much to trust the unit's account-history ROAS.

    Formula::

        n_eff = n_days + camp_evidence * n_camps        # camp_evidence = 4
        w     = n_eff / (n_eff + shrink_k)              # shrink_k = 14; 0 when n_eff == 0
        value = w * self_value + (1 - w) * prior_value

    A unit measured across several campaigns is strong evidence even when
    each ran only a few days, so near-zero ROAS replicated in five campaigns
    must not shrink back to the prior.

    Returns ``(value, w)``: ``(prior_value, 0.0)`` when there is no own
    value; ``(self_value, w)`` when there is no prior.
    """
    cfg = cfg or _CC
    if self_value is None:
        return prior_value, 0.0
    n_eff = max(float(n_days or 0), 0.0) + cfg.camp_evidence * max(float(n_camps or 0), 0.0)
    w = n_eff / (n_eff + cfg.shrink_k) if n_eff else 0.0
    if prior_value is None:
        return self_value, w
    return w * self_value + (1.0 - w) * prior_value, w


def blend_local(own_spend: float | None, own_roas: float | None, shrunk: float | None,
                cfg: ColdStartConfig | None = None, economics: Economics | None = None) -> float | None:
    """Blend the campaign's own early ROAS with the shrunk account number.

    Decision: let a campaign's own evidence move its call, in proportion to
    how much it has spent, so that several campaigns on one term do not all
    receive an identical call.

    Formula: ``(own_spend * own_roas + tau * shrunk) / (own_spend + tau)``
    with ``tau = money(blend_tau_ref)`` (own data gets half weight at
    ``tau`` of own spend).

    Returns ``shrunk`` when there is no own ROAS or no own spend,
    ``own_roas`` when there is nothing to shrink towards, else the blend.
    """
    cfg = cfg or _CC
    if own_roas is None or not own_spend:
        return shrunk
    if shrunk is None:
        return own_roas
    tau = (economics or _ECON).money(cfg.blend_tau_ref)
    return (own_spend * own_roas + tau * shrunk) / (own_spend + tau)


def siblings(a: str | None, b: str | None, cfg: ColdStartConfig | None = None) -> bool:
    """Do two terms express one shopper intent (spelling / spacing variants)?

    Decision: used to stop the pre-flight funding one variant of a term
    while capping another.

    Rule (terms lower-cased and split on whitespace):

    1. empty or identical terms are not siblings;
    2. the first tokens must be equal, or both at least
       ``sibling_prefix_len`` (5) long with one a prefix of the other;
    3. the remaining tokens, concatenated without separator, must be equal,
       or one of them empty, or the shorter (at least
       ``sibling_remainder_len`` = 4 long) a substring of the longer.

    Returns ``True`` / ``False``.
    """
    cfg = cfg or _CC
    ta, tb = (a or "").lower().split(), (b or "").lower().split()
    if not ta or not tb or ta == tb:
        return False
    h1, h2 = ta[0], tb[0]
    if h1 != h2:
        n = cfg.sibling_prefix_len
        if not (len(h1) >= n and len(h2) >= n and (h1.startswith(h2) or h2.startswith(h1))):
            return False
    ra, rb = "".join(ta[1:]), "".join(tb[1:])
    if ra == rb or not ra or not rb:
        return True
    short, long_ = (ra, rb) if len(ra) <= len(rb) else (rb, ra)
    return len(short) >= cfg.sibling_remainder_len and short in long_


# --------------------------------------------------------------------------- keyword pre-flight

def _role_of(kw: str, roles: Mapping[str, str] | Callable[[str], str | None] | None,
             hist: UnitHistory | None) -> str:
    live: str | None = None
    if callable(roles):
        live = roles(kw)
    elif roles:
        live = roles.get(kw)
    live = getattr(live, "value", live)
    return str(live or (hist.role if hist and hist.role else None) or "generic")


def preflight_keywords(
    keywords: Iterable[str | OwnUnit | Mapping[str, Any]],
    history: Mapping[str, UnitHistory | Mapping[str, Any]] | None,
    cohorts: Mapping[str, Any] | None = None,
    clearing: Mapping[str, ClearingBid | Mapping[str, Any]] | None = None,
    roles: Mapping[str, str] | Callable[[str], str | None] | None = None,
    *,
    own_brand_role: str = "own_brand",
    cfg: ColdStartConfig | None = None,
    economics: Economics | None = None,
) -> Preflight:
    """Call each targeting unit of a young campaign from account history.

    Inputs: the campaign's units (plain strings on day 0, or
    :class:`OwnUnit` with own spend / sales / bid), the account history per
    unit, role cohorts (objects or mappings exposing ``roas_p25/p50/p75``
    or ``p25/p50/p75`` and optionally ``n``), optional top-slot clearing
    bids, and a live role classifier (mapping or callable; falls back to the
    stored role, then ``"generic"``).

    Per unit: ``shrunk, w = shrink(history ROAS, cohort p50, n_days,
    n_camps)``; ``own_roas = own sales / own spend``; the local blend is
    used only when own spend is at least ``money(blend_min_own_spend_ref)``.
    ``judge`` = blended, else shrunk, else history ROAS.  First match:

    * no history -> ``no-history`` (low); history without ROAS ->
      ``thin-history`` (low);
    * own-brand role and ``judge >= 15 x be`` -> ``brand-defense`` (fund to
      hold the top slot, judge by rank held, never scale on the number);
    * ``judge >= cohort p75`` or ``>= 2 x be`` -> ``proven-winner`` when
      ``n_camps >= 2`` or ``n_days >= 10``, else ``thin-promising``;
    * ``judge < cohort p25`` or ``< be`` -> ``known-bleeder`` (cap rather
      than kill when the halo share exceeds 0.2);
    * else ``neutral``.

    Then: bid addendum from the clearing bids, a disclosure line when the
    blend moved the number by ``blend_disclosure`` or more, and the sibling
    pass (a winner and a same-role bleeder that are sibling terms are both
    flagged and the winner is withheld from the winner lists).

    Returns a :class:`Preflight` with the keyword part filled; cards are
    sorted bleeders, winners, thin history, no history, neutral, others,
    then by own spend descending.
    """
    cfg, econ = cfg or _CC, economics or _ECON
    be = econ.be
    history = history or {}
    min_blend = econ.money(cfg.blend_min_own_spend_ref)
    cards: list[KeywordCard] = []
    seen: set[str] = set()
    for item in keywords:
        own = item if isinstance(item, OwnUnit) else (
            OwnUnit(value=item) if isinstance(item, str) else
            OwnUnit(value=str(_g(item, "value", _g(item, "kw", ""))), spend=_g(item, "spend"),
                    sales=_g(item, "sales"), bid=_g(item, "bid")))
        kw = (own.value or "").strip().lower()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        h = _unit_history(history.get(kw))
        role = _role_of(kw, roles, h)
        p25, p50, p75 = _cohort_bars(cohorts, role, cfg.cohort_min_n)
        r = h.roas if h else None
        nd, nc = (h.n_days, h.n_camps) if h else (0, 0)
        sr, w = shrink(r, p50, nd, nc, cfg)
        own_roas = (own.sales / own.spend) if (own.sales is not None and own.spend) else None
        blended = None
        if own_roas is not None and own.spend is not None and own.spend >= min_blend:
            blended = blend_local(own.spend, own_roas, sr, cfg, econ)
        cb = clearing.get(kw) if clearing else None
        c50, c75 = _g(cb, "p50"), _g(cb, "p75")
        card = KeywordCard(
            kw=kw, role=role, call=CALL_NEUTRAL, confidence="low", own_roas=own_roas, blended_roas=blended,
            hist_roas=r, shrunk_roas=sr if h else None, self_weight=w, n_days=nd, n_camps=nc,
            cost_per_new_user=h.cost_per_new_user if h else None, halo=h.halo if h else None,
            top_bid_p50=c50, top_bid_p75=c75, own_bid=own.bid, spend=float(own.spend or 0.0))
        if h is None:
            card.call = CALL_NO_HISTORY
            card.actions.append("No account history: start capped and monitor.")
        elif r is None:
            card.call = CALL_THIN_HISTORY
            card.actions.append("History without a usable return: start conservative.")
        else:
            card.confidence = confidence_from_weight(w, cfg)
            judge = blended if blended is not None else (sr if sr is not None else r)
            if role == own_brand_role and judge >= cfg.brand_defense_roas_be * be:
                card.call = CALL_BRAND_DEFENSE
                card.actions.append(
                    "Own-brand term with an implausibly high measured return (mostly organic capture): "
                    "fund to hold the top slot, judge by rank held, never scale on the number.")
            elif (p75 and judge >= p75) or judge >= cfg.winner_roas_be * be:
                if nc >= cfg.proven_min_camps or nd >= cfg.proven_min_days:
                    card.call = CALL_PROVEN_WINNER
                    card.actions.append(f"Proven in the account (ROAS {_fmt(judge)}): fund from day 1.")
                else:
                    card.call = CALL_THIN_PROMISING
                    card.actions.append(f"Promising on thin evidence (ROAS {_fmt(judge)}): fund small, re-check weekly.")
            elif (p25 and judge < p25) or judge < cfg.bleeder_roas_be * be:
                card.call = CALL_KNOWN_BLEEDER
                if (h.halo or 0.0) > cfg.halo_spare_share:
                    card.actions.append(f"Known bleeder (ROAS {_fmt(judge)}) with indirect sales: cap, don't kill.")
                else:
                    card.actions.append(f"Known bleeder (ROAS {_fmt(judge)}): start capped or negative.")
            else:
                card.actions.append("No strong prior: start at the type-median bid and watch.")
        if own.bid is not None and c50 is not None and own.bid < c50:
            card.actions.append(f"Bid {_fmt(own.bid)} is under the top-slot clearing median {_fmt(c50)}: "
                                "underbidding, expect thin impressions.")
        elif own.bid is None and (c50 is not None or c75 is not None):
            card.actions.append(f"Set the opening bid at the top-slot clearing p50 {_fmt(c50)} / p75 {_fmt(c75)}.")
        if (blended is not None and sr is not None and abs(blended - sr) >= cfg.blend_disclosure
                and card.call not in (CALL_NO_HISTORY, CALL_THIN_HISTORY)):
            card.actions.append(f"Call uses the blended return {_fmt(blended)} (own early data moved the "
                                f"account number {_fmt(sr)}).")
        cards.append(card)

    # sibling conflict pass
    bleed_cards = [c for c in cards if c.call == CALL_KNOWN_BLEEDER]
    for c in cards:
        if c.call not in (CALL_PROVEN_WINNER, CALL_THIN_PROMISING):
            continue
        for b in bleed_cards:
            if b.role == c.role and siblings(c.kw, b.kw, cfg):
                line = ("Sibling term conflict with '{}': one shopper intent, one decision: do not fund one "
                        "side while capping the other.")
                if c.sibling_conflict is None:
                    c.sibling_conflict = b.kw
                    c.actions.append(line.format(b.kw))
                if b.sibling_conflict is None:
                    b.sibling_conflict = c.kw
                    b.actions.append(line.format(c.kw))
                break

    cards.sort(key=lambda c: (_KEYWORD_ORDER.get(c.call, 9), -c.spend, c.kw))
    n = len(cards)
    n_hist = sum(1 for c in cards if c.call not in (CALL_NO_HISTORY, CALL_THIN_HISTORY))
    return Preflight(
        status="ok" if n else "empty",
        reason=None if n else "no targeting units supplied",
        n_keywords=n, n_with_history=n_hist, coverage_pct=int(round(100.0 * n_hist / n)) if n else 0,
        winners=[c for c in cards if c.call == CALL_PROVEN_WINNER and not c.sibling_conflict],
        thin_promising=[c for c in cards if c.call == CALL_THIN_PROMISING and not c.sibling_conflict],
        bleeders=[c for c in cards if c.call == CALL_KNOWN_BLEEDER],
        brand_defense=[c for c in cards if c.call == CALL_BRAND_DEFENSE],
        cards=cards)


# --------------------------------------------------------------------------- product and geo pre-flight

def preflight_products(
    product_ids: Sequence[str] | None,
    history: Mapping[str, ProductHistory | Mapping[str, Any]] | None,
    availability: Mapping[str, float | None] | None = None,
    *,
    cfg: ColdStartConfig | None = None,
    economics: Economics | None = None,
    availability_policy: AvailabilityPolicy | None = None,
) -> tuple[list[ProductCard], list[ProductCard], list[ProductCard]]:
    """Call each product a campaign advertises from account history.

    Prior = the account's *median* product ROAS over the history store (a
    flat prior, because no product cohorts exist), or
    ``product_prior_roas_be x be`` when the store is empty.
    ``shrunk, w = shrink(history ROAS, prior, n_days, n_camps)``;
    ``judge = shrunk or history ROAS``.  ``judge >= 2 x be`` -> proven;
    ``judge < be`` -> bleeder (with availability under the block line:
    "fix supply before funding"); else neutral.  Any non-bleeder under the
    availability block line carries a warning.  A campaign with exactly one
    product is ``solo``: a solo proven product is not listed as a
    reallocation winner (its budget is already wholly on it).

    Availability comes from ``availability`` (0..1) when given, else from
    the history record.  Products with no history at all are skipped.

    Returns ``(cards, winners, bleeders)``; cards sorted bleeder, proven,
    neutral, then by campaign count descending.
    """
    cfg, econ, pol = cfg or _CC, economics or _ECON, availability_policy or _AVAIL
    if not product_ids or not history:
        return [], [], []
    be = econ.be
    known = [x.roas for x in (_product_history(v) for v in history.values()) if x is not None and x.roas is not None]
    prior = float(median(known)) if known else cfg.product_prior_roas_be * be
    ids = list(dict.fromkeys(product_ids))
    solo = len(ids) == 1
    cards: list[ProductCard] = []
    for pid in ids:
        h = _product_history(history.get(pid))
        if h is None:
            continue
        sr, w = shrink(h.roas, prior, h.n_days, h.n_camps, cfg)
        judge = sr or h.roas
        av = pol.normalise(availability.get(pid)) if availability and pid in availability else pol.normalise(h.availability)
        low_av = pol.enabled and av is not None and av < pol.block
        card = ProductCard(product_id=pid, call=CALL_NEUTRAL_PRODUCT, confidence=confidence_from_weight(w, cfg),
                           hist_roas=h.roas, shrunk_roas=sr, self_weight=w, n_days=h.n_days, n_camps=h.n_camps,
                           availability=av, solo=solo)
        if judge is not None and judge >= cfg.product_proven_roas_be * be:
            card.call = CALL_PROVEN_PRODUCT
            card.actions.append("Solo proven product: budget is already 100% on it; scale via daily budget and "
                                "bids, not reallocation." if solo else
                                f"Proven product (ROAS {_fmt(judge)}): allocate budget to it.")
        elif judge is not None and judge < cfg.product_bleeder_roas_be * be:
            card.call = CALL_BLEEDER_PRODUCT
            card.actions.append(f"Bleeding product (ROAS {_fmt(judge)}): "
                                + ("fix supply before funding." if low_av else "hold spend."))
        else:
            card.actions.append("No strong prior on this product: watch.")
        if low_av and card.call != CALL_BLEEDER_PRODUCT:
            card.actions.append(f"Warning: availability {av:.0%} is under the {pol.block:.0%} block line.")
        cards.append(card)
    cards.sort(key=lambda c: (_PRODUCT_ORDER.get(c.call, 9), -c.n_camps, c.product_id))
    winners = [c for c in cards if c.call == CALL_PROVEN_PRODUCT and not c.solo]
    bleeders = [c for c in cards if c.call == CALL_BLEEDER_PRODUCT]
    return cards, winners, bleeders


def geo_signal(
    history: Mapping[str, GeoHistory | Mapping[str, Any] | float] | None,
    availability: Mapping[str, float | None] | None = None,
    tiers: Mapping[str, str] | None = None,
    *,
    cfg: ColdStartConfig | None = None,
    economics: Economics | None = None,
) -> GeoSignal | None:
    """Where does the account already win and lose by geo?

    Geos with a known history ROAS are sorted descending; at least
    ``geo_min_count`` (2) are needed.  ``strong`` = up to ``geo_list_n`` (3)
    top geos with ROAS >= ``geo_strong_roas_be x be``; ``weak`` = up to 3
    bottom geos below it (worst first).  Rows are enriched with availability
    and an investment tier when supplied.

    Returns a :class:`GeoSignal` or ``None`` (fewer than two geos).
    """
    cfg, econ = cfg or _CC, economics or _ECON
    rows: list[tuple[str, float]] = []
    for geo, h in (history or {}).items():
        r = h if isinstance(h, (int, float)) else _g(h, "roas")
        if r is not None:
            rows.append((geo, float(r)))
    if len(rows) < cfg.geo_min_count:
        return None
    rows.sort(key=lambda t: (-t[1], t[0]))
    bar = cfg.geo_strong_roas_be * econ.be

    def row(geo: str, r: float) -> dict[str, Any]:
        d: dict[str, Any] = {"geo": geo, "roas": r}
        if availability and availability.get(geo) is not None:
            d["availability"] = availability[geo]
        if tiers and tiers.get(geo):
            d["tier"] = tiers[geo]
        return d

    strong = tuple(row(g, r) for g, r in rows[: cfg.geo_list_n] if r >= bar)
    weak = tuple(row(g, r) for g, r in reversed(rows[-cfg.geo_list_n:]) if r < bar)
    parts = []
    if strong:
        parts.append("account history is strongest in " + ", ".join(f"{d['geo']} ({d['roas']:.2f})" for d in strong))
    if weak:
        parts.append("weakest in " + ", ".join(f"{d['geo']} ({d['roas']:.2f})" for d in weak))
    note = ("Geo history: " + "; ".join(parts) + ".") if parts else "Geo history shows no clear strong or weak geo."
    return GeoSignal(strong=strong, weak=weak, note=note)


def preflight(
    keywords: Iterable[str | OwnUnit | Mapping[str, Any]],
    history: AccountHistory | None = None,
    cohorts: Mapping[str, Any] | None = None,
    clearing: Mapping[str, ClearingBid | Mapping[str, Any]] | None = None,
    roles: Mapping[str, str] | Callable[[str], str | None] | None = None,
    *,
    product_ids: Sequence[str] | None = None,
    product_availability: Mapping[str, float | None] | None = None,
    geo_availability: Mapping[str, float | None] | None = None,
    geo_tiers: Mapping[str, str] | None = None,
    own_brand_role: str = "own_brand",
    cfg: ColdStartConfig | None = None,
    economics: Economics | None = None,
    availability_policy: AvailabilityPolicy | None = None,
) -> Preflight:
    """Build the whole pre-flight card of one campaign.

    Combines :func:`preflight_keywords`, :func:`preflight_products` (only
    when ``product_ids`` is given) and :func:`geo_signal` over one
    :class:`AccountHistory`.  Missing history degrades: units become
    ``no-history`` cards, product lists stay empty, the geo signal is
    ``None``.  Returns the :class:`Preflight`.
    """
    history = history or AccountHistory()
    out = preflight_keywords(keywords, history.units, cohorts, clearing, roles,
                             own_brand_role=own_brand_role, cfg=cfg, economics=economics)
    out.product_cards, out.product_winners, out.product_bleeders = preflight_products(
        product_ids, history.products, product_availability, cfg=cfg, economics=economics,
        availability_policy=availability_policy)
    out.geo_signal = geo_signal(history.geos, geo_availability, geo_tiers, cfg=cfg, economics=economics)
    if out.status == "empty" and (out.product_cards or out.geo_signal):
        out.status, out.reason = "ok", None
    return out


# --------------------------------------------------------------------------- history builder

def history_from_rows(
    rows: Iterable[PerfRow],
    *,
    exclude_entity_id: str | None = None,
    roles: Mapping[str, str] | Callable[[str], str | None] | None = None,
    is_search_keyword: Callable[[str], bool] | None = None,
    is_aggregate_geo: Callable[[str], bool] | None = None,
    product_availability: Mapping[str, float | None] | None = None,
    window_days: int | None = None,
) -> AccountHistory:
    """Pivot account performance rows onto the persistent grains.

    Campaigns churn, but targeting units, products and geos persist.  For
    each grain, over rows with spend > 0: ``roas = sum(sales) / sum(spend)``,
    ``n_days`` = distinct days, ``n_camps`` = distinct campaigns,
    ``last_seen`` = latest day; units also get ``halo = indirect /
    (indirect + direct)`` and ``cost_per_new_user = spend / new users``
    when the platform reports them.  ``exclude_entity_id`` leaves the
    campaign being pre-flighted out of its own prior.  ``is_search_keyword``
    and ``is_aggregate_geo`` (adapter predicates) drop non-keyword
    placements and aggregate geo buckets.

    Returns an :class:`AccountHistory`.
    """
    acc: dict[str, dict[str, dict[str, Any]]] = {"u": {}, "p": {}, "g": {}}

    def add(kind: str, key: str, r: PerfRow) -> None:
        a = acc[kind].setdefault(key, {"spend": 0.0, "sales": 0.0, "ind": None, "nu": None,
                                       "days": set(), "camps": set(), "last": r.date})
        a["spend"] += r.spend
        a["sales"] += r.sales
        if r.indirect_sales is not None:
            a["ind"] = (a["ind"] or 0.0) + r.indirect_sales
        if r.new_users is not None:
            a["nu"] = (a["nu"] or 0.0) + r.new_users
        a["days"].add(r.date)
        a["camps"].add(r.entity_id)
        a["last"] = max(a["last"], r.date)

    for r in rows:
        if r.spend <= 0 or (exclude_entity_id is not None and r.entity_id == exclude_entity_id):
            continue
        if r.targeting_unit:
            kw = r.targeting_unit.strip().lower()
            if kw and (is_search_keyword is None or is_search_keyword(kw)):
                add("u", kw, r)
        if r.product_id:
            add("p", r.product_id, r)
        if r.geo and not (is_aggregate_geo and is_aggregate_geo(r.geo)):
            add("g", r.geo, r)

    def base(a: dict[str, Any]) -> dict[str, Any]:
        return {"roas": a["sales"] / a["spend"], "n_days": len(a["days"]), "n_camps": len(a["camps"]),
                "spend": a["spend"], "last_seen": a["last"]}

    units: dict[str, UnitHistory] = {}
    for kw, a in acc["u"].items():
        role = roles(kw) if callable(roles) else (roles.get(kw) if roles else None)
        tot = a["sales"] + (a["ind"] or 0.0)
        units[kw] = UnitHistory(
            role=getattr(role, "value", role),
            halo=None if a["ind"] is None else (a["ind"] / tot if tot > 0 else 0.0),
            cost_per_new_user=(a["spend"] / a["nu"]) if a["nu"] else None, **base(a))
    products = {pid: ProductHistory(availability=(product_availability or {}).get(pid), **base(a))
                for pid, a in acc["p"].items()}
    geos = {g: GeoHistory(**base(a)) for g, a in acc["g"].items()}
    return AccountHistory(units=units, products=products, geos=geos, window_days=window_days)


# --------------------------------------------------------------------------- regime guard

def daily_weighted_bid(rows: Iterable[PerfRow], entity_id: str | None = None) -> dict[date, float]:
    """Spend-weighted average bid per day (``sum(bid x spend) / sum(spend)``
    over rows that report a bid), date-sorted.  Input to the bid-telemetry
    branch of :func:`detect_regime`."""
    num: dict[date, float] = {}
    den: dict[date, float] = {}
    for r in rows:
        if r.bid is None or r.spend <= 0 or (entity_id is not None and r.entity_id != entity_id):
            continue
        num[r.date] = num.get(r.date, 0.0) + r.bid * r.spend
        den[r.date] = den.get(r.date, 0.0) + r.spend
    return {d: num[d] / den[d] for d in sorted(num)}


def _as_date(x: Any) -> date | None:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    if isinstance(x, str):
        try:
            return date.fromisoformat(x[:10])
        except ValueError:
            return None
    return None


def detect_regime(
    anchor: date,
    change_events: Iterable[ChangeEvent] | None = None,
    declared_changes: Iterable[tuple[Any, str] | Mapping[str, Any]] | None = None,
    daily_bids: Mapping[date, float] | None = None,
    *,
    entity_id: str | None = None,
    automated_sources: Iterable[str] = (),
    cfg: ColdStartConfig | None = None,
) -> RegimeChange | None:
    """Find the last operator change and decide whether the data predates it.

    Decision: if the operator changed bid, budget or status a day or two
    ago, the window's numbers describe the *previous* setup and must not
    drive a repeat of a cut that was already made.

    Sources, first one that yields a change inside ``regime_lookback_days``
    (30) of ``anchor`` wins:

    1. the change log: events on ``regime_fields`` that are not marked
       unverified; automated bid changes (``automated`` flag or a source in
       ``automated_sources``) are ignored;
    2. operator-declared changes (``(date, note)`` pairs or mappings with
       ``date`` / ``note``), for changes made outside the tracked tools;
    3. bid telemetry: the latest day whose spend-weighted bid moved by
       ``regime_bid_step`` (25%) or more against the previous observed day.

    ``post_change_days = anchor - change date``; ``prior_regime`` is true
    when it is at most ``regime_post_days_max`` (2).

    Returns a :class:`RegimeChange` or ``None`` when no change is found (the
    guard is then silently absent).
    """
    cfg = cfg or _CC
    auto = {s.lower() for s in automated_sources}

    def inside(d: date | None) -> bool:
        return d is not None and 0 <= (anchor - d).days <= cfg.regime_lookback_days

    found: tuple[date, str, str] | None = None
    best_ts: Any = None
    for ev in change_events or ():
        if entity_id is not None and ev.entity_id != entity_id:
            continue
        fld = (ev.field or "").lower()
        if fld not in cfg.regime_fields or ev.verified is False:
            continue
        if fld in cfg.regime_bid_fields and (ev.automated or (ev.source or "").lower() in auto):
            continue
        d = _as_date(ev.timestamp)
        if inside(d) and (best_ts is None or ev.timestamp > best_ts):
            best_ts = ev.timestamp
            found = (d, "change_log", f"{fld} changed from {ev.old} to {ev.new}")  # type: ignore[assignment]
    if found is None:
        for item in declared_changes or ():
            d = _as_date(item[0] if isinstance(item, tuple) else _g(item, "date"))
            note = str(item[1] if isinstance(item, tuple) else _g(item, "note", ""))
            if inside(d) and (found is None or d > found[0]):  # type: ignore[operator]
                found = (d, "declared", note or "declared operator change")  # type: ignore[assignment]
    if found is None and daily_bids:
        days = sorted(d for d in daily_bids if d <= anchor)
        for prev, cur in zip(days, days[1:]):
            b0, b1 = daily_bids[prev], daily_bids[cur]
            if b0 and b0 > 0 and abs(b1 / b0 - 1.0) >= cfg.regime_bid_step - 1e-12 and inside(cur):
                found = (cur, "bid_telemetry", f"spend-weighted bid moved {100.0 * (b1 / b0 - 1.0):+.0f}% in a day")
    if found is None:
        return None
    post = (anchor - found[0]).days
    prior = post <= cfg.regime_post_days_max
    warning = None
    if prior:
        warning = (f"Operator change on {found[0].isoformat()} ({found[2]}): these numbers describe the PRIOR "
                   f"regime; do NOT re-apply cuts already made; re-run after {cfg.regime_rerun_days}+ post-change days.")
    return RegimeChange(changed_on=found[0], source=found[1], detail=found[2], post_change_days=post,
                        prior_regime=prior, warning=warning)


# --------------------------------------------------------------------------- state mode

def diagnose_state(
    entity_id: str,
    status: Any,
    n_active_days: int,
    card: Preflight | None,
    *,
    spend_7d: float | None = None,
    spend_3d: float | None = None,
    regime: RegimeChange | None = None,
    own_roas: float | None = None,
    window_facts: Sequence[str] = (),
    untracked: Sequence[str] = (),
    status_map: Mapping[str, Any] | None = None,
    tiers: TierConfig | None = None,
    cfg: ColdStartConfig | None = None,
) -> StateDiagnosis:
    """State-mode diagnosis of a campaign too young for attribution.

    Steps:

    1. Exclusion: a hard-dead campaign with no spend in the last 7 days
       (``liveness.stale_stopped``) returns ``status="excluded"``.
    2. Tier from active days (COLD / WARM / ...).
    3. Candidates from the pre-flight card.  Bleed: up to two keyword
       bleeders ("Cap / negative (reversible)", half-step cap, re-check in 7
       days) and one product bleeder ("Fix supply / hold spend").  Win: one
       brand-defense ("Hold top slot (brand defense)"), up to two keyword
       winners ("Fund from day 1"), one product winner ("Allocate budget").
       ``top3 = (bleed[:2] + win[:1] + bleed[2:] + win[1:])[:3]``: stop
       loss first, one reserved growth slot, then backfill.
    4. Confidence: High at history coverage >= 80%, Medium at >= 40%, else
       Low.  Priority ``p0`` if any bleed candidate exists, else ``p2``.
    5. Regime guard: when ``regime.prior_regime`` is true its warning leads
       the facts and confidence is forced to Low.
    6. Facts: regime warning, caller-supplied window facts, the geo note,
       declared untracked campaigns as a coverage caveat, and the campaign's
       own ROAS labelled as noisy and unused.

    Returns a :class:`StateDiagnosis` (``mode="state"``,
    ``verdict="state-only"``, zero move, ``short=True``).
    """
    cfg = cfg or _CC
    cls = liveness_class(status, status_map)
    if cls is LivenessClass.HARD_DEAD and stale_stopped(status, spend_7d, spend_3d, status_map=status_map):
        return StateDiagnosis(status="excluded", reason="stopped with no spend in the recent window",
                              entity_id=entity_id, n_active_days=n_active_days)
    tier: DataTier = route_tier(n_active_days, tiers)
    card = card or Preflight(status="empty", reason="no pre-flight card")

    bleed: list[StateAction] = []
    for c in card.bleeders[: cfg.max_keyword_bleed_actions]:
        bleed.append(StateAction(
            verb="Cap / negative (reversible)", lever="keyword", kind="bleed", targets=(c.kw,),
            headline=f"'{c.kw}' is a known bleeder in the account (ROAS {_fmt(c.blended_roas if c.blended_roas is not None else c.shrunk_roas)})",
            sizing="half-step cap, recheck in 7 days"))
    for p in card.product_bleeders[: cfg.max_product_bleed_actions]:
        bleed.append(StateAction(
            verb="Fix supply / hold spend", lever="product", kind="bleed", targets=(p.product_id,),
            headline=f"Product {p.product_id} loses money across the account (ROAS {_fmt(p.shrunk_roas)})",
            sizing="hold spend until supply and return recover"))
    win: list[StateAction] = []
    for c in card.brand_defense[: cfg.max_defense_actions]:
        win.append(StateAction(
            verb="Hold top slot (brand defense)", lever="keyword", kind="win", targets=(c.kw,),
            headline=f"'{c.kw}' is an own-brand term: defend the position, do not scale on the number",
            sizing="fund to hold the top slot; judge by rank held"))
    for c in card.winners[: cfg.max_winner_actions]:
        win.append(StateAction(
            verb="Fund from day 1", lever="keyword", kind="win", targets=(c.kw,),
            headline=f"'{c.kw}' is proven in the account ({c.n_camps} campaigns, {c.n_days} days)",
            sizing="open at the type-median bid with full budget share"))
    for p in card.product_winners[: cfg.max_product_winner_actions]:
        win.append(StateAction(
            verb="Allocate budget", lever="product", kind="win", targets=(p.product_id,),
            headline=f"Product {p.product_id} is proven in the account (ROAS {_fmt(p.shrunk_roas)})",
            sizing="shift budget share towards it"))
    top3 = (bleed[:2] + win[:1] + bleed[2:] + win[1:])[: cfg.max_state_actions]

    cov = card.coverage_pct
    confidence = "High" if cov >= cfg.coverage_high_pct else "Medium" if cov >= cfg.coverage_med_pct else "Low"
    facts: list[str] = []
    if regime is not None and regime.prior_regime:
        confidence = "Low"
        if regime.warning:
            facts.append(regime.warning)
    facts.append(f"State mode: {n_active_days} active days ({tier.value} tier) is too few to attribute a move; "
                 f"calls come from account history ({card.n_with_history} of {card.n_keywords} units known, "
                 f"{cov}% coverage).")
    facts.extend(window_facts)
    if card.geo_signal is not None and card.geo_signal.note:
        facts.append(card.geo_signal.note)
    if untracked:
        facts.append("Coverage caveat: " + ", ".join(untracked) + " run outside the tracked system and are not "
                     "in these numbers.")
    if own_roas is not None:
        facts.append(f"Own {n_active_days}-day ROAS {own_roas:.2f}: noisy, not used for attribution at this age.")
    return StateDiagnosis(
        status="ok", entity_id=entity_id, tier=tier.value, n_active_days=n_active_days, confidence=confidence,
        priority="p0" if bleed else "p2", top3=top3, regime=regime, untracked=list(untracked), facts=facts,
        preflight=card)
