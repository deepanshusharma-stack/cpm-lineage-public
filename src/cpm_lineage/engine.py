"""Orchestration: two calls that compose the subsystems end to end.

``diagnose(data)``  answers "what happened and why":

    account identity decomposition (layer 1)  ->  ad-type shift-share and
    Pareto drag  ->  CPM-move classification, availability drill, keyword
    cut / leak scan, own-campaign collisions, budget telemetry and moves
    ->  per-entity attribution, budget probe, recommendation and one-row
    prescription  ->  a flat list of typed ``Finding`` rows.

``plan(data)``  answers "what should be done":

    rule families emit candidate cards  ->  the plan pipeline gates,
    resolves, scores, caps and orders them  ->  the verdict spine types
    every card (ACT / OBSERVE / HOLD / ESCALATE / NO-ACTION) and mints its
    own sweeps  ->  optionally the plan is saved to an action ledger.

Nothing here contains decision logic of its own beyond the thresholds on
``OrchestrationConfig`` that turn subsystem outputs into ``Finding`` rows;
every number comes from the module named in ``Finding.source``.  There is no
I/O: ``data`` is an in-memory ``Dataset``, an adapter, or a list of
``PerfRow``; feeds that are absent make the dependent blocks ``None`` or
empty (with a note in ``Diagnosis.notes``), never an error.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

from . import verdict as _verdict
from .actions.bridge import plan_context_from_actions, scoreboard_entries
from .actions.config import ActionsConfig
from .actions.families import Collision, find_collisions, run_families
from .actions.pipeline import Plan, build_plan, donor_exclusions, holdout_ids, under_test_entities
from .actions.rebalance import rebalance
from .actions.scoring import expected_value
from .actions.pipeline_config import PipelineConfig
from .actions.primitives import ActionContext, context_from_dataset
from .adapters.base import Dataset, PlatformProfile
from .adapters.csv import derive_entities
from .attribution import Attribution, attribute, build_lever_panel, fit_prior
from .budget import (BudgetMoves, BudgetProbe, RaiseVerdict, budget_moves_from_rows,
                     build_budget_telemetry, fit_entity_curve, fit_pooled_curve, probe_budget,
                     raise_verdict)
from .config import Economics, EngineConfig, TradingDay
from .coldstart import OwnUnit, StateDiagnosis, diagnose_state, history_from_rows, preflight
from .cpm import explain_cpm_moves
from .cpm.config import CpmConfig
from .decompose import (CompositionResult, ParetoResult, ShiftShareResult, ad_type_shift_share,
                        entry_exit_rows, pareto_drag_rows)
from .geo import GeoDrillResult, geo_drill, geo_roas
from .identity import Layer1Result, Layer1Verdict, decompose_rows, layer1_verdict
from .keywords import KeywordRecs, LeakResult, cannibalisation_rate, keyword_leaks, keyword_recs
from .liveness import dead_for_window, is_dead, recent_spend
from .metrics import fmt_money
from .market import weighted_availability
from .prescribe import (PrescribeInput, PrescribeKeyword, Prescription, budget_from_probe,
                        prescribe, top_actions_from)
from .recommend import (BudgetState, RecommendContext, Recommendation, priority, recommend,
                        role_cohorts, structural_bleed, unit_stats)
from .schema import (AppliedAction, BudgetDayRow, BudgetTelemetry, PerfRow, RunCheck, Window,
                     WindowPair)
from .windows import filter_rows, route_tier, trailing_windows

__all__ = [
    "OrchestrationConfig", "Finding", "EntityDiagnosis", "Diagnosis",
    "FINDING_KINDS", "as_dataset", "diagnose", "plan", "render_report",
]

# Finding kinds, in the order ``Diagnosis.findings`` groups them.
FINDING_KINDS: tuple[str, ...] = (
    "cpm_rise", "availability_drop", "budget_bound_winner", "bleeding_keyword", "own_collision",
)


@dataclass(frozen=True)
class OrchestrationConfig:
    """Thresholds of the orchestration layer only (subsystems keep their own)."""

    window_days: int = 30                 # default comparison: last 30 days vs the 30 before
    max_entities: int = 25                # per-entity diagnosis is run on the top entities by window spend
    availability_drop_min: float = 0.05   # a geo's own availability fall (0..1) that counts as an event
    product_drop_min: float = 0.10        # a product-in-geo fall that names the product as the cause
    max_findings_per_kind: int = 10       # keep the report reviewable
    rca: bool = True                      # run the market root-cause read when the sell-out feed exists
    min_finding_days: int = 7             # keyword-cut and collision findings need this many delivery days in the current window


@dataclass(frozen=True)
class Finding:
    """One typed finding, traceable to the subsystem that produced it.

    ``size`` is the subsystem's own magnitude for ranking inside a kind (a
    fraction for a CPM rise or availability fall, money for the others);
    sizes are not comparable across kinds.
    """

    kind: str                          # one of FINDING_KINDS
    summary: str
    source: str                        # module that produced the evidence
    entity_ids: tuple[str, ...] = ()
    targeting_unit: str | None = None
    geo: str | None = None
    product_id: str | None = None
    size: float | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class EntityDiagnosis:
    """Everything known about one entity (campaign) for the comparison."""

    entity_id: str
    name: str | None
    ad_type: str | None
    status: str | None
    tier: str                                   # data tier from active days
    spend: float
    roas: float | None
    layer1: Layer1Result
    layer1_verdict: Layer1Verdict
    attribution: Attribution | None = None      # None for ad types with no attributed sales
    recommendation: Recommendation | None = None
    probe: BudgetProbe | None = None            # None without a budget feed
    raise_verdict: RaiseVerdict | None = None
    bleed: Mapping[str, Any] | None = None      # structural bleed over the whole history
    priority: str | None = None                 # "p0" | "p1" | "p2"
    state: StateDiagnosis | None = None         # state-mode read when too young for a move model
    prescription: Prescription | None = None


@dataclass
class Diagnosis:
    """Output of :func:`diagnose`."""

    windows: WindowPair
    layer1: Layer1Result
    layer1_verdict: Layer1Verdict
    ad_type_mix: ShiftShareResult
    pareto: ParetoResult
    composition: CompositionResult | None
    cpm_moves: list[dict]
    availability: GeoDrillResult | None
    keywords: KeywordRecs | None
    leaks: LeakResult | None
    collisions: list[Collision]
    telemetry: dict[str, BudgetTelemetry]
    budget_moves: BudgetMoves | None
    market: Any | None                          # rca.RcaResult when the sell-out feed exists
    entities: dict[str, EntityDiagnosis]
    findings: list[Finding]
    notes: list[str] = field(default_factory=list)
    has_comparison: bool = True                 # False when the prior (or current) window holds no delivery
    roas_basis: str = "direct + indirect"       # how entity ROAS counts indirect sales (EngineConfig.indirect_sales_weight)

    def of_kind(self, kind: str) -> list[Finding]:
        """Findings of one kind, largest first."""
        return [f for f in self.findings if f.kind == kind]


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------

def as_dataset(data: Any, profile: PlatformProfile | None = None) -> Dataset:
    """Coerce the engine's input to a ``Dataset``.

    Accepts a ``Dataset``; an adapter exposing ``.dataset`` or the
    ``PlatformAdapter`` methods; or an iterable of ``PerfRow`` (entities are
    then derived from the rows).  ``profile`` overrides the dataset's own.
    Returns a ``Dataset`` (a shallow copy when the profile is overridden).
    """
    if isinstance(data, Dataset):
        ds = data
    elif isinstance(getattr(data, "dataset", None), Dataset):
        ds = data.dataset
    elif hasattr(data, "perf_rows"):
        def feed(name: str) -> list:
            fn = getattr(data, name, None)
            return list(fn()) if callable(fn) else []
        ds = Dataset(
            profile=getattr(data, "profile", None) or PlatformProfile(),
            perf=feed("perf_rows"), entities=feed("entities"), targeting_units=feed("targeting_units"),
            products=feed("products"), market=feed("market_rows"), offtake=feed("offtake_rows"),
            shelf=feed("shelf_rows"), budget_days=feed("budget_days"), change_events=feed("change_events"))
    else:
        rows = list(data)
        if rows and not isinstance(rows[0], PerfRow):
            raise TypeError("data must be a Dataset, an adapter or an iterable of PerfRow")
        ds = Dataset(profile=profile or PlatformProfile(), perf=rows, entities=derive_entities(rows))
    if profile is not None and ds.profile is not profile:
        ds = replace(ds, profile=profile)
    if not ds.entities and ds.perf:
        ds = replace(ds, entities=derive_entities(ds.perf))
    return ds


def _budget_days(ds: Dataset) -> tuple[list[BudgetDayRow], bool]:
    """The budget pacing feed, or one derived from delivery when it is absent.

    Derived rows are entity x day sums of spend, sales and impressions with
    the entity's current daily cap (``None`` when unknown) and no exhaustion
    telemetry, so nothing can read as budget-bound from them.
    """
    if ds.budget_days:
        return ds.budget_days, True
    caps = {e.entity_id: e.daily_budget for e in ds.entities}
    acc: dict[tuple[str, date], list[float]] = {}
    for r in ds.perf:
        a = acc.setdefault((r.entity_id, r.date), [0.0, 0.0, 0.0])
        a[0] += r.spend
        a[1] += r.sales
        a[2] += r.impressions
    rows = [BudgetDayRow(date=d, entity_id=e, daily_budget=caps.get(e), spend=v[0], sales=v[1], impressions=v[2])
            for (e, d), v in sorted(acc.items())]
    return rows, False


def _setup(data: Any, profile: PlatformProfile | None, config: EngineConfig | None,
           windows: WindowPair | None, days: int) -> tuple[Dataset, EngineConfig, WindowPair, date]:
    ds = as_dataset(data, profile)
    anchor = ds.latest_date
    if anchor is None:
        raise ValueError("no performance rows: nothing to analyse")
    for r in ds.perf[:1]:
        if not isinstance(r.date, date) or isinstance(r.date, datetime):
            raise TypeError(f"PerfRow.date must be a datetime.date, got {type(r.date).__name__} ({r.date!r}); "
                            "parse dates in the adapter (see cpm_lineage.windows.parse_iso_date)")
    cfg = config or EngineConfig()
    prof = ds.profile
    # One source of truth for the money scale and the trading day: an explicit config value
    # wins, otherwise the platform profile's declaration is used.  Both calls then read the same.
    td = cfg.trading_day or TradingDay(start_hour=prof.trading_day_start_hour, end_hour=prof.trading_day_end_hour,
                                       funding_target_hour=prof.funding_target_hour)
    cfg = replace(cfg, economics=cfg.economics.resolved(prof.currency_unit), trading_day=td)
    if (prof.funding_target_hour, prof.trading_day_start_hour, prof.trading_day_end_hour) != (
            td.funding_target_hour, td.start_hour, td.end_hour):
        ds = replace(ds, profile=replace(prof, funding_target_hour=td.funding_target_hour,
                                         trading_day_start_hour=td.start_hour, trading_day_end_hour=td.end_hour))
    return ds, cfg, windows or trailing_windows(anchor, days), anchor


def _scale_note(ds: Dataset, cfg: EngineConfig, windows: WindowPair, budget_cfg_placeholder: float = 100.0,
                ref_daily_spend: float = 1000.0) -> str | None:
    """Warn when the data sits far below the money scale the thresholds assume.

    The absolute thresholds are written for entities that spend thousands of
    currency units per day.  When the median active entity spends less than
    ``ref_daily_spend x currency_unit / 10`` per day, or most daily caps sit
    under the placeholder-cap line, the note suggests a ``currency_unit``.
    """
    econ = cfg.economics
    per_entity: dict[str, float] = {}
    for r in ds.perf:
        if windows.current.contains(r.date) and r.spend > 0:
            per_entity[r.entity_id] = per_entity.get(r.entity_id, 0.0) + r.spend
    if not per_entity:
        return None
    daily = sorted(v / max(windows.current.days, 1) for v in per_entity.values())
    median = daily[len(daily) // 2]
    caps = [e.daily_budget for e in ds.entities if e.daily_budget]
    small_caps = caps and sum(1 for c in caps if c < econ.money(budget_cfg_placeholder)) > len(caps) / 2
    if median >= econ.money(ref_daily_spend) / 10.0 and not small_caps:
        return None
    suggested = median / ref_daily_spend
    mag = 10.0 ** round(__import__("math").log10(suggested)) if suggested > 0 else econ.unit
    return (f"money scale: the median entity spends {fmt_money(median)}/day, far below the scale the money "
            f"thresholds assume at currency_unit={econ.unit:g} (thousands per day); most money rules will "
            f"abstain or misread caps as placeholders. Set Economics(currency_unit={mag:g}) "
            f"or PlatformProfile.currency_unit")


# --------------------------------------------------------------------------
# diagnose
# --------------------------------------------------------------------------

def _entity_diagnosis(
    ds: Dataset, cfg: EngineConfig, windows: WindowPair, anchor: date, entity: Any,
    rows: list[PerfRow], prior: Any, pooled_fit: Any, budget_rows: list[BudgetDayRow],
    has_budget_feed: bool, telemetry: Mapping[str, BudgetTelemetry], cohorts: Mapping[str, Any],
    roles: Mapping[str, str], cann_rate: float | None, spend3: float, spend7: float,
) -> EntityDiagnosis:
    profile, econ = ds.profile, cfg.economics
    eid = entity.entity_id
    cap = profile.capability(entity.ad_type)
    cur_rows = filter_rows(rows, windows.current)
    spend = sum(r.spend for r in cur_rows)
    direct = sum(r.sales for r in cur_rows)
    # Entity ROAS on the configured basis (the same one plan() uses); layer 1 stays on direct sales.
    sales = direct + cfg.indirect_sales_weight * sum(r.indirect_sales or 0.0 for r in cur_rows)
    l1 = decompose_rows(rows, windows)
    out = EntityDiagnosis(
        entity_id=eid, name=entity.name, ad_type=profile.canonical_ad_type(entity.ad_type),
        status=entity.status,
        tier=route_tier(len({r.date for r in rows if r.spend > 0 and r.impressions > 0}), cfg.tiers).value,
        spend=spend, roas=(sales / spend if spend > 0 else None),
        layer1=l1, layer1_verdict=layer1_verdict(l1, economics=econ))

    tel = telemetry.get(eid)
    probe: BudgetProbe | None = None
    if has_budget_feed:
        days = [b for b in budget_rows if b.entity_id == eid]
        if days:
            probe = probe_budget(
                eid, days, entity.daily_budget, status=entity.status, status_map=profile.status_map,
                anchor=anchor, own_fit=fit_entity_curve(budget_rows, eid, anchor=anchor),
                pooled_fit=pooled_fit, current_roas=out.roas, top_position=profile.position.top_value,
                lower_is_better=profile.position.lower_is_better, economics=econ,
                trading_day=cfg.trading_day)
            out.probe = probe
            if probe.capped:
                out.raise_verdict = raise_verdict(
                    probe, current_roas=out.roas, status=entity.status, status_map=profile.status_map,
                    economics=econ)

    if not cap.sales_attributed:
        # Reach-class: never judged on ROAS, so no attribution or bleed read.
        out.prescription = prescribe(
            PrescribeInput(roas=out.roas, spend=spend, days=windows.current.days, status=entity.status,
                           status_map=profile.status_map, reach_judged=True, entity_name=entity.name,
                           budget=budget_from_probe(probe)),
            economics=econ, availability_policy=cfg.availability)
        return out

    panel = build_lever_panel(ds.perf, market_rows=ds.market, shelf_rows=ds.shelf,
                              targeting_units=ds.targeting_units, entity_id=eid, position=cfg.position)
    att = attribute(panel, windows, prior=prior,
                    position_lower_is_better=profile.position.lower_is_better,
                    position_top=profile.position.top_value, tiers=cfg.tiers)
    out.attribution = att
    life = unit_stats(ds.perf, ds.targeting_units, entity_id=eid)
    recent = unit_stats(ds.perf, ds.targeting_units, entity_id=eid, window=windows.current)
    bleed = structural_bleed(life, recent, economics=econ)
    out.bleed = bleed
    out.priority = priority(att.baseline_roas, att.comparison_roas, bleed.get("pct"), economics=econ)
    capped = bool(probe.capped) if probe is not None else bool(tel and tel.is_bound(cfg.budget_bound))
    budget = BudgetState(
        capped=capped, dayparted=bool(probe and probe.dayparted),
        marginal_roas=probe.mroas_raw if probe else None, curve_r2=probe.sat_r2 if probe else None,
        rec_range=probe.rec_range if probe else None,
        revenue_at_stake=probe.rev_at_stake if probe else None,
        utilisation=probe.util if probe else None, ladder=probe.ladder if probe else None)
    prior_rows = filter_rows(rows, windows.prior)
    prior_spend = sum(r.spend for r in prior_rows)
    indirect = sum(r.indirect_sales or 0.0 for r in cur_rows)
    has_indirect = any(r.indirect_sales is not None for r in cur_rows)
    product_ids = sorted({r.product_id for r in rows if r.product_id is not None} | set(entity.product_ids))
    avail = avail_prior = None
    if ds.shelf and product_ids:
        avail = weighted_availability(ds.shelf, windows.current, product_ids=product_ids)
        avail_prior = weighted_availability(ds.shelf, windows.prior, product_ids=product_ids)
    rctx = RecommendContext(
        status=entity.status, status_map=profile.status_map, auto_targeted=cap.auto_targeted,
        availability=avail, availability_prior=avail_prior,
        spend=spend, spend_prior=prior_spend,
        roas_prior=(sum(r.sales for r in prior_rows) / prior_spend if prior_spend > 0 else None),
        total_roas=((direct + indirect) / spend if spend > 0 and has_indirect else None),
        direct_roas=(direct / spend if spend > 0 else None), bleed_pct=bleed.get("pct"), short=att.short, cohorts=cohorts,
        position_lower_is_better=profile.position.lower_is_better)
    rec = recommend(att.drivers, recent, budget=budget, cur_roas=att.comparison_roas,
                    verdict=att.verdict, verdict_note=att.note or "", ctx=rctx, economics=econ,
                    availability=cfg.availability)
    out.recommendation = rec

    state_verb = None
    young = att.status != "ok"
    if young:
        # Too young for a move model: inherit the account's history on the same
        # targeting units, products and geos (pre-flight) and read state, not a move.
        history = history_from_rows(ds.perf, exclude_entity_id=eid, roles=roles or None,
                                    is_search_keyword=profile.is_search_keyword,
                                    is_aggregate_geo=profile.is_aggregate_geo)
        card = preflight(
            [OwnUnit(value=u.value, spend=u.spend, sales=u.sales, bid=None) for u in life if not u.is_placement],
            history, cohorts, roles=roles or None, product_ids=product_ids or None,
            economics=econ, availability_policy=cfg.availability)
        n_active = len({r.date for r in rows if r.spend > 0 and r.impressions > 0})
        out.state = diagnose_state(eid, entity.status, n_active, card, spend_7d=spend7, spend_3d=spend3,
                                   own_roas=out.roas, status_map=profile.status_map, tiers=cfg.tiers)
        out.priority = out.state.priority
        state_verb = out.state.top3[0].verb if out.state.top3 else "Let it run (state mode)"
    out.prescription = prescribe(
        PrescribeInput(
            roas=None if young else out.roas, total_roas=None if young else rctx.total_roas, spend=spend,
            days=windows.current.days, status=entity.status, status_map=profile.status_map,
            auto_targeted=cap.auto_targeted, actions=() if young else top_actions_from(rec.actions),
            keywords=tuple(PrescribeKeyword(value=u.value, role=u.role, spend=u.spend,
                                            searches_per_day=u.search_volume, is_placement=u.is_placement)
                           for u in recent),
            entity_name=entity.name, account_cann_rate=cann_rate, availability=avail,
            availability_prior=avail_prior, budget=budget_from_probe(probe),
            confidence=(att.confidence or {}).get("level"), state_verb=state_verb),
        economics=econ, availability_policy=cfg.availability)
    return out


def _availability_findings(ds: Dataset, windows: WindowPair, drill: GeoDrillResult | None,
                           ocfg: OrchestrationConfig) -> list[Finding]:
    out: list[Finding] = []
    if drill is None:
        return out
    own_products = sorted({s.product_id for s in ds.shelf if s.is_own})
    products_by_entity: dict[str, set[str]] = {e.entity_id: set(e.product_ids) for e in ds.entities}
    for r in ds.perf:
        if r.product_id is not None:
            products_by_entity.setdefault(r.entity_id, set()).add(r.product_id)
    for row in drill.rows:
        if row.drop is None or row.drop < ocfg.availability_drop_min:
            continue
        worst: tuple[float, str, float, float] | None = None
        for pid in own_products:
            before = weighted_availability(ds.shelf, windows.prior, product_ids=[pid], geo=row.geo)
            after = weighted_availability(ds.shelf, windows.current, product_ids=[pid], geo=row.geo)
            if before is None or after is None:
                continue
            if worst is None or before - after > worst[0]:
                worst = (before - after, pid, before, after)
        product = worst[1] if worst and worst[0] >= ocfg.product_drop_min else None
        hit = [e for e, pids in products_by_entity.items() if product and product in pids]
        what = f"{product} in {row.geo}" if product else f"own products in {row.geo}"
        detail = (f"{worst[2]:.2f} -> {worst[3]:.2f}" if product and worst
                  else f"{row.h_own:.2f} -> {row.w_own:.2f}")
        out.append(Finding(
            kind="availability_drop", source="geo.drill",
            summary=f"availability of {what} fell {detail}", entity_ids=tuple(sorted(hit)),
            geo=row.geo, product_id=product, size=row.drop,
            evidence={"geo_before": row.h_own, "geo_after": row.w_own, "rival": row.comp,
                      "product_before": worst[2] if worst else None,
                      "product_after": worst[3] if worst else None, "tier": row.tier}))
    return out


def _findings(ds: Dataset, windows: WindowPair, d: Diagnosis, ocfg: OrchestrationConfig) -> list[Finding]:
    cur = filter_rows(ds.perf, windows.current)
    spend_by_kw: dict[str, dict[str, float]] = {}
    for r in cur:
        if r.targeting_unit:
            by = spend_by_kw.setdefault(r.targeting_unit, {})
            by[r.entity_id] = by.get(r.entity_id, 0.0) + r.spend

    def holders(keyword: str) -> tuple[str, ...]:
        by = spend_by_kw.get(keyword, {})
        return tuple(sorted(by, key=lambda e: (-by[e], e)))

    out: list[Finding] = []
    for m in d.cpm_moves:
        out.append(Finding(
            kind="cpm_rise", source="cpm.explain", targeting_unit=m["keyword"],
            entity_ids=holders(m["keyword"]), size=m.get("pct"),
            summary=(f"CPM on '{m['keyword']}' rose {100 * m['pct']:.0f}% "
                     f"({fmt_money(m['reference_cpm'])} -> {fmt_money(m['current_cpm'])}): {m['kind']}"),
            evidence=dict(m)))
    out.extend(_availability_findings(ds, windows, d.availability, ocfg))
    for eid, e in d.entities.items():
        rv = e.raise_verdict
        if rv is not None and rv.raise_ok:
            out.append(Finding(
                kind="budget_bound_winner", source="budget.probe", entity_ids=(eid,),
                size=rv.rev_at_stake,
                summary=(f"{eid} is budget-bound at ROAS {e.roas:.2f}: {rv.verb.lower()}"
                         + (f" (range {fmt_money(rv.rec_range[0])}-{fmt_money(rv.rec_range[1])} per day)"
                            if rv.rec_range else "")),
                evidence={"code": rv.code, "reason": rv.reason, "rec_range": rv.rec_range,
                          "revenue_at_stake": rv.rev_at_stake, "marginal_roas": rv.mroas,
                          "estimator": rv.estimator, "days_capped": e.probe.days_capped if e.probe else None,
                          "exhaust_hour": e.probe.exhaust_hour if e.probe else None}))
    cur_days = len({r.date for r in cur if r.spend > 0})
    enough_days = cur_days >= ocfg.min_finding_days       # a cut or a collision call needs more than a few days
    if d.keywords is not None and enough_days:
        for c in d.keywords.cut.rows:
            if c.verdict != "cut":
                continue
            out.append(Finding(
                kind="bleeding_keyword", source="keywords.recommend", targeting_unit=c.keyword,
                entity_ids=holders(c.keyword), size=c.bleed,
                summary=f"'{c.keyword}' ({c.role}) returns ROAS {c.roas:.2f} on {fmt_money(c.spend)} spend: cut",
                evidence={"spend": c.spend, "roas": c.roas, "bleed": c.bleed, "new_users": c.new_users,
                          "why": c.why}))
    for col in (d.collisions if enough_days else ()):
        rows = [r for _geo, grp in col.groups for r in grp]
        by: dict[str, list[float]] = {}
        for r in rows:
            a = by.setdefault(r.entity_id, [0.0, 0.0])
            a[0] += r.spend
            a[1] += r.sales
        ranked = sorted(by, key=lambda e: (-(by[e][1] / by[e][0] if by[e][0] > 0 else 0.0), e))
        out.append(Finding(
            kind="own_collision", source="actions.families.collision", targeting_unit=col.keyword,
            entity_ids=tuple(ranked), size=col.waste,
            summary=(f"{len(ranked)} own campaigns bid '{col.keyword}' in {len(col.groups)} auction(s); "
                     f"keep it on {ranked[0]}, {fmt_money(col.waste)} sits on the weaker one(s)"),
            evidence={"keep": ranked[0], "waste": col.waste,
                      "geos": [g for g, _ in col.groups],
                      "roas": {e: (v[1] / v[0] if v[0] > 0 else None) for e, v in by.items()}}))
    ordered: list[Finding] = []
    for kind in FINDING_KINDS:
        rows = sorted((f for f in out if f.kind == kind), key=lambda f: -(f.size or 0.0))
        ordered.extend(rows[: ocfg.max_findings_per_kind])
    return ordered


def diagnose(
    data: Any,
    profile: PlatformProfile | None = None,
    config: EngineConfig | None = None,
    *,
    windows: WindowPair | None = None,
    entity_ids: Iterable[str] | None = None,
    own_brand: str | None = None,
    segment: str | None = None,
    options: OrchestrationConfig | None = None,
    cpm_cfg: CpmConfig | None = None,
) -> Diagnosis:
    """Diagnose an account: what moved, why, and where the money leaks.

    ``data`` is a ``Dataset``, an adapter or a list of ``PerfRow``;
    ``profile`` overrides the dataset's platform profile; ``config`` carries
    economics, policies and tiers (defaults when ``None``).  ``windows``
    defaults to the last ``options.window_days`` days versus the same span
    before, anchored on the latest delivery date.

    Account level: ``ln(ROAS ratio) = d ln ICR + d ln ASP - d ln CPM``
    (identity), midpoint shift-share by ad type, Pareto drag by entity,
    entry / exit composition, the CPM-move classifier per keyword, the
    availability geo drill, keyword cut / add and leak scans, own-campaign
    collisions, budget telemetry and scale / tighten / release moves, and
    (with a sell-out feed and a known own brand) the market root-cause read.

    Entity level (``entity_ids`` or the top ``max_entities`` live entities
    by window spend): layer 1, the lever attribution with its verdict, the
    budget probe and raise verdict, structural bleed and priority, the
    recommendation gate stack and the one-row prescription.

    Entity ROAS counts ``config.indirect_sales_weight`` of the indirect
    (halo / view-through) sales, the same basis ``plan()`` decides on; the
    identity decomposition always reads direct sales.  The money scale and
    the trading-day hours come from ``config`` when set there, otherwise
    from the platform profile.  ``cpm_cfg`` tunes the CPM views.

    Guards: when the prior window holds no delivery the comparison is
    reported as absent (``Diagnosis.has_comparison`` is False, with a note)
    instead of as a move from zero, and keyword-cut and collision findings
    need ``options.min_finding_days`` delivery days in the current window.
    Notes also flag a money scale far below what the thresholds assume and
    targeting types the CPM views skipped.

    Returns a ``Diagnosis``; ``Diagnosis.findings`` is the flat list of
    typed events, each naming the module whose numbers it carries.
    """
    ocfg = options or OrchestrationConfig()
    ds, cfg, windows, anchor = _setup(data, profile, config, windows, ocfg.window_days)
    prof, econ = ds.profile, cfg.economics
    notes: list[str] = []
    if windows.note:
        notes.append(windows.note)
    has_prior = any(windows.prior.contains(r.date) and r.spend > 0 for r in ds.perf)
    has_current = any(windows.current.contains(r.date) and r.spend > 0 for r in ds.perf)
    if not (has_prior and has_current):
        notes.append("no comparison window: " + ("the prior" if has_current else "the current")
                     + " window holds no delivery, so no move is decomposed or attributed")
    scale = _scale_note(ds, cfg, windows)
    if scale:
        notes.append(scale)
    skipped: dict[str, int] = {}
    for r in ds.perf:
        kind = (r.targeting_type or "").strip().lower()
        if r.targeting_unit and kind and kind != (cpm_cfg or CpmConfig()).keyword_targeting_type \
                and kind not in prof.biddable_targeting_types:
            skipped[kind] = skipped.get(kind, 0) + 1
    if skipped:
        notes.append("CPM views skipped rows with targeting_type "
                     + ", ".join(f"'{k}' ({n} rows)" for k, n in sorted(skipped.items()))
                     + ": add the type to PlatformProfile.biddable_targeting_types to treat it as a biddable unit")

    l1 = decompose_rows(ds.perf, windows)
    mix = ad_type_shift_share(ds.perf, windows, prof, ds.entities)
    pareto = pareto_drag_rows(ds.perf, windows.current, prof, ds.entities)
    composition = entry_exit_rows(ds.perf, windows, economics=econ)

    has_keywords = any(r.targeting_unit for r in ds.perf)
    cpm_moves: list[dict] = []
    kw_recs: KeywordRecs | None = None
    leaks: LeakResult | None = None
    if has_keywords:
        cpm_moves = explain_cpm_moves(ds.perf, windows.prior, windows.current,
                                      market_rows=ds.market or None, profile=prof, cfg=cpm_cfg)
        roles = {u.value: u.role for u in ds.targeting_units}
        own_products = [p.product_id for p in ds.products if p.is_own] or None
        kw_recs = keyword_recs(ds.perf, windows.current, roles=roles or None,
                               market_rows=ds.market or None, own_product_ids=own_products,
                               profile=prof, economics=econ)
        leaks = keyword_leaks(ds.perf, windows.current, profile=prof, economics=econ)
    else:
        notes.append("no targeting-unit grain in the delivery feed: targeting-unit (keyword) blocks skipped")

    availability: GeoDrillResult | None = None
    if ds.shelf:
        availability = geo_drill(ds.shelf, windows, roas_by_geo=geo_roas(ds.perf, windows.current, prof),
                                 profile=prof, availability=cfg.availability)
    else:
        notes.append("no shelf feed: availability is not assessed")

    budget_rows, has_budget_feed = _budget_days(ds)
    if not has_budget_feed:
        notes.append("no budget pacing feed: budget-bound tests and raise sizing are not assessed")
    telemetry = build_budget_telemetry(budget_rows, anchor=anchor, days=windows.current.days,
                                       entities=ds.entities, policy=cfg.budget_bound, economics=econ)
    moves = (budget_moves_from_rows(budget_rows, ds.entities, anchor=anchor,
                                    account_roas=l1.roas_comparison or None,
                                    status_map=prof.status_map, economics=econ)
             if has_budget_feed else None)

    actx = context_from_dataset(ds, windows, economics=econ, rule_profile=cfg.rule_profile,
                                telemetry=telemetry, budget_policy=cfg.budget_bound,
                                indirect_weight=cfg.indirect_sales_weight)
    collisions = find_collisions(actx)

    market: Any | None = None
    if ocfg.rca and ds.offtake:
        brand = own_brand or next((p.brand for p in ds.products if p.is_own and p.brand), None) \
            or next((o.brand for o in ds.offtake if o.is_own), None)
        if brand:
            from .rca import run_rca

            market = run_rca(ds, own_brand=brand, segment=segment, date_from=windows.current.start,
                             date_to=windows.current.end, economics=econ)

    # ---- entities -------------------------------------------------------
    spend3 = recent_spend(ds.perf, anchor, cfg.liveness.soft_dead_days)
    assert isinstance(spend3, dict)
    by_entity: dict[str, list[PerfRow]] = {}
    for r in ds.perf:
        by_entity.setdefault(r.entity_id, []).append(r)
    window_spend = {e: sum(r.spend for r in filter_rows(rows, windows.current))
                    for e, rows in by_entity.items()}
    meta = {e.entity_id: e for e in ds.entities}
    if entity_ids is not None:
        chosen = [e for e in entity_ids if e in by_entity and e in meta]
    else:
        live = [e for e in by_entity if e in meta and not dead_for_window(
            meta[e].status, window_spend.get(e), spend3.get(e),
            status_map=prof.status_map)]
        chosen = sorted(live, key=lambda e: (-window_spend.get(e, 0.0), e))[: ocfg.max_entities]
        if len(live) > len(chosen):
            notes.append(f"{len(live) - len(chosen)} smaller entities not diagnosed individually")

    prior = None
    if chosen:
        prior = fit_prior(build_lever_panel(ds.perf, market_rows=ds.market, shelf_rows=ds.shelf,
                                            targeting_units=ds.targeting_units, position=cfg.position),
                          position_lower_is_better=prof.position.lower_is_better)
    pooled = fit_pooled_curve(budget_rows, anchor=anchor) if has_budget_feed else None
    cohorts = role_cohorts(unit_stats(ds.perf, ds.targeting_units, window=windows.current), economics=econ)
    roles_all = {u.value: u.role for u in ds.targeting_units}
    cann = cannibalisation_rate(ds.market, windows.current, roles=roles_all or None) if ds.market else None
    spend7 = recent_spend(ds.perf, anchor, cfg.liveness.stale_days)
    assert isinstance(spend7, dict)
    entities = {
        e: _entity_diagnosis(ds, cfg, windows, anchor, meta[e], by_entity[e], prior, pooled,
                             budget_rows, has_budget_feed, telemetry, cohorts, roles_all, cann,
                             spend3.get(e, 0.0), spend7.get(e, 0.0))
        for e in chosen
    }

    d = Diagnosis(
        windows=windows, layer1=l1, layer1_verdict=layer1_verdict(l1, economics=econ),
        ad_type_mix=mix, pareto=pareto, composition=composition, cpm_moves=cpm_moves,
        availability=availability, keywords=kw_recs, leaks=leaks, collisions=collisions,
        telemetry=telemetry, budget_moves=moves, market=market, entities=entities, findings=[],
        notes=notes, has_comparison=has_prior and has_current, roas_basis=_basis_label(cfg))
    d.findings = _findings(ds, windows, d, ocfg)
    return d


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def _days_for(ds: Dataset, window: Window):
    def days_for(card: Any) -> Mapping[str, Iterable[Any]] | None:
        if card.family != "collision" or not card.keywords:
            return None
        cids = card.evidence.get("cids") or ([card.entity_id] if card.entity_id else [])
        if len(cids) < 2:
            return None
        return _verdict.contested_days(ds.perf, cids, card.keywords[0].kw,
                                       geo=card.evidence.get("geo"), window=window)
    return days_for


def plan(
    data: Any,
    profile: PlatformProfile | None = None,
    config: EngineConfig | None = None,
    *,
    windows: WindowPair | None = None,
    rule_profile: str | None = None,
    goal: RunCheck | Mapping[str, Any] | None = None,
    check: RunCheck | None = None,
    applied: Sequence[AppliedAction] = (),
    operator_floors: Mapping[str, float] | None = None,
    entity_ids: Iterable[str] | None = None,
    families: Iterable[str] | None = None,
    actions_cfg: ActionsConfig | None = None,
    pipeline_cfg: PipelineConfig | None = None,
    verdict_cfg: _verdict.VerdictConfig | None = None,
    budget_neutral: bool = False,
    holdout: Iterable[str] | None = None,
    spine: bool = True,
    ledger: Any | None = None,
    account: str = "",
    options: OrchestrationConfig | None = None,
    plan_id: str | None = None,
    plan_key: str = "",
) -> Plan:
    """Build the reviewable action plan for an account.

    1. ``actions.context_from_dataset`` aggregates the feeds for
       ``windows.current`` versus ``windows.prior``; the cooldown register
       comes from ``applied``.
    2. ``actions.run_families`` emits candidate cards under ``rule_profile``
       (default ``config.rule_profile``): ``"classic"``, ``"marginal"`` or
       ``"verdict"``.
       The holdout is drawn first (``holdout`` names the held-out entities;
       by default a stable 15% of eligible entities is drawn per month, set
       ``PipelineConfig(holdout_pct=0)`` to disable it) and held entities
       are removed from the families' inputs, so a segment's card moves to
       the next-best candidate and no card instructs a change on a held
       entity.
    3. ``actions.build_plan`` scrubs, vetoes, runs the conflict
       table, confidence and expected value, the run ``check`` (a ROAS floor
       or a spend ceiling), caps, waves and ordering.  Under the marginal
       profile budget cards are re-sized on finite-difference marginal ROAS.
       Win rates come from ``ledger`` when given.
    4. ``verdict.run_spine`` (unless ``spine=False``) types every card and
       appends its own sweeps (bleeders, unowned ramps, dark winners,
       untracked spend, probes) for entities that are in scope, outside the
       holdout, not dead and not under a running experiment; a sweep never
       cuts an ad type that has no attributed sales or lacks the lever.
       Collision cards whose campaigns take turns rather than overlap are
       demoted.  ``Plan.verdict_states`` is filled.
    5. ``budget_neutral=True`` (opt-in) funds the plan's raises with donor
       cuts taken from the weakest earners on the verdict scoreboard, never
       from a held-out entity.  It runs on the final live cards, after the
       spine, and each donor must itself pass the spine, so the disclosed
       totals describe the plan that ships.
    6. With a ``ledger`` (``store.ActionLedger``) the plan is saved and its
       predictions recorded under ``(account, profile.name)``.  Saving a
       regenerated plan under an existing id keeps the operator's applied /
       skipped decisions.

    The clock is the latest delivery date, so the same data and options
    give the same plan and the same id (see ``actions.default_plan_id``;
    the rule profile is part of it).  Pass ``plan_key`` (for example a run
    timestamp) to make every generation a new plan, or ``plan_id`` to name
    it.  Entity ROAS counts ``config.indirect_sales_weight`` of indirect
    sales, as in ``diagnose()``.  Returns the ``Plan``; cards are in
    ``Plan.actions``.
    """
    ocfg = options or OrchestrationConfig()
    ds, cfg, windows, anchor = _setup(data, profile, config, windows, ocfg.window_days)
    prof, econ = ds.profile, cfg.economics
    rule_profile = rule_profile or cfg.rule_profile
    applied = list(applied)
    pcfg = pipeline_cfg or PipelineConfig()
    vcfg = verdict_cfg or _verdict.VerdictConfig()
    budget_rows, _ = _budget_days(ds)
    telemetry = build_budget_telemetry(budget_rows, anchor=anchor, days=windows.current.days,
                                       entities=ds.entities, policy=cfg.budget_bound, economics=econ)
    actx: ActionContext = context_from_dataset(
        ds, windows, economics=econ, cfg=actions_cfg, rule_profile=rule_profile, check=check,
        operator_floors=operator_floors, applied=applied, selected_entities=entity_ids,
        telemetry=telemetry, budget_policy=cfg.budget_bound, indirect_weight=cfg.indirect_sales_weight)
    if cfg.availability is not actx.availability_policy:
        actx = replace(actx, availability_policy=cfg.availability)

    live_days: dict[str, set[date]] = {}
    for r in ds.perf:
        if r.spend > 0:
            live_days.setdefault(r.entity_id, set()).add(r.date)
    n_live = {e: len(v) for e, v in live_days.items()}
    winrates = ledger.win_rates(account, prof.name, today=anchor) if ledger is not None else None
    under_test = under_test_entities(applied, anchor, pcfg.under_test_days)
    pctx = plan_context_from_actions(
        actx, today=anchor, cfg=pcfg, lookback_days=windows.current.days,
        under_test=under_test, winrates=winrates, live_days=n_live,
        scope_entity_ids=frozenset(entity_ids) if entity_ids is not None else None)

    # The holdout is drawn BEFORE the families run and the held entities leave the families'
    # view of the account: the next-best candidate is carded instead of the segment's card
    # being lost, and no card instructs a change on a held-out entity.
    if holdout is None:
        held = holdout_ids((e.entity_id for e in pctx.entities.values() if e.eligible), anchor,
                           pcfg.holdout_pct, pcfg.holdout_period)
    else:
        held = sorted(set(holdout))
    fam_ctx = _without_entities(actx, held) if held else actx
    run = run_families(fam_ctx, families)

    donor_excluded = donor_exclusions(applied, anchor, pcfg.under_test_days)
    board = scoreboard_entries(run.board, fam_ctx)
    plan_key = "|".join(x for x in (plan_key, "neutral" if (budget_neutral and spine) else "") if x)
    result = build_plan(
        run.cards, pctx, goal=goal, check=check, day_rows=ds.perf, board=board,
        budget_neutral=budget_neutral and not spine,      # with the spine on, funding is settled after it
        donor_excluded=donor_excluded, holdout=held, plan_id=plan_id, plan_key=plan_key)

    if spine:
        cutoff = [r for r in ds.perf if (anchor - r.date).days < vcfg.freeze_recent_days]
        recent: dict[str, float] = {}
        for r in cutoff:
            recent[r.entity_id] = recent.get(r.entity_id, 0.0) + r.spend
        sctx = _verdict.SpineContext(
            days=windows.current.days, telemetry=telemetry,
            book_max_live_days=max(n_live.values(), default=0) or None,
            open_entities=_verdict.open_entities(applied, anchor, vcfg.lock_days),
            bid_down_entities=_verdict.recent_bid_downs(applied, anchor, vcfg.bid_down_memory_days),
            owned_entities=_verdict.owned_entities(applied), spend_recent=recent,
            untracked_ad_types=prof.no_attribution_types(), allowed_levers=prof.allowed_levers,
            economics=econ, cfg=vcfg)
        scope = set(entity_ids) if entity_ids is not None else set(telemetry)
        scope -= set(result.holdout)          # held-out entities get no sweep cards either
        scope -= set(under_test)              # a sweep card would confound the running experiment
        scope -= {eid for eid, st in pctx.entities.items()          # dead entities receive no card of any family
                  if st.status is not None and is_dead(st.status, st.spend_3d, status_map=prof.status_map)}
        if scope != set(telemetry):
            sctx = sctx.scoped(scope) if scope else replace(sctx, telemetry={})
        res = _verdict.run_spine(result.actions, sctx, days_for=_days_for(ds, windows.current))
        result.actions = list(res.cards)
        tel = dict(result.telemetry, spine=dict(res.telemetry))
        if budget_neutral:
            # Budget-neutral funding is settled on the FINAL live cards: raises the spine trimmed or
            # held need less money, and each donor cut must itself pass the spine before it funds.
            def refused(card: Any) -> bool:
                _verdict.apply_spine([card], sctx)
                return not _verdict.is_act(card)

            rb = rebalance(result.actions, board, pctx, excluded=frozenset(donor_excluded) | frozenset(held),
                           demote=refused)
            for donor in rb.donors:
                expected_value(donor, pctx)
                ent = pctx.entities.get(donor.entity_id)
                if ent is not None:
                    donor.entity_name = donor.entity_name or ent.name
                    donor.entity_status = ent.status
            result.actions = result.actions + rb.donors
            tel["rebalance"] = rb.telemetry()
            if rb.note:
                result.guard_note = " · ".join(x for x in (result.guard_note, rb.note) if x)
            result.n_entities = len({c.entity_id for c in result.actions if c.entity_id is not None})
        result.verdict_states = _verdict.verdict_states(result.actions)
        result.telemetry = tel

    if ledger is not None:
        # save_plan keeps operator decisions when the same plan is regenerated; refresh_emit
        # then lands late-born cards and re-syncs unpaired predictions without touching them.
        ledger.save_plan(account, prof.name, result)
        ledger.refresh_emit(account, prof.name, result)
    return result


def _without_entities(actx: ActionContext, held: Iterable[str]) -> ActionContext:
    """The families' context with the held-out entities removed from every entity-grain input."""
    gone = set(held)
    return replace(
        actx,
        campaigns=[c for c in actx.campaigns if c.entity_id not in gone],
        keywords=[k for k in actx.keywords if k.entity_id not in gone],
        entity_geo=[g for g in actx.entity_geo if g.entity_id not in gone],
        collisions=[r for r in actx.collisions if r.entity_id not in gone],
        _memo={})


# --------------------------------------------------------------------------
# Plain-text report
# --------------------------------------------------------------------------

def _basis_label(cfg: EngineConfig) -> str:
    w = cfg.indirect_sales_weight
    return "direct sales only" if w == 0 else ("direct + indirect sales" if w == 1 else f"direct + {w:g} x indirect sales")


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.1f}%"


def render_report(diagnosis: Diagnosis, action_plan: Plan | None = None, *, max_cards: int = 12) -> str:
    """Render a diagnosis (and optionally a plan) as plain text.

    Display only: every number is read from the two objects.  Returns a
    multi-line string.
    """
    d = diagnosis
    w = d.windows
    out: list[str] = []
    out.append(f"DIAGNOSIS  {w.current.start} .. {w.current.end}  vs  {w.prior.start} .. {w.prior.end}")
    l1 = d.layer1
    if d.has_comparison:
        out.append(f"  ROAS {l1.roas_baseline:.2f} -> {l1.roas_comparison:.2f} ({_pct(l1.roas_pct)})  [direct sales]")
        for name, ch in l1.channels.items():
            share = "" if ch.share_pct is None else f"  share {ch.share_pct:+.0f}%"
            out.append(f"    {name:<6} {ch.baseline:10.3f} -> {ch.comparison:10.3f}  "
                       f"log-points {ch.log_points:+.3f}{share}")
        v = d.layer1_verdict
        out.append(f"  first read: {v.text if v.eligible else 'no drop to explain (' + str(v.reason) + ')'}")
    else:
        out.append(f"  no comparison window: current-window ROAS {l1.roas_comparison:.2f}, nothing to compare it with")
    out.append(f"  ad-type shift-share: primary {d.ad_type_mix.primary}, "
               f"mix {d.ad_type_mix.mix_total:+.3f}, rate {d.ad_type_mix.rate_total:+.3f}")
    for it in d.ad_type_mix.items:
        out.append(f"    {str(it.key):<18} mix {it.mix:+.3f}  rate {it.rate:+.3f}")
    top = [r for r in d.pareto.rows if r.drag > 0][:3]
    if top:
        out.append("  largest drags on account ROAS: "
                   + ", ".join(f"{r.key} ({r.cum_pct:.0f}% cum.)" for r in top))
    out.append("")
    out.append(f"FINDINGS ({len(d.findings)})")
    for f in d.findings:
        out.append(f"  [{f.kind}] {f.summary}")
    if not d.findings:
        out.append("  none")
    out.append("")
    out.append(f"ENTITIES  (ROAS basis: {d.roas_basis})")
    for e in sorted(d.entities.values(), key=lambda x: -x.spend):
        roas = "n/t" if e.attribution is None else ("n/a" if e.roas is None else f"{e.roas:.2f}")
        verdict = e.attribution.verdict if e.attribution is not None else "reach"
        rx = e.prescription
        line = (f"  {e.entity_id:<14} {e.tier:<7} spend {fmt_money(e.spend):>10}  ROAS {roas:>5}  "
                f"{(e.priority or '-'):<3} {str(verdict):<12}")
        if rx is not None:
            line += f" -> {rx.verb}" + (f": {rx.target}" if rx.target else "")
        out.append(line)
    if d.market is not None and getattr(d.market, "status", "") == "ok":
        out.append("")
        out.append("MARKET")
        out.append("  " + d.market.verdict)
    for n in d.notes:
        out.append(f"  note: {n}")
    if action_plan is not None:
        p = action_plan
        out.append("")
        out.append(f"PLAN  {p.id}  profile {p.generator}  {len(p.actionable)} actionable of {len(p.actions)} cards")
        if p.verdict_states:
            out.append("  verdicts: " + ", ".join(f"{k} {n}" for k, n in sorted(p.verdict_states.items())))
        if p.holdout:
            out.append("  holdout (untouched this period): " + ", ".join(p.holdout))
        for c in p.actions[:max_cards]:
            state = (c.verdict or {}).get("state", "-")
            kws = ", ".join(c.keyword_values()[:3])
            step = ""
            if c.change is not None and c.change.pct is not None:
                step = f" {c.change.pct:+.0f}%"
            out.append(f"  w{c.wave or '-'} {state:<9} {c.status:<9} conf {c.confidence or 0:>2}  "
                       f"{c.title}{step}  [{c.entity_id or c.segment or 'account'}"
                       + (f": {kws}" if kws else "") + "]")
            if c.trigger:
                out.append(f"       why: {c.trigger}")
        if len(p.actions) > max_cards:
            out.append(f"  ... {len(p.actions) - max_cards} more")
        if p.guard_note:
            out.append(f"  note: {p.guard_note}")
    return "\n".join(out)
