"""Concave spend -> attributed-sales saturation curve and marginal ROAS.

Model: ``sales = b * ln(1 + spend / theta)`` - two parameters, concave,
through the origin (zero spend attributes zero direct sales).  On about 90
noisy daily points a third steepness parameter only absorbs noise.

What the curve is and is not:

* correlational, with no demand controls (seasonality, price, availability
  and rival activity are not in the fit);
* same-day attribution only, no carry-over;
* ``r2`` is in-sample and there are no confidence intervals;
* beyond the largest observed spend it is functional-form extrapolation.

Parameters are kept unrounded internally so decisions are invariant to the
currency scale; rounding happens only in ``CurveFit.as_dict`` and in the
display helpers.

Fitting uses a deterministic profile search (for a fixed ``theta`` the best
``b`` is a closed-form least-squares ratio; ``theta`` is searched on a log
grid and refined by golden section), so the library needs numpy only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Sequence

import numpy as np

from .config import BudgetConfig

__all__ = [
    "FAIL_REASONS", "CurveFit", "WhatIfPoint", "fit_curve", "marginal", "marginal_raw", "unit_marginal_spend",
    "predict", "whatif", "daily_series", "pooled_points", "fit_entity_curve",
    "fit_pooled_curve",
]

_CFG = BudgetConfig()

FAIL_REASONS: tuple[str, ...] = ("thin", "var_starved", "no_relation", "regime_step", "degenerate")


@dataclass(frozen=True)
class CurveFit:
    """Result of ``fit_curve``.  ``fit=False`` carries ``reason`` (one of
    ``FAIL_REASONS``); a failure is a value, never an exception.

    ``sat_spend`` is the daily spend at which marginal ROAS falls to 1
    (``b - theta``): a number when identified, ``0.0`` when marginal is
    already below 1 at zero spend, ``None`` when the ceiling lies beyond the
    supported range (near-linear data - the slope is known, the ceiling is
    not).
    """

    fit: bool
    n: int = 0
    reason: str | None = None
    cv: float | None = None
    b: float | None = None
    theta: float | None = None
    r2: float | None = None
    xmax: float | None = None
    sat_spend: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """Display form: ``b``, ``theta``, ``r2`` to 2 dp, ``cv`` to 3 dp, money rounded."""
        if not self.fit:
            d: dict[str, Any] = {"fit": False, "reason": self.reason, "n": self.n}
            if self.cv is not None:
                d["cv"] = round(self.cv, 3)
            return d
        return {
            "fit": True, "b": round(self.b or 0.0, 2), "theta": round(self.theta or 0.0, 2),
            "r2": round(self.r2 or 0.0, 2), "cv": round(self.cv or 0.0, 3), "n": self.n,
            "xmax": round(self.xmax or 0.0),
            "sat_spend": None if self.sat_spend is None else round(self.sat_spend),
        }


@dataclass(frozen=True)
class WhatIfPoint:
    """One row of the what-if grid."""

    mult: float            # multiple of the current cap
    spend: float           # mult * cap
    sales: float           # predicted daily attributed sales at that spend
    marginal: float        # marginal ROAS at that spend (2 dp)
    extrapolated: bool     # beyond the observed spend range: form extrapolation, not evidence


def _cv(a: np.ndarray) -> float:
    m = float(np.mean(a)) if a.size else 0.0
    return float(np.std(a)) / m if m > 0 else 0.0


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    sa, sb = float(np.std(a)), float(np.std(b))
    if sa == 0.0 or sb == 0.0:
        return 0.0
    return float(np.mean((a - a.mean()) * (b - b.mean())) / (sa * sb))


def _profile(x: np.ndarray, y: np.ndarray, u: float, b_min: float) -> tuple[float, float]:
    """Best ``b`` and its sum of squared residuals for ``theta = exp(u)``."""
    g = np.log1p(x / math.exp(u))
    gg = float(g @ g)
    b = max(float(g @ y) / gg, b_min) if gg > 0 else b_min
    r = b * g - y
    return b, float(r @ r)


def fit_curve(spend: Sequence[float], sales: Sequence[float],
              cfg: BudgetConfig | None = None) -> CurveFit:
    """Fit ``sales = b * ln(1 + spend / theta)`` to time-ordered daily points.

    Gates, in order (each returns a failed ``CurveFit`` with the reason):

    1. keep points with finite values and ``spend > 0``; clip sales at 0
    2. ``n < sat_min_days`` -> ``"thin"``
    3. ``cv = std(spend) / mean(spend) < sat_min_cv`` -> ``"var_starved"``
    4. constant sales or ``pearson(spend, sales) <= sat_min_corr`` -> ``"no_relation"``
    5. ``|pearson(spend, time)| > regime_time_corr`` with both half-window
       CVs below ``regime_half_cv`` -> ``"regime_step"`` (a cap change plus
       drift is two flat clusters confounded with time)
    6. bounded least squares (``b >= sat_b_min``, ``theta >= sat_theta_min``);
       a non-finite solution -> ``"degenerate"``
    7. ``r2 = 1 - SSR / SST < sat_min_r2`` -> ``"no_relation"``

    Ceiling: ``b - theta`` when ``b > theta`` and it lies within
    ``sat_ceiling_support * max(spend)``; ``0`` when ``b <= theta``; else
    ``None``.

    Returns a ``CurveFit``.
    """
    cfg = cfg or _CFG
    try:
        x = np.asarray(list(spend), dtype=float)
        y = np.asarray(list(sales), dtype=float)
    except (TypeError, ValueError):
        return CurveFit(fit=False, reason="degenerate", n=0)
    if x.shape != y.shape or x.ndim != 1:
        return CurveFit(fit=False, reason="degenerate", n=0)
    keep = np.isfinite(x) & np.isfinite(y) & (x > 0)
    x, y = x[keep], np.clip(y[keep], 0.0, None)
    n = int(x.size)
    if n < cfg.sat_min_days:
        return CurveFit(fit=False, reason="thin", n=n)
    cv = _cv(x)
    if cv < cfg.sat_min_cv:
        return CurveFit(fit=False, reason="var_starved", n=n, cv=cv)
    if float(np.std(y)) == 0.0 or _pearson(x, y) <= cfg.sat_min_corr:
        return CurveFit(fit=False, reason="no_relation", n=n, cv=cv)
    t = np.arange(n, dtype=float)
    h = n // 2
    if (abs(_pearson(x, t)) > cfg.regime_time_corr
            and _cv(x[:h]) < cfg.regime_half_cv and _cv(x[h:]) < cfg.regime_half_cv):
        return CurveFit(fit=False, reason="regime_step", n=n, cv=cv)

    xmax = float(np.max(x))
    lo_u = math.log(cfg.sat_theta_min)
    hi_u = math.log(max(cfg.sat_theta_span * xmax, cfg.sat_theta_min * 10.0))
    grid = np.linspace(lo_u, hi_u, max(cfg.sat_grid_points, 3))
    ssr = [_profile(x, y, float(u), cfg.sat_b_min)[1] for u in grid]
    i = int(np.argmin(ssr))
    a, c = float(grid[max(i - 1, 0)]), float(grid[min(i + 1, len(grid) - 1)])
    phi = (math.sqrt(5.0) - 1.0) / 2.0
    p, q = c - phi * (c - a), a + phi * (c - a)
    fp, fq = _profile(x, y, p, cfg.sat_b_min)[1], _profile(x, y, q, cfg.sat_b_min)[1]
    for _ in range(cfg.sat_refine_iters):
        if fp <= fq:
            c, q, fq = q, p, fp
            p = c - phi * (c - a)
            fp = _profile(x, y, p, cfg.sat_b_min)[1]
        else:
            a, p, fp = p, q, fq
            q = a + phi * (c - a)
            fq = _profile(x, y, q, cfg.sat_b_min)[1]
    u = (a + c) / 2.0
    b, resid = _profile(x, y, u, cfg.sat_b_min)
    if ssr[i] < resid:                       # refinement never worsens the grid optimum
        u = float(grid[i])
        b, resid = _profile(x, y, u, cfg.sat_b_min)
    theta = math.exp(u)
    if not (math.isfinite(b) and math.isfinite(theta) and math.isfinite(resid)):
        return CurveFit(fit=False, reason="degenerate", n=n, cv=cv)
    sst = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - resid / sst if sst > 0 else 0.0
    if r2 < cfg.sat_min_r2 or b <= 0:
        return CurveFit(fit=False, reason="no_relation", n=n, cv=cv)
    if b <= theta:
        sat: float | None = 0.0
    elif (b - theta) <= cfg.sat_ceiling_support * xmax:
        sat = b - theta
    else:
        sat = None
    return CurveFit(fit=True, n=n, cv=cv, b=b, theta=theta, r2=r2, xmax=xmax, sat_spend=sat)


def unit_marginal_spend(fit: CurveFit | None) -> float | None:
    """Daily spend at which the curve's marginal ROAS reaches 1: ``max(b - theta, 0)``.

    Unlike ``CurveFit.sat_spend`` this is returned even when it lies beyond
    the supported range, so a raise can always be clamped to it; whether to
    *display* it as a ceiling is the support flag's business.  ``None`` when
    there is no fit.
    """
    if fit is None or not fit.fit or fit.b is None or fit.theta is None:
        return None
    return max(fit.b - fit.theta, 0.0)


def marginal_raw(fit: CurveFit | None, x: float) -> float | None:
    """Unrounded marginal ROAS at daily spend ``x``: ``b / (theta + max(x, 0))``.

    Use this (not the rounded display value) for any decision, so a block
    never rides a rounding boundary.  ``None`` when there is no fit.
    """
    if fit is None or not fit.fit or fit.b is None or fit.theta is None:
        return None
    return fit.b / (fit.theta + max(float(x), 0.0))


def marginal(fit: CurveFit | None, x: float) -> float | None:
    """Marginal ROAS at daily spend ``x`` for display, rounded to 2 dp.  ``None`` when no fit."""
    m = marginal_raw(fit, x)
    return None if m is None else round(m, 2)


def predict(fit: CurveFit | None, x: float, ndigits: int | None = None) -> float | None:
    """Predicted daily attributed sales at daily spend ``x``:
    ``b * ln(1 + max(x, 0) / theta)``.  Unrounded unless ``ndigits`` is
    given.  ``None`` when there is no fit.
    """
    if fit is None or not fit.fit or fit.b is None or fit.theta is None:
        return None
    v = fit.b * math.log1p(max(float(x), 0.0) / fit.theta)
    return v if ndigits is None else round(v, ndigits)


def whatif(fit: CurveFit | None, cap: float | None,
           mults: Sequence[float] | None = None,
           cfg: BudgetConfig | None = None) -> list[WhatIfPoint] | None:
    """What-if grid around the current daily cap.

    For each multiplier: spend ``= mult * cap``, predicted sales, marginal
    ROAS, and ``extrapolated = spend > extrapolation_mult * max observed
    spend``.  Money is rounded to ``display_decimals``.  ``None`` when there
    is no fit or no cap.
    """
    cfg = cfg or _CFG
    if fit is None or not fit.fit or not cap or cap <= 0:
        return None
    nd = cfg.display_decimals
    out = []
    for m in (mults or cfg.whatif_mults):
        s = m * cap
        out.append(WhatIfPoint(
            mult=m, spend=round(s, nd), sales=round(predict(fit, s) or 0.0, nd),
            marginal=marginal(fit, s) or 0.0,
            extrapolated=s > cfg.extrapolation_mult * (fit.xmax or 0.0)))
    return out


def _window_start(anchor: date, lookback_days: int) -> date:
    return anchor - timedelta(days=lookback_days - 1)


def daily_series(rows: Iterable[Any], entity_id: str | None = None, *,
                 anchor: date | None = None,
                 lookback_days: int | None = None,
                 cfg: BudgetConfig | None = None) -> tuple[list[float], list[float]]:
    """Time-ordered daily ``(spend, sales)`` series for the curve fit.

    ``rows`` are any dated records with ``spend`` and ``sales`` (pacing rows
    or performance rows).  Rows are summed per date (so a label that changed
    mid-period cannot split a day into partial points), restricted to
    ``entity_id`` when given and to the ``lookback_days`` ending on
    ``anchor`` (default: latest date in the rows).

    Returns ``(spend, sales)`` lists in date order.
    """
    cfg = cfg or _CFG
    lookback_days = lookback_days or cfg.sat_lookback_days
    sel = [r for r in rows if entity_id is None or r.entity_id == entity_id]
    if not sel:
        return [], []
    anchor = anchor or max(r.date for r in sel)
    start = _window_start(anchor, lookback_days)
    acc: dict[date, list[float]] = {}
    for r in sel:
        if start <= r.date <= anchor:
            a = acc.setdefault(r.date, [0.0, 0.0])
            a[0] += r.spend or 0.0
            a[1] += r.sales or 0.0
    ds = sorted(acc)
    return [acc[d][0] for d in ds], [acc[d][1] for d in ds]


def pooled_points(rows: Iterable[Any], *, anchor: date | None = None,
                  lookback_days: int | None = None,
                  cfg: BudgetConfig | None = None) -> tuple[list[float], list[float]]:
    """Account-pooled ``(spend, sales)`` points: one per (entity, day).

    Points are *not* summed across entities, so the x-axis stays on a
    single-entity daily-spend scale.  They are ordered by (date, entity) so
    the regime-step guard sees a time order (pooling in
    arbitrary order would make that guard meaningless).

    Returns ``(spend, sales)`` lists.
    """
    cfg = cfg or _CFG
    lookback_days = lookback_days or cfg.sat_lookback_days
    sel = list(rows)
    if not sel:
        return [], []
    anchor = anchor or max(r.date for r in sel)
    start = _window_start(anchor, lookback_days)
    acc: dict[tuple[date, str], list[float]] = {}
    for r in sel:
        if start <= r.date <= anchor:
            a = acc.setdefault((r.date, r.entity_id), [0.0, 0.0])
            a[0] += r.spend or 0.0
            a[1] += r.sales or 0.0
    ks = sorted(acc)
    return [acc[k][0] for k in ks], [acc[k][1] for k in ks]


def fit_entity_curve(rows: Iterable[Any], entity_id: str, *, anchor: date | None = None,
                     cfg: BudgetConfig | None = None) -> CurveFit:
    """Fit one entity's curve over the standard lookback.  Returns a ``CurveFit``."""
    x, y = daily_series(rows, entity_id, anchor=anchor, cfg=cfg)
    return fit_curve(x, y, cfg)


def fit_pooled_curve(rows: Iterable[Any], *, anchor: date | None = None,
                     cfg: BudgetConfig | None = None) -> CurveFit:
    """Fit the account-pooled fallback curve over all (entity, day) points.

    It may lend a marginal ROAS to an entity with no own fit; its ceiling is
    never borrowed.  Returns a ``CurveFit``.
    """
    x, y = pooled_points(rows, anchor=anchor, cfg=cfg)
    return fit_curve(x, y, cfg)
