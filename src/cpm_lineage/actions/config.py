"""Thresholds of the action rule families.

Every trigger, step size and stake share used by ``cpm_lineage.actions`` is a
named field here.  Money fields are written in reference-currency units and
are always read through ``Economics.money()`` (see ``primitives.money``), so
one ``currency_unit`` re-scales all of them.  Ratio fields that say
"x breakeven" are multiplied by the account's effective breakeven at use.
Positions assume the platform reports a rank where lower is better; the bands
are plain numbers so an adapter with a different page layout can move them.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ActionsConfig", "FAMILY_PRIORITY", "GOAL_BIAS", "COOLDOWN_KINDS"]

# Deterministic conflict priority per family (consumed by the plan pipeline).
FAMILY_PRIORITY: dict[str, float] = {
    "defense": 4.0, "supply": 3.5, "efficiency-cut": 3.0, "collision": 2.5, "growth": 2.0,
    "sov-gap": 2.0, "geo": 1.8, "efficiency-scale": 1.5, "bid": 1.2, "experiment": 1.0,
}

# Plan-ordering bias per goal metric; never flips a veto or a priority.
GOAL_BIAS: dict[str, dict[str, float]] = {
    "total_sales": {"growth": 2.0, "efficiency-scale": 1.0},
    "roas": {"efficiency-cut": 2.0, "collision": 1.5, "bid": 1.0},
    "direct_roas": {"efficiency-cut": 2.0, "bid": 1.0},
    "ad_sov": {"defense": 1.5, "bid": 1.5, "sov-gap": 2.0, "growth": 1.0},
    "ad_revenue": {"efficiency-scale": 2.0, "growth": 1.5},
    "ad_spend": {"efficiency-cut": 2.5},
}

COOLDOWN_KINDS: tuple[str, ...] = ("cut", "defend", "biddown")


@dataclass(frozen=True)
class ActionsConfig:
    """Every threshold of the rule families (every field has a default)."""

    # ---- scope ----
    max_segments: int = 6                       # segments analysed per run, top by spend: keeps a plan reviewable
    scoped_bench_min_n: int = 12                # verdict profile, user-scoped run: thinner role cohorts abstain

    # ---- classic budget cut ----
    cut_roas_mult: float = 0.9                  # cut when ROAS < 0.9 x breakeven: sustained loss, not noise at the line
    cut_min_spend: float = 25_000.0             # money: only campaigns at scale earn a cut card
    cut_pct: float = 20.0                       # fixed step: small enough to read out, large enough to matter
    cut_cooldown_days: int = 7                  # a cut needs a week to read out; stops cuts compounding on their echo

    # ---- classic budget scale ----
    scale_min_mult: float = 3.0                 # scale band floor, x breakeven: clearly proven
    scale_max_mult: float = 12.0                # band ceiling: above it ROAS is usually carried by own-brand terms
    scale_high_own_share_max: float = 0.5       # the ceiling lifts only when own-brand keyword share is known and below this
    scale_min_spend: float = 10_000.0           # money: evidence floor for scaling and for verdict-profile judging
    scale_certainty_mult: float = 2.0           # lower ROAS bound must hold 2 x breakeven: one lucky week buys nothing
    scale_haircut: float = 0.9                  # diminishing-returns haircut on the computed raise
    scale_pct_min: float = 5.0                  # raise clamp floor, percent
    scale_pct_max: float = 50.0                 # raise clamp ceiling: survive a mis-read exhaust hour
    scale_stake_roas_cap_mult: float = 6.0      # stake counts ROAS up to 6 x breakeven only

    # ---- verdict profile: unified threshold and band ladder ----
    raise_margin: float = 0.10                  # raise only when ROAS beats the bar by >= 10% (dead-zone edge)
    dead_zone_low: float = 0.90                 # ratio >= 0.90 of bar: no budget move
    deep_band_max: float = 0.50                 # ratio < 0.50 of bar: budget cut and bid cuts together
    exit_needed_min: float = 0.40               # > 40% below bar and below breakeven = mischosen, start exiting
    exit_step_pct: float = 30.0                 # first step of an exit
    cut_step_min: float = 0.10                  # proportional cut clamp floor (share of the shortfall)
    cut_step_max: float = 0.30                  # proportional cut clamp ceiling
    sufficiency_floor: float = 0.05             # floor on sufficiency inside the raise formula (avoids divide-by-zero)
    console_cut_tolerance: float = 0.5          # money: latest cap lower than the cap 3 days ago by more than this = already cut
    bid_probe_days: int = 14                    # an applied bid cut holds the next one for two weeks
    reread_days: int = 5                        # days before a verdict-profile move is re-read
    drag_roas_mult: float = 0.90                # drag keyword: ROAS below 0.90 x bar
    drag_roas_mult_top_volume: float = 0.80     # wider tolerance for top-search-volume terms
    drag_min_spend: float = 5_000.0             # money: drag keyword evidence floor
    drag_max_listed: int = 6                    # keywords listed on a campaign bid-cut card
    new_user_top_share: float = 0.25            # top 25% of terms by new users per (segment, bucket): never bid-cut
    volume_top_share: float = 0.10              # top 10% by search volume: wider dead-zone
    daypart_stake_share: float = 0.10           # day-part experiment stake = 10% of spend

    # ---- keyword pause ----
    brand_floor_mult: float = 3.0               # own/sibling-brand terms carry 3 x the bar: brand ROAS is cheap
    pause_conquest_min_spend: float = 10_000.0  # money: waste concentrates in conquest terms, so a lower floor
    pause_other_roas_mult: float = 0.35         # classic, non-conquest: ROAS <= 0.35 x floor
    pause_other_min_spend: float = 25_000.0     # money
    rescue_bucket_min_spend: float = 50_000.0   # money: bucket must be big enough to define "normal for its kind"
    rescue_bucket_avg_mult: float = 0.5         # bucket average must itself be >= 0.5 x floor (no self-blessing)
    rescue_keyword_mult: float = 0.6            # keyword within 0.6 x its bucket average is normal, not waste
    pause_verdict_mult: float = 0.50            # verdict: ROAS <= 0.50 x bar ...
    pause_verdict_conquest_mult: float = 0.60   # ... 0.60 x for conquest terms, both capped at breakeven
    pause_verdict_min_spend: float = 10_000.0   # money
    pause_max_listed: int = 5                   # keywords listed on a pause card
    conquest_keep_roas_mult: float = 1.5        # wording only: conquest terms at >= 1.5 x breakeven are kept

    # ---- growth ----
    growth_min_pct: float = 5.0                 # segment sales growth that counts as a tide
    growth_share_delta_min_pp: float = -0.1     # own share must not be falling
    growth_roas_min_mult: float = 2.0           # candidate band floor, x breakeven (ceiling = scale_max_mult)
    growth_raise_pct: float = 10.0              # fixed raise
    growth_stake_share: float = 0.10            # stake = own sales x growth x 10%
    growth_keyword_segments: int = 2            # add-keyword cards only for the biggest segments
    growth_keyword_max_terms: int = 5           # terms kept from the recommender's first 8
    growth_keyword_ask: int = 8                 # terms read from the recommender output

    # ---- defense ----
    gainer_min_pp: float = 0.3                  # rival share gain that counts
    entrant_prior_share_max: float = 0.5        # percent: was negligible ...
    entrant_current_share_min: float = 2.0      # ... and is now material
    defend_share_drop_pp: float = 0.3           # own share loss that triggers a defend raise
    moat_min_keyword_spend: float = 5_000.0     # money: moat campaign evidence floor
    moat_min_own_share: float = 0.4             # moat = at least 40% of keyword spend on own/sibling terms
    defend_raise_pct: float = 15.0              # fixed raise
    defend_cooldown_days: int = 14              # one defend raise per campaign per fortnight (anti-spoof)

    # ---- bid ----
    head_min_spend: float = 50_000.0            # money: head-term pressure check only on a real head term
    head_cpm_rise: float = 1.10                 # geo CPM up > 10% counts as pressure
    head_min_geos: int = 3                      # pressure must be broad
    head_yield_pct: float = 15.0                # bid-down when the head term cannot afford the fight
    head_stake_share: float = 0.10              # stake = 10% of head-term spend
    bid_min_spend_classic: float = 25_000.0     # money
    bid_min_spend_verdict: float = 2_500.0      # money: a raise is reversible, so a low floor
    bid_cut_min_spend_verdict: float = 5_000.0  # money: cuts need more evidence than raises
    bid_up_bucket_mult: float = 1.2             # classic up: beats its own kind by 20% ...
    bid_up_be_mult: float = 1.5                 # ... and clears 1.5 x breakeven
    bid_up_min_position: float = 5.0            # classic up: position worse than 5 (unseen)
    bid_step_pct: float = 15.0                  # classic step, both directions
    bid_down_be_mult: float = 0.8               # classic down: below 0.8 x breakeven ...
    bid_down_bucket_mult: float = 0.5           # ... and below half its bucket average
    bid_top_slot: float = 2.0                   # classic down only at position <= 2 (paying the top-slot premium)
    bid_settle_position: float = 3.0            # wording only: where a bid-down should settle
    bid_raise_min_position: float = 6.0         # verdict raise: position worse than 6
    bid_headroom_min: float = 0.08              # verdict raise: headroom under 8% is not worth a move
    bid_raise_cap: float = 0.25                 # verdict raise step ceiling
    bid_raise_cap_deep: float = 0.40            # ceiling when buried (position >= bid_deep_position)
    bid_deep_position: float = 11.0             # buried: off the first screen
    rented_owner_share: float = 0.60            # a rival owning >= 60% of a term cancels the deep escalation
    bid_cut_max_position: float = 9.0           # verdict cut only while still visible; never below the fold
    bid_dead_zone: float = 0.10                 # verdict cut: shortfall under 10% is noise
    bid_dead_zone_top_volume: float = 0.20      # 20% for top-search-volume terms
    bid_fixed_step_pct: float = 10.0            # raise step ceiling where the auction is not pay-your-bid

    # ---- experiment ----
    experiment_exhaust_minutes: float = 600.0   # budget gone before 10:00 local
    experiment_credible_spend_fraction: float = 0.5  # exhaustion is believable only if spend >= half the cap
    experiment_roas_min_mult: float = 0.8       # undecided band, x breakeven
    experiment_roas_max_mult: float = 2.5  # undecided band ceiling, x breakeven: above it the entity is a clear winner, not a test
    experiment_min_days: int = 14               # young campaigns do not need the test
    experiment_days: int = 7                    # test length
    experiment_success_pct: float = 15.0        # worst-slot ROAS improvement that counts as success
    experiment_stake_share: float = 0.10  # stake = 10% of the entity's window spend
    experiment_keywords: int = 5                # keywords attached to an experiment card

    # ---- sov-gap ----
    sov_under_gap_pp: float = 8.0               # market share above ad share of voice by 8 points
    sov_under_min_worth: float = 50_000.0       # money: 8 points of a tiny segment is noise money
    sov_roas_min_mult: float = 1.5              # candidate band floor (ceiling = scale_max_mult)
    sov_generic_own_share_max: float = 0.4      # prefer generic-heavy campaigns
    sov_raise_pct: float = 15.0  # fixed raise on the share-of-voice gap card, percent
    sov_stake_share: float = 0.10  # stake = segment sales x gap x 10%
    sov_over_gap_pp: float = 10.0               # share of voice above market share by 10 points
    sov_over_blended_mult: float = 2.0          # ... and blended ROAS below 2 x breakeven
    sov_over_stake_share: float = 0.15  # stake of the over-invested card = 15% of the segment's ad spend

    # ---- geo ----
    geo_row_min_spend: float = 2_000.0          # money: campaign x geo rows below this are ignored
    geo_top_entities: int = 8                   # campaigns scanned, top by spend
    geo_min_spend: float = 50_000.0             # money: campaign must be big enough to hide a geography
    geo_min_rows: int = 3  # a campaign needs at least this many geo rows before one can be called weak
    geo_weak_spend_share: float = 0.15          # weak geo must carry >= 15% of the campaign's spend
    geo_weak_roas_mult: float = 0.6             # ... at < 0.6 x the campaign average
    geo_decisive_roas_mult: float = 0.4         # below 0.4 x = decisive (split); in between = experiment
    geo_experiment_days: int = 14  # length of the geo carve test, days
    geo_controls: int = 2                       # matched control geos
    geo_experiment_stake_share: float = 0.20  # stake = 20% of the weak geo's spend
    geo_control_default_availability: float = 0.85  # availability assumed when a geo does not report it
    geo_expand_share_gap_pp: float = 3.0        # own share at least 3 points under the blend
    geo_expand_min_volume_share: float = 0.03   # geo must hold >= 3% of segment volume
    geo_expand_min_availability: float = 0.85   # classic profile only; verdict does not gate media on availability

    # ---- collision ----
    collision_min_spend: float = 10_000.0       # money: per campaign per geo
    collision_stake_share: float = 0.15         # stake = 15% of the non-best spend

    # ---- operator floors ----
    operator_floor_min: float = 0.1             # stated floors outside [0.1, 100] are discarded
    operator_floor_max: float = 100.0  # upper bound of an accepted operator floor (see operator_floor_min)
