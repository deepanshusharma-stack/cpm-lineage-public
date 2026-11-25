import math

import numpy as np
import pytest

from cpm_lineage.budget import (BudgetConfig, CurveFit, daily_series, fit_curve, fit_entity_curve,
                                fit_pooled_curve, marginal, marginal_raw, pooled_points, predict,
                                whatif)


def scattered(n=24, lo=200, hi=1500):
    """Spend values with no time trend (avoids the regime-step guard)."""
    return [lo + (i * 37) % (hi - lo) for i in range(n)]


def curve(xs, b, theta):
    return [b * math.log1p(x / theta) for x in xs]


def test_parameter_recovery_noiseless():
    xs = scattered(40, 100, 3000)
    f = fit_curve(xs, curve(xs, 3200.0, 400.0))
    assert f.fit and f.reason is None and f.n == 40
    assert f.b == pytest.approx(3200.0, rel=1e-5) and f.theta == pytest.approx(400.0, rel=1e-5)
    assert f.r2 == pytest.approx(1.0, abs=1e-9)
    assert f.sat_spend == pytest.approx(2800.0, rel=1e-4)        # b - theta, inside 2x max spend
    assert f.as_dict()["sat_spend"] == 2800 and f.as_dict()["b"] == 3200.0


def test_scale_invariance_of_decisions():
    xs = scattered(40, 100, 3000)
    ys = curve(xs, 3200.0, 400.0)
    big = fit_curve(xs, ys)
    small = fit_curve([x / 1000 for x in xs], [y / 1000 for y in ys], BudgetConfig(sat_theta_min=1e-6))
    assert marginal_raw(small, 1.0) == pytest.approx(marginal_raw(big, 1000.0), rel=1e-4)
    assert small.sat_spend * 1000 == pytest.approx(big.sat_spend, rel=1e-4)


def test_thin_at_threshold():
    xs = scattered(21, 100, 3000)
    assert fit_curve(xs[:20], curve(xs[:20], 3200, 400)).reason == "thin"
    assert fit_curve(xs, curve(xs, 3200, 400)).fit
    zeros = xs + [0.0, -5.0, float("nan")]                       # non-positive / non-finite points are dropped
    assert fit_curve(zeros, curve(xs, 3200, 400) + [1.0, 1.0, 1.0]).n == 21


def test_var_starved():
    xs = [1000 + (i % 5) for i in range(30)]
    f = fit_curve(xs, [3000 + i for i in range(30)])
    assert not f.fit and f.reason == "var_starved" and f.cv < 0.15
    assert f.as_dict() == {"fit": False, "reason": "var_starved", "n": 30, "cv": round(f.cv, 3)}


def test_no_relation_constant_sales_and_low_correlation():
    xs = scattered(30)
    assert fit_curve(xs, [500.0] * 30).reason == "no_relation"
    rng = np.random.default_rng(3)
    assert fit_curve(xs, list(rng.uniform(100, 200, 30) - np.array(xs) * 0.01)).reason == "no_relation"


def test_regime_step_two_flat_clusters():
    xs = [1000.0 + (i % 3) for i in range(15)] + [2000.0 + (i % 3) for i in range(15)]
    ys = [2000.0 + (i % 4) for i in range(15)] + [3500.0 + (i % 4) for i in range(15)]
    assert fit_curve(xs, ys).reason == "regime_step"
    shuffled = [xs[(i * 7) % 30] for i in range(30)]             # same points, no time order: fits
    assert fit_curve(shuffled, [ys[(i * 7) % 30] for i in range(30)]).reason != "regime_step"


def test_sat_spend_three_way_rule():
    xs = scattered(30, 100, 1000)
    assert fit_curve(xs, curve(xs, 600.0, 200.0)).sat_spend == pytest.approx(400.0, rel=1e-4)   # identified
    low = fit_curve(xs, curve(xs, 150.0, 300.0))
    assert low.fit and low.sat_spend == 0.0                       # marginal < 1 already at zero spend
    lin = fit_curve(xs, [3.0 * x for x in xs])
    assert lin.fit and lin.sat_spend is None                      # near-linear: ceiling not identified
    assert marginal_raw(lin, 500.0) == pytest.approx(3.0, rel=1e-3)   # slope stays pinned in range
    far = fit_curve(xs, curve(xs, 9000.0, 3000.0))                # b - theta = 6000 > 2 x max spend
    assert far.fit and far.sat_spend is None


def test_low_r2_rejected_and_never_raises():
    rng = np.random.default_rng(11)
    xs = scattered(60)
    ys = [max(0.0, 0.02 * x + rng.normal(0, 400)) for x in xs]
    f = fit_curve(xs, ys)
    assert (not f.fit and f.reason == "no_relation") or (f.fit and f.r2 >= 0.05)
    assert fit_curve([1, 2, 3], [1, 2]).reason == "degenerate"
    assert fit_curve(["x"], [1]).reason == "degenerate"
    assert fit_curve([], []).reason == "thin"


def test_marginal_predict_edges():
    f = CurveFit(fit=True, n=30, b=3200.0, theta=400.0, r2=0.9, xmax=1500.0, sat_spend=2800.0)
    assert marginal(f, 1000) == round(3200 / 1400, 2)
    assert marginal(f, -50) == marginal(f, 0) == 8.0
    assert marginal_raw(f, 2800.0) == pytest.approx(1.0)          # ceiling is where marginal = 1
    assert predict(f, 0) == 0.0 and predict(f, -10) == 0.0
    assert predict(f, 400) == pytest.approx(3200 * math.log(2))
    assert predict(f, 400, 0) == round(3200 * math.log(2))
    bad = CurveFit(fit=False, reason="thin")
    assert marginal(bad, 10) is None and predict(bad, 10) is None and marginal_raw(None, 1) is None
    assert whatif(bad, 1000) is None and whatif(f, None) is None and whatif(f, 0) is None


def test_whatif_grid_and_extrapolation_flag():
    f = CurveFit(fit=True, n=30, b=3200.0, theta=400.0, r2=0.9, xmax=1500.0, sat_spend=2800.0)
    pts = whatif(f, 1000.0)
    assert [p.mult for p in pts] == [0.7, 1.0, 1.3, 1.5, 2.0]
    assert [p.extrapolated for p in pts] == [False, False, False, False, True]   # 1575 = 1.05 x 1500 boundary
    assert not whatif(f, 1050.0, mults=(1.5,))[0].extrapolated                   # exactly 1.05 x xmax
    assert pts[1].sales == round(predict(f, 1000.0)) and pts[1].marginal == marginal(f, 1000.0)
    assert pts[0].sales < pts[1].sales < pts[4].sales and pts[0].marginal > pts[4].marginal


def test_series_helpers(budget_days):
    x, y = daily_series(budget_days, "cmp-winner")
    assert len(x) == len(y) == 90
    x30, _ = daily_series(budget_days, "cmp-winner", lookback_days=30)
    assert len(x30) == 30 and x30 == x[-30:]
    px, py = pooled_points(budget_days)
    assert len(px) == len(py) == len([r for r in budget_days])    # one point per entity-day
    assert isinstance(fit_entity_curve(budget_days, "cmp-winner"), CurveFit)
    assert isinstance(fit_pooled_curve(budget_days), CurveFit)
    assert daily_series([], "x") == ([], [])


def test_capped_entity_is_var_starved(budget_days):
    f = fit_entity_curve(budget_days, "cmp-winner")
    assert not f.fit and f.reason == "var_starved"                # spend pinned at the cap every day
