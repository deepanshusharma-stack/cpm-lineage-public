"""Thresholds of the budget subsystem.

Every value is a named field with a default and a one-line
rationale.  Absolute money amounts are written in *reference-currency units*
and must be passed through ``Economics.money()`` before use (``money_of``
below does that), so no currency is baked in.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Economics

__all__ = ["BudgetConfig", "money_of"]


@dataclass(frozen=True)
class BudgetConfig:
    """Named thresholds for sufficiency, the saturation curve, raise gating,
    budget moves, the ad-type mix and the three money estimators."""

    # --- sufficiency / required budget ----------------------------------------
    oob_spend_fraction: float = 0.5        # an exhaustion flag counts only when spend reached half the cap (a flag raised far below the cap is a pacing artefact, not exhaustion)
    sufficiency_min: float = 0.01          # clamp floor: guards division by zero and caps the multiplier at 100x
    sufficiency_max: float = 1.0           # clamp ceiling: a day cannot be more than fully served
    placeholder_cap: float = 100.0         # MONEY: a cap below this is a placeholder, no top-up maths on it
    oob_min_extra: float = 1.0             # MONEY: extra below this is rounding dust, kept out of the summary
    projection_days: int = 30              # display-only monthly projection of the daily extra
    exhaust_minute_origin: float = 0.0     # minutes after local midnight at which serving starts; subtracted when the exhaust minute stands in for live minutes (0 = the clock starts at midnight)
    tag_separator: str = "/"               # tag identity is group + separator + name: one name can exist under two groups
    display_decimals: int = 0              # rounding of money figures in display outputs (never used inside decisions)

    # --- budget telemetry -------------------------------------------------------
    telemetry_days: int = 30               # lookback of the per-entity telemetry
    telemetry_min_spend: float = 5000.0    # MONEY: entities at or below this window spend carry too little evidence
    telemetry_se_min_days: int = 5         # standard error of daily ROAS needs at least 5 days
    telemetry_z: float = 1.28              # about 90% one-sided normal bound on ROAS
    recent_days: int = 3                   # "last 3 days" for served-time sufficiency and the before-recent cap

    # --- saturation curve -------------------------------------------------------
    sat_lookback_days: int = 90            # every curve fit reads about 90 daily points
    sat_min_days: int = 21                 # fewer spending days cannot identify two parameters
    sat_min_cv: float = 0.15               # spend that never varies cannot reveal a response
    sat_min_corr: float = 0.2              # no positive spend-sales relation (must be strictly greater) = fiction
    regime_time_corr: float = 0.6          # |corr(spend, time)| above this ...
    regime_half_cv: float = 0.15           # ... with both half-window CVs below this = two flat clusters confounded with time
    sat_b_min: float = 1e-6                # lower bound of the scale parameter
    sat_theta_min: float = 1e-2            # lower bound of the half-saturation parameter
    sat_theta_span: float = 1e6            # theta is searched up to this multiple of the largest observed spend (near-linear data runs to the bound)
    sat_grid_points: int = 96              # coarse log-spaced theta grid of the profile search
    sat_refine_iters: int = 80             # golden-section refinement steps around the best grid cell
    sat_min_r2: float = 0.05               # below this the fit is rejected outright
    sat_decision_r2: float = 0.3           # only fits at or above this may block a raise or size a ceiling
    sat_ceiling_support: float = 2.0       # a ceiling beyond 2x the largest observed spend is an optimiser artefact
    whatif_mults: tuple[float, ...] = (0.7, 1.0, 1.3, 1.5, 2.0)  # cap multipliers of the what-if grid
    extrapolation_mult: float = 1.05       # a what-if point beyond 1.05x observed spend is extrapolation

    # --- per-entity probe and raise gating -------------------------------------
    probe_days: int = 30                   # lookback of the budget probe
    cap_hit_fraction: float = 0.9          # a day is "capped" at spend >= 90% of the daily budget
    capped_day_share: float = 0.6          # the cap binds when capped on >= 60% of active days
    min_active_days: int = 7               # fewer spending days is not enough to call anything
    util_anomaly: float = 5.0              # mean spend above 5x the cap: the cap does not constrain
    soft_cap_exhaust_share: float = 0.3    # capped by spend but flagged exhausted on < 30% of days: pacing artefact
    canonical_days: int = 7                # window of the serving-evidence override
    held_days_canonical: int = 5           # held (or day-parted) on >= 5 of the last 7 days is canonical
    small_slot_cap: float = 2000.0         # MONEY: a day-parted entity below this cap risks exhausting inside a slot
    marginal_block: float = 0.9            # unrounded marginal ROAS below this blocks a raise (below 1 with a noise margin)
    marginal_ceiling: float = 1.0          # a raise needs a marginal ROAS of at least this at the cap: below it the next unit of spend returns less than it costs
    range_min_headroom: float = 0.01       # the marginal-1 spend must exceed the cap by this fraction before a raise range is offered
    capped_roas_min: float = 1.0           # x breakeven: capped-day ROAS below this = exhausting but inefficient
    range_low_mult: float = 1.3            # recommended range, low end
    range_rec_mult: float = 1.5            # recommended range, recommended point
    range_high_mult: float = 2.0           # recommended range, high end (before the ceiling clamp)
    range_requires_headroom: bool = True   # no range when a decision-grade ceiling sits at or below the cap or the gating curve's marginal is under marginal_ceiling (False = fall back to the full range)
    util_ladder_max: float = 0.85          # the under-utilisation ladder applies below this utilisation
    util_severe: float = 0.20              # below this utilisation: investigate before anything else
    ladder_top_position_gap: float = 0.5   # mean position within half a slot of the top slot = already at the top
    ladder_rank1_share: float = 0.6        # top-slot impression share at or above this confirms a volume ceiling
    stepwise_cpm_ratio: float = 0.85       # realised CPM under 85% of the own bid = winning below bid
    stepwise_spend_share: float = 0.5      # ... on at least half of spend
    stepwise_min_targets: int = 3          # ... across at least 3 targets
    bleed_share_hold: float = 0.40         # 40% of spend below breakeven: fix the bleed before raising
    organic_share_hold: float = 0.70       # 70% organic share: paid demand is mostly non-incremental
    hold_monitor_days: int = 7             # profitable but not scale-worthy: hold and re-read in a week

    # --- account-level budget moves ---------------------------------------------
    moves_days: int = 30                   # lookback of the budget-constraint scan
    release_util_max: float = 0.70         # parked budget: utilisation below 70% ...
    release_roas_max: float = 1.0          # x breakeven: ... and ROAS below breakeven
    release_fraction: float = 0.4          # release 40% of the cap, bounded by realised spend
    tighten_roas_max: float = 1.0          # x breakeven: a binding cap funding a loss
    scale_step: float = 0.5                # first-pass raise of +50%, an upper bound only
    moves_top_n: int = 8                   # rows kept per bucket
    moves_require_decision_r2: bool = True  # a fit below sat_decision_r2 neither sizes nor ejects a raise (False = any accepted fit may do both)
    release_blocked_statuses: tuple[str, ...] = (  # canonical statuses whose budget is not "parked"
        "operator_paused", "ended", "removed", "budget_held", "platform_paused")

    # --- ad-type mix --------------------------------------------------------------
    mix_days: int = 30                     # length of each of the two adjacent windows
    plateau_marginal: float = 1.0          # ad-type marginal ROAS below this = near plateau, not a rebalance sink
    material_type_spend: float = 50000.0   # MONEY: an ad type below this spend is not worth a recommendation
    reduce_type_spend: float = 100000.0    # MONEY: a non-drillable loss-making type above this gets "reduce / cap"
    pareto_share: float = 0.80             # entities inside this cumulative share of drag are "the few"

    # --- estimator (a): stake and expected value --------------------------------
    stake_roas_cap_mult: float = 6.0       # x breakeven: caps ROAS in the stake so outliers do not dominate
    exhaust_haircut: float = 0.9           # haircut on the hours-to-fund raise
    raise_pct_min: float = 5.0             # smallest raise step, percent
    raise_pct_max: float = 50.0            # largest raise step, percent
    ev_prior: float = 0.5                  # success probability before any realised outcomes
    ev_min_outcomes: int = 5               # a family's realised win rate replaces the prior at >= 5 paired outcomes
    ev_breakeven_relative: bool = False    # False = bleed-avoided uses literal 1 - ROAS (the default); True = breakeven - ROAS

    # --- estimator (c): finite-difference marginal ROAS -------------------------
    fd_min_days: int = 8                   # need at least 8 spending days to split low / high halves
    fd_min_spend_gap: float = 100.0        # MONEY: mean-spend gap between halves must exceed this
    fd_veto_mult: float = 0.8              # x breakeven: a raise is vetoed below this marginal
    fd_halve_mult: float = 1.5             # x breakeven: a raise is halved below this marginal
    fd_deepen_mult: float = 0.5            # x breakeven: a cut is deepened below this marginal
    fd_min_step_pct: float = 5.0           # a halved raise never goes below 5%
    fd_deepen_step_pct: float = 5.0        # a deepened cut adds 5 points ...
    fd_cut_floor_pct: float = -25.0        # ... and is at least a 25% cut
    fd_exempt_families: tuple[str, ...] = ("defense",)  # families the marginal pass never touches


def money_of(reference_amount: float, economics: Economics | None = None) -> float:
    """Scale a reference-currency threshold into account currency.

    Returns ``reference_amount * economics.currency_unit`` (unchanged when no
    economics are supplied).
    """
    return (economics or Economics()).money(reference_amount)
