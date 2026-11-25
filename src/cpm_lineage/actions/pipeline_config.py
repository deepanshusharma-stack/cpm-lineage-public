"""Configuration and input records of the post-rule action pipeline.

The pipeline takes the candidate ``ActionCard`` list emitted by the rule
families and decides which cards a human may act on, in what order and with
what confidence.  This module holds

* ``PipelineConfig``: every threshold of the pipeline as a named field
  with a default;
* the plain input records the pipeline reads (``EntityState``,
  ``SegmentStat``, ``TermOwner``, ``RunCheck``, ``PlanContext``);
* reason codes, so telemetry counts gates by code and never by matching
  prose.

Units: availability is 0..1 (values above 1.5 are read as percent by
``AvailabilityPolicy.normalise``); share-of-voice and market share are
percent (0..100); money thresholds are reference-currency numbers scaled by
``Economics.money``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping

from ..adapters.base import PlatformProfile
from ..config import AvailabilityPolicy, BudgetBoundPolicy, Economics
from ..schema import ActionCard, BudgetTelemetry, RunCheck
from .config import FAMILY_PRIORITY, GOAL_BIAS

__all__ = [
    "PRIORITY", "GOAL_BIAS", "FAMILY_CAPS", "PipelineConfig", "RunCheck", "EntityState",
    "SegmentStat", "TermOwner", "PlanContext", "R_UNDER_TEST", "R_UNSTATED", "R_UNFUNDABLE",
    "R_CAPABILITY", "R_SUPPLY", "R_GUARDRAIL", "R_EQUITY", "R_PRIORITY", "R_BID_CONFLICT",
    "R_TENSION", "R_MARGINAL", "R_PARTITIONED", "veto_card", "is_live", "reason_code",
    "pauses_entity", "change_bounds",
]

# Deterministic family priority: the winner of a budget-grain contradiction.
PRIORITY: dict[str, float] = FAMILY_PRIORITY   # one table, defined in actions.config

# Pending-card caps per (family, lever type): reviewability.
FAMILY_CAPS: dict[tuple[str, str], int] = {
    ("efficiency-cut", "budget"): 8, ("efficiency-cut", "keyword"): 8, ("bid", "bid"): 6,
    ("growth", "budget"): 5, ("growth", "keyword"): 3, ("experiment", "experiment"): 3,
    ("sov-gap", "budget"): 4, ("sov-gap", "watch"): 2, ("geo", "geo"): 6,
    ("collision", "keyword"): 6, ("defense", "watch"): 3, ("efficiency-scale", "budget"): 6,
    ("defense", "budget"): 5,
}

# Reason codes written to ``card.extras["reason_code"]``.
R_UNDER_TEST = "under_test"
R_UNSTATED = "unstated_budget"
R_UNFUNDABLE = "not_budget_bound"
R_CAPABILITY = "capability_playbook"
R_SUPPLY = "supply_gate"
R_GUARDRAIL = "guardrail"
R_EQUITY = "equity_gate"
R_PRIORITY = "priority_table"
R_BID_CONFLICT = "bid_conflict"
R_TENSION = "tension"
R_MARGINAL = "marginal_gate"
R_PARTITIONED = "partitioned"


@dataclass(frozen=True)
class PipelineConfig:
    """Every threshold of the post-rule pipeline (every field has a default)."""

    # ---- holdout ----
    holdout_pct: float = 15.0                    # share of eligible entities receiving no cards: standing counterfactual
    holdout_period: str = "month"                # "month" | "week": the assignment is stable within a period, then rotates
    # ---- vetoes ----
    under_test_days: int = 14                    # an applied experiment blocks other moves this long: concurrent moves confound it
    budget_bound_method: str | None = None       # None = by rule profile ("exhaustion" classic, "sufficiency" verdict)
    supply_gate_enabled: bool | None = None      # None = by rule profile (on for classic/marginal, off for verdict)
    # ---- conflict table ----
    priority: Mapping[str, float] = field(default_factory=lambda: dict(PRIORITY))  # deterministic table, never a model
    stake_ratio_valve: float = 3.0               # loser stake > 3 x winner stake: abstain, a table should not pre-decide it
    valve_min_winner_stake: float = 1.0          # floor on the winner stake in the ratio (avoids divide-by-zero)
    valve_counts_allies: bool = False            # False = only the opposing side can trip the valve (intended); True = allies count too
    merge_keeper: str = "higher_stake"           # "higher_stake" (first-seen tiebreak) | "first_seen"
    contested_min_ratio: float = 0.30            # collision cards below this share of contested days are a schedule, not a collision
    # ---- confidence ----
    conf_base: float = 40.0                      # additive score starts here
    conf_min: float = 25.0                       # lower bound: never claim nothing is known
    conf_max: float = 90.0                       # upper bound: never claim certainty
    conf_long_window_days: int = 30              # a lookback at least this long earns the full window credit
    conf_long_window: float = 15.0               # credit for a long evidence window
    conf_short_window: float = 8.0               # credit for a shorter window
    conf_spend_per_point: float = 20_000.0       # money: one point per this much evidence spend
    conf_spend_max: float = 10.0                 # spend-depth credit ceiling
    conf_clarity_far: float = 0.5                # |ROAS - breakeven| at least this: effect is clear
    conf_clarity_far_pts: float = 15.0           # credit for a clear effect
    conf_clarity_mid: float = 0.25               # |ROAS - breakeven| at least this: effect is visible
    conf_clarity_mid_pts: float = 8.0            # credit for a visible effect
    conf_clarity_near_pts: float = 3.0           # credit when ROAS sits at the line
    conf_keyword_down: float = 12.0              # keyword pause: pure waste, little inference
    conf_bid: float = 8.0                        # bid card: rank-economics signal
    conf_experiment: float = 5.0                 # experiment: a test, not a bet
    conf_geo: float = 10.0                       # geo card: arithmetic, not inference
    conf_collision: float = 12.0                 # collision: structural
    conf_watch: float = 4.0                      # watch card: informational
    conf_avail_broken: float = -8.0              # availability below the caution band
    conf_avail_shaky: float = -5.0               # availability between caution and floor
    conf_avail_unmeasured: float = -8.0          # scale-up in a segment with no availability reading
    conf_young_days: int = 14                    # entities live for fewer days are young
    conf_young: float = -10.0                    # young-entity penalty: history too short
    winrate_min_n: int = 5                       # realised family win rate is used only with this many paired outcomes
    conf_winrate_scale: float = 20.0             # points per unit of (rate - 0.5)
    conf_winrate_max: float = 10.0               # win-rate swing bound
    conf_precedent_scale: float = 16.0           # points per unit of (won/n - 0.5) for this family on this entity
    conf_precedent_max: float = 8.0              # precedent swing bound
    # ---- expected value ----
    ev_prior: float = 0.5                        # P(success) without a realised win rate: flat and auditable
    ev_use_effective_breakeven: bool = False     # False = bleed avoided uses literal 1 - ROAS (the default); True = breakeven - ROAS
    # ---- guardrail ----
    spend_base_floor: float = 1.0                # spend-ceiling base when no budget telemetry exists
    # ---- equity gate ----
    equity_own_share_min: float = 0.4            # entity carries the brand moat at this own-brand keyword spend share
    equity_share_loss_pp: float = -0.2           # own market share change below this (pp) = already losing share
    equity_freefall_pct: float = -10.0           # segment total below this: gate stands down, do not ride a dying segment
    # ---- annotations ----
    posture_saturation_share: float = 60.0       # own market share (pct) at which growth has a steep marginal cost
    posture_decline_pct: float = -10.0           # segment growth below this: efficiency posture, not expansion
    term_owner_share: float = 0.60               # a brand taking this share of a term's organic impressions owns it
    term_crowded_advertisers: int = 8            # this many advertisers on a term = crowded auction
    # ---- caps ----
    family_caps: Mapping[tuple[str, str], int] = field(default_factory=lambda: dict(FAMILY_CAPS))  # reviewability
    family_cap_default: int = 99                 # cap for (family, type) pairs not listed
    pending_cap: int = 25                        # a plan an operator can review in one sitting
    polish_batch: int = 12                       # pending cards offered to the optional prose polisher
    polish_why_chars: int = 300                  # polished wording truncation
    polish_caution_chars: int = 200              # polished caution truncation
    # ---- marginal sizing ----
    marginal_min_days: int = 8                   # spending days needed for a finite-difference reading
    marginal_min_spend_gap: float = 100.0        # money per day: the two halves must differ by more than this
    marginal_veto_mult: float = 0.8              # raise vetoed when marginal ROAS < 0.8 x breakeven
    marginal_halve_mult: float = 1.5             # raise halved when marginal ROAS < 1.5 x breakeven
    marginal_min_pct: float = 5.0                # floor of a halved raise, percent
    marginal_deepen_mult: float = 0.5            # cut deepened when marginal ROAS < 0.5 x breakeven
    marginal_deepen_step: float = 5.0            # extra percent on a deepened cut
    marginal_deepen_floor: float = 25.0          # a deepened cut is at least this deep, percent
    # ---- budget-neutral rebalance ----
    rebalance_max_bleed: float = 0.30            # never take more than 30% of a donor's cap per step
    rebalance_min_pct: float = 5.0               # smaller donor steps are console noise
    rebalance_confidence: int = 66               # flat donor confidence: the swap, not the donor, is the thesis


@dataclass(frozen=True)
class EntityState:
    """What the pipeline needs to know about one entity (campaign)."""

    entity_id: str
    status: str | None = None             # raw platform status (mapped by the profile's status map)
    spend_3d: float | None = None         # spend over the last few days, for the soft-dead rule
    ad_type: str | None = None            # raw or canonical ad type (capability class)
    segment: str | None = None
    name: str | None = None
    eligible: bool = True                 # maps cleanly to one segment ("pure"); only eligible entities enter the holdout draw
    live_days: int | None = None          # days live; None = unknown (no young penalty)
    own_brand_share: float | None = None  # own + sibling brand share of keyword spend, 0..1


@dataclass(frozen=True)
class SegmentStat:
    """Segment-level market readings; every field is optional and fails open."""

    availability: float | None = None       # 0..1 (percent accepted)
    total_sales: float | None = None        # all brands, current window
    total_sales_prior: float | None = None
    own_sales: float | None = None          # own brand, current window
    own_sales_prior: float | None = None
    sov: float | None = None                # own share of ad voice, percent
    own_spend: float | None = None          # own ad spend in the segment over the lookback


@dataclass(frozen=True)
class TermOwner:
    """Who holds a keyword: organic owner and ad pressure (all optional)."""

    owner: str | None = None                  # brand taking the largest organic share
    owner_share: float | None = None          # 0..1 of the term's organic impressions
    owned_by_us: bool = False
    rival_ad_impressions: float | None = None  # strongest rival's ad impressions
    rival: str | None = None
    own_ad_impressions: float | None = None
    advertisers: int | None = None


@dataclass
class PlanContext:
    """Everything the pipeline reads besides the cards.  All maps may be empty.

    * ``under_test``: entity ids with an applied experiment inside the
      under-test window (see ``pipeline.under_test_entities``).
    * ``winrates``: ``{family: (rate, n)}`` realised win rates from a ledger.
    * ``precedent``: ``{(family, entity_id): {"n", "won", "line"}}``.
    * ``keyword_priority``: ``{(segment, keyword): rank}``, 0 = highest
      demand bucket (see ``pipeline.demand_priority``).
    * ``scope_entity_ids``: when set, the spend-ceiling base sums only these
      entities' budgets (a scoped run).
    * ``today``: injected clock (holdout period, plan date).
    """

    entities: Mapping[str, EntityState] = field(default_factory=dict)
    telemetry: Mapping[str, BudgetTelemetry] = field(default_factory=dict)
    segments: Mapping[str, SegmentStat] = field(default_factory=dict)
    under_test: frozenset[str] = frozenset()
    winrates: Mapping[str, tuple[float, int]] = field(default_factory=dict)
    precedent: Mapping[tuple[str, str | None], Mapping[str, Any]] = field(default_factory=dict)
    keyword_priority: Mapping[tuple[str | None, str], int] = field(default_factory=dict)
    term_owners: Mapping[str, TermOwner] = field(default_factory=dict)
    lookback_days: int = 30
    today: date | None = None
    economics: Economics = field(default_factory=Economics)
    profile: PlatformProfile = field(default_factory=PlatformProfile)
    availability_policy: AvailabilityPolicy = field(default_factory=AvailabilityPolicy)
    budget_policy: BudgetBoundPolicy = field(default_factory=BudgetBoundPolicy)
    rule_profile: str = "classic"
    cfg: PipelineConfig = field(default_factory=PipelineConfig)
    scope_entity_ids: frozenset[str] | None = None
    operator_floors: Mapping[str, float] = field(default_factory=dict)
    status_source: str = "ok"             # structured source-quality tag: "ok" | "degraded" | "missing"

    @property
    def be(self) -> float:
        """Effective breakeven ROAS (``Economics.be``)."""
        return self.economics.be

    @property
    def verdict(self) -> bool:
        return self.rule_profile == "verdict"

    @property
    def supply_gate_on(self) -> bool:
        """Whether availability gates and penalises anything in this run."""
        if not self.availability_policy.enabled:
            return False
        if self.cfg.supply_gate_enabled is not None:
            return self.cfg.supply_gate_enabled
        return not self.verdict

    def money(self, reference_amount: float) -> float:
        return self.economics.money(reference_amount)

    def availability(self, segment: str | None) -> float | None:
        """Segment availability on the 0..1 scale, ``None`` when unmeasured."""
        seg = self.segments.get(segment) if segment is not None else None
        if seg is None or seg.availability is None:
            return None
        return self.availability_policy.normalise(seg.availability)

    def ad_type(self, entity_id: str | None) -> str | None:
        """Ad type of an entity: budget telemetry first, entity record as fallback."""
        if entity_id is None:
            return None
        tel = self.telemetry.get(entity_id)
        if tel is not None and tel.ad_type:
            return tel.ad_type
        ent = self.entities.get(entity_id)
        return ent.ad_type if ent is not None else None

    def live_days(self, entity_id: str | None) -> int | None:
        if entity_id is None:
            return None
        ent = self.entities.get(entity_id)
        if ent is not None and ent.live_days is not None:
            return ent.live_days
        tel = self.telemetry.get(entity_id)
        return tel.n_days if tel is not None and tel.n_days > 0 else None


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------

def is_live(card: ActionCard) -> bool:
    """True for an actionable card (status ``pending`` or ``tension``)."""
    return card.status in ("pending", "tension")


def veto_card(card: ActionCard, code: str, reason: str) -> None:
    """Mark a card vetoed with a human reason and a machine reason code."""
    card.status = "vetoed"
    card.veto_reason = reason
    card.extras["reason_code"] = code


def reason_code(card: ActionCard) -> str | None:
    """The machine reason code a gate stamped on the card, if any."""
    return card.extras.get("reason_code")


def pauses_entity(card: ActionCard) -> bool:
    """True when an experiment card pauses its entity (a down-move in disguise).

    Read from the explicit boolean ``evidence["pauses"]`` (or
    ``extras["pauses"]``), never from prose.
    """
    return card.type == "experiment" and bool(card.evidence.get("pauses") or card.extras.get("pauses"))


def change_bounds(card: ActionCard) -> tuple[float, float] | None:
    """``(frm, to)`` of a sized move, or ``None`` when either side is missing."""
    ch = card.change
    if ch is None or ch.frm is None or ch.to is None:
        return None
    return float(ch.frm), float(ch.to)
