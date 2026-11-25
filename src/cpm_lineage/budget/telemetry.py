"""Per-entity budget telemetry and the budget-bound policy.

``build_budget_telemetry`` turns the entity x day pacing feed into one
``BudgetTelemetry`` per entity; ``is_budget_bound`` answers "is this entity
constrained by its daily budget" under one named definition.

Four definitions of "budget-bound" are in common use.  They are
kept as four named methods of one policy (``config.BudgetBoundPolicy``):

==============  ==============================================================
method          rule
==============  ==============================================================
sufficiency     served under 90% of expected live time over the last 3 days
exhaustion      mean exhaust minute before 12 h, spend >= half the cap, and
                exhausted on >= 40% of days
capped_days     spend >= 90% of cap on >= 60% of active days
held_days       platform-held for budget on >= 3 days
==============  ==============================================================
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Iterable, Mapping

from ..config import BudgetBoundPolicy, Economics
from ..schema import BudgetDayRow, BudgetTelemetry, Entity
from .config import BudgetConfig, money_of
from .sufficiency import collapse_entity_days, day_sufficiency

__all__ = ["build_budget_telemetry", "utilisation", "is_budget_bound", "bound_method_for_profile"]

_CFG = BudgetConfig()

_PROFILE_METHOD = {"classic": "exhaustion", "marginal": "exhaustion", "verdict": "sufficiency"}


def build_budget_telemetry(
    budget_days: Iterable[BudgetDayRow],
    *,
    anchor: date | None = None,
    days: int | None = None,
    entities: Iterable[Entity] | Mapping[str, Entity] | None = None,
    cfg: BudgetConfig | None = None,
    policy: BudgetBoundPolicy | None = None,
    economics: Economics | None = None,
    collapse: bool = True,
) -> dict[str, BudgetTelemetry]:
    """Summarise each entity's budget behaviour over the lookback window.

    Window: the ``days`` days ending on ``anchor`` (default: latest date in
    the feed).  Entities whose window spend is at or below the minimum
    evidence spend are dropped.  Per entity:

    * ``avg_budget`` / ``avg_spend``: mean daily cap / mean daily spend
    * ``roas``: sum sales / sum spend; ``roas_recent``: same over the recent
      half (``date > anchor - max(days // 2, 1)``)
    * ``roas_sd``: sample standard deviation of daily sales/spend;
      ``roas_se = sd / sqrt(n_days)`` when ``n_days >= 5``;
      ``roas_lo / roas_hi = roas -/+ 1.28 * se``
    * ``exhaust_minute``: mean exhaust minute over days where it is > 0;
      ``exhaust_days``: days with a non-null exhaust minute
    * ``capped_days``: days with ``spend >= capped_spend_fraction * cap``;
      ``held_days``: days the platform held the entity (``None`` when the
      feed never reports it); ``active_days``: days with spend > 0
    * ``budget_latest``: cap on the latest day; ``budget_before_recent``:
      latest cap before the last 3 days
    * ``sufficiency_3d``: mean over the last 3 days of the served share of
      the expected day (1.0 on days that did not genuinely exhaust).  It is
      ``None`` when the entity's rows carry no served-time telemetry, so the
      "auto" bound method falls back to exhaustion instead of reading
      "always sufficient".

    Returns ``{entity_id: BudgetTelemetry}``.
    """
    cfg = cfg or _CFG
    policy = policy or BudgetBoundPolicy()
    days = days or cfg.telemetry_days
    rows = list(budget_days)
    if collapse:
        rows = collapse_entity_days(rows)
    if not rows:
        return {}
    anchor = anchor or max(r.date for r in rows)
    start = anchor - timedelta(days=days - 1)
    recent_half_from = anchor - timedelta(days=max(days // 2, 1))
    last_from = anchor - timedelta(days=cfg.recent_days)
    if entities is None:
        meta: Mapping[str, Entity] = {}
    elif isinstance(entities, Mapping):
        meta = entities
    else:
        meta = {e.entity_id: e for e in entities}

    by_entity: dict[str, list[BudgetDayRow]] = {}
    for r in rows:
        if start <= r.date <= anchor:
            by_entity.setdefault(r.entity_id, []).append(r)

    min_spend = money_of(cfg.telemetry_min_spend, economics)
    out: dict[str, BudgetTelemetry] = {}
    for eid, ds in by_entity.items():
        ds.sort(key=lambda r: r.date)
        spend = sum(d.spend or 0.0 for d in ds)
        if spend <= min_spend:
            continue
        n = len(ds)
        sales = sum(d.sales or 0.0 for d in ds)
        caps = [d.daily_budget for d in ds if d.daily_budget is not None]
        roas = sales / spend if spend > 0 else None
        rec = [d for d in ds if d.date > recent_half_from]
        rec_spend = sum(d.spend or 0.0 for d in rec)
        roas_recent = (sum(d.sales or 0.0 for d in rec) / rec_spend) if rec_spend > 0 else None
        ratios = [(d.sales or 0.0) / d.spend for d in ds if d.spend and d.spend > 0]
        sd = None
        if len(ratios) >= 2:
            m = sum(ratios) / len(ratios)
            sd = math.sqrt(sum((x - m) ** 2 for x in ratios) / (len(ratios) - 1))
        se = sd / math.sqrt(n) if (sd is not None and n >= cfg.telemetry_se_min_days) else None
        exh = [d.exhausted_at_minute for d in ds if d.exhausted_at_minute is not None]
        exh_pos = [x for x in exh if x > 0]
        any_exhaust_field = any(
            d.exhausted_at_minute is not None or d.live_minutes is not None
            or d.expected_live_minutes is not None for d in ds)
        capped = sum(1 for d in ds if d.daily_budget and d.daily_budget > 0
                     and d.spend >= policy.capped_spend_fraction * d.daily_budget)
        held_known = any(d.platform_held is not None for d in ds)
        last = [d for d in ds if d.date > last_from]
        suff3 = None
        if last and any_exhaust_field:
            vals = [s for s in (day_sufficiency(d, cfg) for d in last) if s is not None]
            suff3 = sum(vals) / len(vals) if vals else None
        before = [d.daily_budget for d in ds if d.date <= last_from and d.daily_budget is not None]
        ent = meta.get(eid)
        out[eid] = BudgetTelemetry(
            entity_id=eid, n_days=n,
            avg_budget=(sum(caps) / len(caps)) if caps else None,
            avg_spend=spend / n, spend=spend,
            impressions=sum(d.impressions or 0.0 for d in ds),
            roas=roas, roas_recent=roas_recent, roas_sd=sd, roas_se=se,
            roas_lo=(roas - cfg.telemetry_z * se) if (roas is not None and se is not None) else None,
            roas_hi=(roas + cfg.telemetry_z * se) if (roas is not None and se is not None) else None,
            exhaust_minute=(sum(exh_pos) / len(exh_pos)) if exh_pos else None,
            exhaust_days=len(exh) if any_exhaust_field else None,
            capped_days=capped if caps else None,
            held_days=sum(1 for d in ds if d.platform_held) if held_known else None,
            active_days=sum(1 for d in ds if d.spend and d.spend > 0),
            budget_latest=caps[-1] if caps else None,
            budget_before_recent=before[-1] if before else None,
            sufficiency_3d=suff3,
            ad_type=ent.ad_type if ent else None,
        )
    return out


def utilisation(telemetry: BudgetTelemetry | None) -> float:
    """Budget utilisation in percent: ``100 * avg_spend / avg_budget`` (0 with no cap or no telemetry)."""
    return telemetry.utilisation if telemetry is not None else 0.0


def bound_method_for_profile(rule_profile: str) -> str:
    """Budget-bound definition used by each action rule profile.

    ``"verdict"`` -> ``"sufficiency"`` (day-part aware served time);
    ``"classic"`` and ``"marginal"`` -> ``"exhaustion"``.
    """
    try:
        return _PROFILE_METHOD[rule_profile]
    except KeyError:
        raise ValueError(f"unknown rule profile: {rule_profile!r}") from None


def is_budget_bound(
    telemetry: BudgetTelemetry | None,
    policy: BudgetBoundPolicy | None = None,
    *,
    method: str | None = None,
    rule_profile: str | None = None,
) -> bool:
    """Decide whether an entity is constrained by its daily budget.

    No telemetry or no cap -> not bound.  Otherwise delegates to
    ``BudgetTelemetry.is_bound`` with ``method`` (explicit), else the method
    implied by ``rule_profile``, else ``policy.method``.  Returns a bool.
    """
    if telemetry is None:
        return False
    if method is None and rule_profile is not None:
        method = bound_method_for_profile(rule_profile)
    return telemetry.is_bound(policy, method)
