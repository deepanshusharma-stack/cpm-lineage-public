"""cpm-lineage: optimisation and diagnosis for CPM-billed ad platforms.

Quick start::

    from cpm_lineage import SyntheticAdapter, diagnose, plan, render_report

    data = SyntheticAdapter().dataset
    print(render_report(diagnose(data), plan(data)))

Layout
------
``schema``, ``config``, ``metrics``, ``windows``, ``liveness``
    Neutral records, shared configuration, metric algebra, windows, liveness.
``adapters``
    ``PlatformProfile`` and the CSV, DataFrame and synthetic adapters.
``identity``, ``decompose``, ``attribution``, ``recommend``
    Exact ROAS identity, shift-share / Pareto / composition, lever
    attribution, per-entity recommendation.
``budget``, ``cpm``, ``keywords``, ``market``, ``geo``, ``scoping``, ``drill``
    Budget sufficiency and saturation, CPM and auction views, keyword
    roles and recommenders, market and share-of-voice reads, geo reads.
``actions``, ``verdict``, ``conflicts``, ``outcomes``, ``store``
    Rule families, plan pipeline, typed verdicts, outcome pairing, ledger.
``coldstart``, ``prescribe``, ``screen``, ``watchdog``, ``queue``, ``scorecard``
    Cold-start pre-flight, one-row prescription, pre-screen, tripwires,
    the unified action queue, the checkpoint scorecard.
``rca``
    Market root-cause analysis for "why did sales or share move".
``engine``
    ``diagnose`` and ``plan``: the orchestration over all of the above.

The names below are the stable top-level API; everything else is imported
from its subpackage.
"""

from . import (actions, attribution, budget, coldstart, conflicts, cpm, decompose, drill, geo,
               identity, keywords, liveness, market, metrics, outcomes, prescribe, queue, rca,
               recommend, scoping, scorecard, screen, store, verdict, watchdog, windows)
from .actions.config import ActionsConfig
from .actions.pipeline import Plan
from .actions.pipeline_config import PipelineConfig
from .adapters.base import build_records
from .cpm.config import CpmConfig
from .verdict import VerdictConfig
from .adapters import (AdTypeCapability, BaseAdapter, Dataset, PlatformAdapter, PlatformProfile,
                       PositionSemantics)
from .adapters.csv import CsvAdapter
from .adapters.dataframe import DataFrameAdapter
from .adapters.synthetic import SyntheticAdapter
from .config import (AvailabilityPolicy, BudgetBoundPolicy, Economics, EngineConfig, LivenessConfig,
                     PositionConfig, TierConfig, TradingDay, WindowDefaults, apply_overrides)
from .engine import (FINDING_KINDS, Diagnosis, EntityDiagnosis, Finding, OrchestrationConfig,
                     as_dataset, diagnose, plan, render_report)
from .schema import (ActionCard, AppliedAction, AuctionType, BidDecision, BidRecord, BudgetDayRow,
                     BudgetTelemetry, CanonicalStatus, CapabilityClass, Change, ChangeEvent, Entity, Lever,
                     MarketRow, MetricCell, OfftakeRow, PerfRow, Product, RoleCohort, RunCheck, TargetingRole,
                     ShelfRow, SuggestedBid, TargetingUnit, Window, WindowPair, make_card)
from .windows import default_windows, resolve_window, trailing_window, trailing_windows

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # orchestration
    "diagnose", "plan", "render_report", "as_dataset", "Diagnosis", "EntityDiagnosis", "Finding",
    "FINDING_KINDS", "OrchestrationConfig", "Plan",
    # configuration
    "EngineConfig", "Economics", "AvailabilityPolicy", "BudgetBoundPolicy", "LivenessConfig",
    "PositionConfig", "TierConfig", "TradingDay", "WindowDefaults", "apply_overrides",
    "ActionsConfig", "PipelineConfig", "VerdictConfig", "CpmConfig",
    # enums (pass these instead of bare strings)
    "CanonicalStatus", "CapabilityClass", "TargetingRole", "AuctionType", "Lever",
    # platform and adapters
    "PlatformProfile", "AdTypeCapability", "PositionSemantics", "PlatformAdapter", "BaseAdapter",
    "Dataset", "CsvAdapter", "DataFrameAdapter", "SyntheticAdapter", "build_records",
    # records
    "PerfRow", "Entity", "TargetingUnit", "Product", "MarketRow", "OfftakeRow", "ShelfRow",
    "BudgetDayRow", "BudgetTelemetry", "ChangeEvent", "BidRecord", "BidDecision", "SuggestedBid",
    "AppliedAction", "RunCheck", "RoleCohort", "Window", "WindowPair", "MetricCell", "ActionCard", "Change", "make_card",
    # windows
    "trailing_window", "trailing_windows", "default_windows", "resolve_window",
    # subsystems
    "actions", "attribution", "budget", "coldstart", "conflicts", "cpm", "decompose", "drill", "geo",
    "identity", "keywords", "liveness", "market", "metrics", "outcomes", "prescribe", "queue", "rca",
    "recommend", "scoping", "scorecard", "screen", "store", "verdict", "watchdog", "windows",
]
