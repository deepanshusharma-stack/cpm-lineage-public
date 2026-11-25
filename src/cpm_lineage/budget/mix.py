"""Ad-type (channel) mix: did account ROAS move because spend shifted
between ad types (mix) or because ROAS changed inside a type (rate), and
which type can absorb budget without hitting its plateau.

The decomposition is the exact midpoint shift-share:
``sum(mix_i) + sum(rate_i) == account ROAS change``.  Never combine it with
the prior-rate two-factor convention inside one identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from ..config import Economics
from ..decompose import DecomposeConfig, DragRow, ShiftShareInput, pareto_drag, shift_share
from .config import BudgetConfig, money_of
from .saturation import CurveFit, daily_series, fit_curve, marginal_raw

__all__ = ["TypeMix", "DragRow", "MixResult", "MixAction", "MixRecommendation",
           "ad_type_mix", "entity_drag", "attach_plateau", "recommend_mix"]

_CFG = BudgetConfig()


@dataclass
class TypeMix:
    """One ad type's contribution to the account ROAS change."""

    ad_type: str
    spend_cur: float
    spend_prev: float
    sales_cur: float
    sales_prev: float
    share_cur: float
    share_prev: float
    roas_cur: float | None
    roas_prev: float | None
    mix: float                 # (w_cur - w_prev) * (r_cur + r_prev) / 2
    rate: float                # (w_cur + w_prev) / 2 * (r_cur - r_prev)
    total: float               # mix + rate
    drillable: bool = True     # has targeting-unit grain data
    no_attrib: bool = False    # platform attributes no sales to this type
    mroas: float | None = None          # marginal ROAS at current mean daily spend
    near_plateau: bool | None = None    # None = no curve


@dataclass
class MixResult:
    """Output of ``ad_type_mix``: types sorted by ``total`` ascending (biggest drag first)."""

    types: list[TypeMix]
    roas_cur: float | None
    roas_prev: float | None
    delta: float               # roas_cur - roas_prev (0 when either is undefined)
    mix_total: float
    rate_total: float
    residual: float            # delta - (mix_total + rate_total); ~0 by construction
    orphan_sales_cur: float = 0.0   # sales on types with no spend in the period: excluded from roas_cur
    orphan_sales_prev: float = 0.0


@dataclass(frozen=True)
class MixAction:
    """One recommendation line.  ``kind`` is machine-readable."""

    kind: str                  # "feed" | "cap" | "judge_halo" | "reduce"
    ad_type: str
    reason: str


@dataclass
class MixRecommendation:
    primary: str               # "MIX" | "RATE"
    actions: list[MixAction] = field(default_factory=list)
    all_near_plateau: bool = False


def _type_of(row: Any, entity_types: Mapping[str, str | None] | None,
             canonical: Callable[[str | None], str] | None) -> str:
    raw = getattr(row, "ad_type", None)
    if raw is None and entity_types is not None:
        raw = entity_types.get(row.entity_id)
    if canonical is not None:
        return canonical(raw)
    return raw if raw else "Unknown"


def _sums(rows: Iterable[Any], entity_types, canonical) -> dict[str, list[float]]:
    acc: dict[str, list[float]] = {}
    for r in rows:
        a = acc.setdefault(_type_of(r, entity_types, canonical), [0.0, 0.0])
        a[0] += r.spend or 0.0
        a[1] += r.sales or 0.0
    return acc


def ad_type_mix(
    prior_rows: Iterable[Any],
    current_rows: Iterable[Any],
    *,
    entity_types: Mapping[str, str | None] | None = None,
    canonical: Callable[[str | None], str] | None = None,
    no_attribution_types: Iterable[str] = (),
    drillable_types: Iterable[str] | None = None,
) -> MixResult:
    """Decompose the account ROAS change into ad-type mix and rate effects.

    Per canonical ad type ``i`` with spend share ``w`` and ROAS ``r``:
    ``mix_i = (w_cur - w_prev) * (r_cur + r_prev) / 2``,
    ``rate_i = (w_cur + w_prev) / 2 * (r_cur - r_prev)``.  A type absent
    from a period has share 0 and ROAS taken as 0 there, which keeps the sum
    exact.  ``canonical`` folds raw labels (pass
    ``profile.canonical_ad_type``); ``entity_types`` supplies a type for
    rows that carry none.

    ``roas_cur`` / ``roas_prev`` are sales over spend of the types that
    spent in that period; sales recorded on a type with no spend there are
    reported as ``orphan_sales_*`` and left out of the blended ROAS, which is
    what keeps the identity exact.

    Returns a ``MixResult``; ``mix_total + rate_total`` equals ``delta``.
    """
    prev = _sums(prior_rows, entity_types, canonical)
    cur = _sums(current_rows, entity_types, canonical)
    no_attr = set(no_attribution_types)
    drill = None if drillable_types is None else set(drillable_types)
    # The midpoint arithmetic is the library's single shift-share primitive.
    res = shift_share(
        [ShiftShareInput(key=t, weight_prior=prev.get(t, [0.0, 0.0])[0], value_prior=prev.get(t, [0.0, 0.0])[1],
                         weight_current=cur.get(t, [0.0, 0.0])[0], value_current=cur.get(t, [0.0, 0.0])[1])
         for t in sorted(set(prev) | set(cur))],
        one_sided="zero", sort=False)
    types: list[TypeMix] = []
    for it in res.items:
        types.append(TypeMix(
            ad_type=it.key, spend_cur=it.weight_current, spend_prev=it.weight_prior,
            sales_cur=it.value_current, sales_prev=it.value_prior,
            share_cur=it.share_current, share_prev=it.share_prior,
            roas_cur=it.rate_current if it.weight_current > 0 else None,
            roas_prev=it.rate_prior if it.weight_prior > 0 else None,
            mix=it.mix, rate=it.rate, total=it.total,
            drillable=True if drill is None else it.key in drill, no_attrib=it.key in no_attr))
    types.sort(key=lambda x: (x.total, x.ad_type))
    # Headline from the same primitive, so mix + rate equals delta by construction.  Sales on a
    # type with no spend in a period (lagged attribution after a pause) have no rate; they are
    # reported as orphan sales instead of being folded into the blended ROAS.
    tp = sum(v[0] for v in prev.values())
    tc = sum(v[0] for v in cur.values())
    roas_p = res.rate_prior if tp > 0 else None
    roas_c = res.rate_current if tc > 0 else None
    return MixResult(types=types, roas_cur=roas_c, roas_prev=roas_p, delta=res.delta,
                     mix_total=res.mix_total, rate_total=res.rate_total, residual=res.delta - (res.mix_total + res.rate_total),
                     orphan_sales_cur=res.orphan_value_current, orphan_sales_prev=res.orphan_value_prior)


def entity_drag(
    current_rows: Iterable[Any],
    *,
    entity_types: Mapping[str, str | None] | None = None,
    canonical: Callable[[str | None], str] | None = None,
    no_attribution_types: Iterable[str] = (),
    cfg: BudgetConfig | None = None,
) -> tuple[list[DragRow], int]:
    """Find the few entities that pull account ROAS down.

    Current period, excluding ad types with no attributed sales.  A thin
    wrapper over :func:`cpm_lineage.decompose.pareto_drag`, the library's
    one definition of drag: ``drag = spend * (account_roas - entity_roas)``,
    ``cum_pct`` in percent of positive drag.

    Returns ``(rows, n80)``: the positive-drag
    :class:`~cpm_lineage.decompose.DragRow` rows in descending order (``key``
    is the entity id) and the smallest number of entities whose cumulative
    drag reaches ``pareto_share``.
    """
    cfg = cfg or _CFG
    no_attr = set(no_attribution_types)
    items = [{"key": r.entity_id, "spend": r.spend or 0.0, "sales": r.sales or 0.0}
             for r in current_rows if _type_of(r, entity_types, canonical) not in no_attr]
    res = pareto_drag(items, cfg=DecomposeConfig(pareto_top_n=max(len(items), 1),
                                                 pareto_cum_pct=100.0 * cfg.pareto_share))
    return [r for r in res.rows if r.drag > 0], res.n80


def attach_plateau(
    result: MixResult,
    history_rows: Iterable[Any],
    current_days: int,
    *,
    entity_types: Mapping[str, str | None] | None = None,
    canonical: Callable[[str | None], str] | None = None,
    cfg: BudgetConfig | None = None,
) -> dict[str, CurveFit]:
    """Attach a saturation hint to each ad type of a mix result (in place).

    Daily series per canonical type over ``sat_lookback_days``, summed per
    (type, date) first so a mid-period label rename cannot split a day.
    Types with fewer than ``sat_min_days`` points are skipped.  ``mroas`` is
    the marginal ROAS at the current-window *mean* daily spend
    (``spend_cur / current_days``); ``near_plateau = mroas <
    plateau_marginal``.

    Returns the fits by ad type.
    """
    cfg = cfg or _CFG
    rows = list(history_rows)
    by_type: dict[str, list[Any]] = {}
    for r in rows:
        by_type.setdefault(_type_of(r, entity_types, canonical), []).append(r)
    anchor = max((r.date for r in rows), default=None)
    fits: dict[str, CurveFit] = {}
    for t in result.types:
        x, y = daily_series(by_type.get(t.ad_type, []), anchor=anchor, cfg=cfg)
        if len(x) < cfg.sat_min_days:
            continue
        f = fit_curve(x, y, cfg)
        fits[t.ad_type] = f
        if f.fit and current_days > 0:
            m = marginal_raw(f, t.spend_cur / current_days)
            t.mroas = None if m is None else round(m, 2)
            t.near_plateau = bool(m is not None and m < cfg.plateau_marginal)
    return fits


def recommend_mix(result: MixResult, cfg: BudgetConfig | None = None,
                  economics: Economics | None = None) -> MixRecommendation:
    """Recommend where to move ad-type budget.

    * ``primary = "MIX"`` if ``|sum mix| >= |sum rate|`` else ``"RATE"``
    * material types: current spend above the material-spend threshold
    * **feed**: the material type with the highest current ROAS that is not
      near its plateau (all material types if every one is near plateau -
      flagged ``all_near_plateau``)
    * **cap**: material types whose share rose while their ROAS is below
      the account ROAS
    * **judge_halo**: types with no attributed sales and spend > 0 - judge
      on reach / new users before cutting
    * **reduce**: non-drillable types below breakeven with spend above the
      reduce threshold

    Returns a ``MixRecommendation``.
    """
    cfg = cfg or _CFG
    eco = economics or Economics()
    material_bar = money_of(cfg.material_type_spend, eco)
    reduce_bar = money_of(cfg.reduce_type_spend, eco)
    rec = MixRecommendation(primary="MIX" if abs(result.mix_total) >= abs(result.rate_total) else "RATE")
    material = [t for t in result.types if t.spend_cur > material_bar and not t.no_attrib
                and t.roas_cur is not None]
    sinks = [t for t in material if not t.near_plateau]
    if material and not sinks:
        rec.all_near_plateau = True
        sinks = material
    if sinks:
        best = max(sinks, key=lambda t: (t.roas_cur or 0.0, t.ad_type))
        rec.actions.append(MixAction("feed", best.ad_type,
                                     "highest current return among material types with headroom"))
    for t in material:
        if (t.share_cur > t.share_prev and result.roas_cur is not None
                and (t.roas_cur or 0.0) < result.roas_cur):
            rec.actions.append(MixAction("cap", t.ad_type,
                                         "gained spend share while returning less than the account"))
    for t in result.types:
        if t.no_attrib and t.spend_cur > 0:
            rec.actions.append(MixAction("judge_halo", t.ad_type,
                                         "no attributed sales by design: judge on reach or new users before cutting"))
        elif (not t.drillable and t.roas_cur is not None and t.roas_cur < eco.be
              and t.spend_cur > reduce_bar):
            rec.actions.append(MixAction("reduce", t.ad_type,
                                         "below breakeven with no finer grain to fix: reduce or cap"))
    return rec
