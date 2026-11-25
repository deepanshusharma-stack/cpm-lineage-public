"""Bounded ridge, CV folds, the first-difference skill and exact Shapley."""

import itertools
import math

import numpy as np
import pytest

from cpm_lineage.attribution import (AttributionConfig, BoostedTrees, adstock, bounded_ridge, choose_lambda,
                                     delta_skill, exact_shapley, fit_delta, shapley_attribute, sign_bounds,
                                     time_series_splits)


def _xy(n=300, seed=0, noise=0.01):
    rng = np.random.default_rng(seed)
    X = rng.normal(size=(n, 3))
    w = np.array([0.5, -0.3, 0.2])
    return X, X @ w + 1.7 + rng.normal(scale=noise, size=n), w


def test_recovers_known_coefficients_and_intercept():
    X, y, w = _xy()
    est, c = bounded_ridge(X, y, 1e-6)
    assert est == pytest.approx(w, abs=5e-3) and c == pytest.approx(1.7, abs=5e-3)


def test_respects_sign_bounds_and_matches_kkt():
    X, y, w = _xy()
    lo = np.array([-np.inf, 0.0, -np.inf])
    hi = np.array([0.1, np.inf, np.inf])
    est, _ = bounded_ridge(X, y, 4.0, None, lo, hi)
    assert est[0] == pytest.approx(0.1) and est[1] == pytest.approx(0.0) and est[2] == pytest.approx(0.2, abs=0.02)
    # optimality: no feasible perturbation lowers the objective
    xc, yc = X - X.mean(0), y - y.mean()
    obj = lambda v: np.sum((xc @ v - yc) ** 2) + 4.0 * np.sum(v ** 2)
    rng = np.random.default_rng(1)
    for _ in range(200):
        cand = np.clip(est + rng.normal(scale=1e-3, size=3), lo, hi)
        assert obj(cand) >= obj(est) - 1e-9


def test_huge_lambda_returns_b0_clipped():
    X, y, _ = _xy()
    b0 = np.array([0.9, -0.9, 0.4])
    est, _ = bounded_ridge(X, y, 1e12, b0)
    assert est == pytest.approx(b0, abs=1e-6)
    est, _ = bounded_ridge(X, y, 1e12, b0, hi=np.array([0.5, np.inf, np.inf]))
    assert est[0] == pytest.approx(0.5)


def test_invariant_to_column_shift():
    X, y, _ = _xy(n=40, noise=0.3)
    b0 = np.array([0.2, 0.1, -0.1])
    a, _ = bounded_ridge(X, y, 16.0, b0)
    Xs = X.copy()
    Xs[:, 0] += 25.0
    b, _ = bounded_ridge(Xs, y, 16.0, b0)
    assert a == pytest.approx(b, abs=1e-9)


def test_degenerate_shapes():
    w, c = bounded_ridge(np.zeros((5, 0)), np.arange(5.0), 4.0)
    assert w.shape == (0,) and c == 2.0
    w, _ = bounded_ridge(np.zeros((0, 2)), np.zeros(0), 4.0, [1.0, -1.0], hi=[0.5, np.inf])
    assert list(w) == [0.5, -1.0]


def test_sign_bounds_table():
    lev = ["bid", "position", "own_availability", "own_discount", "price_gap", "own_paid_sov", "comp_discount"]
    lo, hi = sign_bounds("icr", lev)
    assert list(lo) == [-np.inf, -np.inf, 0, 0, -np.inf, 0, -np.inf]
    assert list(hi) == [np.inf, 0, np.inf, np.inf, 0, np.inf, np.inf]
    lo, hi = sign_bounds("cpm", lev)
    assert lo[0] == 0 and np.isinf(lo[1:]).all() and np.isinf(hi).all()
    lo, hi = sign_bounds("asp", lev)
    assert hi[3] == 0 and np.isinf(lo).all()
    lo, hi = sign_bounds("icr", ["position"], position_lower_is_better=False)
    assert lo[0] == 0 and hi[0] == np.inf


def test_time_series_splits():
    s = time_series_splits(90, 4)
    assert [(len(a), len(b)) for a, b in s] == [(18, 18), (36, 18), (54, 18), (72, 18)]
    assert all(a.max() < b.min() for a, b in s) and s[-1][1][-1] == 89
    assert [len(a) for a, _ in time_series_splits(25, 3, min_train=10)] == [13, 19]   # 7-row first fold dropped
    assert time_series_splits(3, 4) == []


def test_choose_lambda_prefers_weak_pooling_on_clean_signal_and_strong_on_noise():
    X, y, w = _xy(n=120)
    splits = time_series_splits(120, 4, 10)
    inf = np.full(3, np.inf)
    assert choose_lambda(X, y, np.zeros(3), -inf, inf, splits) == 1.0
    rng = np.random.default_rng(4)
    noise_y = rng.normal(size=120)
    assert choose_lambda(X, noise_y, np.zeros(3), -inf, inf, splits) == 64.0
    assert choose_lambda(X, y, np.zeros(3), -inf, inf, []) == 4.0     # nothing scorable -> default


def test_delta_skill():
    rng = np.random.default_rng(2)
    n = 120
    X = rng.normal(size=(n, 2))
    y = X @ np.array([0.6, -0.4]) + np.cumsum(rng.normal(scale=0.02, size=n))
    splits = time_series_splits(n, 4, 10)
    assert delta_skill(X, y, splits) > 0.9
    assert fit_delta(X, y) == pytest.approx([0.6, -0.4], abs=0.05)
    walk = np.cumsum(rng.normal(size=n))
    sk = delta_skill(X, walk, splits)
    assert sk is not None and sk < 0.1                               # no better than persistence
    assert delta_skill(X, np.full(n, 3.0), splits) is None           # constant channel
    assert delta_skill(X[:14], y[:14], time_series_splits(14, 4, 10)) is None   # < 5 predictions
    assert delta_skill(X, y, splits) == round(delta_skill(X, y, splits), 2)


def test_adstock_only_touches_carryover_levers():
    rng = np.random.default_rng(3)
    X = rng.normal(size=(30, 2))
    out = adstock(X, ["own_paid_sov", "position"], 0.4)
    assert np.array_equal(out[:, 1], X[:, 1])
    assert out[:, 0].mean() == pytest.approx(0.0, abs=1e-12) and out[:, 0].std() == pytest.approx(1.0)
    v = X[:, 0].copy()
    for t in range(1, 30):
        v[t] += 0.4 * v[t - 1]
    assert out[:, 0] == pytest.approx((v - v.mean()) / v.std())
    assert not np.shares_memory(out, X)


# --------------------------------------------------------------------------- Shapley

class _Linear:
    def __init__(self, w, c=0.0):
        self.w, self.c = np.asarray(w, float), c

    def predict(self, M):
        return M @ self.w + self.c


def _nonlinear(M):
    return M[:, 0] * M[:, 1] + np.sin(M[:, 2]) + 0.5 * M[:, 3] ** 2 + M[:, 0] * M[:, 3] * M[:, 4]


def _brute(f, xb, xt):
    n = len(xb)
    phi = np.zeros(n)
    perms = list(itertools.permutations(range(n)))
    for perm in perms:
        x = xb.copy()
        prev = f(x[None, :])[0]
        for j in perm:
            x[j] = xt[j]
            cur = f(x[None, :])[0]
            phi[j] += cur - prev
            prev = cur
    return phi / len(perms)


def test_shapley_linear_model_is_w_times_dx():
    w = np.array([0.3, -1.2, 0.0, 2.0])
    xb, xt = np.array([1.0, 2.0, 3.0, 4.0]), np.array([1.5, 1.0, 9.0, 4.5])
    phi = exact_shapley(_Linear(w, 7.0), xb, xt)
    assert phi == pytest.approx(w * (xt - xb), abs=1e-12)
    assert phi[2] == 0.0                                              # ignored feature


def test_shapley_efficiency_and_brute_force():
    rng = np.random.default_rng(0)
    for n in (1, 3, 6):
        xb, xt = rng.normal(size=n), rng.normal(size=n)
        f = (lambda M: _nonlinear(np.pad(M, ((0, 0), (0, 6 - M.shape[1]))))) if n < 6 else \
            (lambda M: _nonlinear(M) + M[:, 5] * M[:, 1])
        phi = exact_shapley(f, xb, xt)
        assert phi.sum() == pytest.approx(f(xt[None, :])[0] - f(xb[None, :])[0], abs=1e-9)
        assert phi == pytest.approx(_brute(f, xb, xt), abs=1e-9)


def test_shapley_symmetry_and_model_average():
    f = lambda M: M[:, 0] * M[:, 1] + M[:, 2]
    phi = exact_shapley(f, np.array([1.0, 1.0, 0.0]), np.array([3.0, 3.0, 1.0]))
    assert phi[0] == pytest.approx(phi[1]) and phi[2] == pytest.approx(1.0)
    a, b = _Linear([1.0, 0.0]), _Linear([0.0, 1.0])
    assert exact_shapley([a, b], np.zeros(2), np.ones(2)) == pytest.approx([0.5, 0.5])
    assert exact_shapley(a, np.ones(2), np.ones(2)) == pytest.approx([0.0, 0.0])


def test_shapley_feature_limit():
    with pytest.raises(ValueError):
        exact_shapley(_Linear(np.ones(19)), np.zeros(19), np.ones(19))
    with pytest.raises(ValueError):
        exact_shapley(_Linear(np.ones(3)), np.zeros(3), np.ones(3), AttributionConfig(shapley_max_features=2))
    with pytest.raises(ValueError):
        exact_shapley(_Linear(np.ones(3)), np.zeros(3), np.ones(2))


def test_boosted_trees_learn_a_step_and_shapley_attribute_reports_residual():
    rng = np.random.default_rng(1)
    n = 120
    X = rng.normal(size=(n, 3))
    X[60:, 0] += 2.0                                                 # lever 0 shifts in the comparison window
    icr = -4.0 + 0.5 * (X[:, 0] > 1.0) + rng.normal(scale=0.02, size=n)
    model = BoostedTrees().fit(X, icr)
    assert np.mean((model.predict(X) - icr) ** 2) < 0.01
    b = np.arange(n) < 60
    res = shapley_attribute(X, {"icr": icr, "asp": np.full(n, 5.0), "cpm": np.full(n, 4.0)}, b, ~b, ["a", "b", "c"])
    assert res["method"] == "shapley"
    assert sum(res["contrib"].values()) == pytest.approx(res["predicted_dln"], abs=1e-9)
    assert res["residual"] == pytest.approx(res["observed_dln"] - res["predicted_dln"], abs=1e-12)
    assert res["contrib"]["a"] > 0.2 and abs(res["contrib"]["b"]) < 0.05 and abs(res["contrib"]["c"]) < 0.05
    # per-channel efficiency against the fitted model
    xb, xt = X[b].mean(0), X[~b].mean(0)
    assert sum(res["channels"]["icr"].values()) == pytest.approx(
        model.predict(xt[None, :])[0] - model.predict(xb[None, :])[0], abs=1e-9)
    assert all(v == 0.0 for v in res["channels"]["asp"].values())
    with pytest.raises(ValueError):
        shapley_attribute(X, {"icr": icr}, np.zeros(n, bool), ~b, ["a", "b", "c"])


def test_shapley_attribute_linear_factory_matches_ridge_contribution():
    rng = np.random.default_rng(5)
    n = 80
    X = rng.normal(size=(n, 2))
    X[40:] += np.array([1.0, -0.5])
    y = X @ np.array([0.2, 0.4])

    class Ols:
        def fit(self, X, y):
            self.w, self.c = bounded_ridge(X, y, 1e-9)
            return self

        def predict(self, M):
            return M @ self.w + self.c

    b = np.arange(n) < 40
    res = shapley_attribute(X, {"cpm": y}, b, ~b, ["u", "v"], model_factory=Ols)
    dx = X[~b].mean(0) - X[b].mean(0)
    assert res["contrib"]["u"] == pytest.approx(-0.2 * dx[0], abs=1e-6)       # CPM enters with a minus sign
    assert res["residual"] == pytest.approx(0.0, abs=1e-6)
    assert math.isfinite(res["observed_dln"])
