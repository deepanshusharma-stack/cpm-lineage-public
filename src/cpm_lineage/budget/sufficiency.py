"""Budget sufficiency: which entities run out of daily budget, and how much
daily budget each needs to serve the full expected day.

Input is the entity x day pacing feed (``BudgetDayRow``).  The feed contract
is "already collapsed": additive measures summed, entity-level attributes
maxed.  ``collapse_entity_days`` enforces that contract for feeds delivered
at a finer grain.

Assumption stated once: a CPM platform bills actual spend, not allocated
budget, so moving budget away from a sufficient entity frees no cash.  The
extra budget computed here is net-new money either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Mapping, Sequence

from ..config import Economics, LivenessConfig
from ..liveness import is_dead
from ..schema import BudgetDayRow, Entity, Window
from .config import BudgetConfig, money_of

__all__ = [
    "SufficiencyRow", "SufficiencyReport", "derive", "collapse_entity_days",
    "is_oob_day", "day_sufficiency", "budget_allocation", "summarise_oob",
    "tag_key", "filter_rows_by_tags",
]

_CFG = BudgetConfig()


@dataclass(frozen=True)
class SufficiencyRow:
    """One entity's budget sufficiency over the window."""

    entity_id: str
    name: str | None
    status: str | None
    live: bool                    # not dead by the shared liveness rule
    tags: tuple[str, ...]         # qualified tag keys (group + separator + name)
    ad_type: str | None
    days: int                     # spending days in the window
    oob_days: int                 # genuine out-of-budget days
    avg_budget: float
    avg_spend: float
    sufficiency: float | None     # mean served share of the expected day, <= 1; None = no exhaustion telemetry
    roas: float | None            # ratio of sums
    required: float               # daily budget needed to serve the full day
    extra: float                  # required - avg_budget, >= 0
    anomaly: bool                 # placeholder cap: no top-up maths


@dataclass(frozen=True)
class SufficiencyReport:
    """Output of ``budget_allocation``: rows sorted by ``extra`` descending."""

    window: Window
    rows: tuple[SufficiencyRow, ...]
    tag_groups: Mapping[str, tuple[str, ...]]   # group -> qualified tag keys seen on the rows


def derive(avg_budget: float, avg_sufficiency: float | None,
           cfg: BudgetConfig | None = None) -> tuple[float, float]:
    """Size the daily budget an entity needs to serve the whole expected day.

    Formula: ``suff = clamp(avg_sufficiency or 1.0, 0.01, 1.0)``;
    ``required = avg_budget / suff``; ``extra = max(required - avg_budget, 0)``.
    20% sufficiency means 5x the budget; the clamp caps the multiplier at
    100x.  ``None`` sufficiency (no exhaustion data) means sufficient.

    Returns ``(required, extra)``.
    """
    cfg = cfg or _CFG
    s = 1.0 if avg_sufficiency is None else float(avg_sufficiency)
    s = min(max(s, cfg.sufficiency_min), cfg.sufficiency_max)
    required = avg_budget / s
    return required, max(required - avg_budget, 0.0)


def _max_opt(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def collapse_entity_days(rows: Iterable[BudgetDayRow]) -> list[BudgetDayRow]:
    """Collapse finer-grain pacing rows to one row per entity x day.

    Rule: SUM the additive measures (spend, sales, impressions); MAX the
    entity-level fields (daily budget, exhaust minute, live minutes,
    expected live minutes); ``platform_held`` is true if any row says so.
    Averaging entity-level fields across the finer grain would be wrong.

    Returns rows sorted by (entity, date).
    """
    acc: dict[tuple[str, object], dict] = {}
    for r in rows:
        k = (r.entity_id, r.date)
        a = acc.get(k)
        if a is None:
            acc[k] = dict(date=r.date, entity_id=r.entity_id, daily_budget=r.daily_budget,
                          spend=r.spend or 0.0, sales=r.sales or 0.0, impressions=r.impressions or 0.0,
                          exhausted_at_minute=r.exhausted_at_minute, live_minutes=r.live_minutes,
                          expected_live_minutes=r.expected_live_minutes,
                          platform_held=r.platform_held, status=r.status)
            continue
        a["daily_budget"] = _max_opt(a["daily_budget"], r.daily_budget)
        a["spend"] += r.spend or 0.0
        a["sales"] += r.sales or 0.0
        a["impressions"] += r.impressions or 0.0
        a["exhausted_at_minute"] = _max_opt(a["exhausted_at_minute"], r.exhausted_at_minute)
        a["live_minutes"] = _max_opt(a["live_minutes"], r.live_minutes)
        a["expected_live_minutes"] = _max_opt(a["expected_live_minutes"], r.expected_live_minutes)
        if r.platform_held is not None:
            a["platform_held"] = bool(a["platform_held"]) or bool(r.platform_held)
        a["status"] = a["status"] or r.status
    out = [BudgetDayRow(**v) for v in acc.values()]
    out.sort(key=lambda r: (r.entity_id, r.date))
    return out


def is_oob_day(row: BudgetDayRow, cfg: BudgetConfig | None = None) -> bool:
    """Decide whether a day is a genuine out-of-budget day.

    Rule: the platform flagged exhaustion (non-null exhaust minute) AND
    ``spend >= oob_spend_fraction * daily_budget``.  The flag alone is not
    proof: it has been seen on days that spent a few percent of the cap.
    """
    cfg = cfg or _CFG
    if row.exhausted_at_minute is None or not row.daily_budget or row.daily_budget <= 0:
        return False
    return row.spend >= cfg.oob_spend_fraction * row.daily_budget


def day_sufficiency(row: BudgetDayRow, cfg: BudgetConfig | None = None) -> float | None:
    """Served share of the expected day for one entity-day.

    * not an out-of-budget day -> ``1.0``
    * out-of-budget day -> ``min(live / expected, 1)`` where ``live`` is the
      live minutes, or failing that the exhaust minute less
      ``exhaust_minute_origin``; ``None`` when ``expected`` is missing or 0
      or no live measure exists.
    """
    cfg = cfg or _CFG
    if not is_oob_day(row, cfg):
        return 1.0
    live = row.live_minutes
    if live is None and row.exhausted_at_minute is not None:
        live = max(row.exhausted_at_minute - cfg.exhaust_minute_origin, 0.0)
    if live is None or not row.expected_live_minutes or row.expected_live_minutes <= 0:
        return None
    return min(live / row.expected_live_minutes, 1.0)


def tag_key(group: str | None, name: str, separator: str = _CFG.tag_separator) -> str:
    """Qualified tag identity ``group + separator + name`` (never the name alone)."""
    return f"{group or ''}{separator}{name}"


def budget_allocation(
    budget_days: Iterable[BudgetDayRow],
    window: Window,
    entities: Iterable[Entity] | Mapping[str, Entity] | None = None,
    *,
    owner: str | None = None,
    status_map: Mapping[str, str] | None = None,
    cfg: BudgetConfig | None = None,
    economics: Economics | None = None,
    liveness: LivenessConfig | None = None,
    collapse: bool = True,
) -> SufficiencyReport:
    """Rank entities by the extra daily budget they need to stop running out.

    Per entity over ``window`` (spending days only):
    ``sufficiency = mean(day_sufficiency)`` over all spending days with
    non-exhausted days contributing 1.0 (``None`` when the feed carries no
    exhaustion telemetry at all for the entity); ``roas = sum(sales) /
    sum(spend)``; ``(required, extra) = derive(avg_budget, sufficiency)``.
    A cap below the placeholder threshold is flagged ``anomaly`` with
    ``extra = 0``.  ``live`` uses the shared liveness rule with spend over
    the last ``soft_dead_days`` days of the window (exactly
    that many days, the anchor day included).

    Cross-account guard: when ``owner`` is given and at least one entity
    carries that owner, entities whose owner is known and different are
    dropped; entities with unknown owner stay.

    Returns a ``SufficiencyReport`` with rows sorted by ``extra`` descending.
    """
    cfg = cfg or _CFG
    liveness = liveness or LivenessConfig()
    if entities is None:
        meta: dict[str, Entity] = {}
    elif isinstance(entities, Mapping):
        meta = dict(entities)
    else:
        meta = {e.entity_id: e for e in entities}

    rows = [r for r in budget_days if window.contains(r.date)]
    if collapse:
        rows = collapse_entity_days(rows)
    by_entity: dict[str, list[BudgetDayRow]] = {}
    for r in rows:
        if r.spend and r.spend > 0:
            by_entity.setdefault(r.entity_id, []).append(r)

    guard = bool(owner) and any(e.owner == owner for e in meta.values())
    placeholder = money_of(cfg.placeholder_cap, economics)
    recent_from = window.end - timedelta(days=liveness.soft_dead_days)

    out: list[SufficiencyRow] = []
    groups: dict[str, set[str]] = {}
    for eid, days in by_entity.items():
        ent = meta.get(eid)
        if guard and ent is not None and ent.owner and ent.owner != owner:
            continue
        budgets = [d.daily_budget for d in days if d.daily_budget is not None]
        avg_budget = sum(budgets) / len(budgets) if budgets else 0.0
        spend = sum(d.spend for d in days)
        sales = sum(d.sales or 0.0 for d in days)
        has_telemetry = any(
            d.exhausted_at_minute is not None or d.live_minutes is not None
            or d.expected_live_minutes is not None for d in days)
        suffs = [s for s in (day_sufficiency(d, cfg) for d in days) if s is not None]
        sufficiency = (sum(suffs) / len(suffs)) if (has_telemetry and suffs) else None
        required, extra = derive(avg_budget, sufficiency, cfg)
        anomaly = avg_budget < placeholder
        if anomaly:
            required, extra = avg_budget, 0.0
        spend_recent = sum(d.spend for d in days if d.date > recent_from)
        status = (ent.status if ent is not None and ent.status else None) or days[-1].status
        tags = tuple(tag_key(g, n, cfg.tag_separator) for g, n in (ent.tags if ent else ()))
        for g, n in (ent.tags if ent else ()):
            groups.setdefault(g or "", set()).add(tag_key(g, n, cfg.tag_separator))
        out.append(SufficiencyRow(
            entity_id=eid, name=ent.name if ent else None, status=status,
            live=not is_dead(status, spend_recent, status_map=status_map),
            tags=tags, ad_type=ent.ad_type if ent else None,
            days=len(days), oob_days=sum(1 for d in days if is_oob_day(d, cfg)),
            avg_budget=avg_budget, avg_spend=spend / len(days),
            sufficiency=sufficiency, roas=(sales / spend) if spend > 0 else None,
            required=required, extra=extra, anomaly=anomaly))
    out.sort(key=lambda r: (-r.extra, r.entity_id))
    return SufficiencyReport(window=window, rows=tuple(out),
                             tag_groups={g: tuple(sorted(v)) for g, v in sorted(groups.items())})


def summarise_oob(rows: Sequence[SufficiencyRow], cfg: BudgetConfig | None = None,
                  economics: Economics | None = None) -> dict[str, float]:
    """Total the top-up over entities that genuinely need one.

    Set: ``live AND extra >= oob_min_extra AND not anomaly``.  Returns
    ``{n, current, needed, extra_per_day, extra_projected}`` where
    ``extra_projected = extra_per_day * projection_days`` (display only).
    """
    cfg = cfg or _CFG
    floor = money_of(cfg.oob_min_extra, economics)
    oob = [r for r in rows if r.live and not r.anomaly and r.extra >= floor]
    extra = sum(r.extra for r in oob)
    return {
        "n": float(len(oob)),
        "current": sum(r.avg_budget for r in oob),
        "needed": sum(r.required for r in oob),
        "extra_per_day": extra,
        "extra_projected": extra * cfg.projection_days,
    }


def filter_rows_by_tags(rows: Sequence[SufficiencyRow], tags: Iterable[str] | None,
                        mode: str = "any") -> list[SufficiencyRow]:
    """Keep rows by qualified tag keys.

    No tags -> all rows.  ``mode="all"`` -> the row carries every selected
    tag (subset test); ``mode="any"`` -> at least one.  Returns a list.
    """
    wanted = set(tags or ())
    if not wanted:
        return list(rows)
    if mode == "all":
        return [r for r in rows if wanted <= set(r.tags)]
    if mode == "any":
        return [r for r in rows if wanted & set(r.tags)]
    raise ValueError(f"unknown tag filter mode: {mode!r}")
