"""The recommendation gate stack, structural bleed and priority."""

import itertools

import pytest

from cpm_lineage.attribution import Driver, attribute, build_lever_panel, fit_prior
from cpm_lineage.config import AvailabilityPolicy, Economics
from cpm_lineage.recommend import (BOOST_LEVERS, RULES, Action, BudgetState, Cohort, RecommendConfig,
                                   RecommendContext, UnitStats, bleeder_loop, entity_type, exo_note, head_terms,
                                   history_crosscheck, pick_targets, priority, recommend, role_cohorts,
                                   scale_winners, self_competition, structural_bleed, unit_stats)
from cpm_lineage.attribution.levers import TIER2


def U(value, roas, spend=50_000.0, role="generic", **kw):
    return UnitStats(value=value, role=role, spend=spend, sales=roas * spend, **kw)


def drv(lever, contrib, pct=None, **kw):
    return Driver(lever=lever, contrib=contrib, pct=pct if pct is not None else abs(contrib) * 100, **kw)


COHORTS = {"generic": Cohort(n=20, roas_p10=0.6, roas_p25=1.2, roas_p75=3.0, cpnu_p25=200.0),
           "competitor": Cohort(n=20, roas_p10=0.3, roas_p25=0.9, roas_p75=2.0, cpnu_p25=150.0),
           "own_brand": Cohort(n=20, roas_p10=3.0, roas_p25=5.0, roas_p75=9.0, cpnu_p25=250.0)}

WINNER = U("big winner", 4.0, spend=80_000.0, search_volume=500.0)


def verbs(rec):
    return [a.verb for a in rec.actions]


def classes(rec):
    return [a.verb_class for a in rec.actions]


# --------------------------------------------------------------------------- availability

def test_scale_suppressed_when_availability_dropped_five_points():
    ctx = RecommendContext(availability=0.85, availability_prior=0.90, cohorts=COHORTS)
    rec = recommend([], [WINNER], ctx=ctx, cur_roas=4.0, verdict="confirmed",
                    account_moves={"scale": ["other entity"]})
    assert verbs(rec)[0] == "Fix availability first" and "scale" not in classes(rec) and "raise" not in classes(rec)
    # 4.9 points: no drop, the winner is scaled
    ctx = RecommendContext(availability=0.851, availability_prior=0.90, cohorts=COHORTS)
    rec = recommend([], [WINNER], ctx=ctx, cur_roas=4.0, verdict="confirmed")
    assert "Fix availability first" not in verbs(rec) and "Scale" in verbs(rec)


def test_availability_block_holds_scaling_and_boost_levers():
    ctx = RecommendContext(availability=0.59, availability_prior=0.60, cohorts=COHORTS)
    rec = recommend([drv("own_paid_sov", -0.2), drv("bid", -0.1)], [WINNER, U("weak", 1.2, spend=30_000.0)],
                    ctx=ctx, cur_roas=3.0, verdict="confirmed", budget=BudgetState(capped=True))
    assert verbs(rec)[0] == "Fix availability first"
    assert not ({"scale", "raise", "topup", "bid_lift"} & set(classes(rec)))
    assert any("availability" in c for c in rec.conflicts) and len(rec.conflicts) >= 3
    assert "Cap bids on" in verbs(rec)                                    # a cut lever is still allowed
    # exactly at the block line: not blocked
    ok = recommend([], [WINNER], ctx=RecommendContext(availability=0.60, cohorts=COHORTS), cur_roas=4.0, verdict="confirmed")
    assert "Fix availability first" not in verbs(ok)
    # percentages are normalised; the block line is policy
    pct = recommend([], [], ctx=RecommendContext(availability=59.0), verdict="confirmed")
    assert verbs(pct)[0] == "Fix availability first"
    strict = recommend([], [], ctx=RecommendContext(availability=0.7), verdict="confirmed",
                       availability=AvailabilityPolicy(block=0.75))
    assert verbs(strict)[0] == "Fix availability first"


def test_resume_after_recovery_into_the_healthy_band():
    rec = recommend([], [], ctx=RecommendContext(availability=0.86, availability_prior=0.80), verdict="confirmed")
    assert "Resume held scaling" in verbs(rec)
    below = recommend([], [], ctx=RecommendContext(availability=0.84, availability_prior=0.78), verdict="confirmed")
    assert "Resume held scaling" not in verbs(below)
    sparse = recommend([], [], ctx=RecommendContext(availability=0.95, availability_prior=0.80, availability_days=2),
                       verdict="confirmed")
    assert "Resume held scaling" not in verbs(sparse)                     # sparse data fails closed


# --------------------------------------------------------------------------- budget branch

def _capped(roas=3.0, status="budget_held", **ctx):
    return recommend([], [], budget=BudgetState(capped=True, **ctx.pop("budget", {})), cur_roas=roas,
                     verdict="confirmed", ctx=RecommendContext(status=status, **ctx))


def test_flywheel_pair_self_sustaining_holds_cold_tops_up():
    assert verbs(_capped(organic_share=0.8))[0] == "Hold the budget raise"
    assert "Top up" not in verbs(_capped(organic_share=0.8))
    assert verbs(_capped(organic_share=0.1))[0] == "Top up"
    assert verbs(_capped(organic_share=0.70))[0] == "Hold the budget raise"
    assert verbs(_capped(organic_share=0.69))[0] == "Top up"


def test_capped_ladder_by_roas_and_status():
    assert verbs(_capped(roas=2.0, status="active"))[0] == "Raise daily budget"
    assert verbs(_capped(roas=1.99, status="active"))[0] == "Hold & monitor"
    assert verbs(_capped(roas=1.0, status="active"))[0] == "Hold & monitor"
    assert verbs(_capped(roas=0.99, status="active"))[0] == "Tighten daily budget"
    assert "Tighten daily budget" not in verbs(_capped(roas=0.5, status="operator_paused"))
    assert verbs(_capped(roas=3.0, status="operator_paused"))[0] == "Investigate stop & reactivate"
    assert verbs(_capped(roas=None, status="active"))[0] == "Investigate tracking"
    removed = _capped(roas=3.0, status="removed")
    assert verbs(removed) == ["Launch replacement"] and removed.conflicts


def test_capped_raise_blockers_at_thresholds():
    assert verbs(_capped(status="active", bleed_pct=40.0))[0] == "Hold the budget raise"
    assert verbs(_capped(status="active", bleed_pct=39.0))[0] == "Raise daily budget"
    assert verbs(_capped(status="active", budget={"marginal_roas": 0.89}))[0] == "Hold the budget raise"
    # 0.9 .. 1.0 is still a hold: the next unit of spend returns less than it costs
    assert verbs(_capped(status="active", budget={"marginal_roas": 0.9}))[0] == "Hold the budget raise"
    assert verbs(_capped(status="active", budget={"marginal_roas": 0.99}))[0] == "Hold the budget raise"
    assert verbs(_capped(status="active", budget={"marginal_roas": 1.0}))[0] == "Raise daily budget"
    # an untrusted curve does not block
    assert verbs(_capped(status="active", budget={"marginal_roas": 0.5, "curve_r2": 0.29}))[0] == "Raise daily budget"
    assert verbs(_capped(status="active", budget={"marginal_roas": 0.5, "curve_r2": 0.3}))[0] == "Hold the budget raise"
    day = _capped(status="active", budget={"dayparted": True})
    assert "Raise daily budget" not in verbs(day) and any("dayparted" in c for c in day.conflicts)
    rich = _capped(status="active", budget={"rec_range": (1300.0, 1500.0), "revenue_at_stake": 9000.0, "marginal_roas": 1.4})
    assert rich.actions[0].extras == {"rec_range": [1300.0, 1500.0], "revenue_at_stake": 9000.0,
                                      "revenue_estimator": "budget-probe", "marginal_roas": 1.4}


def test_breakeven_rebases_the_budget_ladder():
    econ = Economics(breakeven=2.0)
    rec = recommend([], [], budget=BudgetState(capped=True), cur_roas=3.0, verdict="confirmed", economics=econ)
    assert verbs(rec)[0] == "Hold & monitor"                               # raise bar is now 4.0
    rec = recommend([], [], budget=BudgetState(capped=True), cur_roas=1.9, verdict="confirmed", economics=econ)
    assert verbs(rec)[0] == "Tighten daily budget"


def test_prefund_gates():
    def go(**kw):
        budget = kw.pop("budget", BudgetState())
        roas = kw.pop("roas", 2.5)
        return recommend([], [], budget=budget, cur_roas=roas, verdict="confirmed",
                         ctx=RecommendContext(demand_trend=kw.pop("trend", 0.25), **kw))
    assert "prefund" in classes(go())
    assert "prefund" not in classes(go(trend=0.249))
    assert "prefund" not in classes(go(roas=0.9))                          # ROAS < 1 never pre-funded
    assert "prefund" in classes(go(roas=None)) and "prefund" in classes(go(roas=1.0))
    assert "prefund" not in classes(go(budget=BudgetState(dayparted=True)))
    assert "prefund" not in classes(go(organic_share=0.75))                # self-sustaining
    assert "prefund" not in classes(go(availability=0.5))
    assert "prefund" not in classes(go(availability=0.80, availability_prior=0.90))
    capped = go(budget=BudgetState(capped=True, dayparted=False))
    assert [a.score for a in capped.actions if a.verb_class == "prefund"] == [64.0]
    assert [a.score for a in go().actions if a.verb_class == "prefund"] == [48.0]


def test_auto_targeted_burn():
    def go(roas, **kw):
        return recommend([], [], cur_roas=roas, verdict="thin-data", ctx=RecommendContext(auto_targeted=True, **kw))
    assert verbs(go(0.9))[0] == "Cap / reduce"
    assert "Cap / reduce" not in verbs(go(1.5))
    assert verbs(go(1.6, spend=106.0, spend_prior=100.0, roas_prior=2.0))[0] == "Cap / reduce"   # +6%, -20%
    assert "Cap / reduce" not in verbs(go(1.61, spend=106.0, spend_prior=100.0, roas_prior=2.0))
    assert "Cap / reduce" not in verbs(go(1.6, spend=104.0, spend_prior=100.0, roas_prior=2.0))
    assert go(0.9, total_roas=1.1, direct_roas=0.9).actions[0].notes
    assert not go(0.9, total_roas=1.3, direct_roas=0.9).actions[0].notes


def test_utilisation_ladder():
    def go(**b):
        return recommend([], [], budget=BudgetState(**b), cur_roas=2.5, verdict="confirmed")
    assert verbs(go(ladder="bid-constrained"))[0] == "Raise bids, not budget"
    assert verbs(go(ladder="bid-constrained", stepwise_bid_test=True))[0] == "Stepwise bid test"
    assert verbs(go(ladder="volume-ceiling"))[0] == "Add keywords"
    assert verbs(go(ladder="severe", position=3.1))[0] == "Raise bids"
    assert verbs(go(ladder="severe", position=3.0))[0] == "Add keywords"
    assert verbs(go(ladder="severe"))[0] == "Audit before spending"
    assert go(ladder="severe").actions[0].score == 78.0 and go(ladder="volume-ceiling").actions[0].score == 56.0
    assert not go(ladder="severe", capped=True).actions or "Audit before spending" not in verbs(go(ladder="severe", capped=True))


# --------------------------------------------------------------------------- bleeder loop

def _loop(units, **ctx):
    return bleeder_loop(units, RecommendContext(cohorts=COHORTS, **ctx), bleed_floor=10_000.0,
                        head=head_terms(units))


def test_bleeder_rules_in_order():
    r = _loop([U("rival a", 0.4, role="competitor", own_organic_impressions=0.0, new_users=900.0)])
    assert r["stops"] == ["rival a"]                                       # rule 1 overrides acquisition
    r = _loop([U("rival b", 0.0, role="competitor", new_users=0.0, indirect_sales=0.0, own_organic_impressions=5.0)])
    assert r["stops"] == ["rival b"]                                       # rule 2
    r = _loop([U("rival c", 0.4, role="competitor", new_users=50_000 / 150.0)])
    assert r["spared"] and "best quartile" in r["spared"][0]["reason"]     # rule 3: cpnu == p25
    r = _loop([U("rival d", 0.4, role="competitor", new_users=50_000 / 300.0)])
    assert r["spared"] and "watch" in r["spared"][0]["reason"]             # rule 4: cpnu == 2 x p25
    r = _loop([U("rival e", 0.4, role="competitor", new_users=50_000 / 301.0, own_organic_impressions=10.0)])
    assert r["stops"] == [] and r["caps"][0]["unit"] == "rival e" and r["caps"][0]["cap"] == "20-30%"   # rule 11
    r = _loop([U("head", 0.5, search_volume=4000.0), U("tail", 0.5, search_volume=60.0, spend=20_000.0),
               U("t2", 3.0, search_volume=3000.0), U("t3", 3.0, search_volume=2000.0)])
    assert [c["unit"] for c in r["caps"]] == ["head"] and r["stops"] == ["tail"]     # rules 5 and 12
    r = _loop([U("g", 0.5, new_users=50_000 / 200.0), U("big", 3.0, spend=90_000.0)])
    assert [s["unit"] for s in r["spared"]] == ["g"]                       # rule 6
    r = _loop([U("own term", 0.8, role="own_brand")], organic_share=0.19)
    assert r["spared"] and r["stops"] == []                                # rule 7
    assert _loop([U("own term", 0.8, role="own_brand")], organic_share=0.20)["stops"] == ["own term"]
    r = _loop([U("rival f", 0.9, role="competitor")])
    assert "norm" in r["spared"][0]["reason"]                              # rule 8: roas == p25
    r = _loop([U("g2", 0.7, indirect_sales=0.3 * 35_000 / 0.7), U("big", 3.0, spend=90_000.0)])
    assert "halo" in r["spared"][0]["reason"]                              # rule 9: 30% indirect, roas >= p10
    r = _loop([U("g3", 0.5, indirect_sales=0.3 * 25_000 / 0.7), U("big", 3.0, spend=90_000.0)])
    assert r["caps"][0]["cap"] == "30%"                                    # rule 10: roas < p10
    r = _loop([U("g4", 0.5, indirect_sales=0.2 * 25_000 / 0.8), U("big", 3.0, spend=90_000.0)])
    assert r["stops"] == ["g4"]                                            # halo exactly 20%: not above the bar


def test_bleeder_candidates_floor_and_limits():
    small = [U("s", 0.5, spend=10_000.0), U("big", 3.0, spend=90_000.0)]
    assert _loop(small)["stops"] == []                                     # spend must exceed the floor
    assert _loop([U("s", 0.5, spend=10_001.0), U("big", 3.0, spend=90_000.0)])["stops"] == ["s"]
    assert _loop([U("be", 1.0), U("big", 3.0, spend=90_000.0)])["stops"] == []      # ROAS 1.0 is not below breakeven
    many = [U(f"k{i}", 0.5, spend=20_000.0 + i) for i in range(6)] + [U("big", 3.0, spend=900_000.0)]
    r = _loop(many)
    assert r["stops"] == ["k5", "k4", "k3"]
    placement = [UnitStats("home feed", role="placement", is_placement=True, spend=99_999.0, sales=0.0)]
    assert _loop(placement)["stops"] == []
    # untrusted cohort (n < 12): the acquisition spare does not apply
    weak = {"competitor": Cohort(n=11, roas_p25=0.1, cpnu_p25=1e9)}
    r = bleeder_loop([U("rival", 0.4, role="competitor", new_users=999.0)], RecommendContext(cohorts=weak),
                     bleed_floor=1.0)
    assert r["spared"] == [] and r["caps"][0]["unit"] == "rival"


def test_relevant_conquest_gets_a_staged_cap_not_a_pause():
    units = [U("rival x", 0.35, spend=140_000.0, role="competitor", new_users=10.0, own_organic_impressions=500.0),
             U("ok", 2.5, spend=60_000.0)]
    rec = recommend([], units, ctx=RecommendContext(cohorts=COHORTS), cur_roas=1.0, verdict="unexplained")
    a = next(a for a in rec.actions if a.action_type == "BID_CAP")
    assert a.targets == ["rival x"] and a.verb == "Stage a bid cap on"
    assert "pause" not in classes(rec) and rec.staged_caps[0]["unit"] == "rival x"


def test_sub_breakeven_head_term_gets_a_bid_cap_row():
    units = [U("category head", 0.7, spend=120_000.0, search_volume=5000.0),
             U("mid", 2.4, spend=40_000.0, search_volume=300.0)]
    rec = recommend([], units, ctx=RecommendContext(cohorts=COHORTS), cur_roas=1.1, verdict="unexplained")
    caps = [a for a in rec.actions if a.action_type == "BID_CAP"]
    assert caps and caps[0].targets == ["category head"] and "pause" not in classes(rec)


def test_head_terms():
    units = [U("a", 2, search_volume=900.0), U("b", 2, search_volume=500.0), U("c", 2, search_volume=49.0),
             U("d", 2, search_volume=40.0), U("o", 2, role="own_brand", search_volume=1e6)]
    assert head_terms(units) == {"a", "b"}                                 # top 3 by volume, >= 50 a day
    flat = [U(f"k{i}", 2, search_volume=100.0) for i in range(5)]
    assert head_terms(flat) == {"k0", "k1", "k2", "k3", "k4"}              # each carries 20% >= 10%
    assert head_terms([U("x", 2, spend=5.0), U("y", 2, spend=9.0)]) == {"y"}   # no volume: largest by spend
    assert head_terms([]) == set()


# --------------------------------------------------------------------------- targets, winners

def test_pick_targets():
    units = [U("rival", 0.5, role="competitor", spend=9_000.0), U("hi", 2.0, spend=30_000.0),
             U("lowvol", 5.0, spend=80_000.0, search_volume=49.0), U("irrelevant", 5.0, spend=70_000.0, relevance=0.29),
             U("lowconf", 5.0, spend=60_000.0, conversion_confidence="low"),
             U("mid", 1.79, spend=20_000.0), U("edge", 1.8, spend=50_000.0), U("tiny", 0.2, spend=5_000.0),
             UnitStats("feed", is_placement=True, spend=1e6, sales=0.0)]
    assert pick_targets("own_availability", units) == ("Fix availability on", [], "product")
    assert pick_targets("price_gap", units)[0] == "Re-price"
    assert pick_targets("competitor_share", units) == ("Negative / pause", ["rival"], "unit")
    assert pick_targets("position", units) == ("Push rank on", ["hi"], "unit")
    assert pick_targets("own_paid_sov", units)[0] == "Raise bids on"
    total = sum(u.spend for u in units)
    assert 0.02 * total > 5000
    assert pick_targets("bid", units)[1] == ["tiny", "rival"]                # nothing clears the 2% floor: two lowest ROAS
    # cap: ROAS < 1.8 and spend above max(5000, 2% of spend); placement excluded
    small = [U("mid", 1.79, spend=20_000.0), U("edge", 1.8, spend=50_000.0), U("tiny", 0.2, spend=5_000.0)]
    assert pick_targets("bid", small) == ("Cap bids on", ["mid"], "unit")
    assert pick_targets("bid", small, spared=["mid"])[1] == ["tiny", "edge"]          # fallback: two lowest ROAS
    assert pick_targets("bid", small, economics=Economics(currency_unit=10.0))[1] == ["tiny", "mid"]   # floor now 50,000
    assert set(BOOST_LEVERS) == {"position", "own_paid_sov", "own_organic_sov"}


def test_scale_winner_gate():
    ctx = RecommendContext(cohorts=COHORTS)
    assert scale_winners([U("w", 3.0)], ctx) == ["w"]                       # >= 1.5 and == p75
    assert scale_winners([U("w", 2.99)], ctx) == []
    assert scale_winners([U("w", 4.0, organic_rank=6.0)], ctx) == []        # already wins the shelf
    assert scale_winners([U("w", 4.0, organic_rank=7.0)], ctx) == ["w"]
    assert scale_winners([U("w", 4.0, search_volume=49.0)], ctx) == []
    assert scale_winners([U("w", 4.0, relevance=0.2)], ctx) == []
    assert scale_winners([U("w", 4.0, conversion_confidence="low")], ctx) == []
    assert scale_winners([U("w", 4.0)], ctx, exclude=["w"]) == []
    assert scale_winners([U("w", 4.0)], RecommendContext()) == []           # no trusted cohort, no halo
    # halo route: total >= 1.5 x direct and >= 3.0
    assert scale_winners([U("h", 2.0, indirect_sales=50_000.0)], RecommendContext()) == ["h"]
    assert scale_winners([U("h", 2.0, indirect_sales=49_000.0)], RecommendContext()) == []
    # own-brand: incremental ROAS, and held back when organic is self-sustaining
    own = U("own", 12.0, role="own_brand", own_organic_impressions=200.0, own_ad_impressions=800.0)
    assert own.incremental_roas == pytest.approx(9.6) and scale_winners([own], ctx) == ["own"]
    half = U("own", 12.0, role="own_brand", own_organic_impressions=500.0, own_ad_impressions=500.0)
    assert scale_winners([half], ctx) == []                                  # 6.0 < p75 9.0
    sat = RecommendContext(cohorts=COHORTS, organic_share=0.7)
    assert scale_winners([own], sat) == []
    assert scale_winners([own], RecommendContext(cohorts=COHORTS, organic_share=0.7, product_age_days=89)) == ["own"]
    top = [U(f"w{i}", 4.0, spend=1000.0 * (i + 1)) for i in range(5)]
    assert scale_winners(top, ctx) == ["w4", "w3", "w2"]


# --------------------------------------------------------------------------- two-tier core

def test_no_lever_story_without_a_trustworthy_verdict():
    drivers = [drv("bid", -0.3, jury="confirm"), drv("own_paid_sov", 0.1)]
    for v in ("unexplained", "thin-data", None):
        rec = recommend(drivers, [U("k", 1.2)], cur_roas=1.5, verdict=v, verdict_note="because")
        assert all(a.lever is None for a in rec.actions) and verbs(rec)[-1] == "Hold lever changes"
        assert rec.exo_note is None and rec.driver is None
    rec = recommend(drivers, [U("k", 1.2)], cur_roas=1.5, verdict="directional")
    assert rec.actions[0].lever == "bid" and rec.actions[0].jury == "confirm" and rec.driver == "bid"
    assert rec.actions[-1].verb == "Protect / maintain" and rec.actions[-1].score == 18.0


def test_tier2_top_mover_never_carries_an_action_and_sets_exo_note():
    drivers = [drv("log_category_impressions", -0.5, evidence={"story": "demand-led", "r": 0.6, "r1": 0.5, "r2": 0.6}),
               drv("comp_discount", -0.2), drv("bid", -0.1), drv("comp_availability", 0.3)]
    rec = recommend(drivers, [U("k", 1.2)], cur_roas=1.5, verdict="confirmed")
    assert all(a.lever not in TIER2 for a in rec.actions)
    assert rec.exo_note and "exogenous" in rec.exo_note and "demand-led" in rec.exo_note
    assert rec.driver == "bid"
    noise = [drv("log_category_impressions", -0.5, evidence={"story": "no-link", "r": 0.1, "r1": 0.2, "r2": -0.1})]
    assert "noise" in exo_note(noise)
    assert "sign-flipping" in exo_note([drv("comp_availability", -0.5, evidence={"story": "unstable"})])
    # picked by magnitude, not by list order
    assert exo_note([drv("bid", -0.1), drv("comp_discount", -0.4)]) is not None
    assert exo_note([drv("comp_discount", -0.1), drv("bid", -0.4)]) is None
    assert exo_note([drv("comp_discount", 0.4)]) is None
    assert exo_note([Driver("comp_discount", contrib=-1.0, na=True), drv("bid", -0.1)]) is None
    assert {k for k, v in RULES.items() if v[2] == "NOTE"} == TIER2


def test_lever_score_range():
    for pct, score in ((0.0, 50.0), (10.0, 54.0), (40.0, 66.0), (250.0, 66.0)):
        rec = recommend([drv("bid", -0.1, pct=pct)], [U("k", 1.2)], cur_roas=1.5, verdict="confirmed")
        assert rec.actions[0].score == pytest.approx(score)


def test_pool_order_follows_scores():
    drivers = [drv("bid", -0.3, pct=40.0)]
    rec = recommend(drivers, [WINNER, U("k", 1.2, spend=30_000.0)], cur_roas=2.5, verdict="confirmed",
                    ctx=RecommendContext(cohorts=COHORTS, demand_trend=0.4),
                    account_moves={"tighten": [{"name": "loser a"}, "loser b", "loser c"], "scale": ["winner z"]})
    assert verbs(rec) == ["Tighten budget on", "Raise budget on", "Scale"]
    assert rec.actions[0].targets == ["loser a", "loser b"]
    assert [a.score for a in rec.actions] == [80.0, 72.0, 70.0]


# --------------------------------------------------------------------------- post-filters

def test_ramp_tag_in_short_mode():
    rec = _capped(status="active", short=True)
    assert rec.actions[0].ramp and "half-step" in rec.actions[0].notes[0]
    assert not _capped(status="active").actions[0].ramp


def test_execution_stale_and_wind_down_warnings():
    assert _capped(status="active", exec_fail_rate=0.51, exec_ops=10).actions[0].warnings
    assert not _capped(status="active", exec_fail_rate=0.5, exec_ops=10).actions[0].warnings
    assert not _capped(status="active", exec_fail_rate=0.9, exec_ops=9).actions[0].warnings
    assert _capped(status="active", stale_days=4).actions[0].warnings
    assert not _capped(status="active", stale_days=3).actions[0].warnings
    assert _capped(status="active", spend=50.0, spend_prior=100.0).actions[0].warnings
    assert not _capped(status="active", spend=51.0, spend_prior=100.0).actions[0].warnings
    hold = _capped(roas=1.5, status="active", stale_days=9, exec_fail_rate=0.9, exec_ops=50)
    assert hold.actions[0].warnings == []                                  # a hold writes nothing


def test_new_user_annotation():
    a = _capped(roas=2.8, status="active", roas_prior=3.0, new_user_rate=0.20).actions[0]
    assert any("healthy acquisition" in n for n in a.notes)
    a = _capped(roas=2.6, status="active", roas_prior=3.0, new_user_rate=0.20).actions[0]
    assert not any("healthy acquisition" in n for n in a.notes)             # softened by more than 10%
    a = _capped(roas=2.8, status="active", roas_prior=3.0, new_user_rate=0.04).actions[0]
    assert any("recapture" in n for n in a.notes)


def test_status_guard_for_platform_held_entities():
    rec = recommend([drv("own_paid_sov", -0.3)], [WINNER, U("hi", 2.5, spend=30_000.0)], cur_roas=3.0,
                    verdict="confirmed", ctx=RecommendContext(status="budget_held", cohorts=COHORTS, demand_trend=0.5),
                    budget=BudgetState(ladder="volume-ceiling"))
    assert verbs(rec)[0] == "Top up"
    assert not ({"scale", "bid_lift", "add_units", "prefund", "raise", "resume"} & set(classes(rec)))
    sat = recommend([], [], cur_roas=3.0, verdict="confirmed",
                    ctx=RecommendContext(status="budget_held", organic_share=0.9))
    assert verbs(sat)[0] == "Hold" and "Top up" not in verbs(sat)
    low = recommend([], [], cur_roas=0.8, verdict="confirmed", ctx=RecommendContext(status="budget_held"))
    assert "Top up" not in verbs(low)
    # raw platform strings go through the adapter's status map
    raw = recommend([], [], cur_roas=3.0, verdict="confirmed",
                    ctx=RecommendContext(status="OUT_OF_FUNDS", status_map={"out_of_funds": "budget_held"}))
    assert verbs(raw)[0] == "Top up"


# --------------------------------------------------------------------------- invariants

def test_never_more_than_three_actions_and_no_target_twice():
    units = [U("rival x", 0.3, spend=90_000.0, role="competitor", own_organic_impressions=0.0),
             U("rival y", 0.4, spend=80_000.0, role="competitor", own_organic_impressions=9.0),
             U("head", 0.6, spend=150_000.0, search_volume=9000.0),
             U("dud", 0.2, spend=70_000.0, search_volume=80.0),
             WINNER, U("hi", 2.6, spend=40_000.0, search_volume=400.0), U("weak", 1.4, spend=45_000.0)]
    levers = ["bid", "position", "own_paid_sov", "own_organic_sov", "competitor_share", "own_availability",
              "own_discount", "price_gap", "log_category_impressions", "comp_discount"]
    seen_verbs = set()
    for combo in itertools.product([None, "confirmed", "unexplained"], [True, False], ["active", "budget_held"],
                                   [0.5, 0.80, 0.95], [None, 0.5], [-1.0, 1.0]):
        v, capped, status, avail, trend, sign = combo
        drivers = [drv(lv, sign * 0.1 * (i + 1) * (-1 if i % 3 == 0 else 1), pct=10.0 * i) for i, lv in enumerate(levers)]
        rec = recommend(drivers, units, cur_roas=2.4, verdict=v,
                        budget=BudgetState(capped=capped, ladder=None if capped else "severe", position=5.0),
                        account_moves={"tighten": ["e1", "e2"], "scale": ["e3"]},
                        ctx=RecommendContext(status=status, availability=avail, availability_prior=0.9,
                                             demand_trend=trend, cohorts=COHORTS, short=True))
        assert len(rec.actions) <= 3
        targets = [t for a in rec.actions for t in a.targets]
        assert len(targets) == len(set(targets))
        assert all(a.lever not in TIER2 for a in rec.actions)
        assert all(a.targets for a in rec.actions if a.kind == "unit")
        assert len(rec.pause_units) <= 6 and len(rec.spared) <= 4
        seen_verbs.update(verbs(rec))
        assert rec.as_dict()["actions"] == [a.as_dict() for a in rec.actions]
    assert {"Fix availability first", "Pause / negative", "Stage a bid cap on", "Top up"} <= seen_verbs


def test_pause_list():
    units = [U(f"k{i}", 1.49, spend=9_000.0 + i) for i in range(8)] + [U("fine", 1.5, spend=50_000.0),
                                                                        U("small", 0.1, spend=8_000.0)]
    rec = recommend([], units, cur_roas=1.5, verdict="unexplained")
    assert rec.pause_units == [f"k{i}" for i in (7, 6, 5, 4, 3, 2)]


def test_money_floors_scale_with_currency_unit():
    units = [U("dud", 0.5, spend=20_000.0), U("big", 3.0, spend=100_000.0)]
    assert "Pause / negative" in verbs(recommend([], units, cur_roas=2.0, verdict="unexplained"))
    rec = recommend([], units, cur_roas=2.0, verdict="unexplained", economics=Economics(currency_unit=3.0))
    assert "Pause / negative" not in verbs(rec)                             # floor is now 30,000
    rec = recommend([], units, cur_roas=2.0, verdict="unexplained", cfg=RecommendConfig(bleed_floor_share=0.2))
    assert "Pause / negative" not in verbs(rec)                             # 20% of 120,000 = 24,000


# --------------------------------------------------------------------------- bleed, priority, annotators

def test_structural_bleed_and_priority():
    life = [U("a", 0.5, spend=300.0), U("b", 0.99, spend=100.0), U("c", 1.0, spend=200.0), U("d", 4.0, spend=400.0),
            UnitStats("feed", is_placement=True, spend=250.0, sales=0.0)]
    recent = [U("a", 1.5, spend=30.0), U("b", 0.2, spend=10.0), UnitStats("feed", is_placement=True, spend=99.0, sales=0.0)]
    bl = structural_bleed(life, recent)
    assert bl["spend"] == 650.0 and bl["total_spend"] == 1250.0 and bl["n"] == 3 and bl["pct"] == 52
    assert bl["worst"] == ["b"]                                             # recent horizon, placements excluded
    assert bl["total_sales"] == pytest.approx(150 + 99 + 200 + 1600)
    assert structural_bleed(life)["worst"] == ["a", "b"]
    assert structural_bleed([])["pct"] == 0
    assert structural_bleed(life, economics=Economics(breakeven=1.5))["n"] == 4
    assert priority(3.0, 0.99) == "p0" and priority(3.0, 1.0) == "p1" and priority(3.0, 2.9, 40.0) == "p0"
    assert priority(4.0, 3.0) == "p1" and priority(4.0, 3.01) == "p2" and priority(4.0, 3.5, 39.0) == "p2"
    assert priority(None, None) == "p2" and priority(None, 2.0) == "p2"
    assert priority(4.0, 1.4, economics=Economics(breakeven=1.5)) == "p0"


def test_entity_type_cohorts_and_annotators():
    units = [U("r", 1.0, spend=10.0, role="competitor"), U("g", 1.0, spend=9.0), U("o", 1.0, spend=8.0, role="own_brand")]
    assert entity_type(units) == "competitor" and entity_type([]) == "generic"
    assert entity_type(units[1:]) == "generic" and entity_type(units[2:]) == "own_brand"
    co = role_cohorts([U(f"k{i}", float(i), spend=1000.0, new_users=10.0) for i in range(1, 6)] + [U("tiny", 9.0, spend=999.0)])
    assert co["generic"].n == 5 and co["generic"].roas_p25 == 2.0 and co["generic"].roas_p75 == 4.0
    assert co["generic"].roas_p10 == pytest.approx(1.4) and co["generic"].cpnu_p25 == 100.0
    assert history_crosscheck("cut", 0.9) == "agree" and history_crosscheck("cut", 2.0) == "disagree"
    assert history_crosscheck("grow", 2.0) == "agree" and history_crosscheck("grow", 0.99) == "disagree"
    assert history_crosscheck("cut", 1.5) is None and history_crosscheck("grow", None) is None
    assert self_competition(units, ["g", "o"])["units"] == ["g", "o"]       # 17 of 27
    assert self_competition([], ["g"]) is None
    hit = self_competition([U("a", 1, spend=30.0), U("b", 1, spend=70.0)], ["a"])
    assert hit == {"share": pytest.approx(0.3), "units": ["a"]}
    assert self_competition([U("a", 1, spend=29.0), U("b", 1, spend=71.0)], ["a"]) is None


def test_synthetic_end_to_end(synthetic_dataset, windows_30):
    d = synthetic_dataset
    feeds = dict(market_rows=d.market, shelf_rows=d.shelf, targeting_units=d.targeting_units)
    prior = fit_prior(build_lever_panel(d.perf, **feeds))
    # CPM spike: the bid lever is named and the spiking keyword is the cap target
    a = attribute(build_lever_panel(d.perf, entity_id="cmp-generic", **feeds), windows_30, prior=prior)
    units = unit_stats(d.perf, d.targeting_units, entity_id="cmp-generic", window=windows_30.current)
    rec = recommend(a.drivers, units, cur_roas=a.comparison_roas, verdict=a.verdict)
    assert rec.driver == "bid" and rec.actions[0].action_type == "BID_CAP" and "protein bar" in rec.actions[0].targets
    assert rec.actions[0].sizing and rec.actions[0].sizing["direction"] == "down"
    # bleeding conquest keyword: structural bleed is material, entity is p0
    life = unit_stats(d.perf, d.targeting_units, entity_id="cmp-conquest")
    recent = unit_stats(d.perf, d.targeting_units, entity_id="cmp-conquest", window=windows_30.current)
    bl = structural_bleed(life, recent)
    assert bl["worst"][0] == "rivalb bar" and bl["pct"] >= 40
    c = attribute(build_lever_panel(d.perf, entity_id="cmp-conquest", **feeds), windows_30, prior=prior)
    assert priority(c.baseline_roas, c.comparison_roas, bl["pct"]) == "p0" and entity_type(recent) == "competitor"
    rec = recommend(c.drivers, recent, cur_roas=c.comparison_roas, verdict=c.verdict,
                    ctx=RecommendContext(bleed_pct=bl["pct"], cohorts=role_cohorts(unit_stats(d.perf, d.targeting_units))))
    named = [t for x in rec.actions for t in x.targets]
    assert "rivalb bar" in named and len(named) == len(set(named)) and len(rec.actions) <= 3
    # budget-capped winner held by the platform: top up
    w = attribute(build_lever_panel(d.perf, entity_id="cmp-winner", **feeds), windows_30, prior=prior)
    wu = unit_stats(d.perf, d.targeting_units, entity_id="cmp-winner", window=windows_30.current)
    rec = recommend(w.drivers, wu, cur_roas=w.comparison_roas, verdict=w.verdict, budget=BudgetState(capped=True),
                    ctx=RecommendContext(status="BUDGET_HELD", status_map=d.profile.status_map))
    assert rec.actions[0].verb == "Top up" and rec.actions[0].action_type == "BUDGET_RAISE"


# --------------------------------------------------------------------------- no raise next to its own hold

def _has_raise(rec):
    return any(c in ("raise", "topup", "prefund", "resume", "reactivate") for c in classes(rec))


def test_prefund_is_not_offered_next_to_a_held_raise():
    spike = RecommendContext(demand_trend=0.4)
    held = recommend([], [], budget=BudgetState(capped=True, marginal_roas=0.5, curve_r2=0.9), cur_roas=3.0, ctx=spike)
    assert verbs(held)[0] == "Hold the budget raise" and not _has_raise(held)
    monitor = recommend([], [], budget=BudgetState(capped=True), cur_roas=1.5, ctx=spike)
    assert verbs(monitor)[0] == "Hold & monitor" and not _has_raise(monitor)
    bleed = recommend([], [], budget=BudgetState(capped=True), cur_roas=3.0,
                      ctx=RecommendContext(demand_trend=0.4, bleed_pct=60.0))
    assert not _has_raise(bleed)
    free = recommend([], [], budget=BudgetState(capped=True, marginal_roas=1.4, curve_r2=0.9), cur_roas=3.0, ctx=spike)
    assert "raise" in classes(free)                                 # the gate only acts on a hold


def test_marginal_between_block_and_one_holds():
    rec = recommend([], [], budget=BudgetState(capped=True, marginal_roas=0.95, curve_r2=0.9), cur_roas=3.0)
    assert verbs(rec)[0] == "Hold the budget raise" and "under 1" in rec.actions[0].advice
    assert not _has_raise(rec)


@pytest.mark.parametrize("budget, ctx", [
    (BudgetState(capped=True), dict(status="budget_held", availability=0.4, availability_prior=0.4)),
    (BudgetState(capped=True, dayparted=True), dict(status="budget_held")),
    (BudgetState(capped=False), dict(status="budget_held", availability=0.4, availability_prior=0.4)),
    (BudgetState(capped=True, marginal_roas=0.5, curve_r2=0.9), dict(status="budget_held")),
])
def test_platform_held_entity_gets_hold_not_top_up_when_the_raise_is_held(budget, ctx):
    rec = recommend([], [], budget=budget, cur_roas=3.0, verdict="confirmed", ctx=RecommendContext(**ctx))
    assert "topup" not in classes(rec) and not _has_raise(rec)
    assert any(a.kind == "budget" and a.verb_class == "hold" for a in rec.actions)


def test_no_top_up_under_an_availability_block_in_any_context():
    import random
    rnd = random.Random(11)
    for _ in range(600):
        ctx = RecommendContext(status=rnd.choice(["budget_held", "active", "paused"]),
                               availability=rnd.choice([None, 0.3, 0.5, 0.8, 0.95]),
                               availability_prior=rnd.choice([None, 0.4, 0.9]),
                               demand_trend=rnd.choice([None, 0.0, 0.5]), bleed_pct=rnd.choice([None, 10.0, 60.0]),
                               organic_share=rnd.choice([None, 0.2, 0.9]))
        budget = BudgetState(capped=rnd.random() < 0.6, dayparted=rnd.random() < 0.2,
                             marginal_roas=rnd.choice([None, 0.5, 0.95, 1.5]), curve_r2=0.9)
        rec = recommend([], [WINNER], budget=budget, cur_roas=rnd.choice([None, 0.5, 1.5, 3.0]),
                        verdict="confirmed", ctx=ctx)
        if any("availability block" in c for c in rec.conflicts):
            assert not ({"raise", "topup", "prefund"} & set(classes(rec))), (ctx, budget, classes(rec))
        if "hold" in classes(rec) and any(a.verb == "Hold the budget raise" for a in rec.actions):
            assert not ({"raise", "topup", "prefund", "resume"} & set(classes(rec))), (ctx, budget, classes(rec))


def test_no_verdict_means_no_lever_story():
    rec = recommend([drv("bid", -0.3, 60)], [U("k", 1.2, spend=100_000.0)], cur_roas=1.2)
    assert verbs(rec) == ["Hold lever changes"]                     # verdict=None is documented as no-story
