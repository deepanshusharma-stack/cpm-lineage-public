import math
from datetime import date, timedelta

import pytest

from cpm_lineage.budget import (CURVE_OWN, CURVE_OWN_LOWCONF, CURVE_POOLED, BudgetConfig, CurveFit,
                                MoveInput, ServingEvidence, budget_moves, budget_moves_from_rows,
                                fit_curve, move_inputs, predict, probe_budget, raise_verdict,
                                recommended_range, revenue_at_stake, size_raise, stepwise_evidence,
                                utilisation_ladder)
from cpm_lineage.config import Economics, TradingDay
from cpm_lineage.schema import BudgetDayRow, Entity, PerfRow

D0 = date(2026, 3, 1)


def days(n=10, capped=10, budget=1000.0, roas=3.0, low_spend=400.0, eid="e1", exhaust=600.0, **kw):
    out = []
    for i in range(n):
        sp = budget if i < capped else low_spend
        out.append(BudgetDayRow(date=D0 + timedelta(days=i), entity_id=eid, daily_budget=budget, spend=sp,
                                sales=sp * roas, exhausted_at_minute=exhaust if i < capped else None, **kw))
    return out


def fit(b, theta, r2=0.9, xmax=1500.0, sat="auto"):
    if sat == "auto":
        sat = 0.0 if b <= theta else (b - theta if b - theta <= 2 * xmax else None)
    return CurveFit(fit=True, n=40, cv=0.4, b=b, theta=theta, r2=r2, xmax=xmax, sat_spend=sat)


def scattered(n=24, lo=200, hi=1500):
    return [lo + (i * 37) % (hi - lo) for i in range(n)]


# --- probe facts ---------------------------------------------------------------------

def test_capped_gate_at_thresholds():
    assert probe_budget("e1", days(10, capped=6)).capped                 # 60% of active days
    assert not probe_budget("e1", days(10, capped=5)).capped
    assert not probe_budget("e1", days(6, capped=6)).capped              # fewer than 7 active days
    p = probe_budget("e1", [BudgetDayRow(D0 + timedelta(days=i), "e1", 1000.0, 900.0 if i < 6 else 899.0, 100.0)
                            for i in range(10)])
    assert p.days_capped == 6 and p.util == pytest.approx(0.8996)


def test_probe_facts_and_anomaly_and_soft_cap():
    p = probe_budget("e1", days(10, capped=8, roas=3.0))
    assert p.act_days == 10 and p.avg_spend == pytest.approx(880.0) and p.exh_pct == pytest.approx(0.8)
    assert p.cap_rev == pytest.approx(3000.0) and p.cap_roas == pytest.approx(3.0) and p.exhaust_hour == 10.0
    assert not p.soft_cap
    assert probe_budget("e1", days(10, budget=99.0)).anomaly and not probe_budget("e1", days(10, budget=99.0)).capped
    assert not probe_budget("e1", days(10, budget=99.0), economics=Economics(currency_unit=0.5)).anomaly
    over = [BudgetDayRow(D0 + timedelta(days=i), "e1", 1000.0, 6000.0, 100.0) for i in range(10)]
    assert probe_budget("e1", over, 1000.0).anomaly
    rows = days(10, capped=10)
    soft = [BudgetDayRow(r.date, r.entity_id, r.daily_budget, r.spend, r.sales,
                         exhausted_at_minute=600.0 if i < 2 else None) for i, r in enumerate(rows)]
    assert probe_budget("e1", soft).soft_cap                             # flagged on 20% < 30% of days


def test_serving_evidence_overrides():
    rows = days(10, capped=3)
    assert probe_budget("e1", rows, serving=ServingEvidence(held_days=5)).capped
    assert not probe_budget("e1", rows, serving=ServingEvidence(held_days=4)).capped
    assert probe_budget("e1", days(10), serving=ServingEvidence(held_days=0)).soft_cap
    p = probe_budget("e1", days(10), serving=ServingEvidence(dayparted_days=5, exhaust_hour=14.0))
    assert p.dayparted and p.slot_exhaust_risk and p.exhaust_hour == 14.0
    assert p.rec_range is None and not p.mroas_block                     # a schedule is never topped up
    p = probe_budget("e1", days(10, budget=2500.0), serving=ServingEvidence(dayparted_days=5))
    assert p.dayparted and not p.slot_exhaust_risk


# --- curve tiers and the marginal block ------------------------------------------------

def test_curve_tiers_r2_decision_floor():
    own = probe_budget("e1", days(10), own_fit=fit(3200, 400, r2=0.30))
    assert own.mroas_src == CURVE_OWN and own.mroas_raw == pytest.approx(3200 / 1400)
    assert own.mroas_cap == 2.29 and own.sat_spend == pytest.approx(2800.0) and own.whatif
    low = probe_budget("e1", days(10), own_fit=fit(3200, 400, r2=0.29))
    assert low.mroas_src == CURVE_OWN_LOWCONF and low.sat_lowconf
    assert low.mroas_cap == 2.29 and low.mroas_raw is None and low.sat_spend is None
    starved = CurveFit(fit=False, reason="var_starved", n=30, cv=0.02)
    pooled = probe_budget("e1", days(10), own_fit=starved, pooled_fit=fit(3200, 400, r2=0.5))
    assert pooled.mroas_src == CURVE_POOLED and pooled.var_starved and pooled.spend_cv == 0.02
    assert pooled.mroas_raw is not None and pooled.sat_spend is None      # ceiling is never borrowed
    none = probe_budget("e1", days(10), own_fit=starved, pooled_fit=fit(3200, 400, r2=0.2))
    assert none.mroas_src is None and none.mroas_raw is None


def test_mroas_block_at_threshold():
    # marginal at cap 1000 = b / (theta + 1000)
    blocked = probe_budget("e1", days(10), own_fit=fit(899.9 * 1.2, 200))
    assert blocked.mroas_raw < 0.9 and blocked.mroas_block and blocked.rec_range is None
    assert blocked.rev_at_stake is None
    open_ = probe_budget("e1", days(10), own_fit=fit(0.9 * 1200, 200))
    assert open_.mroas_raw == pytest.approx(0.9) and not open_.mroas_block
    lowconf = probe_budget("e1", days(10), own_fit=fit(600, 200, r2=0.1))
    assert not lowconf.mroas_block and lowconf.rec_range is not None       # low-r2 fit may not block
    ineff = probe_budget("e1", days(10, roas=0.8), own_fit=fit(600, 200))
    assert not ineff.mroas_block and ineff.exh_inefficient and ineff.rec_range is None


def test_unblocked_band_below_one_offers_no_range():
    p = probe_budget("e1", days(10), own_fit=fit(0.95 * 1200, 200))       # marginal 0.95, ceiling 940 < cap
    assert not p.mroas_block and p.past_ceiling and p.rec_range is None
    ref = probe_budget("e1", days(10), own_fit=fit(0.95 * 1200, 200),
                       cfg=BudgetConfig(range_requires_headroom=False))
    assert ref.rec_range == pytest.approx((1300.0, 2000.0, 1500.0))       # reference fallback, opt-in


def test_rec_range_ceiling_clamp_and_stake():
    assert recommended_range(1000.0, None) == pytest.approx((1300.0, 2000.0, 1500.0))
    assert recommended_range(1000.0, 1400.0) == pytest.approx((1300.0, 1400.0, 1400.0))
    assert recommended_range(1000.0, 1200.0) == pytest.approx((1200.0, 1200.0, 1200.0))
    assert recommended_range(1000.0, 5000.0) == pytest.approx((1300.0, 2000.0, 1500.0))
    assert recommended_range(1000.0, 900.0) == pytest.approx((1300.0, 2000.0, 1500.0))
    p = probe_budget("e1", days(10, capped=8), own_fit=fit(1800, 400))    # ceiling 1400
    assert p.rec_range == pytest.approx((1300.0, 1400.0, 1400.0))
    assert p.unserved_hours == 8.0 and p.rev_at_stake == pytest.approx(3000.0 / 10 * 8 * 8)  # observed capped days
    assert revenue_at_stake(3000.0, 6.0, 8) is None and revenue_at_stake(3000.0, 18.0, 8) is None
    assert revenue_at_stake(3000.0, 20.0, 8, TradingDay(end_hour=23.0)) == pytest.approx((3000 / 20 * 3 * 8, 3.0))
    assert probe_budget("e1", days(10), current_roas=0.9).rec_range is None


# --- utilisation ladder and verdict ------------------------------------------------------

def test_utilisation_ladder():
    base = dict(capped=False, act_days=10, status_active=True)
    assert utilisation_ladder(util=0.19, **base) == "severe"
    assert utilisation_ladder(util=0.5, mean_position=1.5, **base) == "volume-ceiling"
    assert utilisation_ladder(util=0.5, mean_position=1.5, rank1_share=0.59, **base) == "bid-constrained"
    assert utilisation_ladder(util=0.5, mean_position=1.6, rank1_share=0.9, **base) == "bid-constrained"
    assert utilisation_ladder(util=0.5, mean_position=4, below_bid_spend_share=0.5, below_bid_targets=3,
                              **base) == "stepwise"
    assert utilisation_ladder(util=0.5, mean_position=4, below_bid_spend_share=0.5, below_bid_targets=2,
                              **base) == "bid-constrained"
    assert utilisation_ladder(util=0.5, **base) is None                    # no position signal
    assert utilisation_ladder(util=0.85, mean_position=1, **base) is None
    assert utilisation_ladder(util=0.5, mean_position=1, capped=True, act_days=10, status_active=True) is None
    assert utilisation_ladder(util=0.5, mean_position=1, capped=False, act_days=6, status_active=True) is None
    assert utilisation_ladder(util=0.5, mean_position=9.6, top_position=10, lower_is_better=False, **base) \
        == "volume-ceiling"
    p = probe_budget("e1", days(10, capped=0, low_spend=100.0), status="ACTIVE")
    assert p.ladder == "severe"
    assert probe_budget("e1", days(10, capped=0, low_spend=100.0), status="PAUSED").ladder is None


def test_stepwise_evidence():
    def row(kw, spend, imps, bid):
        return PerfRow(date=D0, entity_id="e1", spend=spend, impressions=imps, targeting_unit=kw, bid=bid)
    rows = [row("a", 100.0, 1000.0, 200.0), row("b", 100.0, 1000.0, 150.0), row("c", 100.0, 1000.0, 119.0),
            row("d", 100.0, 1000.0, 100.0)]
    share, n = stepwise_evidence(rows)                                     # CPM 100 vs 0.85 x bid
    assert n == 3 and share == pytest.approx(0.75)


def test_verdict_precedence():
    p = probe_budget("e1", days(10, capped=8), own_fit=fit(1800, 400))
    v = raise_verdict(p, current_roas=3.0, status="ACTIVE")
    assert v.code == "raise" and v.raise_ok and v.rec_range == p.rec_range and v.mroas == p.mroas_cap
    assert raise_verdict(p, current_roas=3.0, status="BUDGET_HELD").code == "top_up"
    assert raise_verdict(p, current_roas=3.0, status="PAUSED").code == "investigate_stop"
    assert raise_verdict(p, current_roas=3.0, status="ENDED").code == "no_action"
    assert raise_verdict(p, current_roas=None).code == "investigate_tracking"
    assert raise_verdict(p, current_roas=2.0, availability_block=True).code == "conflict_supply"
    assert raise_verdict(p, current_roas=1.99, availability_block=True).code == "hold_monitor"
    assert raise_verdict(p, current_roas=1.0).code == "hold_monitor"
    assert raise_verdict(p, current_roas=0.99).code == "tighten"
    assert raise_verdict(p, current_roas=3.0, sub_breakeven_share=0.40).code == "hold_bleed"
    assert raise_verdict(p, current_roas=3.0, sub_breakeven_share=0.39).code == "raise"
    assert raise_verdict(p, current_roas=3.0, organic_share=0.70).code == "hold_organic"
    flat = probe_budget("e1", days(10), own_fit=fit(600, 200))
    assert raise_verdict(flat, current_roas=3.0).code == "hold_flat"
    # bleed is checked before the curve, supply before bleed, day-part first
    assert raise_verdict(flat, current_roas=3.0, sub_breakeven_share=0.5).code == "hold_bleed"
    assert raise_verdict(flat, current_roas=3.0, sub_breakeven_share=0.5, availability_block=True).code \
        == "conflict_supply"
    dp = probe_budget("e1", days(10), serving=ServingEvidence(dayparted_days=6))
    assert raise_verdict(dp, current_roas=3.0, availability_block=True).code == "conflict_daypart"
    # breakeven-relative bars
    eco = Economics(breakeven=2.0)
    assert raise_verdict(p, current_roas=3.0, economics=eco).code == "hold_monitor"
    assert raise_verdict(p, current_roas=4.0, economics=eco).code == "raise"


# --- account-level moves -----------------------------------------------------------------

def mi(eid, roas, capped=10, avg=1000.0, db=1000.0, act=10, status="ACTIVE"):
    return MoveInput(eid, db, act, avg, roas, capped, status)


def test_move_buckets_and_thresholds():
    m = budget_moves([
        mi("win", 3.0), mi("win-below-account", 2.2), mi("mid", 1.5), mi("bleed", 0.6),
        mi("parked", 0.8, capped=0, avg=690.0), mi("parked-util", 0.8, capped=0, avg=700.0),
        mi("parked-ok", 1.0, capped=0, avg=300.0), mi("parked-paused", 0.5, capped=0, avg=300.0, status="PAUSED"),
        mi("unbound-winner", 5.0, capped=5), mi("few-days", 5.0, act=6, capped=6),
    ], account_roas=2.5)
    assert [r.entity_id for r in m.scale] == ["win"]
    assert m.scale[0].extra_daily_sales == pytest.approx(0.5 * 1000 * 3.0) and m.scale[0].sized == "linear"
    assert [r.entity_id for r in m.tighten] == ["bleed"] and m.tighten[0].daily_waste == pytest.approx(400.0)
    assert [r.entity_id for r in m.release] == ["parked"]
    assert m.release[0].freeable == pytest.approx(400.0)                   # 40% of the cap
    assert m.freed == pytest.approx(400.0) and m.need == pytest.approx(500.0) and m.realloc_note
    assert [r.entity_id for r in budget_moves([mi("w", 2.2)]).scale] == ["w"]        # account ROAS unknown
    assert not budget_moves([mi("w", 1.99)]).scale and not budget_moves([mi("w", 1.0)]).tighten
    low = budget_moves([mi("p", 0.5, capped=0, avg=150.0)])
    assert low.release[0].freeable == pytest.approx(150.0) and low.realloc_note is None   # bounded by spend


def test_moves_sorted_and_truncated():
    m = budget_moves([mi(f"w{i}", 2.0 + i) for i in range(12)])
    assert len(m.scale) == 8 and m.scale[0].entity_id == "w11" and m.scale[-1].entity_id == "w4"


@pytest.mark.parametrize("b,theta,keep", [(3200.0, 400.0, True), (600.0, 200.0, False)])
def test_curve_sizing_invariant_noiseless(b, theta, keep):
    xs = scattered(24)
    f = fit_curve(xs, [b * math.log1p(x / theta) for x in xs])
    assert f.fit
    m = budget_moves([mi("e", 3.0)], fits={"e": f})
    if keep:
        (row,) = m.scale
        assert row.sized == "curve" and row.confidence == "high" and row.estimator == "saturation_curve"
        assert row.raise_to == pytest.approx(1500.0) and row.raise_to <= 1.5 * 1000 + 1e-9
        assert row.extra_daily_sales >= row.inc_spend                    # the invariant
        assert row.extra_daily_sales == pytest.approx(predict(f, 1500.0) - predict(f, 1000.0))
    else:
        assert not m.scale and m.past_peak == ["e"]


def test_size_raise_ceiling_and_unidentified_ceiling():
    new, rev, sp = size_raise(fit(1800, 400), 1000.0)                    # ceiling 1400 < 1.5 x cap
    assert new == pytest.approx(1400.0) and rev >= sp
    assert size_raise(fit(1300, 400), 1000.0) is None                    # ceiling 900 <= cap
    assert size_raise(fit(1400, 400), 1000.0) is None                    # ceiling exactly at the cap
    lin = fit(3.0e9, 1.0e9, sat=None)                                    # near-linear, slope 3
    new, rev, sp = size_raise(lin, 1000.0)
    assert new == pytest.approx(1500.0) and rev == pytest.approx(1500.0, rel=1e-3) and rev >= sp
    flat = fit(0.5e9, 1.0e9, sat=None)                                   # slope 0.5 with no ceiling: still rejected
    assert size_raise(flat, 1000.0) is None


def test_unverified_scale_rows_are_kept_and_flagged():
    fits = {"a": CurveFit(fit=False, reason="var_starved", n=30, cv=0.01),
            "c": fit(3200, 400, r2=0.2)}
    m = budget_moves([mi("a", 3.0), mi("b", 4.0), mi("c", 5.0)], fits=fits)
    by = {r.entity_id: r for r in m.scale}
    assert set(by) == {"a", "b", "c"}
    assert by["a"].marginal_unverified and by["a"].var_starved and by["a"].extra_daily_sales is None
    assert by["b"].marginal_unverified and not by["b"].var_starved and by["b"].confidence == "low"
    assert by["c"].marginal_unverified and by["c"].mroas is not None      # low-r2 fit neither sizes nor ejects
    ref = budget_moves([mi("c", 5.0)], fits=fits, cfg=BudgetConfig(moves_require_decision_r2=False))
    assert ref.scale[0].sized == "curve"


def test_breakeven_rebases_move_thresholds():
    eco = Economics(breakeven=2.0)
    m = budget_moves([mi("a", 3.0), mi("b", 1.5), mi("c", 4.0)], economics=eco)
    assert [r.entity_id for r in m.scale] == ["c"] and [r.entity_id for r in m.tighten] == ["b"]
    assert m.tighten[0].daily_waste == pytest.approx(1000 * (1 - 1.5 / 2.0))


def test_move_inputs_and_synthetic_end_to_end(budget_days, entities, synthetic_profile):
    inputs = {m.entity_id: m for m in move_inputs(budget_days, entities)}
    w = inputs["cmp-winner"]
    assert w.capped_days >= 0.6 * w.act_days and w.roas > 4
    m = budget_moves_from_rows(budget_days, entities, status_map=synthetic_profile.status_map)
    assert [r.entity_id for r in m.scale] == ["cmp-winner"]
    row = m.scale[0]
    assert row.marginal_unverified and row.var_starved and row.extra_daily_sales is None
    for r in m.scale:
        if r.sized == "curve":
            assert r.extra_daily_sales >= r.inc_spend
    ent = Entity("x", daily_budget=500.0)
    rows = [BudgetDayRow(D0 + timedelta(days=i), "x", 1000.0, 480.0, 100.0) for i in range(8)]
    assert move_inputs(rows, [ent])[0].capped_days == 8                  # entity budget wins over the row cap
    assert move_inputs(rows[:6], [ent]) == []
