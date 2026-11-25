"""Per-entity recommendation gate stack, structural bleed and priority.

``recommend`` turns one entity's attribution, targeting-unit table and
context into **at most three actions**.  It is a pure function of its
arguments.  The order is deliberate:

* **Phase A, mandatory actions**: availability, auto-targeting burn, the
  budget cap, sub-breakeven targeting units.  These do not depend on the
  attribution model.
* **Phase B, scored pool**: the remaining slots are filled by score.
  Lever stories are told only when the attribution verdict allows it, and
  only ever for tier-1 levers (things the advertiser controls).  A tier-2
  lever as the largest mover produces a context note, never an action.
* **Phase C, post-filters**: ramp, execution health, freshness, new-user
  annotation and the status guard.

Every ROAS bar is a multiple of the account's effective breakeven
(``Economics.be``); every money floor is a reference amount passed through
``Economics.money``.  With default economics the numbers read as written.

Inputs computed by other subsystems (whether the entity is budget-capped,
marginal ROAS at the cap, the utilisation-ladder state, role cohorts) are
consumed here as plain fields; this module never recomputes them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .attribution.jury import NO_STORY_VERDICTS, Driver
from .attribution.levers import TIER2
from .config import AvailabilityPolicy, Economics
from .liveness import canonical_status
from .metrics import percentile
from .schema import CanonicalStatus, PerfRow, RoleCohort, TargetingUnit, Window

# --------------------------------------------------------------------------- vocabulary

BID_CAP, BID_LIFT, BUDGET_RAISE, BUDGET_TIGHTEN = "BID_CAP", "BID_LIFT", "BUDGET_RAISE", "BUDGET_TIGHTEN"
PAUSE_UNIT, RANK_TARGET, AVAILABILITY_GATE, ADD_UNITS = "PAUSE_UNIT", "RANK_TARGET", "AVAILABILITY_GATE", "ADD_UNITS"
REPRICE, LISTING_FIX, HOLD, NOTE, INVESTIGATE, RELAUNCH = "REPRICE", "LISTING_FIX", "HOLD", "NOTE", "INVESTIGATE", "RELAUNCH"

ACTION_TYPES: tuple[str, ...] = (BID_CAP, BID_LIFT, BUDGET_RAISE, BUDGET_TIGHTEN, PAUSE_UNIT, RANK_TARGET,
                                 AVAILABILITY_GATE, ADD_UNITS, REPRICE, LISTING_FIX, HOLD, NOTE, INVESTIGATE, RELAUNCH)

#: lever -> (headline, advice, action type).  The last three are context only (tier 2).
RULES: dict[str, tuple[str, str, str]] = {
    "competitor_share": ("Competitor-conquest targeting mix",
                         "Trim conquest terms below the ROAS floor; keep the ones that convert", PAUSE_UNIT),
    "own_paid_sov": ("Paid share of voice slipping",
                     "Raise bids or budget on contested head terms where ROAS allows", BID_LIFT),
    "bid": ("Bid / CPM inflation", "Review bid against realised ROAS; cap bids on low-ROAS terms", BID_CAP),
    "position": ("Ad position slipped", "Push rank on the highest-ROAS terms", RANK_TARGET),
    "own_availability": ("Own availability drag",
                         "Gate ad spend on low-availability products until stock recovers", AVAILABILITY_GATE),
    "own_discount": ("Pricing / discount depth", "Review discount against competitors on hero products", REPRICE),
    "own_organic_sov": ("Organic share of voice slipping",
                        "Improve listing quality, relevance and ratings; paid will not fix organic", LISTING_FIX),
    "price_gap": ("Price gap against competitors", "Re-check price positioning", REPRICE),
    "comp_discount": ("Competitor discounting", "Respond through own price; do not chase", NOTE),
    "comp_availability": ("Competitor availability",
                          "Scale opportunistically where competitors are out of stock", NOTE),
    "log_category_impressions": ("Category demand shift",
                                 "Largely external; adjust expectations, not levers", NOTE),
}

#: levers whose remedy is to buy more visibility (held under an availability block).
BOOST_LEVERS: dict[str, str] = {"position": "Push rank on", "own_paid_sov": "Raise bids on",
                                "own_organic_sov": "Strengthen listings for"}

#: verb classes that add spend or exposure (ramp tag, freshness guard, new-user annotation).
RAISE_CLASSES: frozenset[str] = frozenset({"raise", "topup", "scale", "prefund", "bid_lift", "resume", "reactivate"})
#: verb classes that write a change to the platform (execution-health warning).
WRITE_CLASSES: frozenset[str] = RAISE_CLASSES | frozenset({"tighten", "pause", "cap", "add_units", "cut"})
#: verb classes stripped while the platform holds the entity for budget.
HELD_STRIP_CLASSES: frozenset[str] = frozenset({"reactivate", "add_units", "bid_lift", "scale", "raise", "prefund",
                                                "tighten", "resume"})


@dataclass(frozen=True)
class RecommendConfig:
    """Thresholds of the recommendation gate stack (every field has a default).

    ``*_ref`` money amounts are in reference-currency units and are scaled by
    ``Economics.money``; ``*_be`` ROAS bars are multiples of ``Economics.be``.
    """

    max_actions: int = 3                    # an operator acts on a short list; more is noise
    # --- materiality floors (relative to the entity's own targeting-unit spend)
    bleed_floor_ref: float = 10000.0        # absolute floor for a bleeder candidate
    bleed_floor_share: float = 0.05         # ... or 5% of unit spend, whichever is larger
    pause_floor_ref: float = 8000.0         # absolute floor for the pause list
    pause_floor_share: float = 0.03         # ... or 3% of unit spend
    cap_floor_ref: float = 5000.0           # absolute floor for a bid-cap target
    cap_floor_share: float = 0.02           # ... or 2% of unit spend
    cohort_spend_floor_ref: float = 1000.0  # a unit needs this much spend to count in its role cohort
    cohort_min_n: int = 12                  # percentile trusted only with this many units (5 was noisy)
    # --- ROAS bars (multiples of effective breakeven)
    bleed_roas_be: float = 1.0              # below breakeven a unit destroys value
    pause_roas_be: float = 1.5              # pause-list bar: thin margin, material spend
    cap_roas_be: float = 1.8                # bid-cap target bar
    boost_roas_be: float = 2.0              # a unit must clear this before visibility is bought on it
    budget_raise_roas_be: float = 2.0       # capped entity: raise at or above, hold between breakeven and this
    winner_roas_be: float = 1.5             # scale candidate floor
    halo_roas_be: float = 3.0               # scale on halo: total ROAS at least this ...
    halo_total_to_direct: float = 1.5       # ... and at least 1.5 x direct ROAS
    thin_halo_ratio: float = 1.3            # auto-targeted burn: total under 1.3 x direct = thin halo
    prefund_min_roas_be: float = 1.0        # never pre-fund a sub-breakeven entity
    # --- targeting-unit quality gates
    min_search_volume: float = 50.0         # searches per day below which a unit is too small to scale or boost
    min_relevance: float = 0.3              # relevance score below which a unit is not boosted
    max_winner_organic_rank: float = 6.0    # organic rank at or better than this already wins the shelf: do not pay for it
    head_term_top_n: int = 3                # generic head terms: top 3 by search volume ...
    head_term_volume_share: float = 0.10    # ... or 10% of generic search volume
    max_targets: int = 3                    # named targets per action
    cap_fallback_n: int = 2                 # bid-cap fallback: the two lowest-ROAS units
    max_stops: int = 3                      # stops per run
    max_staged_caps: int = 3                # staged bid caps per run
    max_pause_list: int = 6                 # length of the pause list
    max_spared: int = 4                     # spared units reported
    # --- availability
    availability_move: float = 0.05         # a 5-point drop or recovery window over window
    availability_sparse_days: int = 2       # 1-2 observed days: fail closed to caution (no resume)
    # --- organic share of own impressions
    organic_cold: float = 0.20              # below: own-brand terms still need paid support
    organic_self_sustaining: float = 0.70   # at or above: paid on own demand is mostly non-incremental
    # --- budget branch
    bleed_hold_pct: float = 40.0            # structural bleed share that blocks a budget raise
    marginal_roas_block: float = 0.9        # marginal ROAS at the cap below this is the hard block (wording)
    marginal_roas_ceiling: float = 1.0      # a raise needs at least this marginal ROAS: below it incremental revenue < incremental spend
    curve_min_r2: float = 0.3               # the saturation curve is trusted only at or above this fit
    severe_position_split: float = 3.0      # severe under-utilisation: position worse than this -> bids, else units
    # --- auto-targeted burn
    burn_spend_up: float = 0.05             # spend up more than 5% ...
    burn_roas_down: float = 0.20            # ... while ROAS fell at least 20%
    # --- bleeder loop
    halo_share: float = 0.20                # indirect share above this spares or caps instead of stopping
    acquisition_watch_mult: float = 2.0     # cost per new user up to 2 x cohort p25: spare, watch
    # --- pre-fund
    demand_trend_min: float = 0.25          # demand up 25% or more
    new_product_days: int = 90              # products younger than this are exempt from the cannibalisation gate
    # --- scores of the candidate pool
    score_tighten_account: float = 80.0     # stop a starved loser first
    score_ladder_severe: float = 78.0       # an entity spending under 20% of its cap
    score_raise_account: float = 72.0       # fund a starved winner
    score_scale_winners: float = 70.0       # scale units that already win
    score_prefund_capped: float = 64.0      # demand spike on a capped entity
    score_ladder: float = 56.0              # other utilisation-ladder candidates
    score_lever_base: float = 50.0          # hurting tier-1 lever: 50 + 0.4 x min(|pct|, 40) -> 50..66
    score_lever_slope: float = 0.4          # ... slope
    score_lever_pct_cap: float = 40.0       # ... cap on |pct|
    score_prefund: float = 48.0             # demand spike, not capped
    score_resume: float = 42.0              # resume scaling after availability recovered
    score_protect: float = 18.0             # helping tier-1 lever: protect / maintain
    account_moves_n: int = 2                # account-level budget moves considered per direction
    # --- post-filters
    exec_fail_rate: float = 0.5             # more than half of recent automated operations failed
    exec_min_ops: int = 10                  # ... out of at least this many
    stale_days: int = 3                     # data older than this is stale
    wind_down_share: float = 0.5            # spend at or below half the prior window = being wound down
    acquisition_rate: float = 0.20          # new-user rate at or above: healthy acquisition
    recapture_rate: float = 0.05            # new-user rate below: existing-user recapture
    soft_roas_band: float = 0.10            # "softened" = within 10% below the prior ROAS
    # --- annotators
    self_competition_share: float = 0.30    # share of unit spend also targeted by sibling entities
    history_cut_roas_be: float = 1.0        # history agrees with a cut below this
    history_grow_roas_be: float = 2.0       # history agrees with growth at or above this
    # --- structural bleed and priority
    bleed_worst_pool: int = 15              # "worst" is drawn from the top 15 recent units by spend
    bleed_worst_n: int = 3                  # ... first three
    priority_bleed_pct: float = 40.0        # p0 at this bleed share
    priority_drop: float = -0.25            # p1 at a 25% ROAS drop


_RC = RecommendConfig()
_ECON = Economics()
_AVAIL = AvailabilityPolicy()


# --------------------------------------------------------------------------- inputs

@dataclass
class UnitStats:
    """One targeting unit's totals over a window, plus optional signals.

    ``sales`` is whatever the adapter attributes directly; ``indirect_sales``
    (halo) is optional.  ``None`` always means "not reported", never 0.
    """

    value: str
    role: str = "generic"
    is_placement: bool = False
    spend: float = 0.0
    sales: float = 0.0
    impressions: float = 0.0
    units: float = 0.0
    indirect_sales: float | None = None
    new_users: float | None = None
    search_volume: float | None = None
    relevance: float | None = None
    conversion_confidence: str | None = None
    own_organic_impressions: float | None = None
    own_ad_impressions: float | None = None
    organic_rank: float | None = None

    @property
    def roas(self) -> float | None:
        """Direct ROAS, ``None`` without spend."""
        return self.sales / self.spend if self.spend > 0 else None

    @property
    def total_roas(self) -> float | None:
        """(direct + indirect sales) / spend."""
        return (self.sales + (self.indirect_sales or 0.0)) / self.spend if self.spend > 0 else None

    @property
    def indirect_share(self) -> float | None:
        """indirect / (direct + indirect) sales; ``None`` when not reported."""
        if self.indirect_sales is None:
            return None
        tot = self.sales + self.indirect_sales
        return self.indirect_sales / tot if tot > 0 else 0.0

    @property
    def cost_per_new_user(self) -> float | None:
        """spend / new users; ``None`` when not reported or zero."""
        return self.spend / self.new_users if self.new_users else None

    @property
    def incremental_roas(self) -> float | None:
        """ROAS discounted by the organic share of the unit's own impressions:
        ``roas x (1 - organic / (organic + ad))``; plain ROAS when impressions are unknown."""
        r = self.roas
        if r is None or self.own_organic_impressions is None or self.own_ad_impressions is None:
            return r
        tot = self.own_organic_impressions + self.own_ad_impressions
        return r * (1.0 - self.own_organic_impressions / tot) if tot > 0 else r


Cohort = RoleCohort   # one cohort record for the whole library (``schema.RoleCohort``)


@dataclass
class BudgetState:
    """Budget-side facts supplied by the budget subsystem.

    ``capped`` is the caller's budget-bound decision.  ``ladder`` is the
    under-utilisation diagnosis for a non-capped entity: ``"bid-constrained"``,
    ``"volume-ceiling"`` or ``"severe"``.
    """

    capped: bool = False
    dayparted: bool = False
    marginal_roas: float | None = None
    curve_r2: float | None = None
    rec_range: tuple[float, ...] | None = None
    revenue_at_stake: float | None = None
    utilisation: float | None = None
    ladder: str | None = None
    position: float | None = None
    stepwise_bid_test: bool = False


@dataclass
class RecommendContext:
    """Everything else the gate stack needs about the entity.

    Availability is on the 0..1 scale.  ``status`` may be a raw platform
    string (mapped with ``status_map``) or a canonical status value.
    """

    status: str | None = None
    status_map: Mapping[str, str] | None = None
    auto_targeted: bool = False
    availability: float | None = None
    availability_prior: float | None = None
    availability_days: int | None = None
    organic_share: float | None = None
    spend: float | None = None
    spend_prior: float | None = None
    roas_prior: float | None = None
    total_roas: float | None = None
    direct_roas: float | None = None
    demand_trend: float | None = None
    bleed_pct: float | None = None
    short: bool = False
    exec_fail_rate: float | None = None
    exec_ops: int = 0
    stale_days: int | None = None
    new_user_rate: float | None = None
    product_age_days: int | None = None
    cohorts: Mapping[str, Cohort] = field(default_factory=dict)
    position_lower_is_better: bool = True


@dataclass
class Action:
    """One recommended action.

    ``kind``: ``"unit"`` (named targeting units), ``"product"``, ``"budget"``
    or ``"note"``.  ``verb_class`` is the machine-readable intent used by the
    post-filters.  ``warnings`` are prefixes added by the post-filters;
    ``notes`` are annotations.
    """

    action_type: str
    headline: str
    verb: str
    verb_class: str
    kind: str = "note"
    lever: str | None = None
    targets: list[str] = field(default_factory=list)
    advice: str = ""
    score: float = 0.0
    pct: float | None = None
    sizing: dict | None = None
    jury: str | None = None
    mandatory: bool = False
    ramp: bool = False
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Recommendation:
    """Result of :func:`recommend`: at most ``max_actions`` actions plus context."""

    actions: list[Action] = field(default_factory=list)
    driver: str | None = None
    headline: str | None = None
    action_type: str | None = None
    pause_units: list[str] = field(default_factory=list)
    staged_caps: list[dict] = field(default_factory=list)
    exo_note: str | None = None
    spared: list[dict] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["actions"] = [a.as_dict() for a in self.actions]
        return d


# --------------------------------------------------------------------------- helpers

def _g(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _is_conquest(u: UnitStats) -> bool:
    return str(u.role).startswith("competitor")


def _is_own(u: UnitStats) -> bool:
    return str(u.role).startswith("own")


def _status(ctx: RecommendContext) -> CanonicalStatus:
    s = ctx.status
    if s is None:
        return CanonicalStatus.UNKNOWN
    try:
        return CanonicalStatus(str(s))
    except ValueError:
        return canonical_status(s, ctx.status_map)


def _cohort_value(ctx: RecommendContext, role: str, key: str, cfg: RecommendConfig) -> float | None:
    """Cohort percentile, or ``None`` when the cohort is too small to trust."""
    c = ctx.cohorts.get(role)
    if c is None:
        for name, cand in ctx.cohorts.items():
            if str(role).startswith(str(name)) or str(name).startswith(str(role)):
                c = cand
                break
    if c is None or c.n < cfg.cohort_min_n:
        return None
    return getattr(c, key)


def _zero_organic_conquest(u: UnitStats) -> bool:
    return _is_conquest(u) and u.own_organic_impressions is not None and u.own_organic_impressions <= 0


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Shared linear-interpolation percentile (``metrics.percentile``) on a non-empty sample."""
    value = percentile(sorted_vals, q)
    assert value is not None
    return value


def role_cohorts(units: Iterable[UnitStats], economics: Economics | None = None,
                 cfg: RecommendConfig | None = None) -> dict[str, Cohort]:
    """Role cohorts: per role, percentiles of ROAS and of cost per new user.

    Members are non-placement units with spend at or above
    ``money(cohort_spend_floor_ref)``.  Percentiles use linear interpolation.
    Pass units from the whole account over the cohort window (reference: 60
    days).  ``recommend`` ignores a cohort with fewer than ``cohort_min_n``
    members.  Returns ``{role: Cohort}``.
    """
    cfg, economics = cfg or _RC, economics or _ECON
    floor = economics.money(cfg.cohort_spend_floor_ref)
    by: dict[str, list[UnitStats]] = {}
    for u in units:
        if not u.is_placement and u.spend >= floor and u.roas is not None:
            by.setdefault(u.role, []).append(u)
    out: dict[str, Cohort] = {}
    for role, us in by.items():
        rs = sorted(u.roas for u in us)  # type: ignore[type-var]
        cp = sorted(u.cost_per_new_user for u in us if u.cost_per_new_user is not None)
        out[role] = Cohort(n=len(us), roas_p10=_percentile(rs, 0.10), roas_p25=_percentile(rs, 0.25),
                           roas_p50=_percentile(rs, 0.50), roas_p75=_percentile(rs, 0.75),
                           cpnu_p25=_percentile(cp, 0.25) if cp else None,
                           cpnu_p50=_percentile(cp, 0.50) if cp else None)
    return out


def unit_stats(perf_rows: Iterable[PerfRow], targeting_units: Iterable[TargetingUnit] = (), *,
               entity_id: str | None = None, window: Window | None = None) -> list[UnitStats]:
    """Aggregate performance rows into one :class:`UnitStats` per targeting unit.

    Sums spend, sales, impressions, units and (when reported) indirect sales
    and new users.  Role, placement flag, search volume and relevance come
    from ``targeting_units`` (exact entity match first); unknown units default
    to role ``generic``.  Rows without a targeting unit are skipped.  Returns
    the list sorted by spend descending.
    """
    exact = {(t.entity_id, t.value): t for t in targeting_units}
    loose: dict[str, TargetingUnit] = {}
    for t in targeting_units:
        loose.setdefault(t.value, t)
    out: dict[str, UnitStats] = {}
    for r in perf_rows:
        if not r.targeting_unit or (entity_id is not None and r.entity_id != entity_id):
            continue
        if window is not None and not (window.start <= r.date <= window.end):
            continue
        u = out.get(r.targeting_unit)
        if u is None:
            t = exact.get((r.entity_id, r.targeting_unit)) or loose.get(r.targeting_unit)
            u = out[r.targeting_unit] = UnitStats(
                value=r.targeting_unit, role=t.role if t else "generic",
                is_placement=bool(t and (t.is_placement or t.role == "placement")),
                search_volume=t.search_volume if t else None, relevance=t.relevance if t else None)
        u.spend += r.spend
        u.sales += r.sales
        u.impressions += r.impressions
        u.units += r.units or 0.0
        if r.indirect_sales is not None:
            u.indirect_sales = (u.indirect_sales or 0.0) + r.indirect_sales
        if r.new_users is not None:
            u.new_users = (u.new_users or 0.0) + r.new_users
    return sorted(out.values(), key=lambda u: -u.spend)


def head_terms(units: Sequence[UnitStats], cfg: RecommendConfig | None = None) -> set[str]:
    """Category head terms among the generic units (never stopped, only bid-capped).

    A generic unit is a head term when it is in the top ``head_term_top_n`` by
    search volume with at least ``min_search_volume`` searches per day, or
    carries at least ``head_term_volume_share`` of generic search volume.  With
    no volume data at all, the single largest generic unit by spend.
    """
    cfg = cfg or _RC
    gen = [u for u in units if not u.is_placement and u.role == "generic"]
    vol = [u for u in gen if u.search_volume is not None]
    if not vol:
        return {max(gen, key=lambda u: u.spend).value} if gen else set()
    total = sum(u.search_volume or 0.0 for u in vol)
    top = sorted(vol, key=lambda u: -(u.search_volume or 0.0))[: cfg.head_term_top_n]
    out = {u.value for u in top if (u.search_volume or 0.0) >= cfg.min_search_volume}
    out |= {u.value for u in vol if total > 0 and (u.search_volume or 0.0) / total >= cfg.head_term_volume_share}
    return out


def _boost_ok(u: UnitStats, econ: Economics, cfg: RecommendConfig) -> bool:
    r = u.roas
    return (r is not None and r >= cfg.boost_roas_be * econ.be
            and (u.search_volume is None or u.search_volume >= cfg.min_search_volume)
            and (u.relevance is None or u.relevance >= cfg.min_relevance)
            and u.conversion_confidence != "low"
            and not _zero_organic_conquest(u))


def pick_targets(lever: str, units: Sequence[UnitStats], spared: Iterable[str] = (),
                 economics: Economics | None = None, cfg: RecommendConfig | None = None
                 ) -> tuple[str, list[str], str]:
    """Choose the verb and the named targets for a lever-driven action.

    * ``own_availability`` -> ("Fix availability on", [], "product")
    * ``own_discount`` / ``price_gap`` -> ("Re-price", [], "product")
    * ``competitor_share`` -> conquest units, verb "Negative / pause"
    * boost levers (``position``, ``own_paid_sov``, ``own_organic_sov``) -> units with ROAS >= the boost
      bar, search volume unknown or >= ``min_search_volume``, relevance unknown or >= ``min_relevance``,
      conversion confidence not low, and not a conquest unit with zero own organic impressions
    * everything else (``bid`` and default) -> "Cap bids on": units with ROAS below the cap bar and spend
      above ``max(money(cap_floor_ref), cap_floor_share x unit spend)``; fallback = the lowest-ROAS units

    Placements and spared units are never candidates; the top ``max_targets``
    by spend are returned.  Returns ``(verb, targets, kind)``.
    """
    cfg, econ = cfg or _RC, economics or _ECON
    if lever == "own_availability":
        return "Fix availability on", [], "product"
    if lever in ("own_discount", "price_gap"):
        return "Re-price", [], "product"
    skip = set(spared)
    cand = sorted((u for u in units if not u.is_placement and u.value not in skip), key=lambda u: -u.spend)
    if lever == "competitor_share":
        return "Negative / pause", [u.value for u in cand if _is_conquest(u)][: cfg.max_targets], "unit"
    if lever in BOOST_LEVERS:
        return BOOST_LEVERS[lever], [u.value for u in cand if _boost_ok(u, econ, cfg)][: cfg.max_targets], "unit"
    tot = sum(u.spend for u in units)
    floor = max(econ.money(cfg.cap_floor_ref), cfg.cap_floor_share * tot)
    bar = cfg.cap_roas_be * econ.be
    pool = [u.value for u in cand if u.roas is not None and u.roas < bar and u.spend > floor][: cfg.max_targets]
    if not pool:
        pool = [u.value for u in sorted((u for u in cand if u.roas is not None), key=lambda u: u.roas)  # type: ignore[arg-type,return-value]
                ][: cfg.cap_fallback_n]
    return "Cap bids on", pool, "unit"


def bleeder_loop(units: Sequence[UnitStats], ctx: RecommendContext | None = None, *, bleed_floor: float,
                 head: Iterable[str] = (), economics: Economics | None = None,
                 cfg: RecommendConfig | None = None) -> dict:
    """Decide the fate of each sub-breakeven targeting unit: stop, staged bid cap, or spare.

    Candidates: non-placement units with ROAS below breakeven and spend above
    ``bleed_floor``, largest spend first.  First matching rule wins:

    1. conquest unit with zero own organic impressions -> stop (relevance fail; overrides acquisition)
    2. conquest unit with 0 new users, 0 ROAS, indirect share <= ``halo_share`` -> stop
    3. conquest unit with cost per new user <= cohort p25 -> spare (acquisition-justified)
    4. conquest unit with cost per new user <= ``acquisition_watch_mult`` x cohort p25 -> spare, watch
    5. generic head term -> never stop; staged bid cap
    6. generic with cost per new user <= generic cohort p25 -> spare
    7. own-brand unit while organic share of own impressions < ``organic_cold`` -> spare (cap, do not pause)
    8. ROAS >= its role's cohort p25 -> spare (within the norm for its type)
    9. indirect share > ``halo_share`` and (no p10 or ROAS >= role p10) -> spare (cap)
    10. indirect share > ``halo_share`` and ROAS < role p10 -> staged bid cap, 30%
    11. conquest unit with own organic presence -> staged bid cap 20-30%; stop only if still failing later
    12. otherwise -> stop

    Untrusted cohorts (fewer than ``cohort_min_n`` units) make their rules not match.
    At most ``max_stops`` stops and ``max_staged_caps`` caps.

    Returns ``{"stops": [unit], "caps": [{"unit", "cap", "reason"}], "spared": [{"unit", "reason"}]}``.
    """
    cfg, econ, ctx = cfg or _RC, economics or _ECON, ctx or RecommendContext()
    head = set(head)
    be = cfg.bleed_roas_be * econ.be
    stops: list[str] = []
    caps: list[dict] = []
    spared: list[dict] = []
    cand = sorted((u for u in units if not u.is_placement and u.roas is not None and u.roas < be
                   and u.spend > bleed_floor), key=lambda u: -u.spend)
    for u in cand:
        roas = u.roas or 0.0
        cpnu = u.cost_per_new_user
        ish = u.indirect_share
        halo = ish is not None and ish > cfg.halo_share
        p25 = _cohort_value(ctx, u.role, "roas_p25", cfg)
        p10 = _cohort_value(ctx, u.role, "roas_p10", cfg)
        cp25 = _cohort_value(ctx, u.role, "cpnu_p25", cfg)
        verdict: tuple[str, str, str | None]
        if _zero_organic_conquest(u):
            verdict = ("stop", "no own organic presence on a conquest term (relevance fail)", None)
        elif (_is_conquest(u) and u.new_users is not None and u.new_users <= 0 and roas <= 0
              and (ish is None or ish <= cfg.halo_share)):
            verdict = ("stop", "conquest term with no new users, no sales and no halo", None)
        elif _is_conquest(u) and cpnu is not None and cp25 is not None and cpnu <= cp25:
            verdict = ("spare", "acquisition-justified: cost per new user within the best quartile", None)
        elif _is_conquest(u) and cpnu is not None and cp25 is not None and cpnu <= cfg.acquisition_watch_mult * cp25:
            verdict = ("spare", "acquisition cost near the cohort norm: watch", None)
        elif u.role == "generic" and u.value in head:
            verdict = ("cap", "category head term: never stopped, stage a bid cap", "20%")
        elif u.role == "generic" and cpnu is not None and cp25 is not None and cpnu <= cp25:
            verdict = ("spare", "acquisition-justified generic term", None)
        elif _is_own(u) and ctx.organic_share is not None and ctx.organic_share < cfg.organic_cold:
            verdict = ("spare", "own-brand term while organic presence is still cold: cap about 20%, do not pause", None)
        elif p25 is not None and roas >= p25:
            verdict = ("spare", "within the norm for its role", None)
        elif halo and (p10 is None or roas >= p10):
            verdict = ("spare", "halo sales above the indirect-share bar: cap about 20%", None)
        elif halo:
            verdict = ("cap", "halo sales but ROAS in the bottom decile of its role", "30%")
        elif _is_conquest(u):
            verdict = ("cap", "relevant conquest term failing ROAS and acquisition: cap, stop only if still failing in two weeks",
                       "20-30%")
        else:
            verdict = ("stop", "below breakeven with no mitigating signal", None)
        kind, reason, cap = verdict
        if kind == "stop" and len(stops) < cfg.max_stops:
            stops.append(u.value)
        elif kind == "cap" and len(caps) < cfg.max_staged_caps:
            caps.append({"unit": u.value, "cap": cap, "reason": reason})
        elif kind == "spare":
            spared.append({"unit": u.value, "reason": reason})
    return {"stops": stops, "caps": caps, "spared": spared}


def scale_winners(units: Sequence[UnitStats], ctx: RecommendContext | None = None, *,
                  exclude: Iterable[str] = (), economics: Economics | None = None,
                  cfg: RecommendConfig | None = None) -> list[str]:
    """Targeting units that have earned more spend.

    A unit qualifies when all hold: not a placement and not in ``exclude``
    (already targeted, spared or bleeding); not an own-brand unit while the
    organic share of own impressions is self-sustaining (waived for products
    younger than ``new_product_days``); search volume unknown or >=
    ``min_search_volume``; relevance unknown or >= ``min_relevance``;
    conversion confidence not low; organic rank unknown or worse than
    ``max_winner_organic_rank``; not a conquest unit with zero own organic
    impressions.  Then either

    (a) ``base >= winner bar`` and ``base >= role cohort p75``, where ``base``
        is the incremental ROAS for own-brand units (which must also not be
        below the own-brand p25) and plain ROAS otherwise; or
    (b) total ROAS >= ``halo_total_to_direct`` x direct ROAS and total ROAS >= the halo bar.

    An untrusted cohort makes (a) fail: a winner must be proven against its
    peers.  Returns the top ``max_targets`` unit names by spend.
    """
    cfg, econ, ctx = cfg or _RC, economics or _ECON, ctx or RecommendContext()
    skip = set(exclude)
    be = econ.be
    newprod = ctx.product_age_days is not None and ctx.product_age_days < cfg.new_product_days
    saturated = ctx.organic_share is not None and ctx.organic_share >= cfg.organic_self_sustaining
    out: list[UnitStats] = []
    for u in units:
        r = u.roas
        if u.is_placement or u.value in skip or r is None or r < cfg.bleed_roas_be * be:
            continue
        if _is_own(u) and saturated and not newprod:
            continue
        if u.search_volume is not None and u.search_volume < cfg.min_search_volume:
            continue
        if u.relevance is not None and u.relevance < cfg.min_relevance:
            continue
        if u.conversion_confidence == "low" or _zero_organic_conquest(u):
            continue
        if u.organic_rank is not None and u.organic_rank <= cfg.max_winner_organic_rank:
            continue
        base = u.incremental_roas if (_is_own(u) and not newprod) else r
        p75 = _cohort_value(ctx, u.role, "roas_p75", cfg)
        ok_a = base is not None and p75 is not None and base >= cfg.winner_roas_be * be and base >= p75
        if ok_a and _is_own(u):
            p25 = _cohort_value(ctx, u.role, "roas_p25", cfg)
            ok_a = p25 is None or base >= p25  # type: ignore[operator]
        tot = u.total_roas
        ok_b = tot is not None and r > 0 and tot >= cfg.halo_total_to_direct * r and tot >= cfg.halo_roas_be * be
        if ok_a or ok_b:
            out.append(u)
    return [u.value for u in sorted(out, key=lambda u: -u.spend)[: cfg.max_targets]]


def exo_note(drivers: Sequence[Driver | Mapping]) -> str | None:
    """Context note when the largest hurting lever is an outside condition (tier 2).

    The lever is picked by ``|contribution|`` among live hurting levers (not
    the first in display order, which is not always the largest).  If its
    demand-evidence story is ``no-link`` or ``unstable`` the note says to
    treat it as noise; otherwise it says "exogenous: defend selectively, do
    not overhaul" and appends the evidence story when present.

    Returns the note, or ``None`` when the largest hurting lever is tier 1.
    """
    neg = [d for d in drivers if not _g(d, "na", False) and (_g(d, "contrib", 0.0) or 0.0) < 0]
    if not neg:
        return None
    top = max(neg, key=lambda d: abs(_g(d, "contrib", 0.0)))
    lever = _g(top, "lever")
    if lever not in TIER2:
        return None
    head = RULES.get(lever, (lever, "", NOTE))[0]
    ev = _g(top, "evidence") or {}
    story = ev.get("story")
    if story in ("no-link", "unstable"):
        return (f"{head} is the biggest mover, but its daily link to conversion is "
                f"{'absent' if story == 'no-link' else 'sign-flipping'} "
                f"(r={ev.get('r')}, halves {ev.get('r1')}/{ev.get('r2')}); treat as noise, "
                f"do not anchor a demand narrative on it")
    note = f"{head} is the biggest mover but exogenous; defend selectively, do not overhaul"
    return f"{note} ({story})" if story else note


# --------------------------------------------------------------------------- the gate stack

def recommend(
    drivers: Sequence[Driver | Mapping],
    units: Sequence[UnitStats],
    *,
    budget: BudgetState | None = None,
    cur_roas: float | None = None,
    verdict: str | None = None,
    verdict_note: str = "",
    account_moves: Mapping[str, Sequence[Any]] | None = None,
    ctx: RecommendContext | None = None,
    economics: Economics | None = None,
    availability: AvailabilityPolicy | None = None,
    cfg: RecommendConfig | None = None,
) -> Recommendation:
    """Select at most ``max_actions`` actions for one entity.

    Arguments: ``drivers`` from :func:`cpm_lineage.attribution.attribute`
    (objects or dicts); ``units`` the entity's targeting units over the
    comparison window; ``cur_roas`` the entity's current ROAS (``None`` =
    unknown: never raise blind); ``verdict`` the attribution verdict;
    ``account_moves`` = ``{"tighten": [...], "scale": [...]}`` account-level
    budget moves (names or mappings with ``name``).

    **Phase A (mandatory, in this order).**

    1. availability fell >= ``availability_move`` window over window -> "Fix availability first";
    2. availability below the block line -> "Fix availability first"; every scale action is held;
    3. auto-targeted entity burning (ROAS < breakeven, or spend up > 5% while ROAS fell >= 20%) -> cap;
    4. budget-capped entity, by status and ROAS: removed -> conflict note; ROAS unknown -> investigate
       tracking; ROAS >= raise bar -> first of {dayparted: conflict; availability block: conflict; bleed
       share >= ``bleed_hold_pct``: hold; trusted marginal ROAS < ``marginal_roas_ceiling`` (1.0): hold; organic
       share self-sustaining: hold; else raise ("Top up" when platform-held, "Investigate stop &
       reactivate" when operator-stopped)}; breakeven <= ROAS < raise bar -> hold & monitor;
       ROAS < breakeven and not stopped -> tighten;
    5. the utilisation-ladder candidate is built (scored in phase B);
    6. :func:`bleeder_loop` -> "Pause / negative" on up to three units;
    7. staged bid cap on up to three units that earned a cap rather than a stop.

    **Phase B (scored pool, highest first, fills the remaining slots).**
    tighten a starved loser 80; severe under-utilisation 78; fund a starved
    winner 72; scale winners 70; pre-fund a demand spike 64 capped / 48 not;
    other ladder 56; hurting tier-1 lever ``50 + 0.4 x min(|pct|, 40)``;
    resume held scaling 42; helping tier-1 lever 18.  Lever candidates need a
    verdict outside ``unexplained`` / ``thin-data``; boost levers are skipped
    with a conflict note under an availability block.  If slots remain and no
    lever story is allowed, "Hold lever changes" is appended.

    **Phase C (post-filters).** ramp tag in short mode; execution-health,
    stale-data and wind-down warnings; new-user annotation on raises; status
    guard (removed -> "Launch replacement" only; platform-held -> strip
    exposure-adding verbs and insert "Top up" or "Hold"); truncate.

    A raise is never recommended against a hold: once phase A holds the
    raise (day-parting, availability block, bleed, marginal, organic, or the
    hold-and-monitor band) the pre-fund and resume candidates are skipped
    and a platform-held entity gets "Hold" instead of "Top up".

    ``verdict=None`` (no attribution was run) is treated like
    ``unexplained`` / ``thin-data``: no lever story is told without a
    verdict that supports one.

    Invariants: never more than ``max_actions`` actions; no target is named
    twice; no action carries a tier-2 lever.

    Returns a :class:`Recommendation`.
    """
    cfg, econ, pol = cfg or _RC, economics or _ECON, availability or _AVAIL
    ctx, budget = ctx or RecommendContext(), budget or BudgetState()
    be = econ.be
    rec = Recommendation()
    status = _status(ctx)
    removed = status in (CanonicalStatus.REMOVED, CanonicalStatus.ENDED)
    held = status == CanonicalStatus.BUDGET_HELD
    stopped = status == CanonicalStatus.OPERATOR_PAUSED
    lever_ok = verdict not in NO_STORY_VERDICTS and verdict is not None
    live = [d for d in drivers if not _g(d, "na", False)]
    organic_saturated = ctx.organic_share is not None and ctx.organic_share >= cfg.organic_self_sustaining

    tot = sum(u.spend for u in units)
    bleed_floor = max(econ.money(cfg.bleed_floor_ref), cfg.bleed_floor_share * tot)
    pause_floor = max(econ.money(cfg.pause_floor_ref), cfg.pause_floor_share * tot)
    head = head_terms(units, cfg)

    a_now, a_pri = pol.normalise(ctx.availability), pol.normalise(ctx.availability_prior)
    sparse = ctx.availability_days is not None and 0 < ctx.availability_days <= cfg.availability_sparse_days
    blocked = a_now is not None and a_now < pol.block and not sparse
    dropped = (not blocked and a_now is not None and a_pri is not None
               and a_pri - a_now >= cfg.availability_move - 1e-12)
    recovered = (a_now is not None and a_pri is not None and not sparse
                 and a_now - a_pri >= cfg.availability_move - 1e-12 and a_now >= pol.floor)
    scale_held = blocked or dropped

    used: set[str] = set()
    actions: list[Action] = []

    def push(a: Action) -> bool:
        """De-duplicate targets across actions; drop a unit action left with no targets."""
        if a.kind == "unit":
            a.targets = [t for t in a.targets if t not in used]
            if not a.targets:
                return False
        used.update(a.targets)
        actions.append(a)
        return True

    # ---- phase A ---------------------------------------------------------
    if dropped:
        push(Action(AVAILABILITY_GATE, "Availability dropped", "Fix availability first", "fix", kind="product",
                    lever="own_availability", mandatory=True, score=100.0,
                    advice=f"Availability fell from {a_pri:.0%} to {a_now:.0%}; scaling is held until it recovers"))
    if blocked:
        push(Action(AVAILABILITY_GATE, "Availability below the block line", "Fix availability first", "fix",
                    kind="product", lever="own_availability", mandatory=True, score=100.0,
                    advice=f"Availability {a_now:.0%} is under {pol.block:.0%}; all scale actions are held"))
        rec.conflicts.append("availability block: scale actions held")

    if ctx.auto_targeted and cur_roas is not None:
        spend_up = (ctx.spend is not None and ctx.spend_prior and ctx.spend / ctx.spend_prior - 1.0 > cfg.burn_spend_up)
        roas_down = (ctx.roas_prior and cur_roas / ctx.roas_prior - 1.0 <= -cfg.burn_roas_down + 1e-12)
        if cur_roas < cfg.bleed_roas_be * be or (spend_up and roas_down):
            a = Action(BUDGET_TIGHTEN, "Auto-targeted spend is burning", "Cap / reduce", "tighten", kind="budget",
                       mandatory=True, score=95.0,
                       advice="No targeting control on this ad type: the only lever is the budget")
            if ctx.total_roas is not None and ctx.direct_roas and ctx.total_roas < cfg.thin_halo_ratio * ctx.direct_roas:
                a.notes.append("thin halo: total ROAS is close to direct ROAS, so indirect sales do not justify the spend")
            push(a)

    budget_action = False
    raise_held = False          # phase A decided against a raise on this entity: nothing later may add one
    if budget.capped:
        raise_bar = cfg.budget_raise_roas_be * be
        marg = budget.marginal_roas
        marg_trusted = marg is not None and (budget.curve_r2 is None or budget.curve_r2 >= cfg.curve_min_r2)
        if removed:
            rec.conflicts.append("budget-capped but the entity is removed: no budget action")
        elif cur_roas is None:
            budget_action = push(Action(INVESTIGATE, "Budget-capped with unknown ROAS", "Investigate tracking",
                                        "investigate", kind="budget", mandatory=True, score=90.0,
                                        advice="Never raise a budget blind: confirm sales attribution first"))
        elif cur_roas >= raise_bar:
            hold_reason = None
            if budget.dayparted:
                rec.conflicts.append("budget-capped but dayparted: the cap is a schedule choice, no raise proposed")
            elif blocked:
                rec.conflicts.append("budget raise held: availability is below the block line")
            elif ctx.bleed_pct is not None and ctx.bleed_pct >= cfg.bleed_hold_pct:
                hold_reason = "fix structural bleed first: a raise would fund sub-breakeven targeting units"
            elif marg_trusted and marg < cfg.marginal_roas_block:  # type: ignore[operator]
                hold_reason = "marginal ROAS at the cap is below the block line: extra budget would not pay back"
            elif marg_trusted and marg < cfg.marginal_roas_ceiling:  # type: ignore[operator]
                hold_reason = "marginal ROAS at the cap is under 1: the next unit of spend returns less than it costs"
            elif organic_saturated:
                hold_reason = "organic presence is self-sustaining: extra paid spend is mostly non-incremental"
            else:
                if held:
                    verb, vclass = "Top up", "topup"
                elif stopped:
                    verb, vclass = "Investigate stop & reactivate", "reactivate"
                else:
                    verb, vclass = "Raise daily budget", "raise"
                a = Action(BUDGET_RAISE, "Budget-capped winner", verb, vclass, kind="budget", mandatory=True,
                           score=90.0, advice="The entity runs out of budget while returning above the scale bar")
                if budget.rec_range:
                    a.extras["rec_range"] = list(budget.rec_range)
                if budget.revenue_at_stake is not None:
                    a.extras["revenue_at_stake"] = budget.revenue_at_stake
                    a.extras["revenue_estimator"] = "budget-probe"
                if marg is not None:
                    a.extras["marginal_roas"] = marg
                budget_action = push(a)
            raise_held = bool(hold_reason) or budget.dayparted or blocked
            if hold_reason:
                budget_action = push(Action(HOLD, "Budget-capped: raise held", "Hold the budget raise", "hold",
                                            kind="budget", mandatory=True, score=90.0, advice=hold_reason))
        elif cur_roas >= cfg.bleed_roas_be * be:
            raise_held = True
            budget_action = push(Action(HOLD, "Budget-capped, middling return", "Hold & monitor", "hold",
                                        kind="budget", mandatory=True, score=90.0,
                                        advice="ROAS is above breakeven but below the scale bar"))
        elif not stopped:
            budget_action = push(Action(BUDGET_TIGHTEN, "Budget-capped below breakeven", "Tighten daily budget",
                                        "tighten", kind="budget", mandatory=True, score=90.0,
                                        advice="The entity spends its whole cap below breakeven"))

    ladder: Action | None = None
    if not budget.capped and budget.ladder:
        if budget.ladder == "bid-constrained":
            ladder = (Action(BID_LIFT, "Under-spending: bids clear well below what is offered", "Stepwise bid test",
                             "bid_lift", kind="budget", score=cfg.score_ladder)
                      if budget.stepwise_bid_test else
                      Action(BID_LIFT, "Under-spending: bid-constrained", "Raise bids, not budget", "bid_lift",
                             kind="budget", score=cfg.score_ladder))
        elif budget.ladder == "volume-ceiling":
            ladder = Action(ADD_UNITS, "Under-spending: volume ceiling", "Add keywords", "add_units", kind="budget",
                            score=cfg.score_ladder)
        elif budget.ladder == "severe":
            pos = budget.position
            if pos is None:
                ladder = Action(INVESTIGATE, "Severe under-utilisation", "Audit before spending", "investigate",
                                kind="budget", score=cfg.score_ladder_severe)
            else:
                worse = pos > cfg.severe_position_split if ctx.position_lower_is_better else pos < cfg.severe_position_split
                ladder = (Action(BID_LIFT, "Severe under-utilisation at a deep position", "Raise bids", "bid_lift",
                                 kind="budget", score=cfg.score_ladder_severe) if worse else
                          Action(ADD_UNITS, "Severe under-utilisation at a top position", "Add keywords", "add_units",
                                 kind="budget", score=cfg.score_ladder_severe))

    bl = bleeder_loop(units, ctx, bleed_floor=bleed_floor, head=head, economics=econ, cfg=cfg)
    spared_names = {s["unit"] for s in bl["spared"]}
    rec.spared = bl["spared"][: cfg.max_spared]
    rec.staged_caps = bl["caps"]
    if bl["stops"]:
        push(Action(PAUSE_UNIT, "Targeting units below breakeven", "Pause / negative", "pause", kind="unit",
                    targets=list(bl["stops"]), mandatory=True, score=88.0,
                    advice="Material spend returning less than it costs, with no mitigating signal"))
    if bl["caps"]:
        push(Action(BID_CAP, "Targeting units that earned a cap, not a stop", "Stage a bid cap on", "cap",
                    kind="unit", targets=[c["unit"] for c in bl["caps"]], mandatory=True, score=86.0,
                    advice="; ".join(f"{c['unit']}: cap {c['cap']}" for c in bl["caps"]),
                    extras={"caps": bl["caps"]}))

    # ---- phase B ---------------------------------------------------------
    pool: list[Action] = []
    moves = account_moves or {}

    def _name(m: Any) -> str:
        return str(_g(m, "name") or _g(m, "entity_id") or m) if not isinstance(m, str) else m

    tighten = [_name(m) for m in list(moves.get("tighten", ()))[: cfg.account_moves_n]]
    if tighten:
        pool.append(Action(BUDGET_TIGHTEN, "Account view: starved loser", "Tighten budget on", "tighten",
                           kind="budget", targets=tighten, score=cfg.score_tighten_account))
    scale_moves = [_name(m) for m in list(moves.get("scale", ()))[: cfg.account_moves_n]]
    if scale_moves and not scale_held:
        pool.append(Action(BUDGET_RAISE, "Account view: starved winner", "Raise budget on", "raise",
                           kind="budget", targets=scale_moves, score=cfg.score_raise_account))
    if ladder is not None:
        pool.append(ladder)
    bleeders = {u.value for u in units if u.roas is not None and u.roas < cfg.bleed_roas_be * be}
    if not scale_held:
        winners = scale_winners(units, ctx, exclude=used | spared_names | bleeders, economics=econ, cfg=cfg)
        if winners:
            pool.append(Action(BID_LIFT, "Proven winners with headroom", "Scale", "scale", kind="unit",
                               targets=winners, score=cfg.score_scale_winners,
                               advice="Units beating their role's top quartile, not yet won organically"))
    if (ctx.demand_trend is not None and ctx.demand_trend >= cfg.demand_trend_min and not scale_held
            and (cur_roas is None or cur_roas >= cfg.prefund_min_roas_be * be)
            and not budget.dayparted and not organic_saturated and not raise_held):
        pool.append(Action(BUDGET_RAISE, "Demand is rising", "Pre-fund ahead of the demand spike", "prefund",
                           kind="budget", score=cfg.score_prefund_capped if budget.capped else cfg.score_prefund,
                           extras={"demand_trend": ctx.demand_trend}))
    if lever_ok:
        for d in live:
            lever = _g(d, "lever")
            contrib = _g(d, "contrib", 0.0) or 0.0
            if lever in TIER2 or lever not in RULES or contrib == 0:
                continue
            headline, advice, atype = RULES[lever]
            pct = _g(d, "pct")
            if contrib < 0:
                if lever in BOOST_LEVERS and blocked:
                    rec.conflicts.append(f"{headline}: boosting visibility is held under the availability block")
                    continue
                verb, targets, kind = pick_targets(lever, units, spared_names, econ, cfg)
                vclass = {PAUSE_UNIT: "pause", BID_LIFT: "bid_lift", RANK_TARGET: "bid_lift", BID_CAP: "cap",
                          AVAILABILITY_GATE: "fix", REPRICE: "fix", LISTING_FIX: "fix"}.get(atype, "note")
                pool.append(Action(atype, headline, verb, vclass, kind=kind, lever=lever, targets=targets,
                                   advice=advice, pct=pct, sizing=_g(d, "sizing"), jury=_g(d, "jury"),
                                   score=cfg.score_lever_base + cfg.score_lever_slope
                                   * min(abs(pct or 0.0), cfg.score_lever_pct_cap)))
            else:
                pool.append(Action(NOTE, headline, "Protect / maintain", "note", kind="note", lever=lever,
                                   advice="This lever is helping: keep it where it is", pct=pct,
                                   jury=_g(d, "jury"), score=cfg.score_protect))
    if recovered and not raise_held:
        pool.append(Action(BUDGET_RAISE, "Availability recovered", "Resume held scaling", "resume", kind="budget",
                           score=cfg.score_resume, advice="Availability is back in the healthy band"))

    for a in sorted(pool, key=lambda x: -x.score):
        if len(actions) >= cfg.max_actions:
            break
        if a.kind == "product" and any(x.lever == a.lever and x.kind == "product" for x in actions):
            continue  # the mandatory availability action already covers this lever
        push(a)
    if len(actions) < cfg.max_actions and not lever_ok:
        actions.append(Action(HOLD, "Attribution not trustworthy enough", "Hold lever changes", "hold",
                              advice=verdict_note or f"verdict: {verdict or 'none'}", score=0.0))

    # ---- phase C ---------------------------------------------------------
    exec_bad = (ctx.exec_fail_rate is not None and ctx.exec_ops >= cfg.exec_min_ops
                and ctx.exec_fail_rate > cfg.exec_fail_rate)
    stale = ctx.stale_days is not None and ctx.stale_days > cfg.stale_days
    wind_down = (ctx.spend is not None and ctx.spend_prior is not None and ctx.spend_prior > 0
                 and ctx.spend <= cfg.wind_down_share * ctx.spend_prior)
    for a in actions:
        if ctx.short and a.verb_class in ("raise", "topup", "scale"):
            a.ramp = True
            a.notes.append("ramp: reversible half-step, re-check in 7 days")
        if exec_bad and a.verb_class in WRITE_CLASSES:
            a.warnings.append("fix execution first: most recent automated operations failed")
        if a.verb_class in RAISE_CLASSES:
            if stale:
                a.warnings.append(f"data is {ctx.stale_days} days stale: confirm before adding spend")
            if wind_down:
                a.warnings.append("spend was cut by half or more against the prior window: confirm this is not a wind-down")
            nur = ctx.new_user_rate
            if nur is not None and a.verb_class in ("raise", "topup", "scale", "prefund"):
                soft = (cur_roas is not None and ctx.roas_prior
                        and 0 < 1.0 - cur_roas / ctx.roas_prior < cfg.soft_roas_band)
                if nur >= cfg.acquisition_rate and soft:
                    a.notes.append("ROAS softened slightly with a high new-user rate: healthy acquisition, not dilution")
                elif nur < cfg.recapture_rate:
                    a.notes.append("low new-user rate: this is mostly existing-user recapture")

    if removed:
        actions = [Action(RELAUNCH, "Entity is removed", "Launch replacement", "note", kind="note",
                          advice="A removed entity cannot be changed; carry the learnings into a new one")]
    elif held:
        actions = [a for a in actions if a.verb_class not in HELD_STRIP_CLASSES]
        has_budget = any(a.kind == "budget" and a.verb_class in ("topup", "hold", "investigate") for a in actions)
        if (cur_roas is None or cur_roas >= cfg.bleed_roas_be * be) and not has_budget:
            if organic_saturated:
                actions.insert(0, Action(HOLD, "Held by the platform for budget", "Hold", "hold", kind="budget",
                                         advice="Organic presence is self-sustaining: no top-up needed"))
            elif raise_held or scale_held or budget.dayparted:
                why = ("availability must recover first" if scale_held else
                       "the entity runs on a schedule, so a bigger cap would not serve more hours"
                       if budget.dayparted else "the raise gates above hold it")
                actions.insert(0, Action(HOLD, "Held by the platform for budget", "Hold", "hold", kind="budget",
                                         advice=f"No top-up: {why}"))
            else:
                actions.insert(0, Action(BUDGET_RAISE, "Held by the platform for budget", "Top up", "topup",
                                         kind="budget", advice="The entity stops serving when the budget runs out"))
    actions = actions[: cfg.max_actions]

    rec.actions = actions
    hurting = [d for d in live if (_g(d, "contrib", 0.0) or 0.0) < 0]
    if lever_ok and hurting:
        rec.exo_note = exo_note(live)
        t1 = [d for d in hurting if _g(d, "lever") not in TIER2]
        if t1:
            rec.driver = _g(max(t1, key=lambda d: abs(_g(d, "contrib", 0.0))), "lever")
    if actions:
        rec.headline, rec.action_type = actions[0].headline, actions[0].action_type
    pause_bar = cfg.pause_roas_be * be
    rec.pause_units = [u.value for u in sorted(units, key=lambda u: -u.spend)
                       if not u.is_placement and u.value not in spared_names and u.roas is not None
                       and u.roas < pause_bar and u.spend > pause_floor][: cfg.max_pause_list]
    return rec


# --------------------------------------------------------------------------- annotators

def history_crosscheck(direction: str, historical_roas: float | None, economics: Economics | None = None,
                       cfg: RecommendConfig | None = None) -> str | None:
    """Does long-run history agree with an action's direction?

    ``direction`` is ``"cut"`` or ``"grow"``.  Agrees when cutting with
    historical ROAS below ``history_cut_roas_be x be`` or growing at or above
    ``history_grow_roas_be x be``; disagrees when cutting at or above the
    grow bar or growing below the cut bar (the caller should damp such an
    action to "watch").  Returns ``"agree"``, ``"disagree"`` or ``None``.
    """
    cfg, econ = cfg or _RC, economics or _ECON
    if historical_roas is None or direction not in ("cut", "grow"):
        return None
    lo, hi = cfg.history_cut_roas_be * econ.be, cfg.history_grow_roas_be * econ.be
    if direction == "cut":
        return "agree" if historical_roas < lo else "disagree" if historical_roas >= hi else None
    return "agree" if historical_roas >= hi else "disagree" if historical_roas < lo else None


def self_competition(units: Sequence[UnitStats], sibling_units: Iterable[str],
                     cfg: RecommendConfig | None = None) -> dict | None:
    """Warn when the entity bids against the advertiser's own other entities.

    ``share`` = spend on non-placement units that sibling entities also target
    / spend on all non-placement units.  Returns ``{"share", "units"}`` when
    ``share >= self_competition_share``, else ``None``.
    """
    cfg = cfg or _RC
    sib = set(sibling_units)
    own = [u for u in units if not u.is_placement]
    tot = sum(u.spend for u in own)
    if tot <= 0:
        return None
    hit = [u for u in own if u.value in sib]
    share = sum(u.spend for u in hit) / tot
    if share < cfg.self_competition_share:
        return None
    return {"share": share, "units": [u.value for u in sorted(hit, key=lambda u: -u.spend)]}


# --------------------------------------------------------------------------- bleed, priority

def structural_bleed(lifetime_units: Iterable[UnitStats], recent_units: Iterable[UnitStats] | None = None,
                     economics: Economics | None = None, cfg: RecommendConfig | None = None) -> dict:
    """Spend that is structurally wasted regardless of any move.

    Over **all** targeting units of the entity across its whole history:
    ``bleed_spend = sum(spend of units with ROAS < breakeven)`` and
    ``pct = round(100 x bleed_spend / total_spend)``.  ``worst`` = the first
    ``bleed_worst_n`` non-placement units below breakeven among the top
    ``bleed_worst_pool`` by spend of ``recent_units`` (default: the lifetime
    list).  Note the two horizons: totals are lifetime, ``worst`` is recent.
    The breakeven is ``Economics.be``.

    Attribution explains the change; bleed reports the level.  ``pct`` feeds
    the budget gate (>= ``bleed_hold_pct`` holds a raise) and :func:`priority`.

    Returns ``{"spend", "pct", "n", "total_spend", "total_sales", "worst"}``.
    """
    cfg, econ = cfg or _RC, economics or _ECON
    be = cfg.bleed_roas_be * econ.be
    life = [u for u in lifetime_units if u.spend > 0]
    total = sum(u.spend for u in life)
    bad = [u for u in life if (u.roas or 0.0) < be]
    spend = sum(u.spend for u in bad)
    recent = sorted((u for u in (life if recent_units is None else recent_units) if u.spend > 0),
                    key=lambda u: -u.spend)[: cfg.bleed_worst_pool]
    worst = [u.value for u in recent if not u.is_placement and (u.roas or 0.0) < be][: cfg.bleed_worst_n]
    return {"spend": spend, "pct": int(round(100.0 * spend / total)) if total > 0 else 0, "n": len(bad),
            "total_spend": total, "total_sales": sum(u.sales for u in life), "worst": worst}


def priority(baseline_roas: float | None, comparison_roas: float | None, bleed_pct: float | None = None,
             economics: Economics | None = None, cfg: RecommendConfig | None = None) -> str:
    """Triage priority of an entity.

    ``p0`` when comparison ROAS is below breakeven or structural bleed is at
    least ``priority_bleed_pct``; ``p1`` when ROAS fell by 25% or more
    (``comparison / baseline - 1 <= priority_drop``, on ROAS rounded to 2 dp);
    else ``p2``.
    """
    cfg, econ = cfg or _RC, economics or _ECON
    tr = round(comparison_roas, 2) if comparison_roas is not None else None
    hr = round(baseline_roas, 2) if baseline_roas is not None else None
    if (tr is not None and tr < cfg.bleed_roas_be * econ.be) or (bleed_pct is not None and bleed_pct >= cfg.priority_bleed_pct):
        return "p0"
    if tr is not None and hr and tr / hr - 1.0 <= cfg.priority_drop:
        return "p1"
    return "p2"


def entity_type(units: Iterable[UnitStats]) -> str:
    """Dominant targeting role by spend: ``"competitor"``, ``"own_brand"`` or ``"generic"`` (placements ignored)."""
    by = {"competitor": 0.0, "own_brand": 0.0, "generic": 0.0}
    for u in units:
        if u.is_placement:
            continue
        key = "competitor" if _is_conquest(u) else "own_brand" if _is_own(u) else "generic"
        by[key] += u.spend
    return max(by, key=lambda k: by[k]) if any(by.values()) else "generic"
