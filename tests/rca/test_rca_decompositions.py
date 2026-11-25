"""Exact decompositions, quadrant, step scan."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from cpm_lineage.rca import (QUADRANT_READS, AdTypeRow, RcaConfig, mix_vs_rate, quadrant, roas_from_identity,
                             roas_identity, rounded_parts, step_scan, two_factor_units, value_decomposition)


def test_d1_units_decomposition_is_exact_on_random_inputs():
    rng = random.Random(11)
    for _ in range(500):
        ip, ic = rng.uniform(0, 5e6), rng.uniform(0, 5e6)
        cp, cc = rng.uniform(0, 0.05), rng.uniform(0, 0.05)
        tf = two_factor_units(ip, cp, ic, cc)
        head = ic * cc - ip * cp
        assert abs(tf.volume_effect + tf.rate_effect - head) <= 1e-9 * max(1.0, abs(head))
        assert tf.mechanism == ("conversion-led" if abs(tf.rate_effect) > abs(tf.volume_effect) else "visibility-led")


def test_d1_exact_with_zero_prior_impressions():
    tf = two_factor_units(0.0, 0.0, 1000.0, 0.01)
    assert tf.volume_effect == 0.0 and tf.rate_effect == pytest.approx(10.0, abs=1e-12)
    assert tf.mechanism == "conversion-led"


def test_d1_interaction_goes_to_the_rate_effect():
    tf = two_factor_units(100.0, 0.10, 200.0, 0.20)
    assert tf.volume_effect == pytest.approx(10.0)      # volume delta at the PRIOR rate
    assert tf.rate_effect == pytest.approx(20.0)        # rate delta at the CURRENT volume


def test_d2_value_decomposition_is_exact():
    rng = random.Random(5)
    for _ in range(300):
        qp, qc, ap, ac = (rng.uniform(0, 1e5) for _ in range(4))
        tf = value_decomposition(qp, ap, qc, ac)
        assert abs(tf.volume_effect + tf.rate_effect - (qc * ac - qp * ap)) <= 1e-9 * max(1.0, qc * ac, qp * ap)


def test_d3_roas_is_revenue_per_impression_over_cpm():
    ident = roas_identity(spend=50_000.0, impressions=2_000_000.0, roas=2.4)
    assert ident.cpm == pytest.approx(25.0)
    assert ident.rpi / ident.cpm == pytest.approx(2.4, abs=1e-12)
    assert roas_identity(0.0, 100.0, 1.0) is None and roas_identity(10.0, 0.0, 1.0) is None


def test_d4_roas_equals_conversion_times_price_over_cpm():
    spend, impressions, units, sales = 40_000.0, 1_600_000.0, 900.0, 126_000.0
    icr, asp, cpm = units / impressions, sales / units, 1000.0 * spend / impressions
    assert roas_from_identity(icr, asp, cpm) == pytest.approx(sales / spend, abs=1e-9)


def test_d5_mix_and_rate_sum_to_the_blended_roas_move():
    rng = random.Random(3)
    for _ in range(200):
        rows = [AdTypeRow(f"type-{i}", rng.uniform(1, 9e4), rng.uniform(1, 9e4), rng.uniform(0, 3e5),
                          rng.uniform(0, 3e5)) for i in range(rng.randint(2, 5))]
        res = mix_vs_rate(rows)
        prior = sum(r.sales_p for r in rows) / sum(r.spend_p for r in rows)
        cur = sum(r.sales_c for r in rows) / sum(r.spend_c for r in rows)
        assert abs(res.mix_total + res.rate_total - (cur - prior)) < 1e-9
        assert res.primary == ("MIX" if abs(res.mix_total) >= abs(res.rate_total) else "RATE")


def test_d5_pure_mix_and_pure_rate_route_correctly():
    mix = mix_vs_rate([AdTypeRow("a", 80.0, 20.0, 320.0, 80.0), AdTypeRow("b", 20.0, 80.0, 20.0, 80.0)])
    assert mix.primary == "MIX" and abs(mix.rate_total) < 1e-12
    rate = mix_vs_rate([AdTypeRow("a", 50.0, 50.0, 100.0, 200.0), AdTypeRow("b", 50.0, 50.0, 50.0, 50.0)])
    assert rate.primary == "RATE" and abs(rate.mix_total) < 1e-12


def test_rounded_parts_add_to_the_rounded_total():
    total, first, second = rounded_parts(-1460.41, 573.6)
    assert first + second == total == -1460


@pytest.mark.parametrize("own,seg,share_pp,expected", [
    (-10.0, -12.0, 0.4, "MARKET_TIDE_OUTPERFORM"),
    (-20.0, -12.0, -1.5, "MARKET_TIDE_PLUS_BRAND"),
    (-4.0, 9.0, -2.0, "SHARE_LOSS_IN_GROWTH"),
    (12.0, 9.0, 0.3, "RIDING_GROWTH"),
    (-3.0, 1.0, -0.8, "SHARE_LOSS_FLAT_MARKET"),
    (1.0, 1.0, 0.0, "STABLE"),
])
def test_quadrant_canonical_cases(own, seg, share_pp, expected):
    key, read = quadrant(own, seg, share_pp)
    assert key == expected and read == QUADRANT_READS[expected]


def test_quadrant_thresholds_fire_exactly_at_the_gate():
    assert quadrant(-5.0, -5.0, 0.0)[0] == "MARKET_TIDE_OUTPERFORM"
    assert quadrant(-5.0, -4.99, 0.0)[0] == "STABLE"
    assert quadrant(5.0, 5.0, 0.0)[0] == "RIDING_GROWTH"
    assert quadrant(5.0, 4.99, 0.0)[0] == "STABLE"
    assert quadrant(0.0, 0.0, -0.10)[0] == "SHARE_LOSS_FLAT_MARKET"
    assert quadrant(0.0, 0.0, -0.0999)[0] == "STABLE"
    assert quadrant(-9.0, -9.0, -0.10)[0] == "MARKET_TIDE_OUTPERFORM"
    assert quadrant(-9.0, -9.0, -0.11)[0] == "MARKET_TIDE_PLUS_BRAND"
    assert quadrant(3.0, 9.0, -0.10)[0] == "SHARE_LOSS_IN_GROWTH"


def test_quadrant_is_total_over_random_inputs():
    rng = random.Random(1)
    seen = set()
    for _ in range(3000):
        key, read = quadrant(rng.uniform(-60, 60), rng.uniform(-40, 40), rng.uniform(-3, 3))
        assert key in QUADRANT_READS and read
        seen.add(key)
    assert seen == set(QUADRANT_READS)


def test_quadrant_respects_overrides():
    cfg = replace(RcaConfig(), segment_move_pct=20.0)
    assert quadrant(-10.0, -12.0, 0.4)[0] == "MARKET_TIDE_OUTPERFORM"
    assert quadrant(-10.0, -12.0, 0.4, cfg)[0] == "STABLE"


def test_step_scan_dates_a_clean_step(series):
    daily = series([100.0] * 12 + [70.0] * 16)
    step = step_scan(daily)
    assert step is not None and step.date == daily[12][0]
    assert step.before == pytest.approx(100.0) and step.after == pytest.approx(70.0)
    assert step.pct == pytest.approx(-30.0)


def test_step_scan_is_quiet_on_small_moves_and_short_series(series):
    assert step_scan(series([100.0] * 14 + [95.0] * 14)) is None           # 5% < 12%
    assert step_scan(series([100.0] * 6 + [50.0] * 7)) is None             # 13 points < 14
    assert step_scan(series([0.0] * 28)) is None                           # nothing to compare against


def test_step_scan_threshold_boundary(series):
    assert step_scan(series([100.0] * 14 + [88.0] * 14)) is not None       # exactly -12%
    assert step_scan(series([100.0] * 14 + [88.5] * 14)) is None
    cfg = replace(RcaConfig(), step_pct=5.0)
    assert step_scan(series([100.0] * 14 + [94.0] * 14), cfg) is not None


def test_step_scan_keeps_both_sides_long_enough(series):
    # the level change sits 2 days from the end: no admissible split isolates it
    step = step_scan(series([100.0] * 26 + [10.0] * 2))
    assert step is None or step.date <= series([0.0] * 28)[23][0]
