"""Collinearity guard: variance inflation factors and the price-cluster fold.

Own discount, competitor discount and the own/competitor price ratio are
three views of two prices.  When all three are in the model and they are
collinear, coefficients swap sign freely between them; the guard keeps the
price ratio as the single representative.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

from .config import AttributionConfig

PRICE_CLUSTER: tuple[str, ...] = ("own_discount", "comp_discount", "price_gap")
PRICE_REPRESENTATIVE = "price_gap"

_CFG = AttributionConfig()


def vif(columns: Mapping[str, Sequence[float]], cols: Sequence[str],
        cfg: AttributionConfig | None = None) -> dict[str, float]:
    """Variance inflation factor of each column in ``cols``.

    Rows with a missing or non-finite value in any of ``cols`` are dropped.
    Each column is regressed on the others by least squares with an
    intercept and ``VIF = 1 / max(vif_r2_floor, 1 - R^2)``.  With no other
    column, or fewer than ``vif_min_rows`` complete rows, the VIF is 1.0
    ("cannot judge").  Pass the **pre-imputation** values: imputed medians
    would fake independence or dependence.

    Returns ``{column: vif}``.
    """
    cfg = cfg or _CFG
    cols = list(cols)
    if not cols:
        return {}
    m = np.column_stack([np.asarray(columns[c], dtype=float) for c in cols])
    m = m[np.all(np.isfinite(m), axis=1)]
    out: dict[str, float] = {}
    for j, c in enumerate(cols):
        if len(cols) < 2 or m.shape[0] < cfg.vif_min_rows:
            out[c] = 1.0
            continue
        y = m[:, j]
        a = np.column_stack([np.ones(m.shape[0]), np.delete(m, j, axis=1)])
        coef, *_ = np.linalg.lstsq(a, y, rcond=None)
        sst = float(np.sum((y - y.mean()) ** 2))
        if sst <= 0.0:
            out[c] = 1.0  # a constant column carries no variance to inflate
            continue
        r2 = 1.0 - float(np.sum((y - a @ coef) ** 2)) / sst
        out[c] = 1.0 / max(cfg.vif_r2_floor, 1.0 - r2)
    return out


def fold_price_cluster(raw: Mapping[str, Sequence[float]], live: Sequence[str],
                       cfg: AttributionConfig | None = None) -> tuple[list[str], dict]:
    """Decide whether the two discount levers are folded into the price ratio.

    Rule: only when **all three** price levers are live and
    ``max(VIF) >= vif_max`` are ``own_discount`` and ``comp_discount``
    removed from the live set (``price_gap`` stays as the representative).

    Returns ``(live_after, guard)`` with
    ``guard = {"checked": [...], "vif": float | None, "folded": [...], "threshold": vif_max}``.
    """
    cfg = cfg or _CFG
    live = list(live)
    checked = [c for c in PRICE_CLUSTER if c in live]
    guard: dict = {"checked": checked, "vif": None, "folded": [], "threshold": cfg.vif_max}
    if len(checked) < 2:
        return live, guard
    worst = max(vif(raw, checked, cfg).values())
    guard["vif"] = round(float(worst), 2)
    if len(checked) == len(PRICE_CLUSTER) and worst >= cfg.vif_max:
        folded = [c for c in checked if c != PRICE_REPRESENTATIVE]
        guard["folded"] = folded
        live = [c for c in live if c not in folded]
    return live, guard
