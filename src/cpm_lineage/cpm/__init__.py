"""CPM, position and auction subsystem.

* ``tracking``: CPM views (overview, daily tracker, window compare, single
  keyword, CPM per observed position, category matrix).
* ``explain``: the why-did-CPM-rise classifier.
* ``sov``: ad share of voice and rivals (optional context).
* ``restatement``: first-seen snapshots and restatement detection.
* ``ownership``: the guard against rows filed under the wrong advertiser.
* ``rules``: neutral automation-rule schema, inventory and text rendering.
* ``guardrails``: bid ceiling and the position-targeting bidder audit.
* ``auction``: clearing bids, competitor threat classes, cheap/dear hours.
* ``overpay``: the three-hypothesis keyword overpay check.
* ``config``: every threshold (``CpmConfig``).
"""

from .auction import (
    CLEARING_CAVEAT, OWN_LOG_CAVEAT, auction_hints, classify_threat, clearing_bids, competitors,
    organic_trend, suggested_bids, suggested_bids_by_geo, threat_class, timing,
)
from .config import CpmConfig, money_of
from .explain import PeriodReading, classify_cpm_move, explain_cpm_moves, reading_from_cell
from .tracking import (
    category_matrix, cpm_by_position, daily_tracker, keyword_category_from_market, keyword_view,
    overview, position_slot, top_keywords, window_compare,
)
from .guardrails import (
    DecisionCounts, GuardrailPolicy, audit_guardrails, ceiling_bid, ceiling_multiplier,
    decision_counts, decision_rates, guardrail_band, guardrail_policy, live_state, position_seen,
    ruleset_members,
)
from .overpay import (
    AgentDay, agent_day_log, band_reference, check_overpay, geo_from_name, h1_flags, overpay_flag,
    position_band, render_overpay_markdown, self_floor_flag,
)
from .ownership import apply_ownership_guard, assign_campaign_owner, leaked_rows
from .restatement import (
    InMemorySnapshotStore, SnapshotStore, first_seen, restated, restated_days, snapshot_payload,
    take_snapshot,
)
from .rules import Rule, RuleSchema, Ruleset, RulesetMember, assemble, rule_text, ruleset_state, select_rulesets, stats
from .sov import brand_ad_shares, rivals, share_of_voice, tracked_keywords

__all__ = [n for n in dir() if not n.startswith("_")]
