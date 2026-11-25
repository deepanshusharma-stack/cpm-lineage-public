import math
from datetime import date, timedelta

import pytest

from cpm_lineage.budget import (ESTIMATORS, BudgetConfig, CurveFit, ad_type_mix, apply_marginal,
                                attach_plateau, compare_estimators, curve_increment, entity_drag,
                                exhaust_raise_pct, expected_value, finite_difference_marginal,
                                marginal_roas_by_entity, predict, probe_budget, raise_verdict,
                                recommend_mix, stake_budget_cut, stake_raise_exhaust,
                                stake_raise_underserved)
from cpm_lineage.config import Economics, TradingDay
from cpm_lineage.schema import PerfRow
from cpm_lineage.windows import split_rows

D0 = date(2026, 1, 1)


def row(i, eid, ad_type, spend, sales):
    return PerfRow(date=D0 + timedelta(days=i), entity_id=eid, spend=spend, impressions=1000.0,
                   sales=sales, ad_type=ad_type)


# --- ad-type mix ------------------------------------------------------------------------

def test_mix_plus_rate_equals_delta_exactly():
    prior = [row(0, "a", "search", 600.0, 2400.0), row(0, "b", "reco", 400.0, 400.0),
             row(0, "c", "gone", 50.0, 100.0)]
    cur = [row(1, "a", "search", 300.0, 1500.0), row(1, "b", "reco", 700.0, 630.0),
           row(1, "d", "new", 80.0, 0.0)]
    m = ad_type_mix(prior, cur)
    assert m.delta == pytest.approx(m.roas_cur - m.roas_prev)
    assert abs(m.mix_total + m.rate_total - m.delta) < 1e-9 and abs(m.residual) < 1e-9
    for t in m.types:
        assert t.total == pytest.approx(t.mix + t.rate)
    assert [t.total for t in m.types] == sorted(t.total for t in m.types)   # biggest drag first
    s = {t.ad_type: t for t in m.types}
    assert s["search"].mix == pytest.approx((300 / 1080 - 600 / 1050) * (5.0 + 4.0) / 2)
    assert s["search"].rate == pytest.approx((300 / 1080 + 600 / 1050) / 2 * (5.0 - 4.0))


def test_mix_identity_on_synthetic(perf_rows, windows_30, synthetic_profile, entities):
    prior, cur = split_rows(perf_rows, windows_30)
    types = {e.entity_id: e.ad_type for e in entities}
    m = ad_type_mix(prior, cur, entity_types=types, canonical=synthetic_profile.canonical_ad_type,
                    no_attribution_types=synthetic_profile.no_attribution_types(),
                    drillable_types=synthetic_profile.keyword_grain_types())
    assert abs(m.mix_total + m.rate_total - m.delta) < 1e-9
    assert any(t.no_attrib for t in m.types) and any(not t.drillable for t in m.types)
    drag, n80 = entity_drag(cur, entity_types=types, canonical=synthetic_profile.canonical_ad_type,
                            no_attribution_types=synthetic_profile.no_attribution_types())
    assert drag and 1 <= n80 <= len(drag) and drag[-1].cum_pct == pytest.approx(100.0)
    assert all(d.drag > 0 for d in drag) and "cmp-reach" not in {d.key for d in drag}
    assert "cmp-conquest" in {d.key for d in drag[:n80]}
    acts = recommend_mix(m).actions
    assert any(a.kind == "judge_halo" for a in acts)


def test_entity_drag_formula():
    cur = [row(0, "good", "s", 100.0, 500.0), row(0, "bad", "s", 100.0, 100.0), row(0, "worse", "s", 300.0, 150.0)]
    drag, n80 = entity_drag(cur)
    acct = 750 / 500
    assert [d.key for d in drag] == ["worse", "bad"]
    assert drag[0].drag == pytest.approx(300 * (acct - 0.5)) and n80 == 1
    # one definition of drag: the same rows, units and count as the decomposition primitive
    from cpm_lineage.budget import DragRow
    from cpm_lineage.decompose import DragRow as CoreDragRow, pareto_drag
    core = pareto_drag([{"key": r.entity_id, "spend": r.spend, "sales": r.sales} for r in cur])
    assert DragRow is CoreDragRow and n80 == core.n80
    assert [(d.key, d.cum_pct) for d in drag] == [(d.key, d.cum_pct) for d in core.rows if d.drag > 0]
    assert drag[-1].cum_pct == pytest.approx(100.0)


def test_mix_identity_holds_with_sales_on_a_type_without_spend():
    # type B is paused in the current period but still books lagged sales
    prior = [row(0, "a", "A", 100.0, 300.0), row(0, "b", "B", 100.0, 100.0)]
    cur = [row(40, "a", "A", 100.0, 300.0), row(40, "b", "B", 0.0, 50.0)]
    m = ad_type_mix(prior, cur)
    assert m.roas_cur == pytest.approx(3.0) and m.roas_prev == pytest.approx(2.0)
    assert m.delta == pytest.approx(1.0) and m.mix_total + m.rate_total == pytest.approx(m.delta)
    assert abs(m.residual) < 1e-12 and m.orphan_sales_cur == pytest.approx(50.0) and m.orphan_sales_prev == 0.0


def _mix_with_types(**plateau):
    prior = [row(0, "a", "search", 60000.0, 180000.0), row(0, "b", "reco", 60000.0, 240000.0),
             row(0, "c", "banner", 60000.0, 0.0), row(0, "d", "display", 150000.0, 100000.0),
             row(0, "e", "tiny", 100.0, 900.0)]
    cur = [row(40, "a", "search", 90000.0, 100000.0), row(40, "b", "reco", 60000.0, 240000.0),
           row(40, "c", "banner", 60000.0, 0.0), row(40, "d", "display", 150000.0, 100000.0),
           row(40, "e", "tiny", 100.0, 900.0)]
    m = ad_type_mix(prior, cur, no_attribution_types={"banner"}, drillable_types={"search", "reco", "tiny"})
    for t in m.types:
        t.near_plateau = plateau.get(t.ad_type)
    return m


def test_recommend_mix_sink_skips_plateau_and_money_thresholds_scale():
    kinds = lambda rec: {(a.kind, a.ad_type) for a in rec.actions}
    rec = recommend_mix(_mix_with_types())
    assert ("feed", "reco") in kinds(rec) and ("cap", "search") in kinds(rec)
    assert ("judge_halo", "banner") in kinds(rec) and ("reduce", "display") in kinds(rec)
    assert ("feed", "tiny") not in kinds(rec)                              # below the material spend
    rec = recommend_mix(_mix_with_types(reco=True))
    assert ("feed", "search") in kinds(rec) and not rec.all_near_plateau
    rec = recommend_mix(_mix_with_types(reco=True, search=True, display=True))
    assert ("feed", "reco") in kinds(rec) and rec.all_near_plateau         # fall back to all material types
    rec = recommend_mix(_mix_with_types(), economics=Economics(currency_unit=0.001))
    assert ("feed", "tiny") in kinds(rec)
    rec = recommend_mix(_mix_with_types(), economics=Economics(currency_unit=10.0))
    assert not [a for a in rec.actions if a.kind in ("feed", "cap", "reduce")]
    assert recommend_mix(_mix_with_types()).primary in ("MIX", "RATE")


def test_attach_plateau_sums_per_type_and_day():
    hist = []
    for i in range(40):
        x = 200 + (i * 37) % 1300
        y = 600.0 * math.log1p(x / 200.0)
        hist += [row(i, "a", "old-label", x / 2, y / 2), row(i, "b", "new-label", x / 2, y / 2)]   # renamed mid-way
        hist.append(row(i, "c", "steep", x, 3200.0 * math.log1p(x / 400.0)))
    canon = lambda raw: "search" if raw in ("old-label", "new-label") else raw
    cur = [r for r in hist if r.date >= D0 + timedelta(days=30)]
    m = ad_type_mix(hist[:30], cur, canonical=canon)
    fits = attach_plateau(m, hist, 10, canonical=canon)
    s = {t.ad_type: t for t in m.types}
    assert fits["search"].b == pytest.approx(600.0, rel=1e-4)             # halves re-joined before the fit
    mean_daily = s["search"].spend_cur / 10
    assert s["search"].mroas == round(600 / (200 + mean_daily), 2) and s["search"].near_plateau
    assert s["steep"].near_plateau is False and s["steep"].mroas > 1


# --- estimator (a): stake and expected value ------------------------------------------------

def test_stake_formulas():
    assert stake_budget_cut(1000.0, 0.6) == pytest.approx(400.0)
    assert stake_budget_cut(1000.0, 1.2) == 0.0
    assert stake_budget_cut(1000.0, 1.0, Economics(breakeven=2.0)) == pytest.approx(500.0)
    assert stake_raise_underserved(1000.0, 3.0, 2.0, 20.0) == pytest.approx(200.0)
    assert stake_raise_exhaust(1000.0, 3.0, 20.0) == pytest.approx(400.0)
    assert stake_raise_exhaust(1000.0, 50.0, 20.0) == pytest.approx(1000.0)       # ROAS capped at 6 x breakeven
    assert stake_raise_exhaust(1000.0, 0.5, 20.0) == 0.0


def test_exhaust_raise_pct_clamps():
    assert exhaust_raise_pct(11.0) == 50.0                                   # 90% -> clamp 50
    assert exhaust_raise_pct(16.0) == float(round(100 * 6 / 16 * 0.9))       # 34
    assert exhaust_raise_pct(21.5) == 5.0 and exhaust_raise_pct(23.0) == 5.0
    assert exhaust_raise_pct(None) is None and exhaust_raise_pct(0) is None
    assert exhaust_raise_pct(16.0, TradingDay(funding_target_hour=18.0)) == float(round(100 * 2 / 16 * 0.9))


def test_expected_value():
    up = expected_value(lever="budget", direction="up", frm=1000.0, to=1500.0, roas=3.0)
    assert (up.est_day, up.p_success, up.ev_day, up.ev_kind) == (1500.0, 0.5, 750.0, "money")
    assert up.ev_density == pytest.approx(1.5) and up.estimator == "stake_ev"
    m = expected_value(lever="budget", direction="up", frm=1000.0, to=1500.0, roas=3.0, mroas=1.2)
    assert m.est_day == pytest.approx(600.0)                                 # marginal wins when present
    down = expected_value(lever="budget", direction="down", frm=1000.0, to=800.0, roas=0.6)
    assert down.est_day == pytest.approx(80.0)
    assert expected_value(lever="budget", direction="down", frm=1000.0, to=800.0, roas=1.5,
                          economics=Economics(breakeven=2.0)).est_day == 0.0  # literal 1 by default
    assert expected_value(lever="budget", direction="down", frm=1000.0, to=800.0, roas=1.5,
                          economics=Economics(breakeven=2.0),
                          cfg=BudgetConfig(ev_breakeven_relative=True)).est_day == pytest.approx(100.0)
    kw = expected_value(lever="keyword", direction="down", leak=3000.0, days=30)
    assert kw.est_day == 100.0 and kw.ev_kind == "money" and kw.ev_density is None
    opp = expected_value(lever="experiment", direction="none", stake=90000.0, days=30)
    assert opp.est_day == 3000.0 and opp.ev_kind == "opportunity"
    assert expected_value(lever="budget", direction="up", frm=1.0, to=2.0, roas=1.0,
                          win_rate=0.8, n_outcomes=5).p_success == 0.8
    assert expected_value(lever="budget", direction="up", frm=1.0, to=2.0, roas=1.0,
                          win_rate=0.8, n_outcomes=4).p_success == 0.5


# --- estimator (c): finite difference ---------------------------------------------------------

def test_finite_difference_gates():
    sp = [100.0, 120.0, 110.0, 130.0, 500.0, 520.0, 510.0, 530.0]
    sa = [300.0, 310.0, 305.0, 315.0, 900.0, 910.0, 905.0, 915.0]
    assert finite_difference_marginal(sp, sa) == pytest.approx((907.5 - 307.5) / (515.0 - 115.0))
    assert finite_difference_marginal(sp[:7], sa[:7]) is None                # fewer than 8 days
    assert finite_difference_marginal(sp + [0.0], sa + [0.0]) is not None    # zero-spend days dropped
    near = [100.0] * 4 + [200.0] * 4
    assert finite_difference_marginal(near, sa) is None                      # gap exactly 100: not above
    assert finite_difference_marginal([100.0] * 4 + [200.1] * 4, sa) is not None
    assert finite_difference_marginal(sp, list(reversed(sa))) == 0.0         # floored at zero
    assert finite_difference_marginal(near, sa, economics=Economics(currency_unit=0.5)) is not None


def test_marginal_roas_by_entity(budget_days):
    out = marginal_roas_by_entity(budget_days, days=30)
    assert out and all(v >= 0 for v in out.values())
    assert "cmp-winner" not in out                                           # pinned at its cap: no spend contrast
    assert marginal_roas_by_entity([]) == {}


def test_apply_marginal_table():
    a = lambda **kw: apply_marginal(frm=1000.0, **kw)
    assert a(direction="up", pct=20.0, marginal=0.79).action == "veto"
    r = a(direction="up", pct=20.0, marginal=0.8)
    assert (r.action, r.pct, r.to) == ("halve", 10.0, pytest.approx(1100.0))
    assert a(direction="up", pct=7.0, marginal=1.49).pct == 5.0              # floor of the halved step
    assert a(direction="up", pct=20.0, marginal=1.5).action == "keep"
    r = a(direction="down", pct=-10.0, marginal=0.49)
    assert (r.action, r.pct, r.to) == ("deepen", -25.0, pytest.approx(750.0))
    assert a(direction="down", pct=-30.0, marginal=0.2).pct == -35.0
    assert a(direction="down", pct=-10.0, marginal=0.5).action == "keep"
    assert a(direction="up", pct=20.0, marginal=0.1, family="defense").action == "exempt"
    assert a(direction="up", pct=20.0, marginal=None).action == "no_estimate"
    assert a(direction="up", pct=20.0, marginal=1.3, roas_deflator=0.6).action == "veto"   # 0.78 < 0.8
    assert a(direction="up", pct=20.0, marginal=1.5, economics=Economics(breakeven=2.0)).action == "veto"
    assert a(direction="up", pct=20.0, marginal=1.5).estimator == "finite_difference"


# --- the three estimators side by side ----------------------------------------------------------

def test_estimators_are_labelled_and_not_reconciled():
    f = CurveFit(fit=True, n=40, b=3200.0, theta=400.0, r2=0.9, xmax=1500.0, sat_spend=2800.0)
    c = compare_estimators(frm=1000.0, to=1500.0, roas=3.0, fit=f, fd_marginal=1.1)
    assert tuple(c["estimates"]) == ESTIMATORS and c["reconciled"] is False
    est = c["estimates"]
    assert est["stake_ev"].per_day == pytest.approx(1500.0)
    assert est["saturation_curve"].per_day == pytest.approx(predict(f, 1500.0) - predict(f, 1000.0))
    assert est["finite_difference"].per_day == pytest.approx(550.0)
    assert all(e.estimator == k for k, e in est.items())
    assert c["spread"] == pytest.approx(1500.0 - 550.0)                      # the numbers genuinely differ
    assert curve_increment(None, 1.0, 2.0) is None
    assert compare_estimators(frm=1.0, to=2.0)["spread"] is None


def test_synthetic_winner_probe_and_verdict(budget_days, entities, synthetic_profile):
    from cpm_lineage.budget import fit_entity_curve, fit_pooled_curve
    ent = next(e for e in entities if e.entity_id == "cmp-winner")
    p = probe_budget("cmp-winner", budget_days, ent.daily_budget, status=ent.status,
                     status_map=synthetic_profile.status_map,
                     own_fit=fit_entity_curve(budget_days, "cmp-winner"),
                     pooled_fit=fit_pooled_curve(budget_days))
    assert p.capped and not p.anomaly and not p.soft_cap and p.var_starved and p.sat_spend is None
    assert p.cap_roas > 4 and 6 < p.exhaust_hour < 18 and p.rev_at_stake > 0
    v = raise_verdict(p, current_roas=p.cap_roas, status_map=synthetic_profile.status_map)
    if p.mroas_block:
        assert v.code == "hold_flat"
    else:
        assert v.code == "top_up" and v.raise_ok and v.rec_range[1] <= 2.0 * ent.daily_budget + 1e-9
