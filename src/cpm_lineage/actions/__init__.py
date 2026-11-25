"""Action generation: rule families emit candidate cards, the plan pipeline
resolves, scores, gates and assembles them.

``primitives``      window aggregates, the ``ActionContext`` and shared rules
``families``        the rule families (``run_families``)
``resolve``         supply gate, duplicate merge, conflict table
``scoring``         confidence, expected value, marginal sizing
``guardrail``       run check (ROAS floor / spend ceiling) and equity gate
``rebalance``       budget-neutral funding of raises
``pipeline``        holdout, scrubs, vetoes, caps, waves, ``build_plan``
``bridge``          ``ActionContext`` -> ``PlanContext`` (one book, two views)
"""

from .bridge import plan_context_from_actions, scoreboard_entries
from .config import COOLDOWN_KINDS, FAMILY_PRIORITY, GOAL_BIAS, ActionsConfig
from .families import FAMILY_RULES, FamilyRun, run_families
from .pipeline import Plan, PlanResult, build_plan, plan_actions
from .pipeline_config import EntityState, PipelineConfig, PlanContext, SegmentStat, TermOwner
from .primitives import ActionContext, context_from_dataset

__all__ = [
    "ActionsConfig", "FAMILY_PRIORITY", "GOAL_BIAS", "COOLDOWN_KINDS",
    "ActionContext", "context_from_dataset", "FAMILY_RULES", "FamilyRun", "run_families",
    "PipelineConfig", "PlanContext", "EntityState", "SegmentStat", "TermOwner",
    "Plan", "PlanResult", "build_plan", "plan_actions",
    "plan_context_from_actions", "scoreboard_entries",
]
