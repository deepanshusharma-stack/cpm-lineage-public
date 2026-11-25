"""Thresholds of the geo subsystem (quadrants, tiers, posture, drilldown,
money-at-stake, product x geo map).  Shares are percent; availability 0..1."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GeoConfig"]


@dataclass(frozen=True)
class GeoConfig:
    """Named thresholds with their defaults."""

    # -- quadrants ----------------------------------------------------------
    window_days: int = 30                   # current vs prior window when no WindowPair is passed
    min_segment_sales_ref: float = 1.0      # reference money: geos with less segment sales are dropped
    exit_segment_growth_pct: float = -5.0   # under-invested geo in a segment shrinking faster than this: exit
    availability_weight_floor: float = 1.0  # velocity weight floor for active products (source: at least 1)
    standing_min_checks: float = 30.0       # a brand needs this many shelf checks to be ranked on availability
    standing_min_brands: int = 3            # a quantile on fewer brands is noise
    tier_min_geos_for_quartile: int = 4     # with fewer geos the ROAS quartile collapses to the median

    # -- posture --------------------------------------------------------------
    posture_defend_q1_pct: float = 60.0     # Defending: > 60 % of own sales in leader geos ...
    posture_defend_gaining_pct: float = 50.0  # ... and > 50 % of geos gaining share
    posture_gaining_pct: float = 75.0       # Gaining: >= 75 % of geos gaining share
    posture_build_q4_pct: float = 40.0      # Building: > 40 % of own sales in under-invested geos ...
    posture_build_growth_pct: float = 15.0  # ... in a segment growing > 15 % ...
    posture_build_gaining_pct: float = 50.0  # ... with > 50 % of geos gaining
    posture_stall_pct: float = 50.0         # Stalling: leader + ad-dependent > 50 % of sales ...
    posture_stall_gaining_pct: float = 50.0  # ... and fewer than 50 % of geos gaining (its own knob)
    posture_pressure_pct: float = 50.0      # Under Pressure: organic + under-invested > 50 % of sales ...
    posture_pressure_gaining_pct: float = 25.0  # ... or fewer than 25 % of geos gaining
    rank_leader_pct: float = 25.0           # rank label from national share (approximate, not a true rank)
    rank_contender_pct: float = 10.0  # national share at or above this (and not a leader) reads as Contender
    rank_mid_pct: float = 3.0  # national share at or above this reads as Mid-Tier; below it the smallest label

    # -- geo drilldown ----------------------------------------------------------
    drill_top_n: int = 10                   # geos listed by impact
    tier_scale_min_availability: float = 0.80  # SCALE needs healthy supply
    # (the FIX-FIRST line is AvailabilityPolicy.block: supply precedes demand)

    # -- money at stake -----------------------------------------------------------
    supply_bleed_availability: float = 0.70  # share loss with availability below this is supply-driven
    run_rate_multiplier: float = 2.0        # capturable = current gain x 2 (a labelled model, not a forecast)
    segment_growth_capture_pct: float = 10.0  # segment growing faster than this: growth worth capturing

    # -- product x geo map -----------------------------------------------------------
    map_top_geos: int = 6                   # geos by own sales
    map_top_products: int = 8               # own products within them
    map_priority_rank: int = 3              # a product is a priority in a geo when in its top 3 by sales
    map_invest_roas_be_mult: float = 1.0    # weak-organic priority cell: invest at or above breakeven, else fix first
    map_position_max: float = 500.0         # organic positions outside 1..500 are discarded
