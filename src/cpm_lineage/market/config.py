"""Thresholds of the market subsystem (share of voice, market stats,
saturation screen, flywheel, competition, price tiers, events)."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["MarketConfig", "OWN_LABEL"]

OWN_LABEL = "~own"  # brand label of the single aggregated own row in competition tables


@dataclass(frozen=True)
class MarketConfig:
    """Named thresholds with their defaults.  Shares are percent."""

    # -- market stats -------------------------------------------------------
    top_segments: int = 4                   # segments listed, by own sales
    ms_sov_block: float = 0.5               # share / voice below this: over-investing, block scale
    ms_sov_over: float = 0.7                # below this: over-investing in ads, attach the barrier chain
    ms_sov_mild: float = 1.0                # below this: mild under-conversion
    ms_sov_healthy_max: float = 1.5         # up to this: healthy; above: may be under-investing

    # -- saturation screen ----------------------------------------------------
    saturation_net_decline: float = 0.95    # both legs down AND newest < 0.95 x oldest (one-leg 2 % moves are noise)
    saturation_cost_rise: float = 1.05      # ad cost share of sales up more than 5 % on the prior period
    saturation_cold_organic: float = 0.20   # organic share below this: flywheel cold, cutting is self-harm
    saturation_confirmed_organic: float = 0.50  # organic share above this: saturation confirmed
    saturation_test_cut: str = "10-15%"     # size of the reduction test suggested in the middle case
    saturation_test_weeks: int = 2          # re-measure horizon of that test

    # -- flywheel -------------------------------------------------------------
    flywheel_cold: float = 0.20             # organic share below: Cold
    flywheel_warming: float = 0.50          # below: Warming
    flywheel_active: float = 0.70           # below: Active; at or above: Self-Sustaining
    flywheel_material_share: float = 0.90   # mixed-state read ignores the long tail beyond 90 % of impressions
    flywheel_top_n: int = 10                # product rows returned

    # -- share of voice ---------------------------------------------------------
    rivals_top: int = 6                     # brands kept per keyword and period

    # -- competition table ------------------------------------------------------
    competition_top_n: int = 8              # rivals listed besides the own row
    advertisers_top_n: int = 5              # top advertisers by ad share of voice

    # -- price tiers ------------------------------------------------------------
    tier_min_products: int = 6              # terciles on fewer priced products are not tiers
    tier_trend_pp: float = 0.3              # share move beyond +-0.3 pp is rising / falling
    tier_owned_pct: float = 85.0            # band cut-offs: five honest bands beat a fake rank
    tier_led_pct: float = 55.0  # own share of the tier at or above this: led
    tier_contested_pct: float = 25.0  # at or above this: contested
    tier_minority_pct: float = 1.0  # at or above this: minority; below it the brand is absent from the tier
    whitespace_max_own_share_pct: float = 0.5  # own share of a tier pool below this is a listing gap
    whitespace_top_geos: int = 8            # geos scanned
    whitespace_top_n: int = 6               # gaps returned
    trend_min_month_volume: float = 0.2     # drop months under 20 % of the mean monthly volume
    trend_min_months: int = 3               # a trend needs three usable months

    # -- pricing power ------------------------------------------------------------
    elasticity_min_points: int = 6          # OLS on fewer points is noise
    elasticity_weeks: int = 12              # weekly points read

    # -- break detection ------------------------------------------------------------
    break_min_points: int = 14              # daily points needed
    break_min_others: int = 5               # other day-over-day deltas needed for the yardstick
    break_sigma: float = 3.0                # the break must exceed 3 sd of the other deltas
    break_min_pp: float = 0.5               # ... and half a share point: never invent an event
    mover_availability_pp: float = 2.0      # same-day mover: own availability change, points
    mover_ad_sov_pp: float = 2.0            # same-day mover: own ad share of voice change, points
    mover_rival_ad_pct: float = 25.0        # same-day mover: rival ad impressions change, percent

    # -- launches -------------------------------------------------------------------
    launch_window_days: int = 90            # first seen within this many days
    launch_history_gate_days: int = 30      # and more than this after the data history starts (honesty gate)
    launch_min_sales_ref: float = 25000.0   # reference money: current-window sales to matter
    launch_ramping: float = 1.3             # current rate >= 1.3 x lifetime rate: ramping
    launch_fading: float = 0.7              # current rate < 0.7 x lifetime rate: fading

    # -- seasonality / small reads ----------------------------------------------------
    seasonality_min_months: int = 6         # one observed cycle needs six months
    rival_slot_min_ad_impressions: float = 1000.0  # a top rival needs this many ad impressions
    geo_role_top_geos: int = 8              # geos listed in the geo x keyword-type read
    entrant_prior_share_pct: float = 0.05   # (kept for callers building growth-decomposition inputs)
