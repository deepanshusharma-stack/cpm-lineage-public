"""VIF must not turn floating-point rounding on a constant column into a verdict."""

import numpy as np

from cpm_lineage.attribution.collinearity import fold_price_cluster, vif


def test_constant_up_to_rounding_columns_have_vif_one():
    n = 60
    cols = {
        "own_discount": list(np.full(n, 0.14) + np.array([0.0, 2.8e-17] * (n // 2))),
        "comp_discount": [0.1398601398601399] * n,
        "price_gap": list(np.full(n, 1.3109756097560976) + np.array([2.2e-16, 0.0] * (n // 2))),
    }
    out = vif(cols, list(cols))
    assert out == {"own_discount": 1.0, "comp_discount": 1.0, "price_gap": 1.0}
    live, guard = fold_price_cluster(cols, list(cols))
    assert guard["folded"] == [] and live == list(cols)


def test_vif_is_never_below_one():
    rng = np.random.default_rng(3)
    cols = {k: list(rng.normal(size=80)) for k in ("a", "b", "c")}
    assert all(v >= 1.0 for v in vif(cols, list(cols)).values())
