"""Per-entity budget probe and raise gating.

A budget raise is allowed and sized by the *marginal* return, not the
average.  The probe collects the facts (is the cap binding, how often, what
the capped days earn), attaches a saturation curve in three confidence
tiers, blocks a raise on the flat part of the curve, and sizes a
ceiling-aware recommended range.  ``raise_verdict`` then applies the ordered
precedence table that turns the probe into one verb.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from ..config import Economics, TradingDay
from ..liveness import canonical_status
from ..schema import BudgetDayRow, CanonicalStatus, PerfRow
from .config import BudgetConfig, money_of
from .saturation import CurveFit, WhatIfPoint, marginal, marginal_raw, unit_marginal_spend, whatif

__all__ = [
    "ServingEvidence", "BudgetProbe", "RaiseVerdict", "probe_budget", "recommended_range",
    "revenue_at_stake", "utilisation_ladder", "stepwise_evidence", "raise_verdict",
    "CURVE_OWN", "CURVE_OWN_LOWCONF", "CURVE_POOLED",
]

_CFG = BudgetConfig()

CURVE_OWN = "entity"                 # the entity's own decision-grade curve
CURVE_OWN_LOWCONF = "entity-lowconf"  # own curve below the decision r2: context only
CURVE_POOLED = "account-pooled"      # account-pooled fallback: marginal only, never a ceiling


@dataclass(frozen=True)
class ServingEvidence:
    """Optional canonical serving evidence from an hourly status history,
    over the last ``canonical_days`` days.  Every field may be ``None``."""

    held_days: int | None = None        # days with at least one budget-held hour
    dayparted_days: int | None = None   # days with operator-paused hours, no held hours and some active hours
    exhaust_hour: float | None = None   # local hour of day at which the budget typically runs out


@dataclass
class BudgetProbe:
    """Budget facts and gating flags for one entity."""

    entity_id: str
    daily_budget: float | None
    status: str | None = None
    act_days: int = 0
    avg_spend: float = 0.0
    util: float | None = None              # avg_spend / daily_budget (a ratio, not percent)
    days_capped: int = 0
    capped: bool = False
    exh_pct: float | None = None           # share of active days the platform flagged exhaustion
    cap_rev: float | None = None           # mean daily attributed sales on capped days
    cap_roas: float | None = None          # sales / spend over capped days
    soft_cap: bool = False
    dayparted: bool = False
    slot_exhaust_risk: bool = False
    anomaly: bool = False
    exhaust_hour: float | None = None
    mroas_cap: float | None = None         # marginal ROAS at the cap, 2 dp, display
    mroas_raw: float | None = None         # unrounded, decision-grade only
    mroas_src: str | None = None
    sat_spend: float | None = None         # own decision-grade ceiling only
    sat_r2: float | None = None
    sat_lowconf: bool = False
    var_starved: bool = False
    spend_cv: float | None = None
    whatif: list[WhatIfPoint] | None = None
    mroas_block: bool = False
    past_ceiling: bool = False             # the gating curve's marginal-1 point is at or below the cap (or within the minimum headroom): no raise
    unit_spend: float | None = None        # spend where the gating curve's marginal ROAS reaches 1 (own decision-grade or pooled curve)
    rev_at_stake: float | None = None
    unserved_hours: float | None = None
    rec_range: tuple[float, float, float] | None = None   # (low, high, recommended)
    exh_inefficient: bool = False
    ladder: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RaiseVerdict:
    """One verb for a capped entity.  ``code`` is machine-readable; ``raise_ok``
    is true only for the three raise outcomes."""

    code: str
    verb: str
    reason: str
    raise_ok: bool = False
    rec_range: tuple[float, float, float] | None = None
    rev_at_stake: float | None = None
    mroas: float | None = None
    r2: float | None = None
    estimator: str = "saturation_curve"


def recommended_range(cap: float, sat_spend: float | None,
                      cfg: BudgetConfig | None = None) -> tuple[float, float, float]:
    """Ceiling-aware raise range for a daily cap.

    ``hi = min(2.0 * cap, sat_spend)`` when a ceiling above the cap is
    known, else ``2.0 * cap``; returns ``(min(1.3 * cap, hi), hi,
    min(1.5 * cap, hi))`` = (low, high, recommended), unrounded.
    """
    cfg = cfg or _CFG
    hi = cfg.range_high_mult * cap
    if sat_spend is not None and sat_spend > cap:
        hi = min(hi, sat_spend)
    return (min(cfg.range_low_mult * cap, hi), hi, min(cfg.range_rec_mult * cap, hi))


def revenue_at_stake(cap_rev: float | None, exhaust_hour: float | None, days_capped: int,
                     trading_day: TradingDay | None = None) -> tuple[float, float] | None:
    """Revenue left unserved because the budget ran out mid-day.

    Only when ``start_hour < exhaust_hour < end_hour``:
    ``unserved = end_hour - exhaust_hour`` and ``rev = cap_rev /
    exhaust_hour * unserved * days_capped``.  It multiplies by the *observed*
    capped days, not a month, and assumes a flat hourly revenue rate: treat
    it as medium confidence.

    Returns ``(revenue, unserved_hours)`` or ``None``.
    """
    td = trading_day or TradingDay()
    if not cap_rev or exhaust_hour is None or not (td.start_hour < exhaust_hour < td.end_hour):
        return None
    unserved = td.end_hour - exhaust_hour
    return cap_rev / exhaust_hour * unserved * days_capped, unserved


def stepwise_evidence(rows: Iterable[PerfRow], cfg: BudgetConfig | None = None) -> tuple[float, int]:
    """Evidence that an entity is winning its auctions below its own bid.

    Over the entity's targeting units with a known bid: a target "clears
    below bid" when its realised CPM (``1000 * spend / impressions``) is
    under ``stepwise_cpm_ratio`` times its spend-weighted mean bid.  Assumes
    bids are expressed per thousand impressions.

    Returns ``(share of spend on such targets, number of such targets)``.
    """
    cfg = cfg or _CFG
    acc: dict[str, list[float]] = {}
    total = 0.0
    for r in rows:
        total += r.spend or 0.0
        if r.targeting_unit is None or r.bid is None or not r.spend or r.spend <= 0:
            continue
        a = acc.setdefault(r.targeting_unit, [0.0, 0.0, 0.0])
        a[0] += r.spend
        a[1] += r.impressions or 0.0
        a[2] += r.bid * r.spend
    below_spend, below_n = 0.0, 0
    for sp, im, bw in acc.values():
        if im <= 0 or sp <= 0:
            continue
        if 1000.0 * sp / im < cfg.stepwise_cpm_ratio * (bw / sp):
            below_spend += sp
            below_n += 1
    return (below_spend / total if total > 0 else 0.0), below_n


def utilisation_ladder(
    *, capped: bool, util: float | None, act_days: int, status_active: bool,
    mean_position: float | None = None, rank1_share: float | None = None,
    below_bid_spend_share: float | None = None, below_bid_targets: int | None = None,
    top_position: float = 1.0, lower_is_better: bool = True,
    cfg: BudgetConfig | None = None,
) -> str | None:
    """Explain why an un-capped entity under-spends its budget.

    Applies only when ``not capped AND util < util_ladder_max AND act_days
    >= min_active_days AND status is active``.  First match:

    * ``util < util_severe`` -> ``"severe"`` (investigate)
    * mean position within ``ladder_top_position_gap`` of the top slot and
      top-slot impression share unknown or ``>= ladder_rank1_share`` ->
      ``"volume-ceiling"`` (already on top; more budget buys nothing)
    * position known otherwise -> ``"bid-constrained"``, upgraded to
      ``"stepwise"`` when ``>= stepwise_spend_share`` of spend sits on
      ``>= stepwise_min_targets`` targets clearing below bid

    Returns the label or ``None`` (no position signal, or the ladder does
    not apply).
    """
    cfg = cfg or _CFG
    if capped or util is None or util >= cfg.util_ladder_max:
        return None
    if act_days < cfg.min_active_days or not status_active:
        return None
    if util < cfg.util_severe:
        return "severe"
    if mean_position is None:
        return None
    gap = (mean_position - top_position) if lower_is_better else (top_position - mean_position)
    if gap <= cfg.ladder_top_position_gap and (rank1_share is None or rank1_share >= cfg.ladder_rank1_share):
        return "volume-ceiling"
    if (below_bid_spend_share is not None and below_bid_targets is not None
            and below_bid_spend_share >= cfg.stepwise_spend_share
            and below_bid_targets >= cfg.stepwise_min_targets):
        return "stepwise"
    return "bid-constrained"


def probe_budget(
    entity_id: str,
    days: Sequence[BudgetDayRow],
    daily_budget: float | None = None,
    *,
    status: str | None = None,
    status_map: Mapping[str, str] | None = None,
    anchor: date | None = None,
    own_fit: CurveFit | None = None,
    pooled_fit: CurveFit | None = None,
    current_roas: float | None = None,
    serving: ServingEvidence | None = None,
    mean_position: float | None = None,
    rank1_share: float | None = None,
    below_bid_spend_share: float | None = None,
    below_bid_targets: int | None = None,
    top_position: float = 1.0,
    lower_is_better: bool = True,
    cfg: BudgetConfig | None = None,
    economics: Economics | None = None,
    trading_day: TradingDay | None = None,
) -> BudgetProbe:
    """Probe one entity's budget and decide whether a raise is blocked.

    Facts over the ``probe_days`` ending on ``anchor`` (default: the
    entity's latest row): ``act_days`` (spend > 0), ``avg_spend``,
    ``days_capped`` (``spend >= cap_hit_fraction * daily_budget``), ``util
    = avg_spend / daily_budget``, ``capped = act_days >= min_active_days AND
    days_capped >= capped_day_share * act_days``, ``exh_pct``, ``cap_rev``
    (mean daily sales on capped days) and ``cap_roas``.  ``cap_roas`` is the
    ratio of sums over capped days, consistent with every other ROAS here
    (averaging daily ratios would over-weight small days).

    Guards: a placeholder cap or ``util > util_anomaly`` flags ``anomaly``
    and clears ``capped``; ``capped`` with a low exhaustion-flag share is a
    ``soft_cap``; serving evidence overrides (held on >= 5 of 7 days ->
    capped; capped with zero held days -> soft cap; day-parted on >= 5 of 7
    -> ``dayparted``, never topped up).

    Curve tiers: own fit with ``r2 >= sat_decision_r2`` sets the display
    marginal, the unrounded decision marginal, the ceiling and the what-if;
    an own fit below that is context only (no decision marginal, no
    ceiling); with no own fit the account-pooled curve may lend a marginal
    but never a ceiling.

    Block: ``capped AND cap_rev AND cap_roas >= capped_roas_min * be AND
    not dayparted AND mroas_raw < marginal_block``.

    When the raise is not blocked (and ``current_roas`` is unknown or at
    least breakeven) the probe adds ``rev_at_stake`` and ``rec_range``.  The
    range is clamped to ``unit_spend``, the spend where the gating curve's
    marginal ROAS reaches 1 (own decision-grade curve, else the pooled
    curve), so every point of the range returns at least what it costs on
    that curve.  If the marginal at the cap is under ``marginal_ceiling``
    (1.0), or ``unit_spend`` is within ``range_min_headroom`` of the cap, no
    range is offered and ``past_ceiling`` is set; :func:`raise_verdict` then
    holds.  ``marginal_block`` (0.9, a noise margin under 1) stays the line
    for the harder "blocked" wording.  Set ``range_requires_headroom=False``
    to offer the full range regardless.

    Returns a ``BudgetProbe``.
    """
    cfg = cfg or _CFG
    eco = economics or Economics()
    td = trading_day or TradingDay()
    rows = sorted((d for d in days if d.entity_id == entity_id), key=lambda d: d.date)
    if rows:
        anchor = anchor or rows[-1].date
        start = anchor - timedelta(days=cfg.probe_days - 1)
        rows = [d for d in rows if start <= d.date <= anchor]
    if daily_budget is None:
        caps = [d.daily_budget for d in rows if d.daily_budget is not None]
        daily_budget = caps[-1] if caps else None
    if status is None and rows:
        status = rows[-1].status
    p = BudgetProbe(entity_id=entity_id, daily_budget=daily_budget, status=status)

    active = [d for d in rows if d.spend and d.spend > 0]
    p.act_days = len(active)
    p.avg_spend = sum(d.spend for d in active) / len(active) if active else 0.0
    has_cap = bool(daily_budget and daily_budget > 0)
    capped_days = [d for d in active if has_cap and d.spend >= cfg.cap_hit_fraction * daily_budget]
    p.days_capped = len(capped_days)
    p.util = (p.avg_spend / daily_budget) if has_cap else None
    p.capped = bool(p.act_days >= cfg.min_active_days
                    and p.days_capped >= cfg.capped_day_share * p.act_days and has_cap)
    flagged_known = any(d.exhausted_at_minute is not None for d in rows)
    if active and flagged_known:
        p.exh_pct = sum(1 for d in active if d.exhausted_at_minute is not None) / len(active)
    if capped_days:
        cs = sum(d.spend for d in capped_days)
        cr = sum(d.sales or 0.0 for d in capped_days)
        p.cap_rev = cr / len(capped_days)
        p.cap_roas = cr / cs if cs > 0 else None

    if has_cap and (daily_budget < money_of(cfg.placeholder_cap, eco)
                    or (p.util is not None and p.util > cfg.util_anomaly)):
        p.anomaly = True
        p.capped = False
        p.notes.append("cap is a placeholder or does not constrain spend")
    if p.capped and p.exh_pct is not None and p.exh_pct < cfg.soft_cap_exhaust_share:
        p.soft_cap = True

    # Canonical override from serving evidence (hourly status history).
    held = serving.held_days if serving else None
    if held is None and rows and any(d.platform_held is not None for d in rows):
        recent_from = rows[-1].date - timedelta(days=cfg.canonical_days)
        held = sum(1 for d in rows if d.date > recent_from and d.platform_held)
        derived_held = True
    else:
        derived_held = False
    if held is not None and not p.anomaly and has_cap:
        if held >= cfg.held_days_canonical:
            p.capped, p.soft_cap = True, False
        elif p.capped and held == 0 and not derived_held:
            p.soft_cap = True
    if serving and serving.dayparted_days is not None and serving.dayparted_days >= cfg.held_days_canonical:
        p.dayparted = True
        if has_cap and daily_budget < money_of(cfg.small_slot_cap, eco):
            p.slot_exhaust_risk = True

    # Exhaust hour: canonical source first, else the platform minute field.
    if serving and serving.exhaust_hour is not None:
        p.exhaust_hour = serving.exhaust_hour
    else:
        mins = [d.exhausted_at_minute for d in capped_days
                if d.exhausted_at_minute is not None and d.exhausted_at_minute > 0]
        if mins:
            p.exhaust_hour = sum(mins) / len(mins) / 60.0

    # Curve attachment, three tiers.
    if has_cap:
        if own_fit is not None and own_fit.fit:
            p.mroas_cap = marginal(own_fit, daily_budget)
            p.sat_r2 = own_fit.r2
            p.whatif = whatif(own_fit, daily_budget, cfg=cfg)
            if (own_fit.r2 or 0.0) >= cfg.sat_decision_r2:
                p.mroas_raw = marginal_raw(own_fit, daily_budget)
                p.sat_spend = own_fit.sat_spend
                p.unit_spend = unit_marginal_spend(own_fit)
                p.mroas_src = CURVE_OWN
            else:
                p.mroas_src = CURVE_OWN_LOWCONF
                p.sat_lowconf = True
        else:
            if own_fit is not None and own_fit.reason == "var_starved":
                p.var_starved = True
                p.spend_cv = own_fit.cv
            if (pooled_fit is not None and pooled_fit.fit
                    and (pooled_fit.r2 or 0.0) >= cfg.sat_decision_r2):
                p.mroas_cap = marginal(pooled_fit, daily_budget)
                p.mroas_raw = marginal_raw(pooled_fit, daily_budget)
                p.sat_r2 = pooled_fit.r2
                p.whatif = whatif(pooled_fit, daily_budget, cfg=cfg)
                p.unit_spend = unit_marginal_spend(pooled_fit)
                p.mroas_src = CURVE_POOLED

    roas_bar = cfg.capped_roas_min * eco.be
    earning = bool(p.capped and p.cap_rev and p.cap_roas is not None
                   and p.cap_roas >= roas_bar and not p.dayparted)
    p.mroas_block = bool(earning and p.mroas_raw is not None and p.mroas_raw < cfg.marginal_block)

    if earning and not p.mroas_block and (current_roas is None or current_roas >= roas_bar):
        ras = revenue_at_stake(p.cap_rev, p.exhaust_hour, p.days_capped, td)
        if ras is not None:
            p.rev_at_stake, p.unserved_hours = ras
        limit = p.unit_spend if p.unit_spend is not None else p.sat_spend
        no_room = ((p.mroas_raw is not None and p.mroas_raw < cfg.marginal_ceiling)
                   or (limit is not None and limit <= daily_budget * (1.0 + cfg.range_min_headroom)))
        if cfg.range_requires_headroom and no_room:
            p.past_ceiling = True
            p.notes.append("marginal ROAS reaches 1 at or just above the current cap: no raise range")
        else:
            p.rec_range = recommended_range(daily_budget, limit, cfg)
    elif p.capped and p.cap_roas is not None and p.cap_roas < roas_bar:
        p.exh_inefficient = True

    status_active = canonical_status(status, status_map) is CanonicalStatus.ACTIVE
    p.ladder = utilisation_ladder(
        capped=p.capped, util=p.util, act_days=p.act_days, status_active=status_active,
        mean_position=mean_position, rank1_share=rank1_share,
        below_bid_spend_share=below_bid_spend_share, below_bid_targets=below_bid_targets,
        top_position=top_position, lower_is_better=lower_is_better, cfg=cfg)
    return p


def raise_verdict(
    probe: BudgetProbe,
    *,
    current_roas: float | None,
    status: str | None = None,
    status_map: Mapping[str, str] | None = None,
    availability_block: bool = False,
    sub_breakeven_share: float | None = None,
    organic_share: float | None = None,
    cfg: BudgetConfig | None = None,
    economics: Economics | None = None,
) -> RaiseVerdict:
    """Turn a capped entity's probe into one verb.  First match wins:

    1. ended / removed status -> ``no_action``
    2. ROAS unknown -> ``investigate_tracking`` (never raise blind)
    3. ROAS >= scale bar and day-parted -> ``conflict_daypart`` (a schedule, not exhaustion)
    4. ... and availability blocked -> ``conflict_supply`` (fix supply first)
    5. ... and sub-breakeven share of spend >= ``bleed_share_hold`` -> ``hold_bleed``
    6. ... and the marginal blocks (flag, or raw marginal below the block) -> ``hold_flat``;
       the marginal is under ``marginal_ceiling`` or the probe is past its
       ceiling -> ``hold_past_ceiling`` (incremental revenue < incremental spend)
    7. ... and organic share >= ``organic_share_hold`` -> ``hold_organic``
    8. ... otherwise raise: budget-held status -> ``top_up``; operator-paused
       -> ``investigate_stop``; else ``raise``, carrying the range, revenue
       at stake, marginal and r2
    9. breakeven <= ROAS < scale bar -> ``hold_monitor``
    10. ROAS < breakeven -> ``tighten`` (sized elsewhere)

    The bleed check (5) precedes the curve check (6) on purpose: the more
    actionable root cause leads the hold.  The scale bar is
    ``Economics.scale_bar`` and breakeven is ``Economics.be``.

    Returns a ``RaiseVerdict``.
    """
    cfg = cfg or _CFG
    eco = economics or Economics()
    st = canonical_status(status if status is not None else probe.status, status_map)
    if st in (CanonicalStatus.ENDED, CanonicalStatus.REMOVED):
        return RaiseVerdict("no_action", "No budget action", "entity has ended; no budget action is valid")
    if current_roas is None:
        return RaiseVerdict("investigate_tracking", "Investigate tracking",
                            "no attributed return is recorded; never raise blind")
    if current_roas >= eco.scale_bar:
        if probe.dayparted:
            return RaiseVerdict("conflict_daypart", "Review schedule",
                                "spend stops on a schedule, not on budget; a raise would not serve more hours")
        if availability_block:
            return RaiseVerdict("conflict_supply", "Fix supply first",
                                "availability is below the block level; more budget buys impressions that cannot convert")
        if sub_breakeven_share is not None and sub_breakeven_share >= cfg.bleed_share_hold:
            return RaiseVerdict("hold_bleed", "Hold the budget raise",
                                "a large share of spend runs below breakeven; fix the bleed first")
        if probe.mroas_block or (probe.mroas_raw is not None and probe.mroas_raw < cfg.marginal_block):
            return RaiseVerdict("hold_flat", "Hold the budget raise",
                                "the next unit of spend returns less than it costs (flat part of the curve; "
                                "correlational, no demand controls)",
                                mroas=probe.mroas_cap, r2=probe.sat_r2)
        if probe.past_ceiling or (cfg.range_requires_headroom and probe.mroas_raw is not None
                                  and probe.mroas_raw < cfg.marginal_ceiling):
            return RaiseVerdict("hold_past_ceiling", "Hold the budget raise",
                                "marginal ROAS at the cap is under 1: by the fitted curve the next unit of "
                                "spend returns less than it costs",
                                mroas=probe.mroas_cap, r2=probe.sat_r2)
        if organic_share is not None and organic_share >= cfg.organic_share_hold:
            return RaiseVerdict("hold_organic", "Hold the budget raise",
                                "most of this demand already arrives organically; paid gains would be non-incremental")
        kw = dict(raise_ok=True, rec_range=probe.rec_range, rev_at_stake=probe.rev_at_stake,
                  mroas=probe.mroas_cap, r2=probe.sat_r2)
        if st is CanonicalStatus.BUDGET_HELD:
            return RaiseVerdict("top_up", "Top up daily budget",
                                "held for budget today while clearly profitable", **kw)
        if st is CanonicalStatus.OPERATOR_PAUSED:
            return RaiseVerdict("investigate_stop", "Investigate stop and reactivate",
                                "clearly profitable but stopped by an operator", **kw)
        return RaiseVerdict("raise", "Raise daily budget",
                            "the cap binds on most days and the return clears the scale bar", **kw)
    if current_roas >= eco.be:
        return RaiseVerdict("hold_monitor", "Hold and monitor",
                            f"profitable but not clearly scale-worthy; re-read in {cfg.hold_monitor_days} days")
    return RaiseVerdict("tighten", "Tighten", "the cap funds spend below breakeven")
