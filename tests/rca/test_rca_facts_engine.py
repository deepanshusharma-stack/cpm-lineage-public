"""Fact extractors on hand-built records, scope resolution, and the pipeline on the synthetic dataset."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from cpm_lineage.adapters.base import AdTypeCapability, PlatformProfile
from cpm_lineage.config import Economics
from cpm_lineage.rca import (INTENTS, LossRow, RcaConfig, analyse, attribute, build_facts, extract_availability,
                             extract_conversion, extract_funnel, extract_offtake, extract_pricing, extract_rivals,
                             extract_voice, how_they_win, is_verified, own_segments, render_markdown,
                             resolve_scope, run_rca, sticker_points, update_history, verify)
from cpm_lineage.schema import MarketRow, OfftakeRow, PerfRow, Product, ShelfRow

END = date(2026, 3, 31)
CUR = [END - timedelta(days=i) for i in range(7)]            # 7-day windows
PRI = [END - timedelta(days=7 + i) for i in range(7)]
SEG = "snack-bars"


def _offtake():
    rows = []
    for day in CUR + PRI:
        cur = day in CUR
        rows += [
            OfftakeRow(day, "own-brand", True, 900.0 if cur else 1000.0, 9.0 if cur else 10.0, SEG, "sku-1", "geo-north",
                       list_value=1200.0 if cur else 1250.0),
            OfftakeRow(day, "own-brand", True, 300.0 if cur else 500.0, 3.0 if cur else 5.0, SEG, "sku-2", "geo-south"),
            OfftakeRow(day, "own-brand", True, 50.0, 1.0, SEG, "sku-1", "others"),
            OfftakeRow(day, "rival-b", False, 2400.0 if cur else 2000.0, 20.0, SEG, "sku-b1", "geo-north"),
            OfftakeRow(day, "rival-c", False, 1500.0, 15.0, SEG, "sku-c1", "geo-south"),
            OfftakeRow(day, "", False, 10.0, 1.0, SEG, None, "geo-south"),                  # unmapped brand
            OfftakeRow(day, "own-brand", True, 700.0 if cur else 400.0, 7.0, "granola", "sku-g1", "geo-north"),
        ]
    return rows


PRODUCTS = [
    Product("sku-1", "own-brand", True, SEG, "own bar one", first_sale_date=date(2025, 1, 1)),
    Product("sku-2", "own-brand", True, SEG, "own bar two", first_sale_date=date(2025, 1, 1)),
    Product("sku-g1", "own-brand", True, "granola", "own granola", first_sale_date=date(2025, 1, 1)),
    Product("sku-b1", "rival-b", False, SEG, "rival bar", first_sale_date=date(2026, 3, 1)),
    Product("sku-c1", "rival-c", False, SEG, "old rival bar", first_sale_date=date(2024, 6, 1)),
]


def _scope(**kw):
    kw.setdefault("days", 7)
    return resolve_scope(own_brand="own-brand", segment=kw.pop("segment", SEG), products=PRODUCTS,
                         offtake=_offtake(), **kw)


# ---- scope ------------------------------------------------------------------


def test_scope_anchors_to_the_feed_and_takes_every_own_product_in_the_segment():
    sc = _scope()
    assert sc.own_products == ("sku-1", "sku-2") and not sc.is_subset
    assert sc.feed_max == END and sc.current.end == END and sc.current.start == END - timedelta(days=6)
    assert sc.prior.end == sc.current.start - timedelta(days=1) and sc.prior.days == sc.current.days == 7
    assert _scope(days=None).days == RcaConfig().default_days == 14
    assert own_segments("own-brand", _offtake(), PRODUCTS) == ["granola", SEG]


def test_scope_from_a_product_selection_infers_the_segment():
    sc = resolve_scope(own_brand="own-brand", product_ids=["sku-2"], products=PRODUCTS, offtake=_offtake(), days=7)
    assert sc.segment == SEG and sc.own_products == ("sku-2",) and sc.is_subset
    off = extract_offtake(_offtake(), sc)
    assert off.cur_value == pytest.approx(7 * 300.0) and [p.product_id for p in off.product_rows] == ["sku-2"]


def test_scope_failures_raise():
    with pytest.raises(ValueError):
        resolve_scope(own_brand="own-brand", products=PRODUCTS, offtake=_offtake())
    with pytest.raises(ValueError):
        _scope(segment="pet-food")
    with pytest.raises(ValueError):
        _scope(date_from="2026-3-1", date_to="2026-03-07")          # strict date format
    with pytest.raises(ValueError):
        resolve_scope(own_brand="own-brand", segment=SEG, products=PRODUCTS, offtake=[])


def test_custom_range_is_clamped_to_the_feed_and_shifted_a_week():
    sc = _scope(date_from="2026-03-28", date_to="2026-04-05", cmp="wow")
    assert sc.current.end == END and sc.current.start == date(2026, 3, 28)
    assert sc.prior.start == date(2026, 3, 21) and sc.prior.days == sc.current.days
    assert sc.note                                                   # the clamp is stated


def test_overrides_are_isolated_recorded_and_unknown_keys_ignored():
    base = RcaConfig()
    sc = _scope(overrides={"promo_depth_pp": "8", "availability_floor": 0.7, "not_a_gate": 1})
    assert sc.config.promo_depth_pp == 8.0 and sc.config.availability_floor == 0.7
    assert sc.overrides == {"promo_depth_pp": (3.0, 8.0), "availability_floor": (0.85, 0.7)}
    assert sc.unknown_overrides == ("not_a_gate",)
    assert base == RcaConfig() and _scope().config == RcaConfig()
    assert sc.roas_guard == pytest.approx(1.1)
    assert _scope(economics=Economics(breakeven=2.0)).roas_guard == pytest.approx(2.2)


# ---- extractors -------------------------------------------------------------


def test_offtake_shapes_and_sums():
    off = extract_offtake(_offtake(), _scope(), is_aggregate_geo=lambda g: g == "others")
    assert off.cur_value == pytest.approx(7 * 1250.0) and off.pri_value == pytest.approx(7 * 1550.0)
    assert off.cur_units == pytest.approx(7 * 13.0)
    assert off.segment_cur_value == pytest.approx(7 * (1250.0 + 2400.0 + 1500.0 + 10.0))
    assert sum(g.cur_value for g in off.geo_rows) == pytest.approx(off.cur_value, abs=1e-9)
    assert sum(p.cur_value for p in off.product_rows) == pytest.approx(off.cur_value, abs=1e-9)
    assert [g.geo for g in off.geo_rows] == ["geo-north", "geo-south", "others"]        # by own PRIOR value
    north = off.geo_rows[0]
    assert north.own_pct == pytest.approx(-10.0) and north.segment_pct == pytest.approx(10.0)
    assert off.geo_rows[2].is_aggregate and not north.is_aggregate
    assert [p.product_id for p in off.product_rows] == ["sku-1", "sku-2"]
    assert len(off.daily) == 14 and off.daily[0][0] < off.daily[-1][0]
    assert sum(v for _, v in off.daily) == pytest.approx(off.cur_value + off.pri_value)


def test_offtake_without_geo_uses_one_synthetic_geo():
    rows = [replace(r, geo=None) for r in _offtake()]
    off = extract_offtake(rows, _scope())
    assert [g.geo for g in off.geo_rows] == ["all"] and off.geo_rows[0].cur_value == pytest.approx(off.cur_value)


def _market():
    rows = []
    for day in CUR + PRI:
        cur = day in CUR
        rows += [
            MarketRow(day, "protein bar", "own-brand", True, 100.0 if cur else 200.0, 300.0, segment=SEG,
                      unit_role="generic"),
            MarketRow(day, "protein bar", "rival-b", False, 700.0 if cur else 400.0, 100.0, segment=SEG,
                      unit_role="generic"),
            MarketRow(day, "oat bar", "rival-c", False, 500.0 if cur else 250.0, 50.0, segment=SEG, unit_role="generic"),
            MarketRow(day, "ownbrand bar", "own-brand", True, 900.0, 900.0, segment=SEG, unit_role="own_brand"),
            MarketRow(day, "cat food", "own-brand", True, 5000.0, 5000.0, segment="pet-food", unit_role="generic"),
        ]
    return rows


def test_conversion_reproduces_units_exactly():
    conv = extract_conversion(_market(), _offtake(), _scope())
    assert conv.impr_c == pytest.approx(7 * (400.0 + 1800.0)) and conv.impr_p == pytest.approx(7 * (500.0 + 1800.0))
    assert conv.impr_c * conv.conv_c == pytest.approx(7 * 13.0, abs=1e-9)        # impressions x conversion == units
    assert conv.impr_p * conv.conv_p == pytest.approx(7 * 16.0, abs=1e-9)
    assert conv.ad_c == pytest.approx(7 * 1000.0) and conv.org_c == pytest.approx(7 * 1200.0)
    assert conv.asp_c == pytest.approx((1200.0 + 300.0 + 50.0) / 13.0)           # list value, sales as fallback
    assert extract_conversion([], _offtake(), _scope()) is None


def test_voice_battleground_is_generic_keywords_the_brand_shows_on():
    v = extract_voice(_market(), _scope())
    assert v.battleground == ("protein bar",)                # the branded term and the other segment are excluded
    assert (v.own_c, v.own_p) == (pytest.approx(700.0), pytest.approx(1400.0))
    assert (v.pool_c, v.pool_p) == (pytest.approx(5600.0), pytest.approx(4200.0))
    assert v.sov_p == pytest.approx(100 * 1400 / 4200) and v.sov_c == pytest.approx(12.5)
    assert [a.brand for a in v.advertisers] == ["rival-b", "own-brand"] and v.advertisers[1].is_own
    assert [k.keyword for k in v.keywords] == ["protein bar", "oat bar"]          # pockets stay visible
    a = attribute(build_facts(_scope(), offtake=_offtake(), market=_market()))
    assert a.has("AD_PULLBACK")                              # own -50% against a pool at +33%
    assert [p.keyword for p in a.pockets] == ["oat bar"]     # pool doubled, own voice absent


def test_voice_battleground_size_and_role_map():
    rows = []
    for i in range(15):
        rows.append(MarketRow(END, f"kw-{i:02d}", "own-brand", True, 100.0 + i, 0.0, segment=SEG))
        rows.append(MarketRow(END, f"kw-{i:02d}", "rival-b", False, 50.0, 0.0, segment=SEG))
    v = extract_voice(rows, _scope())
    assert len(v.battleground) == 12 and v.battleground[0] == "kw-14" and "kw-02" not in v.battleground
    assert len(v.keywords) == 15
    v2 = extract_voice(rows, _scope(), roles={"kw-14": "competitor"})
    assert "kw-14" not in v2.battleground
    assert extract_voice([], _scope()) is None


def _shelf():
    rows = []
    for day in CUR + PRI:
        cur = day in CUR
        rows += [
            ShelfRow(day, "sku-1", "geo-north", 20.0, 20.0, 80.0, 100.0, True, "own-brand", SEG),
            ShelfRow(day, "sku-2", "geo-south", 10.0 if cur else 18.0, 20.0, 90.0 if cur else 70.0, 100.0, True,
                     "own-brand", SEG),
            ShelfRow(day, "sku-b1", "geo-north", 19.0, 20.0, 60.0, 100.0, False, "rival-b", SEG),
            ShelfRow(day, "sku-c1", "geo-south", 0.0, 0.0, None, None, False, "rival-c", SEG),      # no checks
        ]
    return rows


def test_availability_is_units_weighted():
    av = extract_availability(_shelf(), _scope(), offtake=_offtake(), loss_rows=[
        LossRow(CUR[0], "sku-2", 400.0), LossRow(PRI[0], "sku-2", 100.0), LossRow(CUR[0], "sku-b1", 999.0)])
    assert av.own_c == pytest.approx((1.0 * 9 + 0.5 * 3) / 12)        # weighted by sell-out units, not a row mean
    assert av.own_p == pytest.approx((1.0 * 10 + 0.9 * 5) / 15)
    assert av.segment_c == pytest.approx((1.0 * 9 + 0.5 * 3 + 0.95 * 20) / 32)
    assert av.geo_flags == (("geo-south", pytest.approx(0.5)),)
    assert [r.product_id for r in av.product_rows] == ["sku-1", "sku-2"]          # hero first
    assert av.product_rows[1].availability_c == pytest.approx(0.5)
    assert (av.loss_c, av.loss_p) == (400.0, 100.0) and av.product_rows[1].loss_c == 400.0
    a = attribute(build_facts(_scope(), offtake=_offtake(), shelf=_shelf()))
    assert a.has("AVAILABILITY_DRAG") and a.passenger_ids == ("sku-2",) and a.has("PASSENGERS")
    assert extract_availability(_shelf(), _scope(), offtake=_offtake()).loss_c is None          # never invented


def test_availability_weight_fallbacks():
    rows = [ShelfRow(END, "sku-1", "geo-north", 10.0, 10.0, is_own=True, segment=SEG, weight=3.0),
            ShelfRow(END, "sku-2", "geo-north", 0.0, 10.0, is_own=True, segment=SEG, weight=1.0)]
    assert extract_availability(rows, _scope()).own_c == pytest.approx(0.75)                    # row weights
    with_units = [replace(rows[0], units=1.0), replace(rows[1], units=1.0)]
    assert extract_availability(with_units, _scope()).own_c == pytest.approx(0.5)               # row units win
    assert extract_availability([], _scope()) is None


def test_pricing_depth_and_gate():
    p = extract_pricing(_shelf(), _scope(), offtake=_offtake())
    assert p.depth_c == pytest.approx(100 * (1 - (80 * 9 + 90 * 3) / (100 * 12)))      # 17.5
    assert p.depth_p == pytest.approx(100 * (1 - (80 * 10 + 70 * 5) / (100 * 15)))     # 23.33
    assert p.segment_depth_c == pytest.approx(100 * (1 - (80 * 9 + 90 * 3 + 60 * 20) / (100 * 32)))
    assert p.sticker_mix == {}
    assert attribute(build_facts(_scope(), offtake=_offtake(), shelf=_shelf())).has("PROMO_PULLBACK")


def test_sticker_points_never_average():
    obs = [300.0] * 60 + [340.0] * 30 + [345.0] * 6 + [500.0] * 4
    pts = sticker_points(obs, mode_share=0.10, cluster_radius=15.0)
    assert pts == ((300.0, 0.6), (340.0, 0.3))               # 345 is under the share gate; 500 is too rare
    assert sticker_points([300.0] * 50 + [310.0] * 50, mode_share=0.10, cluster_radius=15.0) == ()   # one cluster
    assert sticker_points([300.0] * 95 + [400.0] * 5, mode_share=0.10, cluster_radius=15.0) == ()
    assert sticker_points([300.0] * 90 + [400.0] * 10, mode_share=0.10, cluster_radius=15.0) == \
        ((300.0, 0.9), (400.0, 0.1))
    four = [100.0] * 25 + [200.0] * 25 + [300.0] * 25 + [400.0] * 25
    assert len(sticker_points(four, mode_share=0.10, cluster_radius=15.0, max_points=3)) == 3
    assert sticker_points([], mode_share=0.1, cluster_radius=15.0) == ()
    # the clustering radius is money: it scales with the account's currency unit
    shelf = [ShelfRow(END, "sku-1", "geo-north", 1.0, 1.0, 2.0, lp, True, "own-brand", SEG)
             for lp in [3.0] * 6 + [3.4] * 4]
    cents = extract_pricing(shelf, _scope(economics=Economics(currency_unit=0.01)))
    assert cents.sticker_mix == {"sku-1": ((3.0, 0.6), (pytest.approx(3.4), 0.4))}
    assert attribute(build_facts(_scope(economics=Economics(currency_unit=0.01)), offtake=_offtake(),
                                 shelf=shelf)).has("STICKER_MIX")


def test_how_they_win_first_match_wins():
    assert how_they_win(discount_pct=30.0, availability=0.95) == "Deep price (30% off) + high availability"
    assert how_they_win(discount_pct=35.0, availability=0.80) == "Deep price (35% off)"
    assert how_they_win(discount_pct=5.0, availability=0.92, ad_sov_pct=15.0).startswith("Distribution + heavy media")
    assert how_they_win(discount_pct=5.0, availability=0.92, ad_sov_pct=14.9) == "Distribution, at almost no discount"
    assert how_they_win(discount_pct=20.0, availability=0.80, ad_sov_pct=15.0).startswith("Media-led")
    assert how_they_win(discount_pct=20.0, availability=0.80, n_products=7) == "Range: 7 products on shelf"
    assert how_they_win(discount_pct=None, availability=None).startswith("Balanced")


def test_rivals_shares_launches_and_other_lines():
    r = extract_rivals(_offtake(), _scope(), products=PRODUCTS, shelf=_shelf(), market=_market())
    by = {m.brand: m for m in r.movers}
    seg_c, seg_p = 7 * 5160.0, 7 * 5060.0
    assert by["own-brand"].share_c == pytest.approx(100 * 7 * 1250 / seg_c) and by["own-brand"].is_own
    assert by["rival-b"].d_pp == pytest.approx(100 * (7 * 2400 / seg_c - 7 * 2000 / seg_p))
    assert sum(m.share_c for m in r.movers) == pytest.approx(r.join_coverage)        # unmapped rows stay in the base
    assert r.join_coverage == pytest.approx(100 * 5150 / 5160)
    assert [abs(m.d_pp) for m in r.movers] == sorted((abs(m.d_pp) for m in r.movers), reverse=True)
    assert by["rival-b"].how == "Deep price (40% off) + high availability"
    assert by["rival-b"].ad_sov_pct == pytest.approx(100 * 700 / 1300) and by["rival-b"].n_products == 1
    assert [(l.brand, l.title) for l in r.launches] == [("rival-b", "rival bar")]    # the old product is not a launch
    assert [(l.segment, l.cur, l.pri) for l in r.own_other_lines] == [("granola", 4900.0, 2800.0)]
    facts = build_facts(_scope(), offtake=_offtake(), products=PRODUCTS, shelf=_shelf(), market=_market())
    a = attribute(facts)
    assert a.has("COMPETITOR_ABSORPTION") and a.has("NEW_ENTRANT") and a.has("MIX_SHIFT")
    assert is_verified(verify(facts, a))                     # 99.8% of segment value maps to a brand

def test_join_coverage_gate_on_extracted_facts():
    facts = build_facts(_scope(), offtake=_offtake(), products=PRODUCTS)
    assert facts.rivals.join_coverage == pytest.approx(99.806, abs=1e-3)
    assert is_verified(verify(facts))
    leaky = _offtake() + [OfftakeRow(d, "", False, 500.0, 1.0, SEG) for d in CUR]
    result = analyse(build_facts(_scope(), offtake=leaky, products=PRODUCTS))
    assert result.status == "refused" and "brand-join coverage" in result.message


def _perf(end=END, clicks=None):
    rows = []
    for i in range(14):
        day = end - timedelta(days=i)
        cur = i < 7
        rows += [
            PerfRow(day, "cmp-1", 80.0 if cur else 100.0, 4000.0, 240.0 if cur else 300.0, 2.0, product_id="sku-1",
                    ad_type="kw", segment=SEG, clicks=clicks),
            PerfRow(day, "cmp-2", 20.0, 2000.0, 0.0, 0.0, product_id="sku-2", ad_type="reach", segment=SEG,
                    clicks=clicks),
            PerfRow(day, "cmp-3", 999.0, 9999.0, 999.0, 9.0, product_id="sku-g1", ad_type="kw", segment="granola"),
        ]
    return rows


PROFILE = PlatformProfile(ad_types={
    "kw": AdTypeCapability("kw"),
    "reach": AdTypeCapability("reach", capability_class="reach", sales_attributed=False, judged_on="reach"),
})


def test_funnel_is_product_exact_and_ad_type_aware():
    f = extract_funnel(_perf(), _scope(), profile=PROFILE)
    assert not f.stale and f.feed_max == END
    assert (f.spend_c, f.spend_p) == (pytest.approx(700.0), pytest.approx(840.0))
    assert f.roas_c == pytest.approx(1680.0 / 700.0) and f.mid_rate_c is None           # no clicks on this platform
    assert [(a.ad_type, a.sales_tracked) for a in f.ad_types] == [("kw", True), ("reach", False)]
    assert f.ad_types[1].roas_c is None and f.ad_types[0].roas_c == pytest.approx(3.0)
    assert f.product_spend == {"sku-1": pytest.approx(560.0), "sku-2": pytest.approx(140.0)}
    a = attribute(build_facts(_scope(), offtake=_offtake(), perf=_perf(), profile=PROFILE))
    assert a.cause("AD_SPEND_CUT").data["spend_pct"] == pytest.approx(-100.0 * 140 / 840)
    clicks = extract_funnel(_perf(clicks=40.0), _scope())
    assert clicks.mid_rate_c == pytest.approx(100.0 * 80 / 6000)


def test_stale_or_missing_ads_feed_is_marked_not_reported():
    stale = extract_funnel(_perf(end=END - timedelta(days=7)), _scope())
    assert stale.stale and stale.feed_max == END - timedelta(days=7) and stale.spend_c == 0.0
    assert extract_funnel([], _scope()).stale
    facts = build_facts(_scope(), offtake=_offtake(), perf=_perf(end=END - timedelta(days=7)))
    result = analyse(facts, "budget_mix")
    assert result.ok and any(c.name == "ads feed freshness" and not c.ok for c in result.checks)
    assert "cannot be judged honestly" in result.answer.text


# ---- pipeline ---------------------------------------------------------------


def _dataset(**kw):
    base = dict(offtake=_offtake(), market=_market(), shelf=_shelf(), perf=_perf(), products=PRODUCTS, profile=PROFILE)
    base.update(kw)
    return SimpleNamespace(**base)


def test_run_rca_scope_fallbacks():
    need = run_rca(_dataset(), own_brand="own-brand", question="why did sales fall")
    assert need.status == "needs_segment" and "granola" in need.message and SEG in need.message
    named = run_rca(_dataset(), own_brand="own-brand", question="why did sales fall on snack-bars this week")
    assert named.ok and named.facts.scope.segment == SEG and named.facts.scope.days == 7 and named.intent == "why"
    assert named.route["via"] == "rules"
    explicit = run_rca(_dataset(), own_brand="own-brand", question="snack-bars availability this week",
                       segment="granola", days=7, intent="geo")
    assert explicit.facts.scope.segment == "granola" and explicit.intent == "geo"          # explicit beats the router
    warn = run_rca(_dataset(), own_brand="own-brand")
    assert warn.status == "warning" and "segment" in warn.message
    single = SimpleNamespace(offtake=[r for r in _offtake() if r.segment == SEG], products=PRODUCTS[:2])
    assert run_rca(single, own_brand="own-brand", days=7).ok                                 # one own segment: used
    assert render_markdown(warn).startswith("> ") and render_markdown(need).startswith("> Which segment?")


def test_run_rca_routes_a_question_to_its_playbook():
    r = run_rca(_dataset(), own_brand="own-brand", segment=SEG, days=7, question="is stock costing us sales?")
    assert r.intent == "availability" and r.answer.verdict == "DRAG"
    assert [s.key for s in r.sections] == list(INTENTS["availability"].sections)
    assert r.levers.primary and all(l.axis == "availability" for l in r.levers.primary) and r.levers.also_open
    with pytest.raises(KeyError):
        run_rca(_dataset(), own_brand="own-brand", segment=SEG, days=7, intent="nonsense")


def test_synthetic_dataset_end_to_end(synthetic_dataset):
    history = []
    for days in (14, 21, 30):
        result = run_rca(synthetic_dataset, own_brand="own-brand", days=days, history=history)
        assert result.ok, result.message
        f, a = result.facts, result.attribution
        assert sum(g.cur_value for g in f.offtake.geo_rows) == pytest.approx(f.offtake.cur_value, abs=1e-6)
        assert sum(p.cur_value for p in f.offtake.product_rows) == pytest.approx(f.offtake.cur_value, abs=1e-6)
        assert a.mech.impr_effect + a.mech.conv_effect == pytest.approx(
            f.conversion.impr_c * f.conversion.conv_c - f.conversion.impr_p * f.conversion.conv_p, abs=1e-6)
        assert f.conversion.impr_c * f.conversion.conv_c == pytest.approx(f.offtake.cur_units, rel=1e-9)
        assert all(c.ok for c in result.checks)
        assert any(g.is_aggregate for g in f.offtake.geo_rows)
        assert all(g != "others" for g, _ in a.geo_avoid) and all(g != "others" for g, _, _ in a.geo_spend)
        assert {t.ad_type: t.sales_tracked for t in f.funnel.ad_types}["featured_listing"] is False
        assert render_markdown(result).count("| pass |") == len(result.checks)
        history = update_history(history, result.snapshot)
    assert len(history) == 1                                 # one snapshot per feed date
    for intent in INTENTS:
        r = run_rca(synthetic_dataset, own_brand="own-brand", intent=intent)
        assert r.ok and r.answer.lines and r.sections


def test_synthetic_availability_drop_is_found(synthetic_dataset, synthetic_events):
    event = synthetic_events["availability_drop"]
    result = run_rca(synthetic_dataset, own_brand="own-brand", product_ids=[event.product_id], days=21)
    assert result.ok and result.facts.scope.is_subset
    a = result.attribution
    assert a.has("AVAILABILITY_DRAG") and event.geo in a.cause("AVAILABILITY_DRAG").data["geos"]
    assert dict(result.facts.availability.geo_flags)[event.geo] == pytest.approx(event.magnitude, abs=0.08)
    assert a.lever("supply_triage").axis == "availability"
    quiet = run_rca(synthetic_dataset, own_brand="own-brand", product_ids=["sku-a1"], days=21)
    assert not quiet.attribution.has("AVAILABILITY_DRAG")


def test_synthetic_rival_escalation_is_read_on_the_voice_axis(synthetic_dataset, synthetic_events):
    event = synthetic_events["cpm_spike"]
    result = run_rca(synthetic_dataset, own_brand="own-brand",
                     date_from=event.start.isoformat(), date_to=event.end.isoformat(), cmp="wow")
    assert result.ok and result.facts.scope.days == 30
    a = result.attribution
    assert a.pool_voice_pct > a.own_voice_pct                # the pool outgrew the brand
    v = result.facts.voice
    assert v.sov_p - v.sov_c >= result.facts.config.sov_drop_pp
    assert a.has("AD_PULLBACK") or a.has("OUT_ESCALATED")
    gap = a.pool_voice_pct - a.own_voice_pct
    assert a.has("AD_PULLBACK") is (a.own_voice_pct < 0 and gap >= result.facts.config.pullback_gap_pp)
    rival = next(x for x in result.facts.voice.advertisers if x.brand == event.details["rival"])
    assert rival.cur > 1.2 * rival.pri                       # the planted rival escalation, diluted across keywords
