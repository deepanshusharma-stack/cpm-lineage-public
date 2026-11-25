"""Layer-1 identity, verdict and prior-rate two-factor decompositions."""

from __future__ import annotations

import math
import random

import pytest

from cpm_lineage.config import Economics
from cpm_lineage.identity import (
    CHANNEL_SIGNS,
    IdentityConfig,
    decompose_rows,
    layer1_verdict,
    log_decomposition,
    quartile_buckets,
    roas_move,
    rounded_parts,
    two_factor,
    unit_decomposition,
    value_decomposition,
    window_aggregate,
)
from cpm_lineage.metrics import cell_from_rows, daily_cells
from cpm_lineage.windows import split_rows


def agg(spend, sales, impressions, units, bid=None):
    d = {"spend": spend, "sales": sales, "impressions": impressions, "units": units}
    if bid is not None:
        d["bid"] = bid
    return d


def test_channel_signs():
    assert CHANNEL_SIGNS == {"icr1k": 1, "asp": 1, "cpm": -1}


def test_window_aggregate_ratios_and_zero_denominators():
    a = window_aggregate(agg(200.0, 800.0, 40_000.0, 16.0))
    assert a.roas == pytest.approx(4.0)
    assert a.cpm == pytest.approx(5.0)
    assert a.icr1k == pytest.approx(0.4)
    assert a.asp == pytest.approx(50.0)
    assert a.icr1k * a.asp / a.cpm == pytest.approx(a.roas, abs=1e-12)
    assert a.rpm == pytest.approx(a.roas * a.cpm)
    z = window_aggregate(agg(0, 0, 0, 0))
    assert (z.roas, z.cpm, z.icr1k, z.asp) == (0.0, 0.0, 0.0, 0.0)
    assert not z.all_positive


def test_identity_exact_on_random_inputs():
    rng = random.Random(11)
    for _ in range(500):
        b = agg(*(rng.uniform(1e-2, 1e6) for _ in range(4)))
        t = agg(*(rng.uniform(1e-2, 1e6) for _ in range(4)))
        res = log_decomposition(b, t)
        target = math.log((t["sales"] / t["spend"]) / (b["sales"] / b["spend"]))
        total = sum(c.log_points for c in res.channels.values())
        assert res.exact
        assert total == pytest.approx(target, abs=1e-9)
        assert res.raw_dln == pytest.approx(target, abs=1e-9)
        assert abs(res.residual) < 1e-9
        if abs(res.raw_dln) > 1e-4:
            assert sum(c.share_pct for c in res.channels.values()) == pytest.approx(100.0, abs=1e-6)


def test_channel_directions():
    base = agg(100.0, 400.0, 10_000.0, 8.0)
    dearer = log_decomposition(base, agg(160.0, 400.0, 10_000.0, 8.0))
    assert dearer.log_points("cpm") == pytest.approx(-math.log(1.6))
    assert dearer.log_points("icr1k") == pytest.approx(0.0, abs=1e-12)
    assert dearer.log_points("asp") == pytest.approx(0.0, abs=1e-12)
    assert dearer.dominant == "cpm" and dearer.worst == "cpm"
    assert dearer.channels["cpm"].own_pct == pytest.approx(60.0)
    assert dearer.channels["cpm"].share_pct == pytest.approx(100.0)
    assert dearer.roas_pct == pytest.approx(100 * (1 / 1.6 - 1))
    assert "->" in dearer.channels["cpm"].change


def test_window_sums_differ_from_daily_means(make_perf_row):
    # two very unequal days: a mean of daily ROAS would say 5.5, the window says ~1.09
    rows = [
        make_perf_row(day=0, spend=1000.0, impressions=100_000, sales=1000.0, units=10),
        make_perf_row(day=1, spend=10.0, impressions=1000, sales=100.0, units=1),
    ]
    a = window_aggregate(rows)
    assert a.roas == pytest.approx(1100.0 / 1010.0)
    daily_mean = (1000.0 / 1000.0 + 100.0 / 10.0) / 2
    assert abs(daily_mean - a.roas) > 4
    assert a.icr1k * a.asp / a.cpm == pytest.approx(a.roas, abs=1e-12)


def test_zero_window_is_floored_not_exact():
    res = log_decomposition(agg(100.0, 300.0, 1000.0, 3.0), agg(100.0, 0.0, 1000.0, 0.0))
    assert not res.exact and res.residual is None
    assert all(math.isfinite(c.log_points) for c in res.channels.values())


def test_small_move_denominator_fallback_and_suppression():
    same = agg(100.0, 300.0, 1000.0, 3.0)
    assert log_decomposition(same, same).dln == pytest.approx(1e-4)
    assert log_decomposition(same, same, model_total=-0.3).dln == pytest.approx(-0.3)
    assert log_decomposition(same, same, model_total=5e-5).dln == pytest.approx(1e-4)
    moved = agg(100.0, 330.0, 1000.0, 3.0)
    res = log_decomposition(same, moved, model_total=9.0)
    assert res.dln == pytest.approx(res.raw_dln)
    sup = log_decomposition(same, moved, suppress_shares=True)
    assert all(c.share_pct is None for c in sup.channels.values())
    assert sup.channels["asp"].log_points == pytest.approx(math.log(1.1))


def test_min_dln_threshold_boundary():
    cfg = IdentityConfig(min_dln=0.05)
    b = agg(100.0, 300.0, 1000.0, 3.0)
    just_under = agg(100.0, 300.0 * math.exp(0.049), 1000.0, 3.0)
    just_over = agg(100.0, 300.0 * math.exp(0.051), 1000.0, 3.0)
    assert log_decomposition(b, just_under, cfg=cfg).dln == pytest.approx(0.05)
    assert log_decomposition(b, just_over, cfg=cfg).dln == pytest.approx(0.051)


def test_decompose_rows_exact_for_every_synthetic_entity(perf_rows, windows_30):
    for entity_id, rows in _by_entity(perf_rows).items():
        res = decompose_rows(rows, windows_30)
        if not res.exact:
            continue
        target = math.log(res.roas_comparison / res.roas_baseline)
        assert sum(c.log_points for c in res.channels.values()) == pytest.approx(target, abs=1e-9), entity_id


def _by_entity(rows):
    out = {}
    for r in rows:
        out.setdefault(r.entity_id, []).append(r)
    return out


def test_synthetic_cpm_spike_is_a_cost_channel_move(perf_rows, windows_30, synthetic_events):
    ev = synthetic_events["cpm_spike"]
    rows = [r for r in perf_rows if r.entity_id in ev.entity_ids and r.targeting_unit == ev.targeting_unit]
    res = decompose_rows(rows, windows_30)
    assert res.exact
    assert res.worst == "cpm" and res.dominant == "cpm"
    assert res.log_points("cpm") == pytest.approx(-math.log(1.6), abs=0.08)
    assert abs(res.log_points("icr1k")) < 0.1 and abs(res.log_points("asp")) < 0.1
    v = layer1_verdict(res)
    assert v.eligible and v.code == "self_inflicted_bid"  # the planted bid rose with CPM
    assert layer1_verdict(res, bid_change_pct=0.0).code == "auction_pressure"
    mv = roas_move(*map(cell_from_rows, split_rows(rows, windows_30)))
    assert mv.side == "cost" and mv.cpm_pct == pytest.approx(60.0, abs=8.0)


# ---------------------------------------------------------------- verdict


BASE = agg(1000.0, 4000.0, 100_000.0, 80.0, bid=10.0)


def test_verdict_branches():
    cpm_up = agg(1500.0, 4000.0, 100_000.0, 80.0, bid=10.0)
    assert layer1_verdict(log_decomposition(BASE, cpm_up)).code == "auction_pressure"
    assert layer1_verdict(log_decomposition(BASE, {**cpm_up, "bid": 15.0})).code == "self_inflicted_bid"
    nobid = {k: v for k, v in cpm_up.items() if k != "bid"}
    assert layer1_verdict(log_decomposition(BASE, nobid)).code == "cpm_rose_bid_unknown"
    icr_down = agg(1000.0, 2000.0, 100_000.0, 40.0, bid=10.0)
    v = layer1_verdict(log_decomposition(BASE, icr_down))
    assert v.code == "conversion_fell" and v.channel == "icr1k" and v.text
    asp_down = agg(1000.0, 2000.0, 100_000.0, 80.0, bid=10.0)
    assert layer1_verdict(log_decomposition(BASE, asp_down)).code == "asp_fell"


def test_verdict_bid_flat_threshold():
    res = log_decomposition(BASE, agg(1500.0, 4000.0, 100_000.0, 80.0))
    assert layer1_verdict(res, bid_change_pct=6.99).code == "auction_pressure"
    assert layer1_verdict(res, bid_change_pct=-6.99).code == "auction_pressure"
    assert layer1_verdict(res, bid_change_pct=7.0).code == "self_inflicted_bid"
    assert layer1_verdict(res, bid_change_pct=-7.0).code == "cpm_rose_bid_cut"     # a cut is not self-inflicted
    assert layer1_verdict(res, bid_change_pct=9.0, cfg=IdentityConfig(verdict_bid_flat_pct=10.0)).code == "auction_pressure"


def test_verdict_gates():
    up = log_decomposition(BASE, agg(1000.0, 5000.0, 100_000.0, 80.0))
    assert layer1_verdict(up).reason == "not_a_drop" and not layer1_verdict(up).eligible
    low = log_decomposition(BASE, agg(500.0, 1000.0, 100_000.0, 80.0))
    assert layer1_verdict(low).reason == "below_min_spend"
    ok = log_decomposition(BASE, agg(500.01, 1000.0, 100_000.0, 80.0))
    assert layer1_verdict(ok).eligible
    # the money floor scales with the currency unit
    assert layer1_verdict(ok, economics=Economics(currency_unit=10.0)).reason == "below_min_spend"
    assert layer1_verdict(low, cfg=IdentityConfig(verdict_min_spend=100.0)).eligible
    dead = log_decomposition(BASE, agg(1000.0, 0.0, 100_000.0, 0.0))
    assert layer1_verdict(dead).reason == "non_positive_channel"


# ---------------------------------------------------------------- two-factor


def test_two_factor_exact_on_random_inputs():
    rng = random.Random(3)
    for _ in range(300):
        vp, vc = rng.uniform(0, 1e6), rng.uniform(0, 1e6)
        rp, rc = rng.uniform(0, 5), rng.uniform(0, 5)
        tf = two_factor(vp, rp, vc, rc)
        assert tf.volume_effect + tf.rate_effect == pytest.approx(vc * rc - vp * rp, abs=1e-6)
        assert tf.total == tf.volume_effect + tf.rate_effect
        assert tf.volume_effect == pytest.approx((vc - vp) * rp)
        assert tf.rate_effect == pytest.approx(vc * (rc - rp))


def test_two_factor_interaction_goes_to_rate():
    # volume +100 at prior rate 0.1 = 10; rate +0.1 at CURRENT volume 200 = 20 (a midpoint split would say 15/15)
    tf = two_factor(100.0, 0.1, 200.0, 0.2)
    assert tf.volume_effect == pytest.approx(10.0)
    assert tf.rate_effect == pytest.approx(20.0)
    assert tf.mechanism == "rate-led"
    assert two_factor(100.0, 0.1, 300.0, 0.1).mechanism == "volume-led"
    assert two_factor(100.0, 0.1, 200.0, 0.15).mechanism == "volume-led"  # tie is not rate-led


def test_unit_decomposition_exact_including_zero_prior():
    tf = unit_decomposition(0.0, 0.0, 5000.0, 25.0)
    assert tf.volume_effect == 0.0 and tf.rate_effect == pytest.approx(25.0)
    assert tf.total == pytest.approx(25.0)
    assert tf.mechanism == "conversion-led"
    tf = unit_decomposition(10_000.0, 50.0, 20_000.0, 100.0)
    assert tf.volume_effect == pytest.approx(50.0) and tf.rate_effect == pytest.approx(0.0, abs=1e-9)
    assert tf.mechanism == "visibility-led"
    rng = random.Random(5)
    for _ in range(200):
        ip, ic = rng.uniform(1, 1e6), rng.uniform(1, 1e6)
        up, uc = rng.uniform(0, 1e3), rng.uniform(0, 1e3)
        assert unit_decomposition(ip, up, ic, uc).total == pytest.approx(uc - up, abs=1e-9)


def test_value_decomposition_exact():
    tf = value_decomposition(10.0, 100.0, 12.0, 90.0)
    assert tf.volume_effect == pytest.approx(200.0)
    assert tf.rate_effect == pytest.approx(-120.0)
    assert tf.total == pytest.approx(12 * 90 - 10 * 100)
    assert tf.mechanism == "quantity-led"


def test_rounded_parts_sum_to_rounded_total():
    total, first, second = rounded_parts(10.0, 4.5 + 0.2)  # 4.7 + 5.3
    assert first + second == total
    total, first, second = rounded_parts(2.0, 0.5)  # independent rounding of 0.5 and 1.5 would not add up
    assert first + second == total == 2.0


# ---------------------------------------------------------------- roas move


def test_roas_move_efficiency_vs_budget_artifact():
    prior = agg(1000.0, 4000.0, 100_000.0, 80.0)
    eff = roas_move(prior, agg(900.0, 2700.0, 90_000.0, 54.0))
    assert eff.driver == "efficiency"  # revenue -32.5% vs spend -10%
    assert eff.roas_prior == pytest.approx(4.0) and eff.roas_current == pytest.approx(3.0)
    assert eff.rpm_current / eff.cpm_current == pytest.approx(eff.roas_current, abs=1e-12)
    assert eff.side == "demand" and eff.cpm_pct == pytest.approx(0.0, abs=1e-9)
    assert not eff.spend_cut_note
    # spend -30%, revenue -20%: spend moved faster than revenue
    art = roas_move(prior, agg(700.0, 3200.0, 100_000.0, 64.0))
    assert art.driver == "budget_artifact"
    assert art.side == "cost" and art.spend_cut_note
    # the rule is a plain comparison of the two % changes: revenue +75% vs spend +100% is an efficiency loss
    assert roas_move(prior, agg(2000.0, 7000.0, 150_000.0, 140.0)).driver == "efficiency"


def test_roas_move_spend_cut_note_threshold():
    prior = agg(1000.0, 4000.0, 100_000.0, 80.0)
    assert roas_move(prior, agg(850.0, 3000.0, 85_000.0, 60.0)).spend_cut_note       # exactly -15%
    assert not roas_move(prior, agg(851.0, 3000.0, 85_000.0, 60.0)).spend_cut_note
    assert roas_move(prior, agg(806.0, 3000.0, 85_000.0, 60.0)).spend_cut_note       # -19.4%
    assert not roas_move(prior, agg(1370.0, 3000.0, 85_000.0, 60.0)).spend_cut_note  # +37%


def test_roas_move_degrades_without_prior():
    mv = roas_move(agg(0, 0, 0, 0), agg(100.0, 300.0, 1000.0, 3.0))
    assert mv.roas_pct is None and mv.driver is None and mv.side is None and mv.rpm_prior is None


# ---------------------------------------------------------------- quartile buckets


def test_quartile_buckets(make_perf_row):
    rows = [make_perf_row(day=i, spend=100.0, impressions=1000, sales=100.0 * (i + 1), units=1 + i) for i in range(9)]
    daily = daily_cells(rows)
    healthy, worst = quartile_buckets(daily)
    # 9 days: top tile has 3 days (ROAS 9, 8, 7), bottom tile 2 days (ROAS 1, 2)
    assert healthy.spend == pytest.approx(300.0) and healthy.sales == pytest.approx(2400.0)
    assert worst.spend == pytest.approx(200.0) and worst.sales == pytest.approx(300.0)
    res = log_decomposition(healthy, worst)
    assert res.raw_dln == pytest.approx(math.log(1.5 / 8.0), abs=1e-9)
    assert quartile_buckets({d: c for d, c in list(daily.items())[:3]}) is None


# ---------------------------------------------------------------- edge-case regressions

def test_exact_flag_is_false_when_a_channel_is_floored():
    tiny = {"spend": 1e-6, "sales": 1e-9, "impressions": 1e9, "units": 1e-3}
    res = log_decomposition(tiny, {"spend": 1.0, "sales": 1.0, "impressions": 1000.0, "units": 1.0})
    assert not res.exact                                   # channels below the log floor are clipped
    ok = log_decomposition(agg(100.0, 300.0, 1000.0, 3.0), agg(120.0, 330.0, 1100.0, 3.0))
    assert ok.exact and ok.raw_dln == pytest.approx(math.log((330 / 120) / (300 / 100)), abs=1e-12)


def test_unit_decomposition_adds_up_when_a_period_has_units_without_impressions():
    from cpm_lineage.identity import unit_decomposition

    for ip, up, ic, uc in ((0, 5, 100, 7), (100, 5, 0, 7), (0, 5, 0, 7), (0, 0, 100, 7), (100, 5, 0, 0),
                           (100, 5, 200, 7)):
        d = unit_decomposition(ip, up, ic, uc)
        assert d.total == pytest.approx(uc - up) and d.volume_effect + d.rate_effect == pytest.approx(uc - up)
    assert unit_decomposition(0, 5, 100, 7).volume_effect == 0.0


def test_cpm_rise_with_a_bid_cut_is_pressure_not_self_inflicted():
    res = log_decomposition(BASE, agg(1500.0, 4000.0, 100_000.0, 80.0))
    cut = layer1_verdict(res, bid_change_pct=-20.0)
    assert cut.code == "cpm_rose_bid_cut" and "cut" in cut.text and "pressure" in cut.text
    assert layer1_verdict(res, bid_change_pct=-7.0).code == "cpm_rose_bid_cut"
    assert layer1_verdict(res, bid_change_pct=20.0).code == "self_inflicted_bid"
