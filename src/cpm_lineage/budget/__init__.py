"""Budget subsystem: sufficiency and required budget, saturation curve and
marginal ROAS, raise gating, scale / tighten / release moves, the
budget-bound policy, ad-type mix and the three (unreconciled) money
estimators."""

from .config import BudgetConfig, money_of
from .estimators import (ESTIMATORS, EVEstimate, MarginalAdjustment, MoneyEstimate, apply_marginal,
                         compare_estimators, curve_increment, exhaust_raise_pct, expected_value,
                         finite_difference_marginal, marginal_roas_by_entity, stake_budget_cut,
                         stake_raise_exhaust, stake_raise_underserved)
from .mix import (DragRow, MixAction, MixRecommendation, MixResult, TypeMix, ad_type_mix,
                  attach_plateau, entity_drag, recommend_mix)
from .moves import (BudgetMoves, MoveInput, ReleaseMove, ScaleMove, TightenMove, budget_moves,
                    budget_moves_from_rows, move_inputs, size_raise)
from .probe import (CURVE_OWN, CURVE_OWN_LOWCONF, CURVE_POOLED, BudgetProbe, RaiseVerdict,
                    ServingEvidence, probe_budget, raise_verdict, recommended_range,
                    revenue_at_stake, stepwise_evidence, utilisation_ladder)
from .saturation import (FAIL_REASONS, CurveFit, WhatIfPoint, daily_series, fit_curve,
                         fit_entity_curve, fit_pooled_curve, marginal, marginal_raw, pooled_points,
                         predict, whatif)
from .sufficiency import (SufficiencyReport, SufficiencyRow, budget_allocation,
                          collapse_entity_days, day_sufficiency, derive, filter_rows_by_tags,
                          is_oob_day, summarise_oob, tag_key)
from .telemetry import (bound_method_for_profile, build_budget_telemetry, is_budget_bound,
                        utilisation)

_MODULES = {"config", "estimators", "mix", "moves", "probe", "saturation", "sufficiency", "telemetry"}
__all__ = [n for n in dir() if not n.startswith("_") and n not in _MODULES]
