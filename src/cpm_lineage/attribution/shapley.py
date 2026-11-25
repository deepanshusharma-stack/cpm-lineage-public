"""Optional attributor: exact Shapley values over any fitted model
(``attribution="shapley"``).

This is the original, model-agnostic method.  The default path is the
pooled ridge + jury in :mod:`cpm_lineage.attribution.jury`; for a linear
model the two coincide (``phi_j = w_j x (xt_j - xb_j)``).  Use this module
when the response is fitted with a non-linear model.

Important: ``sum(phi) = f(xt) - f(xb)`` is the *model's* predicted change,
not the observed change of the channel.  The gap is the unexplained
residual and is reported by :func:`shapley_attribute`.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Mapping, Sequence

import numpy as np

from .config import AttributionConfig
from .ridge import CHANNEL_SIGN, CHANNELS

_CFG = AttributionConfig()


def _predict(model: Any, M: np.ndarray) -> np.ndarray:
    fn = model.predict if hasattr(model, "predict") else model
    return np.asarray(fn(M), dtype=float).reshape(-1)


def exact_shapley(models: Sequence[Any] | Any, xb: Sequence[float], xt: Sequence[float],
                  cfg: AttributionConfig | None = None) -> np.ndarray:
    """Exact Shapley attribution of ``f(xt) - f(xb)`` to features, by full coalition enumeration.

    ``models`` is one model or a list (predictions are averaged); each is an
    object with a batched ``.predict(matrix)`` or a callable.  ``xb`` is the
    baseline feature vector (mean lever values over the baseline days),
    ``xt`` the comparison vector.

    Algorithm: for each of the ``2^n`` coalitions build one row where member
    features take their comparison value and the rest their baseline value;
    predict all rows in one batched call per model; then
    ``phi[j] = sum over coalitions S without j of |S|! (n - |S| - 1)! / n! x (f(S + j) - f(S))``.

    Properties: exact (no sampling); efficiency ``sum(phi) = f(xt) - f(xb)``;
    a feature the model ignores gets 0.  Cost is ``2^n`` rows per model, so
    ``n`` is limited to ``cfg.shapley_max_features`` (``ValueError`` above it).

    Returns ``phi`` as an array of length ``n``.
    """
    cfg = cfg or _CFG
    xb = np.asarray(xb, dtype=float).reshape(-1)
    xt = np.asarray(xt, dtype=float).reshape(-1)
    n = len(xb)
    if len(xt) != n:
        raise ValueError("xb and xt must have the same length")
    if n > cfg.shapley_max_features:
        raise ValueError(f"exact Shapley is limited to {cfg.shapley_max_features} features (2^n model rows)")
    if n == 0:
        return np.zeros(0)
    if not isinstance(models, (list, tuple)):
        models = [models]
    masks = np.arange(1 << n)
    bits = (masks[:, None] >> np.arange(n)[None, :]) & 1
    M = xb[None, :] * (1 - bits) + xt[None, :] * bits
    preds = np.mean([_predict(m, M) for m in models], axis=0)
    sizes = bits.sum(axis=1)
    wsize = np.array([math.factorial(r) * math.factorial(n - r - 1) / math.factorial(n) for r in range(n)])
    phi = np.zeros(n)
    for j in range(n):
        without = masks[bits[:, j] == 0]
        phi[j] = float(np.sum(wsize[sizes[without]] * (preds[without | (1 << j)] - preds[without])))
    return phi


class BoostedTrees:
    """Small gradient-boosted regression trees (squared loss), numpy only.

    A dependency-free stand-in so the Shapley path runs without a modelling
    library.  The defaults are deliberately small: 80 trees of
    depth 2, learning rate 0.1, at least 5 rows per leaf, candidate splits at
    up to 16 quantiles per feature.  Any object with ``fit`` / ``predict``
    can be used instead via ``model_factory``.
    """

    def __init__(self, n_trees: int = 80, depth: int = 2, learning_rate: float = 0.1,
                 min_leaf: int = 5, n_thresholds: int = 16) -> None:
        self.n_trees, self.depth, self.learning_rate = n_trees, depth, learning_rate
        self.min_leaf, self.n_thresholds = min_leaf, n_thresholds
        self.base_ = 0.0
        self.trees_: list[Any] = []

    def _grow(self, X: np.ndarray, r: np.ndarray, depth: int) -> Any:
        if depth == 0 or len(r) < 2 * self.min_leaf:
            return float(r.mean()) if len(r) else 0.0
        best = (float(np.sum((r - r.mean()) ** 2)) - 1e-12, None, None)
        for j in range(X.shape[1]):
            col = X[:, j]
            qs = np.unique(np.quantile(col, np.linspace(0.0, 1.0, self.n_thresholds + 2)[1:-1]))
            for t in qs:
                left = col <= t
                nl = int(left.sum())
                if nl < self.min_leaf or len(r) - nl < self.min_leaf:
                    continue
                rl, rr = r[left], r[~left]
                sse = float(np.sum((rl - rl.mean()) ** 2) + np.sum((rr - rr.mean()) ** 2))
                if sse < best[0]:
                    best = (sse, j, float(t))
        if best[1] is None:
            return float(r.mean())
        _, j, t = best
        left = X[:, j] <= t
        return (j, t, self._grow(X[left], r[left], depth - 1), self._grow(X[~left], r[~left], depth - 1))

    @staticmethod
    def _eval(node: Any, X: np.ndarray) -> np.ndarray:
        if not isinstance(node, tuple):
            return np.full(X.shape[0], node)
        j, t, lnode, rnode = node
        out = np.empty(X.shape[0])
        left = X[:, j] <= t
        out[left] = BoostedTrees._eval(lnode, X[left])
        out[~left] = BoostedTrees._eval(rnode, X[~left])
        return out

    def fit(self, X: np.ndarray, y: np.ndarray) -> "BoostedTrees":
        """Fit by stage-wise least squares on residuals.  Returns ``self``."""
        X, y = np.asarray(X, dtype=float), np.asarray(y, dtype=float)
        self.base_ = float(y.mean()) if len(y) else 0.0
        self.trees_ = []
        pred = np.full(len(y), self.base_)
        for _ in range(self.n_trees):
            tree = self._grow(X, y - pred, self.depth)
            self.trees_.append(tree)
            pred = pred + self.learning_rate * self._eval(tree, X)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        """Batched prediction for a 2-d matrix."""
        X = np.asarray(X, dtype=float)
        out = np.full(X.shape[0], self.base_)
        for tree in self.trees_:
            out = out + self.learning_rate * self._eval(tree, X)
        return out


def shapley_attribute(X: np.ndarray, log_channels: Mapping[str, Sequence[float]], base_mask: Sequence[bool],
                      cmp_mask: Sequence[bool], levers: Sequence[str],
                      model_factory: Callable[[], Any] | None = None,
                      cfg: AttributionConfig | None = None) -> dict:
    """Attribute a ROAS move with one model per channel and exact Shapley values.

    For each channel in ``log_channels`` (``icr`` / ``asp`` / ``cpm``, daily
    ``ln`` values aligned with the rows of ``X``) a model from
    ``model_factory`` (default :class:`BoostedTrees`) is fitted; ``xb`` /
    ``xt`` are the mean lever rows of the baseline and comparison days;
    ``phi_channel = exact_shapley(model, xb, xt)`` and
    ``contrib[j] = + phi_icr[j] + phi_asp[j] - phi_cpm[j]``.

    Returns ``{"method": "shapley", "levers", "contrib": {lever: value},
    "channels": {channel: {lever: phi}}, "predicted_dln", "observed_dln",
    "residual"}`` where ``observed_dln`` is the difference of the window means
    of the daily log channels and ``residual = observed - predicted`` is the
    part of the move the models do not explain.
    """
    cfg = cfg or _CFG
    X = np.asarray(X, dtype=float)
    b, c = np.asarray(base_mask, dtype=bool), np.asarray(cmp_mask, dtype=bool)
    if not b.any() or not c.any():
        raise ValueError("both windows need at least one row")
    factory = model_factory or BoostedTrees
    xb, xt = X[b].mean(axis=0), X[c].mean(axis=0)
    contrib = np.zeros(X.shape[1])
    channels: dict[str, dict[str, float]] = {}
    observed = 0.0
    for ch in CHANNELS:
        if ch not in log_channels:
            continue
        y = np.asarray(log_channels[ch], dtype=float)
        model = factory().fit(X, y)
        phi = exact_shapley(model, xb, xt, cfg)
        channels[ch] = {f: float(phi[j]) for j, f in enumerate(levers)}
        contrib += CHANNEL_SIGN[ch] * phi
        observed += CHANNEL_SIGN[ch] * float(y[c].mean() - y[b].mean())
    predicted = float(contrib.sum())
    return {"method": "shapley", "levers": list(levers),
            "contrib": {f: float(contrib[j]) for j, f in enumerate(levers)}, "channels": channels,
            "predicted_dln": predicted, "observed_dln": observed, "residual": observed - predicted}
