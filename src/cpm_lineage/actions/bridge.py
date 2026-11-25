"""Bridge between the two halves of action generation.

The rule families read an ``ActionContext`` (window aggregates per campaign,
keyword, segment and geo); the plan pipeline reads a ``PlanContext`` (entity
state, budget telemetry, segment stats, ledger feedback).  Both describe the
same book.  ``plan_context_from_actions`` derives the second from the first
so a caller builds the aggregates once and the two halves cannot disagree
about economics, profile, rule profile, telemetry or the run check.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from .pipeline_config import EntityState, PipelineConfig, PlanContext, SegmentStat, TermOwner
from .primitives import ActionContext
from .rebalance import BoardEntry

__all__ = ["plan_context_from_actions", "scoreboard_entries"]


def plan_context_from_actions(
    actx: ActionContext,
    *,
    today: date | None = None,
    cfg: PipelineConfig | None = None,
    lookback_days: int = 30,
    under_test: frozenset[str] = frozenset(),
    winrates: Mapping[str, tuple[float, int]] | None = None,
    precedent: Mapping[tuple[str, str | None], Mapping[str, Any]] | None = None,
    keyword_priority: Mapping[tuple[str | None, str], int] | None = None,
    term_owners: Mapping[str, TermOwner] | None = None,
    live_days: Mapping[str, int] | None = None,
    scope_entity_ids: frozenset[str] | None = None,
    status_source: str = "ok",
) -> PlanContext:
    """Build the pipeline's ``PlanContext`` from the families' ``ActionContext``.

    * entities: one ``EntityState`` per ``CampaignStat`` (status, 3-day
      spend, ad type, segment, name, own-brand share, optional live days).
    * segments: one ``SegmentStat`` per ``SegmentMarket`` (availability on
      0..1, sales totals of both windows, own ad share of voice in percent,
      own ad spend = sum of the segment's campaign spend).
    * telemetry, economics, profile, both policies, rule profile and
      operator floors are passed through unchanged.
    * ``keyword_priority`` defaults to the families' demand priority.

    Ledger feedback (``winrates``, ``precedent``, ``under_test``) is supplied
    by the caller; all default to empty.  Returns a ``PlanContext``.
    """
    live_days = live_days or {}
    entities = {
        c.entity_id: EntityState(
            c.entity_id, status=c.status, spend_3d=c.spend_3d, ad_type=c.ad_type, segment=c.segment,
            name=c.name, live_days=live_days.get(c.entity_id), own_brand_share=c.own_brand_share)
        for c in actx.campaigns
    }
    spend_by_segment: dict[str | None, float] = {}
    for c in actx.campaigns:
        spend_by_segment[c.segment] = spend_by_segment.get(c.segment, 0.0) + c.spend
    segments = {
        name: SegmentStat(
            availability=m.availability, total_sales=m.total_sales_current,
            total_sales_prior=m.total_sales_prior, own_sales=m.own_sales_current,
            own_sales_prior=m.own_sales_prior, sov=m.sov_current,
            own_spend=spend_by_segment.get(name))
        for name, m in actx.segments.items()
    }
    if keyword_priority is None:
        from .primitives import keyword_demand

        keyword_priority = dict(keyword_demand(actx).priority)
    return PlanContext(
        entities=entities, telemetry=actx.telemetry, segments=segments, under_test=under_test,
        winrates=dict(winrates or {}), precedent=dict(precedent or {}),
        keyword_priority=keyword_priority, term_owners=dict(term_owners or {}),
        lookback_days=lookback_days, today=today, economics=actx.economics, profile=actx.profile,
        availability_policy=actx.availability_policy, budget_policy=actx.budget_policy,
        rule_profile=actx.rule_profile, cfg=cfg or PipelineConfig(),
        scope_entity_ids=scope_entity_ids, operator_floors=dict(actx.operator_floors),
        status_source=status_source,
    )


def scoreboard_entries(board: Mapping[str, Mapping[str, Any]], actx: ActionContext | None = None) -> list[BoardEntry]:
    """Turn the families' verdict scoreboard into the rebalancer's ``BoardEntry`` rows.

    ``board`` is ``FamilyRun.board``: ``{entity_id: {r, thr, cap, segment,
    bucket, spend}}``.  Names are filled from ``actx`` when given.
    """
    out: list[BoardEntry] = []
    for entity_id, row in board.items():
        camp = actx.campaign(entity_id) if actx is not None else None
        out.append(BoardEntry(
            entity_id=entity_id, roas=row.get("r"), threshold=row.get("thr"), cap=row.get("cap"),
            segment=row.get("segment"), bucket=row.get("bucket"), spend=row.get("spend"),
            name=camp.name if camp is not None else None))
    return out
