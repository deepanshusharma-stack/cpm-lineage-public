"""Pooled sign-constrained ridge and the first-difference cross-check.

The *backbone* regresses the log of each identity channel (``icr``, ``asp``,
``cpm``) on z-scored levers, shrinking the coefficients toward
account-level elasticities (the :class:`Prior`) and bounding the signs that
economics fixes.  The *delta-evidence* model is an independent second
opinion fitted on day-to-day differences: a levels regression can be
confounded by slow trends, a difference model is only right when daily
changes in a lever line up with daily changes in the channel.

Only numpy is used; the bounded least-squares problem is solved by cyclic
coordinate descent on the normal equations (strictly convex because the
ridge penalty is positive, so the method converges to the unique optimum).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np

from .config import AttributionConfig
from .levers import ADSTOCK_LEVERS, LEVERS, LeverPanel, prepare_levers

#: identity channels and their sign in ``ln ROAS = ln ICR + ln ASP - ln CPM (+ ln 1000)``.
CHANNELS: tuple[str, ...] = ("icr", "asp", "cpm")
CHANNEL_SIGN: dict[str, float] = {"icr": 1.0, "asp": 1.0, "cpm": -1.0}

#: sign constraints; +1 = coefficient >= 0, -1 = coefficient <= 0.  Everything else is unbounded.
#: The ``position`` entry assumes "lower number = better slot" and is flipped otherwise.
SIGNS: dict[str, dict[str, int]] = {
    "icr": {"own_availability": 1,   # more availability cannot lower conversion
            "own_paid_sov": 1,       # more paid visibility cannot lower conversion per impression served
            "position": -1,          # a larger position number is a worse slot
            "own_discount": 1,       # a deeper discount cannot lower conversion
            "price_gap": -1},        # relatively pricier converts worse
    "cpm": {"bid": 1},               # bidding more cannot lower the price paid
    "asp": {"own_discount": -1},     # a deeper discount cannot raise the selling price
}

_CFG = AttributionConfig()
_INF = float("inf")


def sign_bounds(channel: str, levers: Sequence[str], position_lower_is_better: bool = True
                ) -> tuple[np.ndarray, np.ndarray]:
    """Lower and upper coefficient bounds for ``levers`` on ``channel``.

    Returns ``(lo, hi)`` arrays; unbounded entries are ``-inf`` / ``+inf``.
    When the platform's position metric is "higher is better" the position
    bound flips from ``<= 0`` to ``>= 0``.
    """
    lo = np.full(len(levers), -_INF)
    hi = np.full(len(levers), _INF)
    for j, f in enumerate(levers):
        s = SIGNS.get(channel, {}).get(f, 0)
        if f == "position" and not position_lower_is_better:
            s = -s
        if s > 0:
            lo[j] = 0.0
        elif s < 0:
            hi[j] = 0.0
    return lo, hi


def bounded_ridge(X: np.ndarray, y: np.ndarray, lam: float, b0: Sequence[float] | None = None,
                  lo: Sequence[float] | None = None, hi: Sequence[float] | None = None,
                  cfg: AttributionConfig | None = None) -> tuple[np.ndarray, float]:
    """Ridge regression shrunk toward ``b0`` with box constraints on the coefficients.

    Minimises ``||(X - xbar) w - (y - ybar)||^2 + lam * ||w - b0||^2`` subject
    to ``lo <= w <= hi``; the intercept is ``ybar - xbar . w``.

    Centring ``X`` is required: without it the penalty rows add an implicit
    ``n * (xbar . w)^2`` term that shrinks coefficients on entities far from
    the account mean.  A consequence (tested) is that adding a constant to a
    column of ``X`` leaves ``w`` unchanged.

    Returns ``(w, intercept)``.  With zero rows ``w`` is ``b0`` clipped to the bounds.
    """
    cfg = cfg or _CFG
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    p = X.shape[1] if X.ndim == 2 else 0
    b0v = np.zeros(p) if b0 is None else np.asarray(b0, dtype=float)
    lov = np.full(p, -_INF) if lo is None else np.asarray(lo, dtype=float)
    hiv = np.full(p, _INF) if hi is None else np.asarray(hi, dtype=float)
    if p == 0:
        return np.zeros(0), float(y.mean()) if y.size else 0.0
    if X.shape[0] == 0:
        return np.clip(b0v, lov, hiv), 0.0
    xbar = X.mean(axis=0)
    ybar = float(y.mean())
    xc = X - xbar
    lam = max(float(lam), 1e-12)
    h = xc.T @ xc + lam * np.eye(p)
    g = xc.T @ (y - ybar) + lam * b0v
    w = np.linalg.solve(h, g)
    if np.any(w < lov) or np.any(w > hiv):
        w = np.clip(w, lov, hiv)
        diag = np.diag(h)
        for _ in range(cfg.solver_max_sweeps):
            moved = 0.0
            for j in range(p):
                new = (g[j] - h[j] @ w + diag[j] * w[j]) / diag[j]
                new = min(max(new, lov[j]), hiv[j])
                moved = max(moved, abs(new - w[j]))
                w[j] = new
            if moved < cfg.solver_tol:
                break
    return w, ybar - float(xbar @ w)


def time_series_splits(n: int, n_splits: int, min_train: int = 1) -> list[tuple[np.ndarray, np.ndarray]]:
    """Blocked, expanding-window time-series folds.

    ``test_size = n // (n_splits + 1)``; fold ``i`` trains on every row before
    its test block and tests on the next ``test_size`` rows; the last fold
    ends at the last row.  Folds with fewer than ``min_train`` training rows
    are dropped.  Returns ``[(train_idx, test_idx), ...]``.
    """
    if n_splits < 1 or n < n_splits + 1:
        return []
    size = n // (n_splits + 1)
    out = []
    for start in range(n - n_splits * size, n, size):
        if start >= min_train:
            out.append((np.arange(0, start), np.arange(start, start + size)))
    return out


def choose_lambda(X: np.ndarray, y: np.ndarray, b0: Sequence[float], lo: Sequence[float], hi: Sequence[float],
                  splits: Sequence[tuple[np.ndarray, np.ndarray]], cfg: AttributionConfig | None = None) -> float:
    """Pick the pooling strength by blocked time-series cross-validation.

    For each candidate in ``cfg.lambdas`` sum the out-of-fold squared error
    of ``X w + c`` over folds with at least ``cv_min_test`` test rows; the
    lowest total wins (ties and "no scorable fold" -> ``default_lambda``).
    Returns the chosen lambda.
    """
    cfg = cfg or _CFG
    best, best_sse = cfg.default_lambda, _INF
    for lam in cfg.lambdas:
        sse, used = 0.0, False
        for tr, te in splits:
            if len(te) < cfg.cv_min_test:
                continue
            w, c = bounded_ridge(X[tr], y[tr], lam, b0, lo, hi, cfg)
            sse += float(np.sum((X[te] @ w + c - y[te]) ** 2))
            used = True
        if used and sse < best_sse:
            best, best_sse = lam, sse
    return best


def channel_logs(panel: LeverPanel, mask: np.ndarray | None = None,
                 cfg: AttributionConfig | None = None) -> dict[str, np.ndarray]:
    """``ln(max(channel, log_floor))`` per identity channel on the selected days (default: model days)."""
    cfg = cfg or _CFG
    m = panel.model_mask if mask is None else mask
    return {ch: np.log(np.maximum(panel.channel(ch)[m], cfg.log_floor)) for ch in CHANNELS}


def zscore(values: np.ndarray, mu: float, sd: float, cfg: AttributionConfig | None = None) -> np.ndarray:
    """``(values - mu) / max(sd, sd_floor)``."""
    return (values - mu) / max(sd, (cfg or _CFG).sd_floor)


@dataclass
class Prior:
    """Account-level elasticities and the coordinate system they live in.

    ``beta[channel][lever]`` is d ln(channel) per +1 account standard
    deviation of the lever.  An entity that z-scores its levers with ``mu``
    / ``sd`` shares the coordinate system, which is what lets these betas
    act as the shrinkage target of its own fit.  The caller owns caching;
    the library does no file I/O.
    """

    mu: dict[str, float] = field(default_factory=dict)
    sd: dict[str, float] = field(default_factory=dict)
    beta: dict[str, dict[str, float]] = field(default_factory=dict)
    days: int = 0
    levers: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"mu": dict(self.mu), "sd": dict(self.sd), "beta": {c: dict(b) for c, b in self.beta.items()},
                "days": self.days, "levers": list(self.levers)}

    @classmethod
    def from_mapping(cls, m: Mapping) -> "Prior":
        return cls(mu=dict(m.get("mu", {})), sd=dict(m.get("sd", {})),
                   beta={c: dict(b) for c, b in m.get("beta", {}).items()},
                   days=int(m.get("days", 0)), levers=tuple(m.get("levers", ())))


def fit_prior(panel: LeverPanel, *, position_lower_is_better: bool = True,
              cfg: AttributionConfig | None = None) -> Prior | None:
    """Fit account-level elasticities on the all-entity panel.

    Levers with ``coverage > coverage_min`` are z-scored on the panel's own
    mean / sd and, per channel, ``bounded_ridge(Z, ln(channel), prior_lambda,
    b0=0, sign bounds)`` is fitted on the model days.

    Returns a :class:`Prior`, or ``None`` when there are no model days or no covered lever.
    """
    cfg = cfg or _CFG
    mask = panel.model_mask
    if not mask.any():
        return None
    prep = prepare_levers({k: v[mask] for k, v in panel.levers.items()}, cfg=cfg)
    feats = [f for f in LEVERS if prep.coverage[f] > cfg.coverage_min]
    if not feats:
        return None
    mu = {f: float(prep.filled[f].mean()) for f in feats}
    sd = {f: max(float(prep.filled[f].std()), cfg.sd_floor) for f in feats}
    Z = np.column_stack([zscore(prep.filled[f], mu[f], sd[f], cfg) for f in feats])
    ys = channel_logs(panel, mask, cfg)
    beta: dict[str, dict[str, float]] = {}
    for ch in CHANNELS:
        lo, hi = sign_bounds(ch, feats, position_lower_is_better)
        w, _ = bounded_ridge(Z, ys[ch], cfg.prior_lambda, None, lo, hi, cfg)
        beta[ch] = {f: float(w[j]) for j, f in enumerate(feats)}
    return Prior(mu=mu, sd=sd, beta=beta, days=int(mask.sum()), levers=tuple(feats))


# --------------------------------------------------------------------------- delta evidence

def adstock(X: np.ndarray, levers: Sequence[str], rate: float, cfg: AttributionConfig | None = None) -> np.ndarray:
    """Geometric carry-over variant of the design matrix.

    For columns whose lever is in :data:`ADSTOCK_LEVERS`:
    ``v[t] = v[t] + rate * v[t-1]`` (recursive), then the transformed column
    is re-standardised to mean 0, sd 1.  Other columns are untouched.
    Returns a new matrix.
    """
    cfg = cfg or _CFG
    out = np.array(X, dtype=float, copy=True)
    for j, f in enumerate(levers):
        if f not in ADSTOCK_LEVERS:
            continue
        v = out[:, j]
        for t in range(1, len(v)):
            v[t] = v[t] + rate * v[t - 1]
        out[:, j] = (v - v.mean()) / max(float(v.std()), cfg.sd_floor)
    return out


def fit_delta(X: np.ndarray, y: np.ndarray, cfg: AttributionConfig | None = None) -> np.ndarray:
    """First-difference ridge: ``bounded_ridge(diff(X), diff(y), delta_lambda)``, unbounded, toward 0.  Returns ``w``."""
    cfg = cfg or _CFG
    if len(y) < 2:
        return np.zeros(X.shape[1])
    return bounded_ridge(np.diff(X, axis=0), np.diff(y), cfg.delta_lambda, cfg=cfg)[0]


def delta_skill(X: np.ndarray, y: np.ndarray, splits: Sequence[tuple[np.ndarray, np.ndarray]],
                cfg: AttributionConfig | None = None) -> float | None:
    """One-step-ahead skill of the first-difference model against persistence.

    Per fold with at least ``cv_min_train`` training rows: fit
    :func:`fit_delta` on the training block and predict each test row
    ``t >= 1`` as ``y[t-1] + (X[t] - X[t-1]) . w``.
    ``skill = 1 - SSE(model) / SSE(persistence)`` where persistence predicts
    ``y[t-1]``.  0 = no better than "tomorrow equals today"; negative = worse.

    Returns the skill rounded to 2 dp, or ``None`` with fewer than
    ``skill_min_predictions`` predictions or a (near) constant channel.
    """
    cfg = cfg or _CFG
    sse_m = sse_p = 0.0
    n = 0
    for tr, te in splits:
        if len(tr) < cfg.cv_min_train or len(te) < 2:
            continue
        w = fit_delta(X[tr], y[tr], cfg)
        dx = np.diff(X[te], axis=0)
        dy = np.diff(y[te])
        sse_m += float(np.sum((dy - dx @ w) ** 2))
        sse_p += float(np.sum(dy ** 2))
        n += len(dy)
    if n < cfg.skill_min_predictions or sse_p < cfg.skill_min_sse:
        return None
    s = 1.0 - sse_m / sse_p
    return round(s, 2) if math.isfinite(s) else None
