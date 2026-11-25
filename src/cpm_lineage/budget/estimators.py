"""The three money estimators.

The engine puts a currency number on a budget action in three independent
ways.  **They share no model, answer slightly different questions and are
not reconciled with one another** - the same raise can carry three different
numbers.  Every result here therefore carries an ``estimator`` label; show
that label next to the number and never add or average across estimators.

==================  ===========================================================
label               what it is
==================  ===========================================================
``stake_ev``        (a) rule-of-thumb stake per action and its expected value
                    per day, using *average* ROAS (or a marginal if supplied)
``saturation_curve``  (b) the fitted concave curve: predicted sales difference
                    between two daily spend levels
``finite_difference``  (c) marginal ROAS from high-spend days versus low-spend
                    days of the same entity
==================  ===========================================================

Known inconsistency kept on purpose: the expected-value "bleed avoided"
term uses a literal ``1 - ROAS`` while stakes use the account's effective
breakeven.  ``BudgetConfig.ev_breakeven_relative`` switches it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

from ..config import Economics, TradingDay
from .config import BudgetConfig, money_of
from .saturation import CurveFit, predict

__all__ = [
    "ESTIMATORS", "MoneyEstimate", "EVEstimate", "MarginalAdjustment",
    "stake_budget_cut", "stake_raise_underserved", "stake_raise_exhaust", "exhaust_raise_pct",
    "expected_value", "curve_increment", "finite_difference_marginal",
    "marginal_roas_by_entity", "apply_marginal", "compare_estimators",
]

_CFG = BudgetConfig()

ESTIMATORS: tuple[str, ...] = ("stake_ev", "saturation_curve", "finite_difference")


@dataclass(frozen=True)
class MoneyEstimate:
    """A currency number with the estimator that produced it."""

    estimator: str
    per_day: float | None        # incremental sales (or avoided loss) per day
    basis: str = ""              # one line: what was multiplied by what
    marginal_roas: float | None = None


@dataclass(frozen=True)
class EVEstimate:
    """Expected value of an action (estimator ``stake_ev``)."""

    est_day: float               # estimated currency per day if the action works
    p_success: float
    ev_kind: str                 # "money" = ad money moved; "opportunity" = market-size stake
    ev_day: float                # est_day * p_success
    ev_density: float | None     # ev per currency unit of budget moved (budget actions only)
    estimator: str = "stake_ev"


@dataclass(frozen=True)
class MarginalAdjustment:
    """Effect of the finite-difference marginal on one budget step."""

    action: str                  # "keep" | "veto" | "halve" | "deepen" | "exempt" | "no_estimate"
    pct: float                   # step after adjustment, percent (negative = cut)
    to: float | None             # new budget implied by the adjusted step
    marginal: float | None       # marginal used, after any ad-type deflator
    note: str = ""
    estimator: str = "finite_difference"


# --- (a) stake and expected value ------------------------------------------------

def stake_budget_cut(spend: float, roas: float, economics: Economics | None = None) -> float:
    """Stake of cutting a sub-breakeven budget: ``spend * max(be - roas, 0) / be``.

    The slice of window spend that returned below breakeven.
    """
    be = (economics or Economics()).be
    return spend * max(be - roas, 0.0) / be


def stake_raise_underserved(spend: float, roas: float, threshold: float, pct: float) -> float:
    """Stake of raising a budget that serves below its expected hours:
    ``spend * (roas - threshold) * pct / 100`` (``threshold`` = the ROAS bar
    the rule used, ``pct`` = raise step in percent).
    """
    return spend * (roas - threshold) * pct / 100.0


def stake_raise_exhaust(spend: float, roas: float, pct: float,
                        economics: Economics | None = None,
                        cfg: BudgetConfig | None = None) -> float:
    """Stake of raising a budget that exhausts mid-day:
    ``spend * max(min(roas, stake_roas_cap_mult * be) - be, 0) * pct / 100``.
    The ROAS cap stops outliers dominating the ranking.
    """
    cfg = cfg or _CFG
    be = (economics or Economics()).be
    return spend * max(min(roas, cfg.stake_roas_cap_mult * be) - be, 0.0) * pct / 100.0


def exhaust_raise_pct(exhaust_hour: float | None, trading_day: TradingDay | None = None,
                      cfg: BudgetConfig | None = None) -> float | None:
    """Raise step that funds serving up to the funding target hour.

    ``pct = clamp(round(100 * max(0, target - exhaust_hour) / exhaust_hour *
    haircut), raise_pct_min, raise_pct_max)``.  ``None`` without a positive
    exhaust hour.
    """
    cfg = cfg or _CFG
    td = trading_day or TradingDay()
    if exhaust_hour is None or exhaust_hour <= 0:
        return None
    raw = round(100.0 * max(0.0, td.funding_target_hour - exhaust_hour) / exhaust_hour * cfg.exhaust_haircut)
    return float(min(max(raw, cfg.raise_pct_min), cfg.raise_pct_max))


def expected_value(
    *,
    lever: str,
    direction: str,
    frm: float | None = None,
    to: float | None = None,
    roas: float | None = None,
    mroas: float | None = None,
    stake: float = 0.0,
    days: int = 30,
    leak: float | None = None,
    win_rate: float | None = None,
    n_outcomes: int = 0,
    cfg: BudgetConfig | None = None,
    economics: Economics | None = None,
) -> EVEstimate:
    """Expected value per day of one action (estimator ``stake_ev``).

    ``ev_roas`` is the marginal ROAS when supplied, else the average ROAS.

    * budget up: ``est = max(to - frm, 0) * max(ev_roas, 0)``
    * budget down: ``est = |to - frm| * max(1 - ev_roas, 0)``
    * keyword action with a known leak: ``est = leak / days``
    * anything else: ``est = stake / days`` with ``ev_kind = "opportunity"``
      (market-size stakes are far larger than the ad money moved and must
      never outrank ``"money"`` in a ranking)

    ``p_success`` is the family's realised win rate when it has at least
    ``ev_min_outcomes`` paired outcomes, else ``ev_prior``.
    ``ev_day = est * p``; ``ev_density = ev_day / |to - frm|`` for budget
    actions.

    Returns an ``EVEstimate``.
    """
    cfg = cfg or _CFG
    be = (economics or Economics()).be
    ev_roas = mroas if mroas is not None else roas
    kind = "money"
    density_base: float | None = None
    if lever == "budget" and frm is not None and to is not None and ev_roas is not None \
            and direction in ("up", "down"):
        delta = to - frm
        if direction == "up":
            est = max(delta, 0.0) * max(ev_roas, 0.0)
        else:
            bar = be if cfg.ev_breakeven_relative else 1.0
            est = abs(delta) * max(bar - ev_roas, 0.0)
        density_base = abs(delta)
    elif lever == "keyword" and leak:
        est = leak / max(days, 1)
    else:
        est = stake / max(days, 1)
        kind = "opportunity"
    p = win_rate if (win_rate is not None and n_outcomes >= cfg.ev_min_outcomes) else cfg.ev_prior
    ev = est * p
    return EVEstimate(est_day=est, p_success=p, ev_kind=kind, ev_day=ev,
                      ev_density=(ev / density_base) if density_base else None)


# --- (b) saturation curve ---------------------------------------------------------

def curve_increment(fit: CurveFit | None, frm: float, to: float) -> MoneyEstimate | None:
    """Incremental daily sales of moving daily spend ``frm -> to`` on the
    fitted curve (estimator ``saturation_curve``): ``predict(to) -
    predict(frm)``.  ``None`` without a fit.
    """
    a, b = predict(fit, to), predict(fit, frm)
    if a is None or b is None:
        return None
    inc = a - b
    return MoneyEstimate("saturation_curve", inc, "predict(to) - predict(from)",
                         marginal_roas=(inc / (to - frm)) if to != frm else None)


# --- (c) finite-difference marginal ROAS ------------------------------------------

def finite_difference_marginal(spend: Sequence[float], sales: Sequence[float],
                               cfg: BudgetConfig | None = None,
                               economics: Economics | None = None) -> float | None:
    """Marginal ROAS from an entity's own high-spend versus low-spend days.

    Keep days with spend > 0; need at least ``fd_min_days``.  Sort by spend,
    ``lo`` = first ``n // 2`` days, ``hi`` = the rest; ``mroas = max(0,
    (mean sales hi - mean sales lo) / (mean spend hi - mean spend lo))``
    when the spend gap exceeds the minimum gap.

    Returns the estimate or ``None`` (too few days or no spend contrast).
    """
    cfg = cfg or _CFG
    pts = sorted((float(s), float(v)) for s, v in zip(spend, sales) if s is not None and s > 0)
    n = len(pts)
    if n < cfg.fd_min_days:
        return None
    lo, hi = pts[: n // 2], pts[n // 2:]
    dsp = sum(p[0] for p in hi) / len(hi) - sum(p[0] for p in lo) / len(lo)
    drv = sum(p[1] for p in hi) / len(hi) - sum(p[1] for p in lo) / len(lo)
    if dsp <= money_of(cfg.fd_min_spend_gap, economics):
        return None
    return max(0.0, drv / dsp)


def marginal_roas_by_entity(rows: Iterable[Any], *, start: date | None = None,
                            end: date | None = None, days: int | None = None,
                            cfg: BudgetConfig | None = None,
                            economics: Economics | None = None) -> dict[str, float]:
    """Finite-difference marginal ROAS for every entity with an estimate.

    ``rows`` are dated records with ``entity_id``, ``spend`` and ``sales``;
    they are summed to entity x day inside ``[start, end]`` (or the last
    ``days`` ending on the latest date).  Returns ``{entity_id: mroas >= 0}``.
    """
    rows = list(rows)
    if not rows:
        return {}
    end = end or max(r.date for r in rows)
    if start is None and days:
        start = end - timedelta(days=days - 1)
    acc: dict[str, dict[date, list[float]]] = {}
    for r in rows:
        if r.date > end or (start is not None and r.date < start):
            continue
        a = acc.setdefault(r.entity_id, {}).setdefault(r.date, [0.0, 0.0])
        a[0] += r.spend or 0.0
        a[1] += r.sales or 0.0
    out: dict[str, float] = {}
    for eid, by_day in acc.items():
        m = finite_difference_marginal([v[0] for v in by_day.values()],
                                       [v[1] for v in by_day.values()], cfg, economics)
        if m is not None:
            out[eid] = m
    return out


def apply_marginal(
    *,
    direction: str,
    pct: float,
    frm: float | None,
    marginal: float | None,
    family: str | None = None,
    roas_deflator: float = 1.0,
    cfg: BudgetConfig | None = None,
    economics: Economics | None = None,
) -> MarginalAdjustment:
    """Resize one budget step by the finite-difference marginal.

    The marginal is first multiplied by the ad type's ``roas_deflator``
    (attribution-inflated types).  Exempt families are untouched.  With
    ``be`` the effective breakeven:

    ==========  =========================  ====================================
    direction   condition                  effect
    ==========  =========================  ====================================
    up          ``m < fd_veto_mult * be``  veto
    up          ``m < fd_halve_mult * be`` halve: ``pct = max(min step, int(pct / 2))``
    up          otherwise                  keep
    down        ``m < fd_deepen_mult*be``  deepen: ``pct = min(cut floor, pct - 5)``
    ==========  =========================  ====================================

    ``pct`` is signed (negative for a cut); ``to = frm * (1 + pct / 100)``.

    Returns a ``MarginalAdjustment``.
    """
    cfg = cfg or _CFG
    be = (economics or Economics()).be

    def _to(p: float) -> float | None:
        return None if frm is None else frm * (1.0 + p / 100.0)

    if family is not None and family in cfg.fd_exempt_families:
        return MarginalAdjustment("exempt", pct, _to(pct), marginal, "family is exempt from marginal sizing")
    if marginal is None:
        return MarginalAdjustment("no_estimate", pct, _to(pct), None, "no marginal estimate; step unchanged")
    m = marginal * roas_deflator
    if direction == "up":
        if m < cfg.fd_veto_mult * be:
            return MarginalAdjustment("veto", pct, _to(pct), m,
                                      f"marginal return {m:.2f} is below {cfg.fd_veto_mult:g}x breakeven")
        if m < cfg.fd_halve_mult * be:
            new = max(cfg.fd_min_step_pct, float(int(pct / 2)))
            return MarginalAdjustment("halve", new, _to(new), m,
                                      f"marginal return {m:.2f} is below {cfg.fd_halve_mult:g}x breakeven")
        return MarginalAdjustment("keep", pct, _to(pct), m)
    if direction == "down" and m < cfg.fd_deepen_mult * be:
        new = min(cfg.fd_cut_floor_pct, pct - cfg.fd_deepen_step_pct)
        return MarginalAdjustment("deepen", new, _to(new), m,
                                  f"marginal return {m:.2f} is below {cfg.fd_deepen_mult:g}x breakeven")
    return MarginalAdjustment("keep", pct, _to(pct), m)


# --- side by side -----------------------------------------------------------------

def compare_estimators(
    *,
    frm: float,
    to: float,
    roas: float | None = None,
    fit: CurveFit | None = None,
    fd_marginal: float | None = None,
) -> dict[str, Any]:
    """Put the three estimates of one budget raise next to each other.

    For a move of daily budget ``frm -> to``: (a) ``(to - frm) * average
    ROAS``; (b) the curve increment; (c) ``(to - frm) * finite-difference
    marginal``.  Nothing is blended.

    Returns ``{"estimates": {label: MoneyEstimate | None}, "reconciled":
    False, "spread": max - min of the available per-day numbers or None}``.
    """
    delta = to - frm
    est: dict[str, MoneyEstimate | None] = {
        "stake_ev": None if roas is None else MoneyEstimate(
            "stake_ev", max(delta, 0.0) * max(roas, 0.0), "budget change x average ROAS", roas),
        "saturation_curve": curve_increment(fit, frm, to),
        "finite_difference": None if fd_marginal is None else MoneyEstimate(
            "finite_difference", delta * fd_marginal, "budget change x finite-difference marginal", fd_marginal),
    }
    vals = [e.per_day for e in est.values() if e is not None and e.per_day is not None]
    return {"estimates": est, "reconciled": False,
            "spread": (max(vals) - min(vals)) if len(vals) >= 2 else None}
