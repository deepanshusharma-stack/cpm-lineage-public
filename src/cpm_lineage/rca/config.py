"""Thresholds for root-cause analysis.

Every judgment the RCA makes is a named field here.  The dataclass is flat
and frozen so per-account overrides go through
``cpm_lineage.config.apply_overrides`` (known keys override, unknown keys
are reported, the defaults are never mutated).

Scales used throughout the RCA package:

* availability: ``0..1`` (library convention);
* shares, share of voice, discount depth, growth: percent / percentage
  points (``pp``);
* ROAS bars: multiples of the account's effective breakeven
  (``Economics.be``);
* money: reference amounts scaled with ``Economics.money``.
"""

from __future__ import annotations

from dataclasses import dataclass

AXES: tuple[str, ...] = ("sov", "availability", "pricing", "competition", "conversion", "geo")
"""Lever axes: every lever names exactly one."""

FOCUSES: tuple[str, ...] = ("sov", "availability", "pricing", "competition", "conversion", "offtake", "general")


@dataclass(frozen=True)
class RcaConfig:
    """Every RCA threshold as a named field with a default."""

    # ---- gate table (per-account overridable) -----------------------------
    segment_move_pct: float = 5.0           # segment value moved this much: the market itself moved
    share_move_pp: float = 0.10             # share change that counts as share changing hands
    promo_depth_pp: float = 3.0             # discount-depth pullback beyond this fires the promo gate
    sov_drop_pp: float = 1.0                # own ad share-of-voice fall worth explaining
    pullback_gap_pp: float = 10.0           # own impression delta trailing the pool by more = genuine pullback
    availability_floor: float = 0.85        # under this, ad money hits empty shelves
    availability_drop: float = 0.03         # window-over-window fall that fires the supply gate
    mover_pp: float = 0.5                   # rival share move that makes the movers table
    sticker_mode_share: float = 0.10        # 2+ list prices each above this share = mixed rollout
    launch_share_pct: float = 1.0           # a launch at >= this % of segment value is a real entrant
    roas_guard: float = 1.1                 # x breakeven; direct ROAS below this triggers the efficiency lever
    spend_cut_pct: float = 15.0             # first-party ad spend cut that is a budget decision
    step_pct: float = 12.0                  # run-rate level change that is a step, not a drift
    pocket_growth_pct: float = 20.0         # keyword pool growth with ~no own voice = unworked pocket
    pocket_own_sov: float = 2.0             # own ad SOV under this % = effectively absent
    vacuum_drop: float = 0.6                # rival ad volume at <= this fraction of prior has vacated voice
    geo_collapse_pct: float = -20.0         # segment fell beyond this in a geo = no-spend zone
    passenger_availability_gap: float = 0.10  # product this far under the floor (or zero sales) is a passenger

    # ---- windows and routing ----------------------------------------------
    default_days: int = 14                  # default window length, anchored to the market feed's latest date
    allowed_days: tuple[int, ...] = (7, 14, 30, 60, 90)  # window lengths a routed question may ask for
    route_cache_cap: int = 200              # routed questions memoised per cache

    # ---- step scan --------------------------------------------------------
    step_min_points: int = 14               # daily points needed before a change-point is looked for
    step_min_segment: int = 5               # both sides of a split need this many days

    # ---- fact extraction --------------------------------------------------
    battleground_size: int = 12             # generic keywords (by own ad impressions) that define the battleground
    keyword_list_cap: int = 20              # generic keywords listed by current pool
    advertiser_list_cap: int = 8            # advertisers listed on the battleground
    sticker_cluster_radius: float = 15.0    # money; list prices this close are one price point
    sticker_max_points: int = 3             # price points recorded per product
    launch_lookback_days: int = 60          # first sale within this many days of feed max = launch
    other_lines_cap: int = 6                # other own segments listed
    how_deep_discount_pct: float = 30.0     # rival playbook: deep price
    how_high_availability: float = 0.90     # rival playbook: "+ high availability"
    how_distribution_availability: float = 0.92  # rival playbook: distribution-led
    how_low_discount_pct: float = 10.0      # rival playbook: "at almost no discount"
    how_media_sov_pct: float = 15.0         # rival playbook: media-led
    how_range_products: int = 7             # rival playbook: range-led

    # ---- attribution literals ---------------------------------------------
    pocket_min_pool_share: float = 0.03     # a pocket keyword must hold this share of the listed pool
    vacuum_min_prior_sov_pct: float = 5.0   # advertiser needed this prior SOV to leave a vacuum
    vacuum_exclude_own: bool = True         # the own brand cannot vacate voice to itself
    mix_shift_loss_fraction: float = 0.5    # another own line growing by this share of the loss = mix shift
    geo_beat_pp: float = 2.0                # own beats / trails the segment in a geo by more than this
    geo_trail_far_pp: float = 5.0           # "trailed even that" wording in a collapsed geo
    geo_top_n: int = 10                     # geos (by own prior value) scanned for spend / avoid
    share_geo_top_n: int = 8                # geos used for the "beat the segment in N of K" proof
    resilient_geo_count: int = 3            # living geos kept as the shift destination
    conversion_steady_frac: float = 0.10    # conversion within this of prior = steady
    organic_held_frac: float = 0.9          # organic impressions at >= this of prior = held
    growth_multiple_pct: float = 300.0      # growth above this is shown as a multiple
    sticker_checkpoint_share: float = 0.95  # single modal list price target after a migration

    # ---- verification -----------------------------------------------------
    join_coverage_min_pct: float = 99.0     # segment value that must map to a brand
    tolerance_value: float = 1.0            # money; absolute tolerance on value sums
    tolerance_units: float = 0.5            # absolute tolerance on the unit decomposition
    tolerance_rel: float = 1e-9             # relative tolerance, whichever is larger applies

    # ---- playbook literals ------------------------------------------------
    under_voiced_gap_pp: float = -1.0       # voice minus market share below this = under-invested
    over_voiced_gap_pp: float = 3.0         # voice minus market share above this = over-voiced
    mix_ratio: float = 1.3                  # best / worst ad-type ROAS that makes the mix "not optimal"
    mix_worst_share: float = 0.20           # ... while the worst type holds this share of spend
    mix_min_share: float = 0.05             # ad types under this share of spend are not compared
    roas_fell_frac: float = 0.95            # ROAS under this fraction of prior = it fell
    rival_ceding_pp: float = -0.3           # a rival losing more share than this is the exposed one
    loss_stable_frac: float = 0.25          # availability loss within this of prior = unchanged
    not_driver_own_pct: float = -10.0       # ... while own value fell beyond this: availability did not drive it
    share_word_band_pp: float = 0.05        # rose / fell / held wording band
    run_rate_tolerance_pct: float = 10.0    # "spend back within this of the prior run-rate"
    vacuum_gain_pp: float = 2.0             # checkpoint for contesting vacated voice
    voice_parity_weeks: int = 4             # checkpoint horizon for buying voice up to share

    # ---- optional language layer and history ------------------------------
    free_year_min: int = 2020               # calendar years the numeral guard always allows
    free_year_max: int = 2034  # upper end of that calendar-year range
    narrative_max_length_ratio: float = 2.0  # a rewritten block may not exceed this x the template
    context_max_words: int = 120            # external-context caption cap
    history_depth: int = 12                 # snapshots kept per (advertiser, segment)
    diff_decimals: int = 2                  # an axis "moved" when it differs at this precision


DEFAULT_RCA_CONFIG = RcaConfig()
