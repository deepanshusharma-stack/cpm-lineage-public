"""Verifier, playbooks, lever targeting, routing."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cpm_lineage.rca import (ANSWERS, AXES, FOCUS_INTENT, INTENTS, PRESET_INTENTS, SECTIONS, AdTypeRow, FunnelFacts,
                             Mechanism, ProductValueRow, RcaConfig, analyse, answer, attribute, build_sections,
                             focus_answer, geo_buckets, geo_read, hard_failures, intent_levers, invest_verdict,
                             is_verified, keyword_route, mix_verdict, rank_actions, render_markdown, route_intent,
                             target_levers, validate_intent, verdict_paragraph, verify, waste_items)

# ---- verifier ---------------------------------------------------------------


def test_verifier_passes_clean_facts(b):
    f = b.growth_market()
    checks = verify(f, attribute(f))
    names = [c.name for c in checks]
    assert {"geo sums == total", "product sums == total", "decomposition exact",
            "brand-join coverage >= 99%", "market feed freshness"} <= set(names)
    assert all(c.ok for c in checks) and is_verified(checks)
    assert all(len(c.as_tuple()) == 3 for c in checks)


def test_verifier_catches_a_broken_product_sum_and_the_report_is_refused(b):
    f = b.growth_market()
    off = replace(f.offtake, product_rows=(ProductValueRow("sku-1", f.offtake.cur_value - 50.0, 1.0),))
    broken = replace(f, offtake=off)
    failed = hard_failures(verify(broken, attribute(broken)))
    assert [c.name for c in failed] == ["product sums == total"]
    result = analyse(broken)
    assert result.status == "refused" and "product sums == total" in result.message
    assert result.attribution is None and result.answer is None and result.verdict == "" and result.sections == ()
    md = render_markdown(result)
    assert "REFUSED" in md and "## Verdict" not in md


def test_verifier_catches_a_broken_geo_sum_and_low_join_coverage(b):
    f = b.growth_market()
    geos = (replace(f.offtake.geo_rows[0], cur_value=1.0),) + f.offtake.geo_rows[1:]
    assert not is_verified(verify(replace(f, offtake=replace(f.offtake, geo_rows=geos))))
    low = replace(f, rivals=replace(f.rivals, join_coverage=98.9))
    assert [c.name for c in hard_failures(verify(low))] == ["brand-join coverage >= 99%"]
    assert is_verified(verify(replace(f, rivals=replace(f.rivals, join_coverage=99.0))))


def test_verifier_catches_a_tampered_decomposition(b):
    f = b.growth_market()
    a = attribute(f)
    bad = replace(a, mech=Mechanism(a.mech.impr_effect + 5.0, a.mech.conv_effect, a.mech.total, a.mech.read))
    assert [c.name for c in hard_failures(verify(f, bad))] == ["decomposition exact"]
    assert is_verified(verify(f))             # recomputed from the facts when no attribution is passed


def test_value_tolerance_is_absolute_or_relative_whichever_is_larger(b):
    f = b.growth_market()
    off = replace(f.offtake, product_rows=(ProductValueRow("sku-1", f.offtake.cur_value - 0.9, 1.0),))
    assert is_verified(verify(replace(f, offtake=off)))
    off = replace(f.offtake, product_rows=(ProductValueRow("sku-1", f.offtake.cur_value - 1.1, 1.0),))
    assert not is_verified(verify(replace(f, offtake=off)))


def test_stale_ads_feed_degrades_instead_of_refusing(b):
    f = replace(b.growth_market(), funnel=FunnelFacts(stale=True))
    checks = verify(f)
    stale = next(c for c in checks if c.name == "ads feed freshness")
    assert not stale.ok and not stale.hard and is_verified(checks)
    result = analyse(f, "roas_move")
    assert result.ok and "cannot be computed honestly" in result.answer.text


def test_overrides_are_printed_in_the_proof_block(b):
    sc = b.scope(overrides={"promo_depth_pp": (3.0, 8.0)}, unknown_overrides=("made_up",))
    checks = verify(b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), sc))
    over = next(c for c in checks if c.name == "threshold override: promo_depth_pp")
    assert over.ok and over.detail == "3.0 -> 8.0"
    assert any(c.name == "override key ignored" and "made_up" in c.detail for c in checks)


# ---- presets and sections ---------------------------------------------------


def test_every_preset_has_an_answer_distinct_sections_and_resolvable_keys():
    assert set(INTENTS) == set(ANSWERS)
    lists = [INTENTS[k].sections for k in PRESET_INTENTS]
    assert len(set(lists)) == len(lists)                         # pairwise distinct
    for intent in INTENTS.values():
        assert intent.sections and all(key in SECTIONS for key in intent.sections)
        assert all(axis in AXES for axis in intent.axes)
    assert set(FOCUS_INTENT.values()) <= set(INTENTS)
    assert INTENTS["competition"].sections == INTENTS["ms_decline"].sections


def test_every_section_renders_for_every_fixture(b):
    for f in (b.growth_market(), b.tide_market(), b.rich(), b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0))):
        a = attribute(f)
        for intent in INTENTS:
            sections = build_sections(intent, f, a)
            assert [s.key for s in sections] == list(INTENTS[intent].sections)
            assert all(s.title and s.lines and all(isinstance(line, str) and line for line in s.lines)
                       for s in sections)
            assert answer(intent, f, a).intent in INTENTS


def test_markdown_starts_sections_with_headings(b):
    md = render_markdown(analyse(b.rich(), "cut_waste"))
    assert md.startswith("# Root-cause analysis: snack-bars (waste audit)")
    for title in ("## Verdict", "## Answer", "## Geo actions", "## Availability detail", "## Ads funnel",
                  "## Levers", "## Verification"):
        assert title in md
    assert "| geo sums == total | pass |" in md


def test_verdict_paragraph_has_the_market_read_the_geo_proof_and_the_action(b):
    f = b.tide_market()
    text = verdict_paragraph(f, attribute(f))
    assert "market tide plus a brand-specific loss" in text
    assert "Segment collapsed in geo-south (-35%), geo-east (-22%)" in text
    assert "visibility-led: -2,500 from visibility, +300 from conversion" in text
    assert "First action:" in text and "Share ceded" not in text


# ---- answers ----------------------------------------------------------------


def test_mix_not_optimal_on_the_rich_fixture(b):
    m = mix_verdict(b.rich())
    assert m.verdict == "NOT_OPTIMAL"
    assert m.data["best_roas"] == pytest.approx(3.00) and m.data["worst_roas"] == pytest.approx(1.88)
    assert m.data["worst_share"] == pytest.approx(0.27)
    assert "3.00 against 1.88" in m.text and "27% of spend" in m.text


@pytest.mark.parametrize("spend_b,sales_b,verdict", [
    (20_000.0, 46_150.0, "NOT_OPTIMAL"),     # ratio just over 1.30, worst share exactly 20%
    (20_000.0, 46_500.0, "OPTIMAL"),         # ratio 1.29
    (19_000.0, 30_000.0, "OPTIMAL"),         # worst share 19%
    (20_000.0, 0.0, "OPTIMAL"),              # worst ROAS is 0: not compared as a ratio
])
def test_mix_verdict_boundaries(b, spend_b, sales_b, verdict):
    spend_a = 100_000.0 - spend_b
    types = (AdTypeRow("a", spend_a, spend_a, 3.0 * spend_a, 3.0 * spend_a), AdTypeRow("b", spend_b, 1.0, sales_b, 1.0))
    f = b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), funnel=b.funnel(100_000.0, 100_000.0, ad_types=types))
    assert mix_verdict(f).verdict == verdict


def test_untracked_ad_type_prints_not_tracked_never_zero(b):
    types = (AdTypeRow("a", 60.0, 60.0, 180.0, 180.0), AdTypeRow("reach", 40.0, 40.0, 0.0, 0.0, sales_tracked=False))
    f = b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), funnel=b.funnel(100.0, 100.0, ad_types=types))
    m = mix_verdict(f)
    assert "reach 40% of spend at n/t" in m.text and m.verdict == "OPTIMAL"


def test_deflator_is_applied_before_ad_types_are_compared(b):
    types = (AdTypeRow("a", 60.0, 60.0, 180.0, 180.0), AdTypeRow("auto", 40.0, 40.0, 160.0, 160.0, roas_deflator=0.6))
    f = b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), funnel=b.funnel(100.0, 100.0, ad_types=types))
    m = mix_verdict(f)
    assert m.data["worst"] == "auto" and m.data["worst_roas"] == pytest.approx(2.4)


def test_availability_cost_is_sized_against_sales_and_can_be_exonerated(b):
    f = b.rich()
    ans = answer("availability", f, attribute(f))
    assert "42,500 (5.0% of the window's sales)" in ans.text
    assert ans.verdict == "NOT_DRIVER" and "NOT what drove the move" in ans.text
    assert "Largest losses: sku-2 30,000, sku-1 12,500" in ans.text


def test_spend_cut_into_a_growing_pool_is_under_investing(b):
    f = b.facts(b.offtake(1000.0, 1000.0, 5000.0, 5000.0), voice=b.voice(110.0, 112.0, 500.0, 560.0),
                funnel=b.funnel(100.0, 80.0, roas_c=3.0))
    v = invest_verdict(f, attribute(f))
    assert v.verdict == "UNDER" and v.data["cutting_into_growth"]
    assert "ROAS headroom" in v.text and "HELD so far" in v.text


@pytest.mark.parametrize("own_c,roas_c,verdict", [
    (94.9, 3.0, "UNDER"),             # SOV 18.98 vs share 20: gap below -1
    (95.0, 3.0, "PARITY"),            # gap exactly -1
    (115.0, 3.0, "PARITY"),           # gap exactly +3
    (115.5, 3.0, "OVER_PROFITABLE"),
    (115.5, 1.0, "OVER"),
])
def test_invest_verdict_bands(b, own_c, roas_c, verdict):
    f = b.facts(b.offtake(1000.0, 1000.0, 5000.0, 5000.0), voice=b.voice(100.0, own_c, 500.0, 500.0),
                funnel=b.funnel(100.0, 100.0, roas_c=roas_c))
    assert invest_verdict(f, attribute(f)).verdict == verdict


def test_roas_answer_separates_efficiency_from_budget(b):
    f = b.rich()
    ans = answer("roas_move", f, attribute(f))
    assert ans.verdict == "EFFICIENCY"
    assert ans.data["rpi_c"] / ans.data["cpm_c"] == pytest.approx(2.4, abs=1e-12)
    assert "click rate" not in ans.text and "mid-funnel rate moved 4.00% -> 3.50%" in ans.text
    cut = b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0), funnel=b.funnel(100.0, 80.0, roas_p=2.0, roas_c=2.6))
    ans = answer("roas_move", cut, attribute(cut))
    assert ans.verdict == "BUDGET_ARTIFACT" and "separate decisions" in ans.text


def test_geo_buckets_fix_is_trailing_a_living_segment(b):
    f = b.rich()
    buckets = geo_buckets(f)
    assert [g.geo for g in buckets.wait] == ["geo-south"]
    assert [g.geo for g in buckets.fix] == ["geo-north", "geo-east"]       # trailing a LIVING segment: fix, not avoid
    assert buckets.win == () and buckets.shift_to == ()
    assert "Fix first, then shift" in answer("geo", f, attribute(f)).text
    assert [geo_read(g, f) for g in f.offtake.geo_rows] == ["trailed", "collapsed", "trailed"]


def test_geo_read_words(b):
    f = b.facts(b.offtake(1.0, 1.0, 9.0, 9.0))
    assert geo_read(b.geo("g", 100.0, 60.0, -30.0), f) == "collapsed, trailed even that"
    assert geo_read(b.geo("g", 100.0, 72.0, -30.0), f) == "collapsed"
    assert geo_read(b.geo("g", 100.0, 103.0, 0.0), f) == "beat"
    assert geo_read(b.geo("g", 100.0, 102.0, 0.0), f) == "with"
    assert geo_read(b.geo("g", 100.0, 97.0, 0.0), f) == "trailed"


def test_waste_items_in_fixed_order(b):
    f = b.rich()
    items = waste_items(f, attribute(f))
    assert [k for k, _, _ in items] == ["availability_loss", "collapsed_geos", "passenger_spend"]
    assert items[1][2] is None                                     # named, not priced
    assert items[2][2] == pytest.approx(30_000.0)
    clean = b.facts(b.offtake(100.0, 100.0, 1000.0, 1000.0))
    assert answer("cut_waste", clean, attribute(clean)).text == "No priced waste found."


def test_action_ranking_sizes_by_lever_id(b):
    f = b.rich()
    ranked = rank_actions(f, attribute(f))
    sized = {r.lever.id: r.size for r in ranked if r.size is not None}
    assert sized["defend_lane"] == pytest.approx(0.20 * 4_900_000.0 - 850_000.0)   # share shortfall at today's size
    assert sized["passengers"] == pytest.approx(42_500.0)
    assert ranked[0].lever.id == "defend_lane"
    assert all(r.size is None for r in ranked[len(sized):])
    top = answer("top_action", f, attribute(f))
    assert top.verdict == "defend_lane" and "130,000" in top.text


def test_restore_budget_is_priced_only_when_voice_actually_fell(b):
    off = b.offtake(100.0, 100.0, 1000.0, 1000.0)
    fell = b.facts(off, funnel=b.funnel(100.0, 80.0, roas_p=3.0), voice=b.voice(100.0, 90.0, 500.0, 500.0))
    sized = {r.lever.id: r.size for r in rank_actions(fell, attribute(fell))}
    assert sized["restore_budget"] == pytest.approx(60.0)
    held = b.facts(off, funnel=b.funnel(100.0, 80.0, roas_p=3.0), voice=b.voice(100.0, 104.0, 500.0, 500.0))
    assert all(r.size is None for r in rank_actions(held, attribute(held)))


def test_focus_answers_per_axis(b):
    f = b.growth_market()
    a = attribute(f)
    assert "dilution, not a pullback" in focus_answer("sov", f, a).text
    assert "dragging the line" in focus_answer("availability", f, a).text
    assert "pullback is a confirmed cause" in focus_answer("pricing", f, a).text
    assert "Gained: rival-b +2.4pp" in focus_answer("competition", f, a).text
    assert "conversion-led: +500 from visibility and -1,050 from conversion" in focus_answer("conversion", f, a).text
    assert "worst quadrant" in focus_answer("offtake", f, a).text
    assert focus_answer("general", f, a).text == "" and focus_answer(None, f, a).text == ""


# ---- lever targeting --------------------------------------------------------


def test_off_axis_levers_collapse_to_also_open(b):
    f = b.growth_market()
    a = attribute(f)
    full = target_levers("why", f, a)
    assert full.primary == a.levers and full.also_open == ()
    view = target_levers("pricing", f, a)
    assert view.primary and all(l.axis == "pricing" for l in view.primary)
    assert set(view.also_open) == {l.title for l in a.levers if l.axis != "pricing"}
    ms = target_levers("ms_decline", f, a)
    order = [INTENTS["ms_decline"].axes.index(l.axis) for l in ms.primary]
    assert order == sorted(order)


def test_question_levers_appear_exactly_when_their_verdict_warrants(b):
    f = b.rich()
    a = attribute(f)
    assert [l.id for l in intent_levers("budget_mix", f, a)] == ["shift_mix"]
    assert [l.id for l in intent_levers("roas_move", f, a)] == ["recover_conversion"]
    assert [l.id for l in intent_levers("geo", f, a)] == ["close_geo_gap"]
    assert [l.id for l in intent_levers("grow_ms", f, a)] == ["contest_shedding"]      # rival-c shed 0.8pp
    assert intent_levers("why", f, a) == [] and intent_levers("availability", f, a) == []
    for lever in intent_levers("geo", f, a) + intent_levers("budget_mix", f, a):
        assert lever.source == "question" and lever.axis in AXES and all(lever.as_tuple())
    g = b.facts(b.offtake(100.0, 101.0, 1000.0, 1000.0), funnel=b.funnel(100.0, 100.0))   # no mix gap, no FIX geo
    ga = attribute(g)
    assert intent_levers("budget_mix", g, ga) == [] and intent_levers("geo", g, ga) == []
    assert intent_levers("grow_ms", g, ga) == []
    steady = replace(f, funnel=replace(f.funnel, roas_c=2.6))                          # 2.6 >= 0.95 x 2.7
    assert intent_levers("roas_move", steady, attribute(steady)) == []


def test_question_levers_dedupe_against_gate_levers(b):
    off = b.offtake(1000.0, 1000.0, 5000.0, 5000.0)
    # spend cut into a growing pool with voice held: the gate already set a voice floor
    held = b.facts(off, voice=b.voice(110.0, 112.0, 500.0, 560.0), funnel=b.funnel(100.0, 80.0))
    a = attribute(held)
    assert a.lever("voice_floor") is not None
    assert [l.id for l in intent_levers("voice", held, a)] == []
    # voice under share, no gate lever on it: the question implies buying up to share
    under = b.facts(off, voice=b.voice(90.0, 90.0, 500.0, 500.0), funnel=b.funnel(100.0, 100.0))
    ua = attribute(under)
    assert [l.id for l in intent_levers("sov_invest", under, ua)] == ["voice_to_share"]
    # diluted: the gate lever "buy voice to a share target" already covers it
    diluted = b.facts(off, voice=b.voice(90.0, 90.0, 500.0, 600.0))
    da = attribute(diluted)
    assert da.lever("buy_voice_target") is not None
    assert intent_levers("sov_invest", diluted, da) == []


# ---- routing ----------------------------------------------------------------

SEGMENTS = ["bars", "snack bars", "granola"]


def test_keyword_router_routes_without_a_classifier():
    r = keyword_route("Why did share of voice drop on snack bars over the last month?", SEGMENTS)
    assert r == {"segment": "snack bars", "days": 30, "focus": "sov"}             # longest segment name wins
    assert keyword_route("is stock hurting us this quarter", SEGMENTS) == {"days": 90, "focus": "availability"}
    assert keyword_route("promo depth in the last two months", SEGMENTS)["days"] == 60
    assert keyword_route("what happened in the last fortnight", SEGMENTS)["days"] == 14
    assert keyword_route("ROAS over 2 weeks", SEGMENTS) == {"days": 14, "focus": "conversion"}
    assert keyword_route("which rival took our market share this week", SEGMENTS) == {"days": 7, "focus": "competition"}
    assert keyword_route("sales fell", SEGMENTS) == {"focus": "offtake"}
    assert keyword_route("hello", SEGMENTS) == {}


def test_intent_validator_gates_classifier_output():
    ok = validate_intent({"segment": "SNACK BARS", "days": 30, "focus": "Pricing"}, SEGMENTS)
    assert ok == {"segment": "snack bars", "days": 30, "focus": "pricing"}
    assert validate_intent({"segment": "pet food", "days": 31, "focus": "vibes"}, SEGMENTS) == {}
    assert validate_intent({"days": "30"}, SEGMENTS) == {} and validate_intent({"days": True}, SEGMENTS) == {}
    assert validate_intent("garbage", SEGMENTS) == {} and validate_intent(None, SEGMENTS) == {}
    assert validate_intent({"days": 45}, SEGMENTS, replace(RcaConfig(), allowed_days=(45,))) == {"days": 45}


def test_route_intent_merges_classifier_and_rules():
    q = "why did visibility fall on granola this week"
    assert route_intent(q, SEGMENTS) == {"segment": "granola", "days": 7, "focus": "sov", "via": "rules",
                                         "intent": "voice"}
    ai = route_intent(q, SEGMENTS, classifier=lambda question, segs: {"focus": "availability", "days": 999})
    assert ai["focus"] == "availability" and ai["days"] == 7 and ai["via"] == "ai+rules" and ai["intent"] == "availability"
    full = route_intent(q, SEGMENTS, classifier=lambda *_: {"focus": "pricing", "days": 14, "segment": "bars"})
    assert full["via"] == "ai" and full["segment"] == "bars"

    def down(*_):
        raise RuntimeError("classifier offline")

    assert route_intent(q, SEGMENTS, classifier=down)["via"] == "rules"
    assert route_intent("hello", SEGMENTS) == {"via": "rules", "intent": "why"}


def test_route_cache_is_memoised_and_capped():
    calls = []

    def clf(question, segs):
        calls.append(question)
        return {"focus": "availability"}

    cache: dict = {}
    cfg = replace(RcaConfig(), route_cache_cap=2)
    for q in ("a", "a", "b", "c"):
        route_intent(q, SEGMENTS, classifier=clf, cache=cache, cache_key="adv-001", config=cfg)
    assert calls == ["a", "b", "c"] and len(cache) == 2 and ("adv-001", "a") not in cache
