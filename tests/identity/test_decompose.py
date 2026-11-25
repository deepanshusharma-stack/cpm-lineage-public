"""Midpoint shift-share, composition, Pareto drag, ASP split, growth decomposition."""

from __future__ import annotations

import random

import pytest

from cpm_lineage.config import Economics
from cpm_lineage.decompose import (
    BrandShareRow,
    DecomposeConfig,
    LaunchRow,
    ShiftShareInput,
    TierRow,
    ad_type_shift_share,
    asp_mix_price,
    entry_exit,
    entry_exit_rows,
    growth_decomposition,
    pareto_drag,
    pareto_drag_rows,
    period_inputs,
    shift_share,
    shift_share_rows,
)
from cpm_lineage.metrics import cell_from_rows
from cpm_lineage.windows import split_rows

TOL = 1e-9


def random_items(rng, n, one_sided_share=0.3):
    items = []
    for i in range(n):
        sp, sc = rng.uniform(1, 1e5), rng.uniform(1, 1e5)
        u = rng.random()
        if u < one_sided_share / 2:
            sp = 0.0
        elif u < one_sided_share:
            sc = 0.0
        items.append(ShiftShareInput(f"k{i}", sp, sp * rng.uniform(0, 6), sc, sc * rng.uniform(0, 6)))
    return items


def blended(items):
    sp = sum(i.weight_prior for i in items)
    sc = sum(i.weight_current for i in items)
    rp = sum(i.value_prior for i in items) / sp if sp else 0.0
    rc = sum(i.value_current for i in items) / sc if sc else 0.0
    return rp, rc


# ---------------------------------------------------------------- shift-share


@pytest.mark.parametrize("one_sided", ["zero", "carry"])
def test_shift_share_exact_on_random_data(one_sided):
    rng = random.Random(2)
    for _ in range(200):
        items = random_items(rng, rng.randint(1, 12))
        res = shift_share(items, one_sided=one_sided)
        rp, rc = blended(items)
        assert res.rate_prior == pytest.approx(rp, abs=TOL)
        assert res.rate_current == pytest.approx(rc, abs=TOL)
        assert res.mix_total + res.rate_total == pytest.approx(rc - rp, abs=TOL)
        assert sum(i.total for i in res.items) == pytest.approx(res.delta, abs=TOL)
        assert abs(res.residual) < TOL
        assert [i.total for i in res.items] == sorted(i.total for i in res.items)


def test_shift_share_formula_by_hand():
    res = shift_share([
        {"key": "a", "spend_prior": 600.0, "sales_prior": 2400.0, "spend_current": 300.0, "sales_current": 1200.0},
        {"key": "b", "spend_prior": 400.0, "sales_prior": 400.0, "spend_current": 700.0, "sales_current": 700.0},
    ])
    a, b = res.item("a"), res.item("b")
    # pure mix: both rates unchanged (4.0 and 1.0), share moved 60/40 -> 30/70
    assert a.mix == pytest.approx((0.3 - 0.6) * 4.0) and a.rate == pytest.approx(0.0)
    assert b.mix == pytest.approx((0.7 - 0.4) * 1.0) and b.rate == pytest.approx(0.0)
    assert res.rate_prior == pytest.approx(2.8) and res.rate_current == pytest.approx(1.9)
    assert res.mix_total == pytest.approx(-0.9) and res.rate_total == pytest.approx(0.0)
    assert res.primary == "MIX"
    assert res.items[0].key == "a"  # biggest drag first
    d = a.as_dict()
    assert d["share_cur"] == 30 and d["share_prev"] == 60 and d["rate_cur"] == 4.0
    assert a.roas_prior == 4.0 and a.spend_current == 300.0


def test_shift_share_pure_rate_and_primary_tie():
    res = shift_share([
        ShiftShareInput("a", 500.0, 1500.0, 500.0, 1000.0),
        ShiftShareInput("b", 500.0, 500.0, 500.0, 500.0),
    ])
    assert res.mix_total == pytest.approx(0.0) and res.rate_total == pytest.approx(-0.5)
    assert res.primary == "RATE"
    flat = shift_share([ShiftShareInput("a", 1.0, 2.0, 1.0, 2.0)])
    assert flat.primary == "MIX"  # |mix| >= |rate| on a tie


def test_shift_share_one_sided_items_and_zero_periods():
    only_cur = shift_share([ShiftShareInput("new", 0.0, 0.0, 100.0, 300.0), ShiftShareInput("old", 100.0, 200.0, 100.0, 200.0)])
    new = only_cur.item("new")
    assert new.share_prior == 0.0 and new.rate_prior == 0.0
    assert new.mix == pytest.approx(0.5 * 3.0 / 2) and new.rate == pytest.approx(0.5 / 2 * 3.0)
    assert abs(only_cur.residual) < TOL
    carried = shift_share([ShiftShareInput("new", 0.0, 0.0, 100.0, 300.0), ShiftShareInput("old", 100.0, 200.0, 100.0, 200.0)],
                          one_sided="carry").item("new")
    assert carried.rate == 0.0 and carried.mix == pytest.approx(0.5 * 3.0)
    # a whole period without spend: its blended rate is 0 and the identity still holds
    no_prior = shift_share([ShiftShareInput("a", 0.0, 0.0, 50.0, 100.0), ShiftShareInput("b", 0.0, 0.0, 50.0, 300.0)])
    assert no_prior.rate_prior == 0.0 and no_prior.rate_current == pytest.approx(4.0)
    assert no_prior.mix_total + no_prior.rate_total == pytest.approx(4.0, abs=TOL)
    empty = shift_share([])
    assert empty.items == () and empty.delta == 0.0
    with pytest.raises(ValueError):
        shift_share([], one_sided="other")


def test_shift_share_orphan_value_and_duplicate_keys():
    res = shift_share([
        ShiftShareInput("a", 100.0, 200.0, 0.0, 50.0),   # current sales with no current spend
        ShiftShareInput("b", 100.0, 100.0, 60.0, 120.0),
        ShiftShareInput("b", 0.0, 0.0, 40.0, 80.0),      # duplicate key is summed
    ])
    assert res.orphan_value_current == pytest.approx(50.0)
    assert res.rate_current == pytest.approx(2.0)
    assert res.item("b").weight_current == pytest.approx(100.0)
    assert abs(res.residual) < TOL


def test_shift_share_rows_matches_account_roas_at_every_grain(perf_rows, windows_30):
    prior, current = (cell_from_rows(x) for x in split_rows(perf_rows, windows_30))
    for key in ("entity_id", "targeting_unit", "geo", "product_id", ("entity_id", "geo")):
        res = shift_share_rows(perf_rows, windows_30, key)
        assert res.rate_current == pytest.approx(current.roas, abs=TOL), key
        assert res.rate_prior == pytest.approx(prior.roas, abs=TOL), key
        assert res.mix_total + res.rate_total == pytest.approx(current.roas - prior.roas, abs=TOL), key


def test_shift_share_rows_other_ratios(perf_rows, windows_30):
    prior, current = (cell_from_rows(x) for x in split_rows(perf_rows, windows_30))
    cpi = shift_share_rows(perf_rows, windows_30, "entity_id", value="spend", weight="impressions")
    assert 1000 * cpi.delta == pytest.approx(current.cpm - prior.cpm, abs=1e-6)
    assert abs(cpi.residual) < TOL


def test_synthetic_cpm_spike_surfaces_as_rate_drag(perf_rows, windows_30, synthetic_events):
    ev = synthetic_events["cpm_spike"]
    rows = [r for r in perf_rows if r.entity_id in ev.entity_ids]
    res = shift_share_rows(rows, windows_30, "targeting_unit")
    worst = res.items[0]
    assert worst.key == ev.targeting_unit
    assert worst.rate < 0 and worst.rate_current < worst.rate_prior
    assert abs(res.residual) < TOL


def test_ad_type_shift_share_canonicalises_and_flags(perf_rows, windows_30, synthetic_profile, entities):
    res = ad_type_shift_share(perf_rows, windows_30, synthetic_profile, entities)
    prior, current = (cell_from_rows(x) for x in split_rows(perf_rows, windows_30))
    assert res.mix_total + res.rate_total == pytest.approx(current.roas - prior.roas, abs=TOL)
    keys = {i.key for i in res.items}
    assert keys <= set(synthetic_profile.ad_types) | {"Unknown"}
    for it in res.items:
        assert it.extras["drillable"] == (it.key in synthetic_profile.keyword_grain_types())
        assert it.extras["no_attrib"] == (it.key in synthetic_profile.no_attribution_types())
    assert any(i.extras["no_attrib"] for i in res.items)
    assert res.primary in ("MIX", "RATE")


def test_ad_type_labels_renamed_mid_period_fold_into_one_bucket(make_perf_row, synthetic_profile, windows_30):
    canon = next(iter(synthetic_profile.keyword_grain_types()))
    d_prior, d_cur = windows_30.prior.start, windows_30.current.start
    rows = [
        make_perf_row(day=d_prior, ad_type=canon.upper(), spend=100.0, sales=300.0),
        make_perf_row(day=d_cur, ad_type=f"  {canon} ", spend=100.0, sales=200.0),
    ]
    res = ad_type_shift_share(rows, windows_30, synthetic_profile)
    assert [i.key for i in res.items] == [canon]
    assert res.mix_total == pytest.approx(0.0) and res.rate_total == pytest.approx(-1.0)
    no_profile = ad_type_shift_share([make_perf_row(day=d_cur, ad_type=None)], windows_30)
    assert no_profile.items[0].key == "Unknown"


def test_period_inputs_ignores_rows_outside_windows(make_perf_row, windows_30):
    from datetime import timedelta

    rows = [
        make_perf_row(day=windows_30.prior.start - timedelta(days=1), spend=999.0),
        make_perf_row(day=windows_30.prior.start, spend=10.0, sales=20.0),
        make_perf_row(day=windows_30.current.end, spend=30.0, sales=40.0),
    ]
    (inp,) = period_inputs(rows, windows_30, "entity_id")
    assert (inp.weight_prior, inp.value_prior, inp.weight_current, inp.value_current) == (10.0, 20.0, 30.0, 40.0)


# ---------------------------------------------------------------- pareto


def test_pareto_drag_formula_and_counts():
    items = [
        {"key": "a", "spend": 1000.0, "sales": 500.0},
        {"key": "b", "spend": 1000.0, "sales": 1500.0},
        {"key": "c", "spend": 1000.0, "sales": 1800.0},
        {"key": "d", "spend": 1000.0, "sales": 4200.0},
    ]
    res = pareto_drag(items)
    assert res.reference_roas == pytest.approx(2.0)
    assert [r.key for r in res.rows] == ["a", "b", "c", "d"]
    assert [r.drag for r in res.rows] == pytest.approx([1500.0, 500.0, 200.0, -2200.0])
    assert sum(r.drag for r in res.rows) == pytest.approx(0.0, abs=1e-6)  # ranking device, nets to zero
    assert res.total_positive_drag == pytest.approx(2200.0)
    cum = [r.cum_pct for r in res.rows]
    assert cum == sorted(cum) and cum[-1] == pytest.approx(100.0)
    assert cum[0] == pytest.approx(100 * 1500 / 2200)
    # a (68%) + b (91%): two entities reach 80%; the strict count stops at a
    assert res.n80 == 2 and res.n80_strict == 1


def test_pareto_n80_when_one_entity_exceeds_the_cut():
    res = pareto_drag([
        {"key": "a", "spend": 1000.0, "sales": 0.0},
        {"key": "b", "spend": 1000.0, "sales": 1900.0},
        {"key": "c", "spend": 1000.0, "sales": 4100.0},
    ])
    assert res.rows[0].cum_pct > 80
    assert res.n80 == 1 and res.n80_strict == 0


def test_pareto_cut_boundary_and_top_n():
    items = [{"key": f"k{i}", "spend": 100.0, "sales": 0.0} for i in range(5)] + [{"key": "win", "spend": 500.0, "sales": 5000.0}]
    res = pareto_drag(items)
    assert [round(r.cum_pct) for r in res.rows[:5]] == [20, 40, 60, 80, 100]
    assert res.n80 == 4 and res.n80_strict == 4  # exactly 80% counts as inside
    assert pareto_drag(items, cfg=DecomposeConfig(pareto_cum_pct=81.0)).n80 == 5
    short = pareto_drag(items, cfg=DecomposeConfig(pareto_top_n=2))
    assert len(short.rows) == 2 and short.n_items == 6 and short.n80 == 4


def test_pareto_excludes_no_attribution_groups_and_zero_spend():
    items = [
        {"key": "reach", "group": "featured", "spend": 1000.0, "sales": 0.0},
        {"key": "kw", "group": "keyword", "spend": 1000.0, "sales": 3000.0, "name": "KW"},
        {"key": "idle", "group": "keyword", "spend": 0.0, "sales": 0.0},
    ]
    res = pareto_drag(items, exclude_groups={"featured"})
    assert [r.key for r in res.rows] == ["kw"]
    assert res.reference_roas == pytest.approx(1.5)  # the reference still includes the excluded type's spend
    assert res.rows[0].extras["name"] == "KW"
    assert res.n80 == 0 and res.total_positive_drag == 0.0
    assert pareto_drag(items, reference_roas=4.0, exclude_groups={"featured"}).rows[0].drag == pytest.approx(1000.0)


def test_pareto_drag_rows_on_synthetic(perf_rows, windows_30, synthetic_profile, entities, synthetic_events):
    res = pareto_drag_rows(perf_rows, windows_30.current, synthetic_profile, entities)
    excluded = synthetic_profile.no_attribution_types()
    assert all(r.group not in excluded for r in res.rows)
    drags = [r.drag for r in res.rows]
    assert drags == sorted(drags, reverse=True)
    assert [r.cum_pct for r in res.rows] == sorted(r.cum_pct for r in res.rows)
    bleeder = synthetic_events["bleeding_keyword"].entity_ids[0]
    top = {r.key for r in res.rows[:3]}
    assert bleeder in top
    assert all("status" in r.extras for r in res.rows)
    assert 1 <= res.n80 <= res.n_items


# ---------------------------------------------------------------- composition


def test_entry_exit_buckets_sum_to_headline_random():
    rng = random.Random(9)
    for _ in range(200):
        items = random_items(rng, rng.randint(2, 12), one_sided_share=0.5)
        res = entry_exit(items)
        rp, rc = blended(items)
        if res is None:
            assert sum(i.weight_prior for i in items) == 0 or sum(i.weight_current for i in items) == 0
            continue
        assert res.cont_rate + res.cont_mix + res.entrants + res.exits == pytest.approx(rc - rp, abs=TOL)
        assert abs(res.residual) < TOL
        assert res.n_continuing + res.n_entrants + res.n_exits + res.n_below_min == len(items)


def test_entry_exit_low_roas_entrant_is_a_drag():
    res = entry_exit([
        ShiftShareInput("steady", 10_000.0, 40_000.0, 10_000.0, 40_000.0),
        ShiftShareInput("launch", 0.0, 0.0, 10_000.0, 10_000.0),
    ])
    assert res.roas_prior == pytest.approx(4.0) and res.roas_current == pytest.approx(2.5)
    assert res.entrants == pytest.approx(0.5 * (1.0 - 4.0))  # measured against the prior blended ROAS
    assert res.entrants < 0 and res.cont_rate == pytest.approx(0.0) and res.exits == 0.0
    assert res.cont_mix == pytest.approx(0.0, abs=TOL)
    assert res.n_entrants == 1 and res.top_entrants[0].key == "launch"
    assert res.top_entrants[0].kind == "entrant"


def test_entry_exit_exit_relief_and_continuing_rate():
    res = entry_exit([
        ShiftShareInput("steady", 10_000.0, 40_000.0, 10_000.0, 30_000.0),
        ShiftShareInput("stopped", 10_000.0, 5_000.0, 0.0, 0.0),
    ])
    # prior blended 2.25; the stopped entity sat below it, so its exit is a relief
    assert res.exits == pytest.approx(0.5 * (2.25 - 0.5)) and res.exits > 0
    assert res.cont_rate == pytest.approx((0.5 + 1.0) / 2 * (3.0 - 4.0))
    assert res.top_movers[0].impact == pytest.approx((1.0 - 0.5) * 3.5 + 0.75 * -1.0)
    assert abs(res.residual) < TOL
    assert res.as_dict()["n_exit"] == 1


def test_entry_exit_min_spend_threshold_and_currency():
    items = [
        ShiftShareInput("big", 50_000.0, 100_000.0, 50_000.0, 100_000.0),
        ShiftShareInput("edge", 0.0, 0.0, 1000.0, 500.0),     # exactly MIN: does not exist yet
        ShiftShareInput("over", 0.0, 0.0, 1000.01, 500.0),
    ]
    res = entry_exit(items)
    assert res.n_entrants == 1 and res.top_entrants[0].key == "over" and res.n_below_min == 1
    assert abs(res.residual) < TOL
    scaled = entry_exit(items, economics=Economics(currency_unit=2.0))
    assert scaled.n_entrants == 0 and scaled.n_below_min == 2
    low = entry_exit(items, cfg=DecomposeConfig(composition_min_spend=10.0))
    assert low.n_entrants == 2


def test_entry_exit_none_without_spend_and_top_n():
    assert entry_exit([ShiftShareInput("a", 0.0, 0.0, 100.0, 100.0)]) is None
    assert entry_exit([]) is None
    many = [ShiftShareInput(f"e{i}", 0.0, 0.0, 2000.0, 1000.0 * i) for i in range(10)]
    many.append(ShiftShareInput("base", 5000.0, 10_000.0, 5000.0, 10_000.0))
    res = entry_exit(many)
    assert res.n_entrants == 10 and len(res.top_entrants) == 6
    impacts = [e.impact for e in res.top_entrants]
    assert impacts == sorted(impacts)


def test_entry_exit_rows_on_synthetic(perf_rows, windows_30):
    res = entry_exit_rows(perf_rows, windows_30)
    prior, current = (cell_from_rows(x) for x in split_rows(perf_rows, windows_30))
    assert res.delta == pytest.approx(current.roas - prior.roas, abs=TOL)
    assert res.cont_rate + res.cont_mix + res.entrants + res.exits == pytest.approx(res.delta, abs=TOL)


# ---------------------------------------------------------------- ASP split


def prod(key, up, pp, uc, pc):
    return {"key": key, "units_prior": up, "sales_prior": up * pp, "units_current": uc, "sales_current": uc * pc}


def test_asp_split_exact_on_random_data():
    rng = random.Random(4)
    for _ in range(200):
        items = []
        for i in range(rng.randint(1, 8)):
            up = 0.0 if rng.random() < 0.2 else rng.uniform(1, 500)
            uc = 0.0 if rng.random() < 0.2 else rng.uniform(1, 500)
            items.append(prod(f"p{i}", up, rng.uniform(10, 900), uc, rng.uniform(10, 900)))
        res = asp_mix_price(items)
        assert res.mix + res.price == pytest.approx(res.delta, abs=TOL)
        if res.reason != "no_units":
            up = sum(i["units_prior"] for i in items)
            uc = sum(i["units_current"] for i in items)
            assert res.asp_prior == pytest.approx(sum(i["sales_prior"] for i in items) / up, abs=1e-6)
            assert res.asp_current == pytest.approx(sum(i["sales_current"] for i in items) / uc, abs=1e-6)
        if res.delta:
            assert res.mix_pct + res.price_pct == pytest.approx(100.0, abs=1e-6)


def test_asp_split_pure_mix_and_pure_price():
    mix = asp_mix_price([prod("cheap", 50, 100.0, 80, 100.0), prod("dear", 50, 300.0, 20, 300.0)])
    assert mix.delta == pytest.approx(-60.0)
    assert mix.mix == pytest.approx(-60.0) and mix.price == pytest.approx(0.0)
    assert mix.mix_pct == pytest.approx(100.0) and mix.emitted
    price = asp_mix_price([prod("cheap", 50, 100.0, 50, 100.0), prod("dear", 50, 300.0, 50, 240.0)])
    assert price.price == pytest.approx(-30.0) and price.mix == pytest.approx(0.0)
    assert price.price_pct == pytest.approx(100.0) and price.emitted
    assert [m.key for m in price.movers] == ["dear"]  # the flat product contributes under 5%


def test_asp_split_one_window_product_has_zero_price_term():
    res = asp_mix_price([prod("old", 100, 100.0, 100, 100.0), prod("new", 0, 0.0, 100, 300.0)])
    new = next(m for m in res.movers if m.key == "new")
    assert new.rate == 0.0 and new.mix == pytest.approx(0.5 * 300.0)
    assert res.mix + res.price == pytest.approx(res.delta, abs=TOL) and res.delta == pytest.approx(100.0)


def test_asp_split_gates():
    small = asp_mix_price([prod("a", 100, 100.0, 100, 109.0)])
    assert not small.emitted and small.reason == "small_move" and small.price == pytest.approx(9.0)
    assert asp_mix_price([prod("a", 100, 100.0, 100, 110.0)]).emitted            # exactly 10%
    assert asp_mix_price([prod("a", 100, 100.0, 100, 109.0)], cfg=DecomposeConfig(asp_min_move_pct=5.0)).emitted
    # the window ASP (ad-attributed) can be the judge of the 10% gate
    assert asp_mix_price([prod("a", 100, 100.0, 100, 109.0)], window_asp_prior=100.0, window_asp_current=120.0).emitted
    assert not asp_mix_price([prod("a", 100, 100.0, 100, 150.0)], window_asp_prior=100.0, window_asp_current=101.0).emitted
    cheap = asp_mix_price([prod("a", 100, 5.0, 100, 6.0)])                      # +20% but exactly one currency unit
    assert not cheap.emitted and cheap.reason == "below_min_delta"
    assert asp_mix_price([prod("a", 100, 5.0, 100, 6.01)]).emitted
    assert asp_mix_price([prod("a", 100, 5.0, 100, 6.0)], economics=Economics(currency_unit=0.01)).emitted
    assert asp_mix_price([prod("a", 100, 100.0, 0, 0.0)]).reason == "no_units"


def test_asp_split_on_synthetic_products(perf_rows, windows_30):
    res = asp_mix_price(period_inputs(perf_rows, windows_30, "product_id", value="sales", weight="units"))
    prior, current = (cell_from_rows(x) for x in split_rows(perf_rows, windows_30))
    assert res.asp_prior == pytest.approx(prior.asp, abs=1e-6)
    assert res.asp_current == pytest.approx(current.asp, abs=1e-6)
    assert res.mix + res.price == pytest.approx(current.asp - prior.asp, abs=TOL)


# ---------------------------------------------------------------- growth


BRANDS = [
    BrandShareRow("own-brand", 20.0, 1.5, is_own=True),
    BrandShareRow("rival-b", 30.0, 2.0),
    BrandShareRow("rival-c", 25.0, -4.0),
    BrandShareRow("rival-d", 10.0, 0.5),
    BrandShareRow("newcomer", 3.0, 2.98),
]
TIERS = [TierRow("value", 400.0, 0.0), TierRow("mid", 400.0, 0.0), TierRow("premium", 400.0, 100.0)]


def test_growth_decomposition_reads():
    res = growth_decomposition(1200.0, 20.0, BRANDS, TIERS,
                               [LaunchRow("l1", 50.0, "ramping"), LaunchRow("l2", 70.0, "fading"),
                                {"name": "l3", "sales_current": 30.0, "trend": "ramping"}])
    assert res.segment_prior == pytest.approx(1000.0) and res.delta == pytest.approx(200.0)
    assert res.own.share_delta_pp == 1.5 and res.own.value == pytest.approx(18.0)
    assert res.loser.brand == "rival-c" and res.loser.value == pytest.approx(-48.0)
    # the entrant has the largest share delta but is never reported as the incumbent gainer
    assert res.gainer.brand == "rival-b" and res.gainer.value == pytest.approx(24.0)
    assert res.entrants_count == 1 and res.entrant_names == ("newcomer",)
    assert res.entrants_value == pytest.approx(36.0)
    assert res.ramping_count == 2 and res.ramping_sales == pytest.approx(80.0)
    # premium doubled: weight 20% -> 33.3%; the flat tiers lose weight
    by = {t.name: t for t in res.mix_shift}
    assert by["premium"].shift_pp == pytest.approx(100 / 3 - 20.0)
    assert by["value"].shift_pp < 0 and by["mid"].shift_pp < 0
    assert sum(t.shift_pp for t in res.mix_shift) == pytest.approx(0.0, abs=TOL)
    assert res.mix_shift[0].name == "premium"
    assert res.as_dict()["entrants"]["n"] == 1


def test_growth_entrant_thresholds():
    def n_entrants(share, delta):
        return growth_decomposition(1000.0, 10.0, [BrandShareRow("x", share, delta)]).entrants_count

    assert n_entrants(0.30, 0.26) == 1      # prior 0.04 < 0.05, current at the 0.3 floor
    assert n_entrants(0.29, 0.26) == 0      # current under the floor
    assert n_entrants(0.40, 0.35) == 0      # prior exactly 0.05 is an incumbent
    assert n_entrants(0.40, 0.351) == 1
    own_new = growth_decomposition(1000.0, 10.0, [BrandShareRow("me", 2.0, 2.0, is_own=True)])
    assert own_new.entrants_count == 0 and own_new.own.value == pytest.approx(20.0)
    cfg = DecomposeConfig(growth_entrant_share_min=1.0)
    assert growth_decomposition(1000.0, 10.0, [BrandShareRow("x", 0.5, 0.5)], cfg=cfg).entrants_count == 0


def test_growth_gainer_loser_need_the_right_sign_and_many_entrants():
    res = growth_decomposition(1000.0, 0.0, [BrandShareRow("a", 40.0, -1.0), BrandShareRow("b", 30.0, -2.0)])
    assert res.gainer is None and res.loser.brand == "b" and res.delta == 0.0
    ents = [BrandShareRow(f"n{i}", 1.0, 1.0) for i in range(5)]
    res = growth_decomposition(1000.0, 5.0, ents)
    assert res.entrants_count == 5 and res.entrant_names == ("n0", "n1", "n2")
    assert res.gainer is None and res.loser is None and res.own is None


def test_growth_mix_shift_gate():
    flat = [TierRow("a", 500.0, 10.0), TierRow("b", 500.0, 10.0)]
    assert growth_decomposition(1000.0, 10.0, tiers=flat).mix_shift is None
    assert growth_decomposition(1000.0, 10.0, tiers=TIERS[:1]).mix_shift is None  # needs two tiers
    # a = 500 now / 490.2 before, b flat: shift about 0.495 pp, just under the 0.5 pp floor
    near = [TierRow("a", 500.0, 2.0), TierRow("b", 500.0, 0.0)]
    assert growth_decomposition(1000.0, 1.0, tiers=near).mix_shift is None
    over = [TierRow("a", 500.0, 2.1), TierRow("b", 500.0, 0.0)]
    got = growth_decomposition(1000.0, 1.0, tiers=over).mix_shift
    assert got is not None and got[0].shift_pp == pytest.approx(-got[1].shift_pp)
    assert growth_decomposition(1000.0, 1.0, tiers=near, cfg=DecomposeConfig(growth_mix_shift_min_pp=0.4)).mix_shift


def test_growth_undefined_prior():
    assert growth_decomposition(1000.0, -100.0) is None
    shrinking = growth_decomposition(500.0, -50.0)
    assert shrinking.segment_prior == pytest.approx(1000.0) and shrinking.delta == pytest.approx(-500.0)
