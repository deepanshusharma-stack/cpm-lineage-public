"""Hand-built fixtures for the action pipeline tests."""

from __future__ import annotations

from datetime import date

from cpm_lineage.actions.pipeline_config import EntityState, PipelineConfig, PlanContext, SegmentStat
from cpm_lineage.schema import ActionCard, BudgetTelemetry, make_card

TODAY = date(2026, 3, 31)
NO_HOLDOUT = PipelineConfig(holdout_pct=0.0)


def bound(entity_id: str, budget: float = 1000.0, ad_type: str | None = None) -> BudgetTelemetry:
    """Telemetry that is budget-bound under both definitions."""
    return BudgetTelemetry(entity_id=entity_id, n_days=30, avg_budget=budget, avg_spend=budget * 0.95,
                           exhaust_minute=600.0, exhaust_days=20, sufficiency_3d=0.5, ad_type=ad_type)


def slack(entity_id: str, budget: float = 1000.0, ad_type: str | None = None) -> BudgetTelemetry:
    return BudgetTelemetry(entity_id=entity_id, n_days=30, avg_budget=budget, avg_spend=budget * 0.3,
                           exhaust_minute=None, exhaust_days=0, sufficiency_3d=1.0, ad_type=ad_type)


def ctx(entities=("c1", "c2", "c3"), **kw) -> PlanContext:
    kw.setdefault("cfg", NO_HOLDOUT)
    kw.setdefault("today", TODAY)
    kw["entities"] = entities if isinstance(entities, dict) else {
        e: EntityState(e, status="active", spend_3d=100.0, segment="seg", live_days=60) for e in entities}
    kw.setdefault("telemetry", {e: bound(e) for e in entities})
    kw.setdefault("segments", {"seg": SegmentStat(availability=0.95)})
    return PlanContext(**kw)


def budget(entity: str = "c1", direction: str = "up", family: str | None = None, frm: float = 1000.0,
           pct: float = 20.0, roas: float | None = 4.0, stake: float = 1000.0, spend: float = 60000.0,
           segment: str = "seg", title: str | None = None, **ev) -> ActionCard:
    family = family or ("efficiency-scale" if direction == "up" else "efficiency-cut")
    sign = 1 if direction == "up" else -1
    evidence = {"roas": roas, "spend": spend, **ev}
    return make_card(family, "budget", title or f"{family} {direction} {entity}", segment=segment,
                     entity_id=entity, direction=direction,
                     change={"pct": sign * pct, "frm": frm, "to": frm * (1 + sign * pct / 100)},
                     trigger=f"ROAS {roas}", evidence=evidence, stake=stake)


def card(family: str, type: str, entity: str | None = "c1", direction: str = "none", stake: float = 100.0,
         keywords=None, segment: str = "seg", title: str | None = None, **ev) -> ActionCard:
    return make_card(family, type, title or f"{family} {type} {entity} {direction}", segment=segment,
                     entity_id=entity, direction=direction, keywords=keywords, trigger="t",
                     evidence=dict(ev), stake=stake)


def status_of(cards) -> dict[str, str]:
    return {c.title: c.status for c in cards}
