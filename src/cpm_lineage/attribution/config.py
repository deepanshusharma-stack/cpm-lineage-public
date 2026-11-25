"""Thresholds for lever preparation, the collinearity guard, the pooled ridge,
the jury / verdict and move sizing.

Every value is a named field with a default and a one-line
rationale.  Nothing here is a money amount; money floors used by the
recommendation gate stack live in ``cpm_lineage.recommend.RecommendConfig``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttributionConfig:
    """Named thresholds of the lever-attribution layer (every field has a default)."""

    # --- feature preparation -------------------------------------------------
    min_dln: float = 1e-4                   # below this |d ln ROAS| the share denominator falls back (model total, then this)
    log_floor: float = 1e-9                 # floor inside ln() of a channel value; avoids ln(0)
    coverage_min: float = 0.20              # lever is "live" only above this share of real, non-zero days (judged before imputation)
    impute_max: float = 0.40                # lever is suppressed above this share of imputed days (model would mostly see synthetic values)
    thin_min_levers: int = 4                # fewer live levers than this -> no entity fit, account elasticities only
    position_sanity_min: float = 1.0        # positions below this are data errors -> missing
    position_sanity_max: float = 500.0      # positions above this are mis-joined fields -> missing
    share_min: float = 0.0                  # share-type levers must lie in [share_min, share_max]
    share_max: float = 1.0                  # share-type levers must lie in [share_min, share_max]
    discount_min: float = -0.5              # a "discount" below -50% is a price above list by half: data error
    discount_max: float = 0.95              # a discount above 95% is a data error
    price_gap_min: float = 0.1              # own/competitor price ratio below 0.1 is a unit mismatch
    price_gap_max: float = 10.0             # own/competitor price ratio above 10 is a unit mismatch
    adversarial_role: str = "competitor"    # targeting role whose spend share is the targeting-mix lever
    zero_is_real: tuple[str, ...] = ("own_discount", "comp_discount")  # levers where a true 0 is a valid observation, not "missing"

    # --- collinearity guard --------------------------------------------------
    vif_max: float = 8.0                    # price-cluster VIF at or above this folds the discounts into price_gap
    vif_min_rows: int = 8                   # fewer complete rows than this -> VIF reported as 1.0 (cannot judge)
    vif_r2_floor: float = 1e-9              # floor on 1 - R^2 so a perfect fit yields a huge, finite VIF

    # --- pooled sign-constrained ridge ---------------------------------------
    sd_floor: float = 1e-9                  # floor on a lever's standard deviation when z-scoring
    lambdas: tuple[float, ...] = (1.0, 4.0, 16.0, 64.0)   # candidate pooling strengths chosen by blocked time-series CV
    default_lambda: float = 4.0             # used when CV cannot discriminate; also the account-prior strength
    short_lambda: float = 16.0              # fixed heavy pooling in short mode (tiny panel)
    prior_lambda: float = 4.0               # ridge strength of the account-level prior fit (toward zero)
    cv_splits: int = 4                      # blocked time-series folds in full mode
    cv_splits_short: int = 3                # blocked time-series folds in short mode
    cv_min_train: int = 10                  # folds with fewer training rows are dropped
    cv_min_test: int = 3                    # folds with fewer test rows are not scored
    solver_max_sweeps: int = 5000           # coordinate-descent sweep cap of the bounded solver
    solver_tol: float = 1e-12               # largest coefficient change that still counts as "not converged"

    # --- first-difference cross-check ----------------------------------------
    delta_lambda: float = 4.0               # ridge strength of the first-difference model
    delta_min_rows: int = 12                # difference model and adstock variants need at least this many rows
    adstock_lambdas: tuple[float, ...] = (0.2, 0.4)       # geometric carry-over rates tried for visibility levers
    adstock_margin: float = 0.05            # a carry-over variant must beat the current best skill by more than this
    skill_min_predictions: int = 5          # fewer one-step predictions -> skill undefined
    skill_min_sse: float = 1e-8             # persistence error below this means a constant channel -> skill undefined

    # --- jury, verdict, explained share --------------------------------------
    jury_materiality: float = 0.05          # a model "votes" on a lever only at >= 5% of the move
    explained_floor_pct: float = 20.0       # |net explained| below this -> verdict "unexplained"
    skill_floor: float = -0.25              # move skill below this (clearly worse than persistence) -> "unexplained"
    confirmed_min_skill: float = 0.0        # "confirmed" needs the cross-check to be at least as good as persistence
    small_move_dln: float = 0.08            # |log ROAS move| below this may be a "small move"
    small_move_gross_mult: float = 2.0      # ... when gross lever activity exceeds this multiple of the move
    chip_min_lever_pct: float = 3.0         # no per-channel split for levers under 3% of the move
    chip_min_channel_pct: float = 5.0       # a channel is shown at >= 5% of the move
    chip_min_rel: float = 0.15              # ... and >= 15% of the lever's largest channel

    # --- demand-evidence gate (tier-2 levers) --------------------------------
    evidence_min_rows: int = 14             # days with both series positive needed to judge a demand lever
    evidence_min_r: float = 0.35            # |correlation| below this is "no-link"
    evidence_half_r: float = 0.15           # both half-sample correlations must reach this, same sign, to be "stable"
    evidence_lag_r: float = 0.55            # a no-link with |lag-1 correlation| at or above this is "leading"

    # --- elasticity-sized moves ----------------------------------------------
    size_bid_step: float = 0.10             # bid test step: 10% multiplicative
    size_price_gap_step: float = 0.05       # relative price test step: 5% multiplicative
    size_position_step: float = 1.0         # position test step: one slot
    size_share_step: float = 0.05           # share-type levers test step: 5 points, clamped to [0, 1]
    size_min_beta: float = 1e-3             # net elasticity smaller than this is not sized
    size_min_draw: float = 0.005            # raw step smaller than this (after clamping) is not sized
    size_min_droas: float = 0.02            # ROAS effect below this is noise, suppressed
    size_max_droas_share: float = 0.6       # ROAS effect above 60% of current ROAS is implausible, suppressed
    size_roas_floor: float = 0.1            # floor on current ROAS inside the plausibility bound

    # --- confidence label ----------------------------------------------------
    conf_high_min_days: int = 60            # High needs two full comparable windows
    conf_high_max_imputed: float = 0.25     # High needs at most a quarter of live-lever days imputed
    conf_low_max_days: int = 35             # fewer days than this -> Low
    conf_low_min_imputed: float = 0.45      # more imputation than this -> Low

    # --- raw facts -----------------------------------------------------------
    fact_min_days: int = 3                  # each window needs this many real days of a lever to state a fact
    fact_share_points: float = 0.07         # share-type levers: fact at a 7-point difference of window means
    fact_bid_rel: float = 0.20              # bid: fact at a 20% relative change
    fact_other_rel: float = 0.25            # other levers: fact at a 25% relative change
    fact_align_rel: float = 0.10            # aligned channel is mentioned when it moved at least 10%
    fact_max: int = 7                       # cap on the number of facts
    sov_drop_points: float = 0.05           # paid share-of-voice fall that triggers the denominator split
    sov_pool_up: float = 0.10               # ad pool per day up more than this -> rivals scaled in
    sov_own_down: float = 0.10              # own ad impressions per day down more than this -> own pull-back
    displacement_impr_up: float = 0.20      # impressions per day up this much with falling share -> deeper slots

    # --- exact Shapley (optional attributor) ---------------------------------
    shapley_max_features: int = 18          # exact enumeration costs 2^n model rows; 18 -> 262,144
