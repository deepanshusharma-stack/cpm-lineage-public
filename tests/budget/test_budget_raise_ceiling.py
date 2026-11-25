"""A raise is never offered where the gating curve says the next unit of spend loses money."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pytest

from cpm_lineage.budget import fit_entity_curve, probe_budget, raise_verdict, size_raise
from cpm_lineage.budget.saturation import CurveFit, marginal_raw, predict, unit_marginal_spend
from cpm_lineage.schema import BudgetDayRow

D0 = date(2026, 1, 1)


def _rows(b, theta, cap, n=60):
    out = []
    for i in range(n):
        sp = cap * (0.95 + 0.05 * ((i * 7) % 3) / 2) if i % 4 else cap * (0.3 + 0.5 * ((i * 37) % 10) / 10)
        out.append(BudgetDayRow(date=D0 + timedelta(i), entity_id="e", daily_budget=cap, spend=sp,
                                sales=b * math.log1p(sp / theta), impressions=sp * 10,
                                exhausted_at_minute=600 if i % 4 else None))
    return out


def _run(m, theta=50.0, cap=1000.0):
    rows = _rows(m * (theta + cap), theta, cap)
    fit = fit_entity_curve(rows, "e")
    roas = sum(r.sales for r in rows[-30:]) / sum(r.spend for r in rows[-30:])
    p = probe_budget("e", rows, cap, own_fit=fit, current_roas=roas, status="ACTIVE")
    return fit, p, raise_verdict(p, current_roas=roas, status="ACTIVE"), roas


def test_marginal_between_block_and_one_holds_the_raise():
    fit, p, v, roas = _run(0.95)
    assert fit.fit and fit.r2 > 0.99 and roas > 2.0                # a clearly profitable, well-fitted entity
    assert p.mroas_raw == pytest.approx(0.95, abs=0.01) and not p.mroas_block
    assert p.past_ceiling and p.rec_range is None
    assert v.code == "hold_past_ceiling" and not v.raise_ok and v.rec_range is None
    assert v.verb == "Hold the budget raise"


@pytest.mark.parametrize("m, code", [(0.5, "hold_flat"), (0.89, "hold_flat"), (0.92, "hold_past_ceiling"),
                                     (0.999, "hold_past_ceiling"), (1.0, "hold_past_ceiling"), (1.2, "raise"),
                                     (3.0, "raise")])
def test_verdict_by_marginal(m, code):
    _fit, _p, v, _roas = _run(m, theta=10.0)
    assert v.code == code                      # m == 1.0: the ceiling equals the cap, no zero-width raise
    assert v.raise_ok == (code == "raise")


def test_every_offered_range_pays_back_on_its_curve():
    rng = np.random.default_rng(5)
    offered = 0
    for _ in range(60):
        m, theta = float(rng.uniform(0.6, 4.0)), float(rng.uniform(30, 900))
        fit, p, v, _ = _run(m, theta=theta)
        if not (fit.fit and v.rec_range):
            continue
        offered += 1
        cap = 1000.0
        for point in v.rec_range:
            assert point > cap * 1.009
            assert predict(fit, point) - predict(fit, cap) >= (point - cap) - 1e-6
            assert marginal_raw(fit, point) >= 1.0 - 1e-9
    assert offered > 10


def test_pooled_curve_marginal_under_one_gives_no_range():
    cap, theta = 1000.0, 300.0
    pooled = fit_entity_curve(_rows(0.95 * (theta + cap), theta, cap), "e")
    flat = [BudgetDayRow(date=r.date, entity_id="e", daily_budget=cap, spend=cap * 0.97, sales=r.sales,
                         exhausted_at_minute=600) for r in _rows(0.95 * (theta + cap), theta, cap)]
    own = fit_entity_curve(flat, "e")
    assert not own.fit
    p = probe_budget("e", flat, cap, own_fit=own, pooled_fit=pooled, current_roas=3.0, status="ACTIVE")
    assert p.mroas_src == "account-pooled" and p.mroas_raw < 1.0 and p.unit_spend is not None
    assert p.rec_range is None and p.past_ceiling
    v = raise_verdict(p, current_roas=3.0, status="ACTIVE")
    assert v.code == "hold_past_ceiling" and not v.raise_ok
    # a pooled curve with headroom still lends a range, clamped to its own marginal-1 point
    roomy = fit_entity_curve(_rows(1.3 * (theta + cap), theta, cap), "e")
    p = probe_budget("e", flat, cap, own_fit=own, pooled_fit=roomy, current_roas=3.0, status="ACTIVE")
    assert p.rec_range is not None and max(p.rec_range) <= unit_marginal_spend(roomy) + 1e-9
    assert predict(roomy, p.rec_range[2]) - predict(roomy, cap) >= p.rec_range[2] - cap


def test_size_raise_never_passes_the_marginal_one_point():
    # ceiling beyond the supported range (sat_spend None) but inside the 1.5x target
    fit = CurveFit(fit=True, n=40, b=1400.0, theta=100.0, r2=0.9, xmax=600.0, sat_spend=None)
    cap = 1000.0
    target, inc_rev, inc_spend = size_raise(fit, cap)
    assert target == pytest.approx(1300.0) and marginal_raw(fit, target) >= 1.0 - 1e-9 and inc_rev >= inc_spend
    assert size_raise(CurveFit(fit=True, n=40, b=1105.0, theta=100.0, r2=0.9, xmax=600.0), cap) is None  # 0.5% headroom
    assert size_raise(CurveFit(fit=True, n=40, b=900.0, theta=100.0, r2=0.9, xmax=600.0), cap) is None
