"""Ordered attribution: every gate fires and stays silent where it should."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cpm_lineage.rca import (AXES, CAUSE_TITLES, RULED_OUT_HYPOTHESES, AdvertiserRow, AvailabilityFacts,
                             ConversionFacts, KeywordPoolRow, LaunchRow, LineRow, PricingFacts,
                             ProductAvailabilityRow, RcaConfig, attribute, enrich)
from datetime import date


def test_growth_market_fixture(b):
    a = attribute(b.growth_market())
    assert a.quadrant == "SHARE_LOSS_IN_GROWTH"
    assert a.own_pct == pytest.approx(-6.0) and a.segment_pct == pytest.approx(12.0)
    for key in ("PROMO_PULLBACK", "STICKER_MIX", "OUT_ESCALATED", "AVAILABILITY_DRAG", "COMPETITOR_ABSORPTION"):
        assert a.has(key), key
    assert not a.has("AD_PULLBACK")                 # own +54% against a pool at +94% is dilution
    assert not a.has("AD_SPEND_CUT")                # spend rose 37%
    assert a.mech.read == "conversion-led"
    assert a.mech.impr_effect + a.mech.conv_effect == pytest.approx(a.mech.total, abs=1e-9)
    # one story, told once: the share fall is folded into the absorption cause
    assert not a.has("SHARE_CEDED")
    assert "Own share fell" in a.cause("COMPETITOR_ABSORPTION").evidence
    assert a.cause("COMPETITOR_ABSORPTION").data["brand"] == "rival-b"


def test_cause_order_is_evaluation_order(b):
    a = attribute(b.growth_market())
    assert a.cause_keys == ("PROMO_PULLBACK", "STICKER_MIX", "OUT_ESCALATED", "AVAILABILITY_DRAG", "COMPETITOR_ABSORPTION")


def test_tide_fixture(b):
    a = attribute(b.tide_market())
    assert a.quadrant == "MARKET_TIDE_PLUS_BRAND"
    assert a.has("AD_PULLBACK") and not a.has("OUT_ESCALATED")      # own -25% against a pool at -10%
    assert a.is_ruled_out("Price / promo change")
    assert a.is_ruled_out("Availability shock")
    assert a.lever("geo_rebalance") is not None
    assert a.mech.read == "visibility-led"
    spend = a.cause("AD_SPEND_CUT")
    assert spend is not None and spend.data["spend_pct"] == pytest.approx(-19.4)
    assert a.lever("restore_budget") is not None and a.lever("voice_floor") is None
    assert a.cause_keys[0] == "SHARE_CEDED"         # no absorbing rival: inserted first


def test_attribute_is_pure_and_deterministic(b):
    f = b.growth_market()
    before = repr(f)
    first, second = attribute(f), attribute(f)
    assert first == second
    assert repr(f) == before                         # facts untouched (mechanism is returned, not written back)
    assert f.scope.config == RcaConfig()


def test_overrides_change_the_verdict_and_leave_defaults_alone(b):
    f = b.growth_market()
    strict = b.scope(config=replace(RcaConfig(), promo_depth_pp=8.0, availability_floor=0.70,
                                    availability_drop=0.20))
    g = replace(f, scope=strict, availability=replace(f.availability, geo_flags=()))
    a = attribute(g)
    assert not a.has("PROMO_PULLBACK") and a.is_ruled_out("Price / promo change")
    assert not a.has("AVAILABILITY_DRAG") and a.is_ruled_out("Availability shock")
    assert attribute(f).has("PROMO_PULLBACK")
    assert RcaConfig().promo_depth_pp == 3.0 and RcaConfig().availability_floor == 0.85


@pytest.mark.parametrize("depth_p,depth_c,fires,ruled", [
    (20.0, 17.0, True, False),       # exactly -3.0pp fires
    (20.0, 17.1, False, True),       # -2.9pp is ruled out
    (20.0, 22.9, False, True),
    (20.0, 23.0, False, False),      # a deepening at the gate is neither a cause nor ruled out
    (20.0, 30.0, False, False),
])
def test_promo_gate_boundaries(b, depth_p, depth_c, fires, ruled):
    f = b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), pricing=PricingFacts(depth_c=depth_c, depth_p=depth_p))
    a = attribute(f)
    assert a.has("PROMO_PULLBACK") is fires
    assert a.is_ruled_out("Price / promo change") is ruled
    assert ("discount depth stable" in a.held) is ruled


def test_missing_facts_are_silent_not_ruled_out(b):
    a = attribute(b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0)))
    assert a.causes == () and a.levers == () and a.mech is None
    assert [r.hypothesis for r in a.ruled_out] == ["Brand losing share"]


@pytest.mark.parametrize("own_c,pool_c,expected", [
    (99_000.0, 500_000.0, None),              # SOV 20.0 -> 19.8: inside the 1pp gate
    (95_000.0, 500_000.0, "OUT_ESCALATED"),   # SOV drop exactly 1.0pp; own -5% trails a flat pool by 5pp < 10pp
    (80_000.0, 440_000.0, "OUT_ESCALATED"),   # own -20%, pool -12%: trails by 8pp, still dilution
    (80_000.0, 450_000.0, "AD_PULLBACK"),     # own -20%, pool -10%: trails by exactly 10pp
    (110_000.0, 700_000.0, "OUT_ESCALATED"),  # own volume ROSE: can only be dilution
])
def test_voice_discriminator_absolute_versus_pool(b, own_c, pool_c, expected):
    f = b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), voice=b.voice(100_000.0, own_c, 500_000.0, pool_c))
    a = attribute(f)
    fired = next((k for k in ("AD_PULLBACK", "OUT_ESCALATED") if a.has(k)), None)
    assert fired == expected
    assert a.is_ruled_out("Ad-visibility loss") is (expected is None)


@pytest.mark.parametrize("own_p,own_c,flags,fires", [
    (0.90, 0.849, (), True),                   # under the floor
    (0.90, 0.85, (), True),                    # at the floor, but fell 5pp
    (0.95, 0.92, (), True),                    # fell exactly 3pp
    (0.95, 0.93, (), False),
    (0.95, 0.94, (("geo-east", 0.6),), True),  # healthy overall, one geo breached
    (None, 0.90, (), False),                   # no prior: delta is 0
])
def test_availability_gate(b, own_p, own_c, flags, fires):
    f = b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0),
                availability=AvailabilityFacts(own_c=own_c, own_p=own_p, geo_flags=flags))
    a = attribute(f)
    assert a.has("AVAILABILITY_DRAG") is fires
    assert a.is_ruled_out("Availability shock") is (not fires)
    # "healthy" is about the level and the drop only; a single geo breach fires the gate without un-holding it
    assert ("availability healthy" in a.held) is (not fires or bool(flags))


def test_spend_cut_boundary_and_voice_held_variant(b):
    base = b.offtake(100.0, 100.0, 1000.0, 1000.0)
    assert attribute(b.facts(base, funnel=b.funnel(100.0, 85.0))).has("AD_SPEND_CUT")
    assert not attribute(b.facts(base, funnel=b.funnel(100.0, 85.1))).has("AD_SPEND_CUT")
    assert not attribute(b.facts(base, funnel=b.funnel(100.0, 137.0))).has("AD_SPEND_CUT")
    held = attribute(b.facts(base, funnel=b.funnel(100.0, 80.0), voice=b.voice(100.0, 104.0, 500.0, 510.0)))
    assert held.cause("AD_SPEND_CUT").data["voice_held"] is True
    assert held.lever("voice_floor") is not None and held.lever("restore_budget") is None
    unknown = attribute(b.facts(base, funnel=b.funnel(100.0, 80.0)))
    assert unknown.lever("restore_budget") is not None


def test_stale_funnel_silences_every_funnel_gate(b):
    from cpm_lineage.rca import FunnelFacts
    a = attribute(b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), funnel=FunnelFacts(stale=True)))
    assert not a.has("AD_SPEND_CUT") and a.lever("efficiency_guard") is None


def test_efficiency_guard_scales_with_breakeven(b):
    from cpm_lineage.config import Economics
    base = b.offtake(100.0, 100.0, 1000.0, 1000.0)
    assert attribute(b.facts(base, funnel=b.funnel(100.0, 100.0, roas_c=1.09))).lever("efficiency_guard")
    assert attribute(b.facts(base, funnel=b.funnel(100.0, 100.0, roas_c=1.10))).lever("efficiency_guard") is None
    dear = replace(b.scope(), economics=Economics(breakeven=2.0))
    assert attribute(b.facts(base, dear, funnel=b.funnel(100.0, 100.0, roas_c=2.1))).lever("efficiency_guard")


def test_outperformance_inside_a_collapsing_geo_is_avoid_never_spend(b):
    off = b.offtake(1000.0, 800.0, 5000.0, 4000.0, geos=[
        b.geo("geo-north", 600.0, 540.0, -30.0),      # own -10% beats a segment at -30%: still avoid
        b.geo("geo-south", 400.0, 260.0, -8.0)])
    a = attribute(b.facts(off))
    assert [g for g, _ in a.geo_avoid] == ["geo-north"]
    assert all(g != "geo-north" for g, _, _ in a.geo_spend)
    assert a.quadrant.startswith("MARKET_TIDE") and a.lever("geo_rebalance") is not None


def test_geo_lists_are_returned_but_the_lever_needs_a_market_move(b):
    off = b.offtake(1000.0, 1000.0, 5000.0, 5000.0, geos=[
        b.geo("geo-north", 600.0, 640.0, 1.0), b.geo("geo-south", 400.0, 360.0, -25.0),
        b.geo("others", 0.0, 0.0, -50.0, aggregate=True)])
    a = attribute(b.facts(off))
    assert [g for g, _, _ in a.geo_spend] == ["geo-north"]
    assert [g for g, _ in a.geo_avoid] == ["geo-south"]            # the aggregate bucket is never named
    assert a.quadrant == "STABLE" and a.lever("geo_rebalance") is None


def _products(*rows):
    return AvailabilityFacts(own_c=0.92, own_p=0.92, product_rows=tuple(rows))


def test_passengers_flagged_while_the_hero_is_healthy(b):
    av = _products(ProductAvailabilityRow("sku-1", 0.95, 900.0, 880.0),
                   ProductAvailabilityRow("sku-2", 0.55, 60.0, 90.0),      # broken availability
                   ProductAvailabilityRow("sku-3", None, 0.0, 0.0),        # dead pack
                   ProductAvailabilityRow("sku-4", 0.90, 40.0, 50.0))      # fine
    a = attribute(b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), availability=av))
    assert a.passenger_ids == ("sku-2", "sku-3")
    assert a.cause("PASSENGERS").data["hero"] == "sku-1"
    assert "85%" in a.lever("passengers").checkpoint


def test_passenger_gap_boundary_and_unhealthy_hero(b):
    at_low = _products(ProductAvailabilityRow("sku-1", 0.95, 900.0, 880.0),
                       ProductAvailabilityRow("sku-2", 0.75, 60.0, 90.0))   # exactly floor - gap: not under it
    assert attribute(b.facts(b.offtake(1.0, 1.0, 9.0, 9.0), availability=at_low)).passenger_ids == ()
    sick = _products(ProductAvailabilityRow("sku-1", 0.80, 900.0, 880.0),
                     ProductAvailabilityRow("sku-2", 0.50, 60.0, 90.0))
    a = attribute(b.facts(b.offtake(1.0, 1.0, 9.0, 9.0), availability=sick))
    assert a.passenger_ids == ("sku-2",) and not a.has("PASSENGERS")        # ids returned, cause withheld


def test_growing_product_under_the_floor_is_unblocked_never_a_passenger(b):
    av = _products(ProductAvailabilityRow("sku-1", 0.95, 900.0, 880.0),
                   ProductAvailabilityRow("sku-2", 0.60, 500.0, 100.0),
                   ProductAvailabilityRow("sku-3", 0.60, 130.0, 100.0))
    a = attribute(b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), availability=av))
    assert a.passenger_ids == () and not a.has("PASSENGERS")
    assert "x5.0" in a.lever("unblock_scaling:sku-2").why            # +400% is shown as a multiple
    assert "+30%" in a.lever("unblock_scaling:sku-3").why
    assert a.lever("unblock_scaling:sku-2").axis == "availability"


def _kw_voice(b, rows, **kw):
    return b.voice(100.0, 100.0, 1000.0, 1000.0, keywords=tuple(KeywordPoolRow(*r) for r in rows), **kw)


def test_pockets_are_growing_pools_with_no_own_voice(b):
    v = _kw_voice(b, [("oat bar", 50_000.0, 30_000.0, 500.0, 300.0),        # +67%, own SOV 1.0%: pocket
                      ("protein bar", 90_000.0, 60_000.0, 20_000.0, 9_000.0),   # worked: own SOV 22%
                      ("energy bar", 40_000.0, 39_000.0, 100.0, 100.0),     # pool flat
                      ("tiny term", 4_000.0, 1_000.0, 0.0, 0.0),            # under 3% of the listed pool
                      ("new term", 30_000.0, 0.0, 0.0, 0.0)])               # no prior pool
    a = attribute(b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), voice=v))
    assert [p.keyword for p in a.pockets] == ["oat bar"]
    assert a.pockets[0].pool_growth_pct == pytest.approx(66.6667, rel=1e-4)
    assert a.lever("pockets").axis == "sov" and "unscreened" in a.lever("pockets").why


def test_pocket_thresholds(b):
    at_gate = _kw_voice(b, [("oat bar", 60_000.0, 50_000.0, 1_199.0, 0.0)])       # +20%, own SOV 1.998%
    assert len(attribute(b.facts(b.offtake(1.0, 1.0, 9.0, 9.0), voice=at_gate)).pockets) == 1
    worked = _kw_voice(b, [("oat bar", 60_000.0, 50_000.0, 1_200.0, 0.0)])        # own SOV exactly 2.0%
    assert attribute(b.facts(b.offtake(1.0, 1.0, 9.0, 9.0), voice=worked)).pockets == ()
    slow = _kw_voice(b, [("oat bar", 59_900.0, 50_000.0, 0.0, 0.0)])              # +19.8%
    assert attribute(b.facts(b.offtake(1.0, 1.0, 9.0, 9.0), voice=slow)).pockets == ()


def test_relevance_screen_filters_suggestions_but_not_the_sums(b):
    v = _kw_voice(b, [("oat bar", 50_000.0, 30_000.0, 500.0, 300.0),
                      ("pet treats", 45_000.0, 30_000.0, 0.0, 0.0)])
    f = b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), voice=v)
    assert [p.keyword for p in attribute(f).pockets] == ["oat bar", "pet treats"]
    screened = enrich(f, relevance={"pet treats": False, "oat bar": True})
    a = attribute(screened)
    assert [p.keyword for p in a.pockets] == ["oat bar"]
    assert "unscreened" not in a.lever("pockets").why
    assert (screened.voice.own_c, screened.voice.pool_c, screened.voice.keywords) == (v.own_c, v.pool_c, v.keywords)


def test_rival_vacuum(b):
    adv = (AdvertiserRow("own-brand", 10.0, 100.0, is_own=True),      # the own brand never vacates to itself
           AdvertiserRow("rival-small", 0.0, 40.0),                   # 4% prior SOV: too small
           AdvertiserRow("rival-b", 61.0, 100.0),                     # fell to 61%: not vacated
           AdvertiserRow("rival-c", 120.0, 200.0),                    # exactly 60% of prior: vacated
           AdvertiserRow("rival-d", 0.0, 300.0))                      # also vacated, but only the first is named
    v = b.voice(100.0, 100.0, 1000.0, 900.0, advertisers=adv)
    a = attribute(b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), voice=v))
    assert a.lever("contest_vacuum").title == "Contest the voice rival-c just vacated"
    none = b.voice(100.0, 100.0, 1000.0, 900.0, advertisers=adv[:3])
    assert attribute(b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), voice=none)).lever("contest_vacuum") is None


def test_share_ruled_out_with_geo_proof(b):
    off = b.offtake(1000.0, 900.0, 5000.0, 4400.0, geos=[
        b.geo("geo-north", 600.0, 560.0, -12.0), b.geo("geo-south", 400.0, 340.0, -12.0)])
    a = attribute(b.facts(off))
    why = next(r.why for r in a.ruled_out if r.hypothesis == "Brand losing share")
    assert "beat the segment in 1 of the top 2 geos" in why
    assert not a.has("SHARE_CEDED")


def test_mix_shift_needs_another_line_to_grow_by_half_the_loss(b):
    off = b.offtake(1000.0, 800.0, 5000.0, 5000.0)
    grew = b.rivals(b.rival("own-brand", 20.0, 16.0, is_own=True), own_other_lines=(LineRow("granola", 600.0, 500.0),))
    assert attribute(b.facts(off, rivals=grew)).has("MIX_SHIFT")                 # +100 is exactly half of 200
    flat = b.rivals(b.rival("own-brand", 20.0, 16.0, is_own=True), own_other_lines=(LineRow("granola", 599.0, 500.0),))
    a = attribute(b.facts(off, rivals=flat))
    assert not a.has("MIX_SHIFT") and a.is_ruled_out("Cannibalisation inside the brand")
    up = b.offtake(1000.0, 1100.0, 5000.0, 5000.0)
    quiet = attribute(b.facts(up, rivals=grew))
    assert not quiet.has("MIX_SHIFT") and not quiet.is_ruled_out("Cannibalisation inside the brand")


def test_sibling_brand_is_never_the_absorbing_rival(b):
    off = b.offtake(1000.0, 900.0, 5000.0, 5000.0)
    r = b.rivals(b.rival("own-brand", 20.0, 18.0, is_own=True), b.rival("house-two", 5.0, 7.5, is_sibling=True),
                 b.rival("rival-b", 10.0, 10.6))
    a = attribute(b.facts(off, rivals=r))
    assert a.cause("COMPETITOR_ABSORPTION").data["brand"] == "rival-b"
    assert any("house-two" in h and "portfolio shift" in h for h in a.held)
    only = b.rivals(b.rival("own-brand", 20.0, 18.0, is_own=True), b.rival("house-two", 5.0, 7.5, is_sibling=True))
    a2 = attribute(b.facts(off, rivals=only))
    assert not a2.has("COMPETITOR_ABSORPTION") and a2.has("SHARE_CEDED")


def test_sibling_registry_applied_by_enrichment(b):
    off = b.offtake(1000.0, 900.0, 5000.0, 5000.0)
    f = b.facts(off, rivals=b.rivals(b.rival("own-brand", 20.0, 18.0, is_own=True), b.rival("house-two", 5.0, 7.5)))
    assert attribute(f).cause("COMPETITOR_ABSORPTION").data["brand"] == "house-two"     # no registry: a rival
    assert not attribute(enrich(f, siblings=["House-Two"])).has("COMPETITOR_ABSORPTION")


def test_own_brand_is_never_named_as_the_absorbing_rival(b):
    off = b.offtake(1000.0, 1200.0, 5000.0, 5000.0)
    r = b.rivals(b.rival("own-brand", 20.0, 24.0, is_own=True), b.rival("rival-b", 10.0, 9.0))
    a = attribute(b.facts(off, rivals=r))
    assert not a.has("COMPETITOR_ABSORPTION")


def test_mover_threshold_and_defend_lever_wording(b):
    off = b.offtake(1000.0, 1000.0, 5000.0, 5000.0)
    at = b.rivals(b.rival("rival-b", 10.0, 10.5, how="Deep price (35% off)"))
    a = attribute(b.facts(off, rivals=at))
    assert a.has("COMPETITOR_ABSORPTION") and "match the winning lever" in a.lever("defend_lane").why
    under = b.rivals(b.rival("rival-b", 10.0, 10.49))
    assert not attribute(b.facts(off, rivals=under)).has("COMPETITOR_ABSORPTION")
    balanced = attribute(b.facts(off, rivals=b.rivals(b.rival("rival-b", 10.0, 11.0))))
    assert "availability, voice and price together" in balanced.lever("defend_lane").why


def test_lane_lands_in_the_absorption_evidence(b):
    off = b.offtake(1000.0, 1000.0, 5000.0, 5000.0)
    f = b.facts(off, rivals=b.rivals(b.rival("own-brand", 20.0, 20.0, is_own=True), b.rival("rival-b", 10.0, 11.0)))
    assert "same lane" not in attribute(f).cause("COMPETITOR_ABSORPTION").evidence
    g = enrich(f, lanes={"rival-b": "high protein, vegan", "own-brand": "protein snacks"})
    assert "same lane (high protein, vegan)" in attribute(g).cause("COMPETITOR_ABSORPTION").evidence
    h = enrich(f, lanes={"rival-b": "kids lunchbox", "own-brand": "protein snacks"})
    assert "same lane" not in attribute(h).cause("COMPETITOR_ABSORPTION").evidence


def test_new_entrant_has_a_cause_and_no_lever(b):
    off = b.offtake(1000.0, 1000.0, 5000.0, 5000.0)
    r = b.rivals(b.rival("rival-n", 0.0, 0.3), launches=(LaunchRow("rival-n", "new bar", date(2026, 3, 1), 1.4),))
    a = attribute(b.facts(off, rivals=r))
    assert a.has("NEW_ENTRANT") and not a.is_ruled_out("New competitor entry") and a.levers == ()
    assert attribute(b.facts(off, rivals=b.rivals(b.rival("rival-n", 0.0, 0.3)))).is_ruled_out("New competitor entry")
    assert not attribute(b.facts(off)).is_ruled_out("New competitor entry")     # no rival data: silent


def test_what_held_lists_only_gates_that_did_not_fire(b):
    f = b.facts(b.offtake(1000.0, 1000.0, 5000.0, 5000.0),
                conversion=ConversionFacts(1000.0, 1000.0, 0.010, 0.0109, org_p=500.0, org_c=450.0),
                pricing=PricingFacts(18.0, 18.0), availability=AvailabilityFacts(0.9, 0.9))
    assert attribute(f).held == ("discount depth stable", "one list price per product", "conversion steady",
                                 "organic impressions held", "availability healthy")
    g = replace(f, conversion=ConversionFacts(1000.0, 1000.0, 0.010, 0.0111, org_p=500.0, org_c=449.0))
    assert "conversion steady" not in attribute(g).held and "organic impressions held" not in attribute(g).held


def test_every_lever_has_four_fields_and_a_valid_axis(b):
    for f in (b.growth_market(), b.tide_market(), b.rich()):
        a = attribute(f)
        assert a.levers
        for lever in a.levers:
            title, why, checkpoint, axis = lever.as_tuple()
            assert title and why and checkpoint and axis in AXES and lever.source == "gate"
        assert len({l.id for l in a.levers}) == len(a.levers)
        assert all(c.key in CAUSE_TITLES for c in a.causes)
        assert all(r.hypothesis in RULED_OUT_HYPOTHESES for r in a.ruled_out)
