"""Account-level budget moves: scale starved winners, tighten funded
bleeders, release parked budget.

Invariant preserved everywhere in this module: a recommended raise's
incremental revenue is never less than its incremental spend, a raise goes
only up to the point where marginal ROAS reaches 1, and "+50%" is an upper
bound.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from ..config import Economics
from ..liveness import canonical_status
from ..schema import BudgetDayRow, Entity
from .config import BudgetConfig
from .saturation import CurveFit, fit_entity_curve, marginal, predict, unit_marginal_spend

__all__ = ["MoveInput", "ScaleMove", "TightenMove", "ReleaseMove", "BudgetMoves",
           "move_inputs", "budget_moves", "budget_moves_from_rows", "size_raise"]

_CFG = BudgetConfig()


@dataclass(frozen=True)
class MoveInput:
    """One entity's facts for the budget-constraint scan."""

    entity_id: str
    daily_budget: float
    act_days: int                 # days with spend > 0
    avg_spend: float              # mean spend on active days
    roas: float | None            # ratio of sums
    capped_days: int              # active days with spend >= cap_hit_fraction * cap
    status: str | None = None
    name: str | None = None
    ad_type: str | None = None

    @property
    def util(self) -> float:
        return self.avg_spend / self.daily_budget if self.daily_budget > 0 else 0.0


@dataclass
class ScaleMove:
    """A starved winner.  ``sized`` is ``"curve"`` when the saturation curve
    sized the raise, ``"linear"`` before any curve pass, ``None`` when the
    marginal return could not be verified."""

    entity_id: str
    daily_budget: float
    roas: float
    util: float
    extra_daily_sales: float | None     # incremental daily sales of the raise
    raise_to: float | None = None
    inc_spend: float | None = None
    mroas: float | None = None
    sized: str | None = "linear"
    marginal_unverified: bool = False
    var_starved: bool = False
    confidence: str = "low"             # "high" when curve-sized
    advice: str = ""
    estimator: str = "linear_average"   # which money estimator produced extra_daily_sales
    name: str | None = None


@dataclass(frozen=True)
class TightenMove:
    """A binding cap funding spend below breakeven."""

    entity_id: str
    daily_budget: float
    roas: float
    daily_waste: float                  # avg_spend * (1 - roas / breakeven)
    name: str | None = None


@dataclass(frozen=True)
class ReleaseMove:
    """Parked budget: low utilisation and below breakeven."""

    entity_id: str
    daily_budget: float
    roas: float | None
    util: float
    freeable: float
    name: str | None = None


@dataclass
class BudgetMoves:
    """Output of ``budget_moves``."""

    scale: list[ScaleMove] = field(default_factory=list)
    tighten: list[TightenMove] = field(default_factory=list)
    release: list[ReleaseMove] = field(default_factory=list)
    past_peak: list[str] = field(default_factory=list)   # entity ids ejected from scale by the curve
    freed: float = 0.0                                    # sum of freeable budget
    need: float = 0.0                                     # sum of the first-pass raises on the scale list
    realloc_note: str | None = None


def move_inputs(
    budget_days: Iterable[BudgetDayRow],
    entities: Iterable[Entity] | Mapping[str, Entity] | None = None,
    *,
    anchor: date | None = None,
    days: int | None = None,
    cfg: BudgetConfig | None = None,
) -> list[MoveInput]:
    """Build scan inputs from the entity x day pacing feed.

    Window: the ``days`` ending on ``anchor`` (default latest feed date).
    Daily budget is the entity's configured budget, else its latest row cap.
    Only entities with a positive budget and at least ``min_active_days``
    spending days are returned.
    """
    cfg = cfg or _CFG
    days = days or cfg.moves_days
    rows = list(budget_days)
    if not rows:
        return []
    anchor = anchor or max(r.date for r in rows)
    start = anchor - timedelta(days=days - 1)
    if entities is None:
        meta: Mapping[str, Entity] = {}
    elif isinstance(entities, Mapping):
        meta = entities
    else:
        meta = {e.entity_id: e for e in entities}
    by: dict[str, list[BudgetDayRow]] = {}
    for r in rows:
        if start <= r.date <= anchor:
            by.setdefault(r.entity_id, []).append(r)
    out: list[MoveInput] = []
    for eid, ds in sorted(by.items()):
        ds.sort(key=lambda r: r.date)
        ent = meta.get(eid)
        caps = [d.daily_budget for d in ds if d.daily_budget is not None]
        db = (ent.daily_budget if ent is not None and ent.daily_budget else None) or (caps[-1] if caps else None)
        act = [d for d in ds if d.spend and d.spend > 0]
        if not db or db <= 0 or len(act) < cfg.min_active_days:
            continue
        sp = sum(d.spend for d in act)
        out.append(MoveInput(
            entity_id=eid, daily_budget=db, act_days=len(act), avg_spend=sp / len(act),
            roas=(sum(d.sales or 0.0 for d in act) / sp) if sp > 0 else None,
            capped_days=sum(1 for d in act if d.spend >= cfg.cap_hit_fraction * db),
            status=(ent.status if ent is not None and ent.status else None) or ds[-1].status,
            name=ent.name if ent else None, ad_type=ent.ad_type if ent else None))
    return out


def size_raise(fit: CurveFit, daily_budget: float,
               cfg: BudgetConfig | None = None) -> tuple[float, float, float] | None:
    """Size a raise on the curve, or reject it.

    ``new_cap = min((1 + scale_step) * cap, ceiling)``; with no identified
    ceiling inside the supported range (near-linear data: the most headroom)
    the raise goes to ``(1 + scale_step) * cap``.  Rejected (``None``) when
    the ceiling is at or below the cap, or when ``inc_rev = predict(new_cap)
    - predict(cap)`` is less than ``inc_spend = new_cap - cap``.

    The target is clamped to the marginal-1 spend ``b - theta`` whether or
    not that point lies inside the supported range (``sat_spend`` being
    ``None`` is a display decision, not permission to pass it), and a
    ceiling within ``range_min_headroom`` of the cap is no raise at all.

    Returns ``(new_cap, inc_rev, inc_spend)`` or ``None``.
    """
    cfg = cfg or _CFG
    target = (1.0 + cfg.scale_step) * daily_budget
    sat = unit_marginal_spend(fit)
    if sat is not None:
        if sat <= daily_budget * (1.0 + cfg.range_min_headroom):
            return None
        target = min(target, sat)
    a, b = predict(fit, target), predict(fit, daily_budget)
    if a is None or b is None:
        return None
    inc_rev, inc_spend = a - b, target - daily_budget
    if inc_rev < inc_spend:
        return None
    return target, inc_rev, inc_spend


def budget_moves(
    inputs: Sequence[MoveInput],
    *,
    account_roas: float | None = None,
    fits: Mapping[str, CurveFit] | None = None,
    status_map: Mapping[str, str] | None = None,
    cfg: BudgetConfig | None = None,
    economics: Economics | None = None,
) -> BudgetMoves:
    """Sort entities into scale / tighten / release budget moves.

    Per entity (positive budget, ``act_days >= min_active_days``), with
    ``util = avg_spend / cap`` and ``binds = capped_days >= capped_day_share
    * act_days``:

    1. **release** if ``util < release_util_max AND roas < release_roas_max
       * be AND status not stopped/held AND not binds``;
       ``freeable = min(release_fraction * cap, avg_spend)`` - bounded by
       realised spend, because a share of a cap that never binds frees
       nothing.
    2. otherwise only when the cap binds:
       **scale** if ``roas >= scale_bar`` and (account ROAS unknown or
       ``roas >= account_roas``), first-pass ``extra_daily_sales =
       scale_step * cap * roas``;
       else **tighten** if ``roas < tighten_roas_max * be``,
       ``daily_waste = avg_spend * (1 - roas / be)`` (equals ``avg_spend *
       (1 - roas)`` at the default breakeven).
    3. sort scale by ROAS desc, tighten by waste desc, release by freeable
       desc; keep ``moves_top_n`` of each.

    Curve pass over the scale list when ``fits`` is given (a mapping of
    entity id to its 90-day ``CurveFit``; a missing entry counts as "no
    fit"): a usable fit sizes the raise with ``size_raise`` (``sized =
    "curve"``, high confidence) or ejects the entity as past peak; no usable
    fit keeps the row flagged ``marginal_unverified`` with no sales figure
    and the advice to raise as a small test.  With
    ``moves_require_decision_r2`` a fit below the decision r2 is "not
    usable" (with it off, any accepted fit may size or eject).

    ``freed`` / ``need`` total the release and first-pass scale amounts;
    ``realloc_note`` is set when both are non-zero (reallocation, not new
    money).

    Returns a ``BudgetMoves``.
    """
    cfg = cfg or _CFG
    eco = economics or Economics()
    blocked = set(cfg.release_blocked_statuses)
    scale: list[ScaleMove] = []
    tighten: list[TightenMove] = []
    release: list[ReleaseMove] = []
    for m in inputs:
        if m.daily_budget <= 0 or m.act_days < cfg.min_active_days:
            continue
        binds = m.capped_days >= cfg.capped_day_share * m.act_days
        st = canonical_status(m.status, status_map).value
        if (m.util < cfg.release_util_max and m.roas is not None
                and m.roas < cfg.release_roas_max * eco.be and st not in blocked and not binds):
            release.append(ReleaseMove(m.entity_id, m.daily_budget, m.roas, m.util,
                                       min(cfg.release_fraction * m.daily_budget, m.avg_spend), m.name))
            continue
        if not binds or m.roas is None:
            continue
        if m.roas >= eco.scale_bar and (account_roas is None or m.roas >= account_roas):
            scale.append(ScaleMove(m.entity_id, m.daily_budget, m.roas, m.util,
                                   extra_daily_sales=cfg.scale_step * m.daily_budget * m.roas,
                                   name=m.name))
        elif m.roas < cfg.tighten_roas_max * eco.be:
            tighten.append(TightenMove(m.entity_id, m.daily_budget, m.roas,
                                       m.avg_spend * (1.0 - m.roas / eco.be), m.name))
    scale.sort(key=lambda r: (-r.roas, r.entity_id))
    tighten.sort(key=lambda r: (-r.daily_waste, r.entity_id))
    release.sort(key=lambda r: (-r.freeable, r.entity_id))
    scale, tighten, release = scale[:cfg.moves_top_n], tighten[:cfg.moves_top_n], release[:cfg.moves_top_n]

    past_peak: list[str] = []
    if fits is not None:
        kept: list[ScaleMove] = []
        for row in scale:
            f = fits.get(row.entity_id)
            usable = bool(f is not None and f.fit and (
                not cfg.moves_require_decision_r2 or (f.r2 or 0.0) >= cfg.sat_decision_r2))
            if usable and f is not None:
                row.mroas = marginal(f, row.daily_budget)
                sized = size_raise(f, row.daily_budget, cfg)
                if sized is None:
                    past_peak.append(row.entity_id)
                    continue
                row.raise_to, row.extra_daily_sales, row.inc_spend = sized
                row.sized, row.confidence, row.estimator = "curve", "high", "saturation_curve"
            else:
                row.marginal_unverified = True
                row.extra_daily_sales = None
                row.sized, row.confidence, row.estimator = None, "low", "none"
                row.var_starved = bool(f is not None and f.reason == "var_starved")
                row.advice = "raise as a small test (for example +20%) and re-check in 7 days"
                if f is not None and f.fit:
                    row.mroas = marginal(f, row.daily_budget)
            kept.append(row)
        scale = kept

    freed = sum(r.freeable for r in release)
    need = sum(cfg.scale_step * r.daily_budget for r in scale)
    note = None
    if freed > 0 and need > 0:
        note = ("parked budget on the release list can fund part of the scale list: "
                "this is reallocation of allocated budget, not new money")
    return BudgetMoves(scale=scale, tighten=tighten, release=release, past_peak=past_peak,
                       freed=freed, need=need, realloc_note=note)


def budget_moves_from_rows(
    budget_days: Iterable[BudgetDayRow],
    entities: Iterable[Entity] | Mapping[str, Entity] | None = None,
    *,
    anchor: date | None = None,
    account_roas: float | None = None,
    status_map: Mapping[str, str] | None = None,
    cfg: BudgetConfig | None = None,
    economics: Economics | None = None,
) -> BudgetMoves:
    """Run the scan end to end from the pacing feed.

    Builds inputs over ``moves_days``, computes the account ROAS over the
    same window when not supplied, fits each entity's curve over
    ``sat_lookback_days`` and calls ``budget_moves``.  Returns a
    ``BudgetMoves``.
    """
    cfg = cfg or _CFG
    rows = list(budget_days)
    ents = list(entities.values()) if isinstance(entities, Mapping) else list(entities or ())
    inputs = move_inputs(rows, ents, anchor=anchor, cfg=cfg)
    if not rows:
        return BudgetMoves()
    anchor = anchor or max(r.date for r in rows)
    if account_roas is None:
        start = anchor - timedelta(days=cfg.moves_days - 1)
        sp = sum(r.spend or 0.0 for r in rows if start <= r.date <= anchor)
        sa = sum(r.sales or 0.0 for r in rows if start <= r.date <= anchor)
        account_roas = sa / sp if sp > 0 else None
    fits = {m.entity_id: fit_entity_curve(rows, m.entity_id, anchor=anchor, cfg=cfg) for m in inputs}
    return budget_moves(inputs, account_roas=account_roas, fits=fits, status_map=status_map,
                        cfg=cfg, economics=economics)
