"""Layer 2: attributing a ROAS move to levers.

Default path: pooled sign-constrained ridge + first-difference cross-check +
jury (:func:`attribute`).  Optional path: exact Shapley over any fitted
model (:func:`exact_shapley`, :func:`shapley_attribute`).
"""

from .collinearity import PRICE_CLUSTER, fold_price_cluster, vif
from .config import AttributionConfig
from .jury import (NO_STORY_VERDICTS, VERDICTS, Attribution, Driver, attribute, channel_split, confidence,
                   demand_evidence, explained_share, jury_label, move_skill, size_move, verdict)
from .levers import (DRIVER_CLASS, LEVERS, SHORT_KEEP, TIER1, TIER2, LeverPanel, PreparedLevers,
                     build_lever_panel, derive_levers, is_tier2, prepare_levers, raw_facts, sanity)
from .ridge import (CHANNEL_SIGN, CHANNELS, SIGNS, Prior, adstock, bounded_ridge, channel_logs, choose_lambda,
                    delta_skill, fit_delta, fit_prior, sign_bounds, time_series_splits)
from .shapley import BoostedTrees, exact_shapley, shapley_attribute

__all__ = [
    "AttributionConfig", "Attribution", "Driver", "LeverPanel", "PreparedLevers", "Prior", "BoostedTrees",
    "LEVERS", "TIER1", "TIER2", "DRIVER_CLASS", "SHORT_KEEP", "PRICE_CLUSTER", "CHANNELS", "CHANNEL_SIGN",
    "SIGNS", "VERDICTS", "NO_STORY_VERDICTS",
    "attribute", "build_lever_panel", "prepare_levers", "derive_levers", "sanity", "raw_facts", "is_tier2",
    "vif", "fold_price_cluster", "bounded_ridge", "sign_bounds", "time_series_splits", "choose_lambda",
    "channel_logs", "fit_prior", "adstock", "fit_delta", "delta_skill",
    "explained_share", "jury_label", "move_skill", "verdict", "confidence", "channel_split",
    "demand_evidence", "size_move", "exact_shapley", "shapley_attribute",
]
