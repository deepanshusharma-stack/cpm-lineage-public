from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import KW, REACH, RECO, SEG, camp, ctx, kw, kwm, none, one, profile, tel  # noqa: E402

from cpm_lineage.actions.families import (bid, collision, control_geos, defense, experiment,  # noqa: E402
                                          find_collisions, geo, growth, run_families, sov_gap)
from cpm_lineage.actions.primitives import (CollisionRow, EntityGeoStat, GeoMarketStat,  # noqa: E402
                                            MarketPlayer, RunCheck, SegmentMarket)
from cpm_lineage.adapters.base import PositionSemantics  # noqa: E402


def market(tot_c=1_000_000.0, tot_p=900_000.0, own_c=200_000.0, own_p=180_000.0, players=(), sov=None):
    return {SEG: SegmentMarket(SEG, tot_c, tot_p, own_c, own_p, tuple(players), sov)}


# --------------------------------------------------------------------- growth

def test_growth_budget_picks_best_in_band():
    cs = [camp("a", roas=2.5), camp("b", roas=5.0), camp("c", roas=14.0), camp("d", roas=1.9)]
    c = ctx(cs, segments=market(tot_c=1_100_000, tot_p=1_000_000, own_c=220_000, own_p=200_000))
    card = one(growth(c), type="budget")
    assert card.entity_id == "b" and card.change.pct == 10 and card.direction == "up"
    assert card.stake == pytest.approx(220_000 * 0.10 * 0.1)


def test_growth_gates():
    cs = [camp("a", roas=2.5)]
    assert not growth(ctx(cs, segments=market(1_049_000, 1_000_000, 209_800, 200_000)))     # +4.9%
    assert growth(ctx(cs, segments=market(1_050_000, 1_000_000, 210_000, 200_000)))         # +5.0%
    assert not growth(ctx(cs, segments=market(1_100_000, 1_000_000, 218_000, 200_000)))     # share -0.18 pp
    assert not growth(ctx([camp("a", roas=1.99)], segments=market(1_100_000, 1_000_000, 220_000, 200_000)))
    assert not growth(ctx(cs))                                                              # no market feed
    assert not growth(ctx([camp("a", roas=2.5, status="PAUSED")],
                          segments=market(1_100_000, 1_000_000, 220_000, 200_000)))


def test_growth_add_keywords_wraps_supplied_suggestions():
    cs = [camp("a", roas=1.2, spend=90_000), camp("b", roas=1.2, spend=10_000)]
    sugg = {"a": [{"kw": f"new {i}", "vol": 100 - i} for i in range(4)] + ["junk", {"kw": "new 5"}, {"kw": "new 6"}]}
    card = one(growth(ctx(cs, keyword_suggestions=sugg)), type="keyword")
    assert card.entity_id == "a" and card.stake == 0 and card.direction == "up"
    assert [k.kw for k in card.keywords] == ["new 0", "new 1", "new 2", "new 3", "new 5"]
    assert card.keywords[0].volume == 100
    assert not growth(ctx(cs))


# -------------------------------------------------------------------- defense

RIVAL_UP = MarketPlayer("rival-b", share_prior=10.0, share_current=10.4)
ENTRANT = MarketPlayer("rival-new", share_prior=0.4, share_current=2.0)


def _defense_ctx(players, own_c=190_000.0, **kw_):
    cs = [camp("moat", roas=5.0), camp("gen", roas=2.0)]
    rows = [kw("own a", "moat", 4_000, 5.0, role="own_brand"), kw("own b", "moat", 1_000, 5.0, role="sibling_brand"),
            kw("gen a", "moat", 5_000, 2.0), kw("gen b", "gen", 50_000, 2.0)]
    return ctx(cs, keywords=rows, segments=market(1_000_000, 1_000_000, own_c, 200_000, players), **kw_)


def test_entrant_watch():
    card = one(defense(_defense_ctx([ENTRANT], own_c=200_000)), type="watch")
    assert card.entity_id is None and card.stake == pytest.approx(1_000_000 * 2.0 / 100)
    assert not defense(_defense_ctx([MarketPlayer("x", share_prior=0.5, share_current=3.0)], own_c=200_000))
    assert not defense(_defense_ctx([MarketPlayer("x", share_prior=0.1, share_current=1.9)], own_c=200_000))
    assert not defense(_defense_ctx([MarketPlayer("sib", is_sibling=True, share_prior=0.1, share_current=3.0)],
                                    own_c=200_000))


def test_defend_raise_on_moat_and_cooldown():
    c = _defense_ctx([RIVAL_UP])                                     # own share 20% -> 19% = -1 pp
    card = one(defense(c), type="budget")
    assert card.entity_id == "moat" and card.change.pct == 15 and card.family == "defense"
    assert card.stake == pytest.approx(1_000_000 * 1.0 / 100)
    assert card.evidence["moat_share"] == pytest.approx(0.5)
    assert not defense(_defense_ctx([RIVAL_UP], recent_applied=frozenset({("defend", "moat")})))
    assert not defense(_defense_ctx([RIVAL_UP], own_c=197_100))      # -0.29 pp: not enough
    assert defense(_defense_ctx([RIVAL_UP], own_c=197_000))          # -0.30 pp
    assert not defense(_defense_ctx([MarketPlayer("rival-b", share_prior=10.0, share_current=10.29)]))
    assert not defense(_defense_ctx([MarketPlayer("own", is_own=True, share_prior=10.0, share_current=11.0)]))


def test_moat_requires_share_and_keyword_spend():
    cs = [camp("moat", roas=5.0)]
    seg = market(1_000_000, 1_000_000, 190_000, 200_000, [RIVAL_UP])
    low_share = [kw("own a", "moat", 3_900, 5.0, role="own_brand"), kw("gen a", "moat", 6_100, 2.0)]
    assert not defense(ctx(cs, keywords=low_share, segments=seg))
    thin = [kw("own a", "moat", 4_999, 5.0, role="own_brand")]
    assert not defense(ctx(cs, keywords=thin, segments=seg))


# ------------------------------------------------------------------------ bid

def test_classic_bid_is_bucket_relative():
    rows = [kwm("filler", 500_000, 2.0, 3.0), kwm("unseen", 30_000, 2.7, 5.1), kwm("seen", 30_000, 2.7, 5.0),
            kwm("thin", 24_999, 3.0, 9.0), kwm("mid", 30_000, 2.5, 9.0), kwm("noattr", 30_000, None, 9.0),
            kwm("nopos", 30_000, 3.0, None)]
    c = ctx([camp()], keyword_market=rows)
    card = one(bid(c), direction="up")
    bavg = sum(r.spend * r.direct_roas for r in rows if r.direct_roas) / sum(r.spend for r in rows if r.direct_roas)
    assert card.keywords[0].kw == "unseen" and card.change.pct == 15 and card.entity_id is None
    assert card.evidence["bucket_avg"] == pytest.approx(bavg) and 2.5 < 1.2 * bavg < 2.7
    assert card.stake == pytest.approx(30_000 * (2.7 - bavg) * 0.15, abs=1)
    assert len(bid(c)) == 1


def test_classic_no_bid_up_below_breakeven_bar_and_no_bid_down_below_fold():
    rows = [kwm("filler", 500_000, 1.0, 3.0), kwm("best of bad", 30_000, 1.4, 8.0)]
    assert not bid(ctx([camp()], keyword_market=rows))                # beats its kind but < 1.5 x be
    rows = [kwm("filler", 500_000, 3.0, 3.0), kwm("top bleeder", 30_000, 0.7, 2.0),
            kwm("buried bleeder", 30_000, 0.7, 2.1)]
    card = one(bid(ctx([camp()], keyword_market=rows)), direction="down")
    assert card.keywords[0].kw == "top bleeder" and card.change.pct == -15
    assert card.stake == pytest.approx(30_000 * 0.3)
    assert not bid(ctx([camp()], keyword_market=[kwm("filler", 500_000, 3.0, 3.0), kwm("x", 30_000, 0.8, 1.0)]))


def test_verdict_bid_raise_headroom_and_caps():
    filler = kwm("filler", 1_000_000, 2.0, 3.0)
    def run(dr, pos, **kw_):
        return [c for c in bid(ctx([camp()], keyword_market=[filler, kwm("t", 10_000, dr, pos, **kw_)],
                                   rule_profile="verdict")) if c.keywords[0].kw == "t"]
    thr = (1_000_000 * 2.0 + 10_000 * 2.3) / 1_010_000
    card = run(2.3, 7.0, live_bid=100.0)[0]
    step = round((2.3 / thr - 1) * 100)
    assert card.title == "Increase Bid" and card.change.pct == step and card.change.frm == 100.0
    assert card.change.to == pytest.approx(100 * (1 + step / 100))
    assert card.evidence["bid_ceiling"] == pytest.approx(100 * 2.3 / thr)
    assert card.evidence["roas_after"] == pytest.approx(2.3 / (1 + step / 100))
    assert card.stake == pytest.approx(10_000 * (2.3 - thr) * step / 100, abs=1)
    assert not run(2.3, 6.0)                                           # already visible
    assert not run(2.14, 7.0)                                          # headroom under 8%
    assert run(4.0, 7.0)[0].change.pct == 25
    deep = run(4.0, 11.0)[0]
    assert deep.change.pct == 40 and deep.title == "Increase Bid (visibility buy)"
    rented = run(4.0, 11.0, rival_owner_share=0.6, rival_owner_is_own=False)[0]
    assert rented.change.pct == 25 and rented.title == "Increase Bid"
    assert run(4.0, 11.0, rival_owner_share=0.9, rival_owner_is_own=True)[0].change.pct == 40
    assert not run(4.0, 7.0, )[0].keywords[0].kw != "t"


def test_verdict_bid_cut_dead_zone_exit_and_protection():
    filler = kwm("filler", 1_000_000, 2.0, 3.0)
    def run(dr, pos, spend=10_000, keywords=(), **kw_):
        c = ctx([camp()], keywords=list(keywords), keyword_market=[filler, kwm("t", spend, dr, pos, **kw_)],
                rule_profile="verdict")
        return [x for x in bid(c) if x.keywords[0].kw == "t"]
    thr = lambda dr, sp=10_000: (2_000_000 + sp * dr) / (1_000_000 + sp)  # noqa: E731
    card = run(1.5, 4.0)[0]
    needed = 1 - 1.5 / thr(1.5)
    assert card.title == "Decrease Bid" and card.change.pct == -round(needed * 100)
    assert card.stake == pytest.approx(10_000 * needed, abs=1)
    assert not run(1.85, 4.0)                                          # inside the 10% dead-zone
    assert not run(1.5, 9.5)                                           # already below the fold
    assert run(1.5, 9.0)
    assert not run(1.5, 4.0, spend=4_999)                              # cut needs more evidence
    exit1 = run(0.9, 1.5)[0]
    assert exit1.title == "Decrease Bid (step 1 of exit)" and exit1.change.pct == -30
    assert "top-slot" in exit1.targets
    assert run(1.1, 4.0)[0].change.pct == -30 and run(1.1, 4.0)[0].title == "Decrease Bid"   # > be: no exit
    # new-user top terms are never cut; top-volume terms get a 20% dead-zone
    own = [kw("t", "c1", 10_000, 1.5, nu=9.0)] + [kw(f"o{i}", "c1", 1_000, 2.0, nu=0.0) for i in range(3)]
    assert not run(1.5, 4.0, keywords=own)
    vol = [kw("t", "c1", 10_000, 1.7, vol=900.0)] + [kw(f"o{i}", "c1", 1_000, 2.0, vol=1.0) for i in range(3)]
    assert not run(1.7, 4.0, keywords=vol)                             # 15% short, top volume
    assert run(1.7, 4.0)                                               # 15% short, ordinary term
    assert run(1.7, 4.0, keywords=vol[:1] + [kw("o", "c1", 1_000, 2.0, vol=5_000.0)])[0].kw_priority == 0


def test_modal_position_changes_the_band():
    filler = kwm("filler", 1_000_000, 2.0, 3.0)
    def run(**kw_):
        c = ctx([camp()], keyword_market=[filler, kwm("t", 10_000, 4.0, 5.0, **kw_)], rule_profile="verdict")
        return [x for x in bid(c) if x.keywords[0].kw == "t"]
    assert not run()                                                   # mean position 5: visible
    assert run(position_mode=12.0)[0].title == "Increase Bid (visibility buy)"


def test_bid_abstains_without_position_and_caps_step_off_first_price():
    rows = [kwm("filler", 1_000_000, 2.0, 3.0), kwm("t", 10_000, 4.0, 8.0, live_bid=50.0)]
    no_pos = profile(position=PositionSemantics(available=False))
    assert not bid(ctx([camp()], keyword_market=rows, rule_profile="verdict", profile_=no_pos))
    second = profile(auction_type="second_price")
    card = one(bid(ctx([camp()], keyword_market=rows, rule_profile="verdict", profile_=second)), direction="up")
    assert card.change.pct == 10 and card.change.frm is None
    assert card.evidence["sizing"] == "fixed_step" and "bid_ceiling" not in card.evidence


def test_keyword_attributed_per_segment_and_one_card_per_term_per_campaign():
    cs = [camp("a", segment="seg-a"), camp("b", segment="seg-b"), camp("dead", segment="seg-a", status="PAUSED")]
    rows = [kw("shared", "a", 10_000, segment="seg-a"), kw("shared", "b", 90_000, segment="seg-b"),
            kw("shared", "dead", 500_000, segment="seg-a")]
    mk = [kwm("fill", 900_000, 3.0, 3.0, segment="seg-a"), kwm("fill", 900_000, 3.0, 3.0, segment="seg-b"),
          kwm("shared", 30_000, 0.5, 1.0, segment="seg-a"), kwm("shared", 30_000, 0.5, 1.0, segment="seg-b")]
    cards = bid(ctx(cs, keywords=rows, keyword_market=mk))
    assert sorted((c.entity_id, c.segment) for c in cards) == [("a", "seg-a"), ("b", "seg-b")]
    assert len({c.id for c in cards}) == 2
    dup = [kwm("fill", 900_000, 3.0, 3.0), kwm("x", 30_000, 0.5, 1.0), kwm("x", 40_000, 0.5, 1.0)]
    cards = bid(ctx([camp()], keywords=[kw("x", "c1", 10_000)], keyword_market=dup))
    assert len(cards) == 1


def test_head_term_pressure_variants():
    probe = {"head": {"g1": (120.0, 100.0), "g2": (111.0, 100.0), "g3": (130.0, 100.0), "g4": (100.0, 100.0),
                      "others": (500.0, 100.0)}}
    def run(dr, spend=50_000, probe_=probe, **kw_):
        rows = [kwm("head", spend, dr, 3.0), kwm("tail", 1_000, 2.0, 3.0)]
        return [c for c in bid(ctx([camp()], keyword_market=rows, head_cpm_by_geo=probe_, **kw_))
                if "Head" in c.title or "Hold" in c.title or "Yield" in c.title]
    hold = run(1.0)[0]
    assert hold.title == "Hold Bid Under Pressure" and hold.direction == "none" and hold.change is None
    assert hold.stake == pytest.approx(5_000) and hold.evidence["n_geos_up"] == 3
    yld = run(0.99)[0]
    assert yld.title == "Yield the Inflated Auction" and yld.change.pct == -15
    assert run(1.5, check=RunCheck("roas", ">=", 200, "pct"))[0].title == "Yield the Inflated Auction"
    watch = run(None)[0]
    assert watch.type == "watch" and "attribution incomplete" in watch.title
    assert not run(1.0, spend=49_999)
    assert not run(1.0, probe_=None)
    two = {"head": {"g1": (120.0, 100.0), "g2": (111.0, 100.0), "g3": (110.0, 100.0), "others": (500.0, 100.0)}}
    assert not run(1.0, probe_=two)                                    # aggregates are not geos; +10% exactly is not up


# ----------------------------------------------------------------- experiment

def test_experiment_gates():
    def run(ad_type=KW, **kw_):
        base = dict(bud=1000, spd=900, exh=599, roas=1.5, nd=14, sp=30_000)
        base.update(kw_)
        return experiment(ctx([camp(ad_type=ad_type)], telemetry=[tel(**base)],
                              keywords=[kw(f"k{i}", spend=1_000 * (i + 1)) for i in range(7)]))
    card = one(run(), family="experiment", type="experiment")
    assert card.direction == "none" and card.stake == pytest.approx(3_000)
    assert [k.kw for k in card.keywords] == ["k6", "k5", "k4", "k3", "k2"]
    assert card.evidence["pauses"] is True and card.evidence["days"] == 7
    assert not run(exh=600) and not run(exh=None)
    assert not run(spd=499)
    assert not run(roas=0.79) and run(roas=0.8) and run(roas=2.5) and not run(roas=2.51)
    assert not run(nd=13)
    assert not run(ad_type=RECO) and not run(ad_type=REACH)
    assert not experiment(ctx([camp()]))


# -------------------------------------------------------------------- sov-gap

def test_sov_under_invested_budget_and_bid_watch():
    seg = market(tot_c=1_000_000, own_c=300_000, sov=22.0)            # ms 30 - sov 22 = 8 pp, worth 80k
    cs = [camp("gen", roas=2.0, own=0.1), camp("brand", roas=5.0, own=0.9), camp("reco", roas=6.0, ad_type=RECO)]
    t = [tel("gen", exh=500), tel("brand", exh=500), tel("reco", exh=500)]
    card = one(sov_gap(ctx(cs, segments=seg, telemetry=t)), type="budget")
    assert card.entity_id == "gen" and card.change.pct == 15           # generic-heavy preferred over max ROAS
    assert card.stake == pytest.approx(1_000_000 * 8 / 100 * 0.1)
    # unknown own share reads as generic-heavy here
    cs2 = [camp("gen", roas=2.0, own=None), camp("brand", roas=5.0, own=0.9)]
    assert one(sov_gap(ctx(cs2, segments=seg, telemetry=t)), type="budget").entity_id == "gen"
    # efficient campaigns exist but none is held back by budget -> watch, not a raise
    watch = one(sov_gap(ctx(cs, segments=seg)), type="watch")
    assert watch.title == "Close the SOV Gap on Bids, Not Budget" and watch.entity_id is None
    assert not sov_gap(ctx(cs, segments=market(tot_c=1_000_000, own_c=300_000, sov=22.1), telemetry=t))
    assert not sov_gap(ctx(cs, segments=market(tot_c=600_000, own_c=180_000, sov=22.0), telemetry=t))  # worth 48k
    assert not sov_gap(ctx([camp("weak", roas=1.4)], segments=seg, telemetry=[tel("weak", exh=500)]))
    assert not sov_gap(ctx(cs, segments=market(tot_c=1_000_000, own_c=300_000, sov=None), telemetry=t))


def test_sov_over_invested_watch():
    seg = market(tot_c=1_000_000, own_c=200_000, sov=30.0)
    cs = [camp("a", roas=1.5, spend=60_000), camp("b", roas=2.4, spend=40_000), camp("f", roas=None, spend=90_000, ad_type=REACH)]
    card = one(sov_gap(ctx(cs, segments=seg)), type="watch")
    assert card.title == "Over-invested vs Share" and card.stake == pytest.approx(100_000 * 0.15)
    assert card.evidence["blended"] == pytest.approx(1.86)
    assert not sov_gap(ctx([camp("a", roas=2.0)], segments=seg))
    assert not sov_gap(ctx(cs, segments=market(tot_c=1_000_000, own_c=200_000, sov=29.9)))


# ------------------------------------------------------------------------ geo

def _geo_rows(weak_sales, weak_spend=20_000.0, name="g-weak"):
    return [EntityGeoStat("c1", "g1", 30_000, 90_000), EntityGeoStat("c1", "g2", 30_000, 90_000),
            EntityGeoStat("c1", name, weak_spend, weak_sales), EntityGeoStat("c1", "tiny", 2_000, 0)]


GEO_TABLE = [GeoMarketStat("g-weak", 1_000_000, 20.0, 0.9), GeoMarketStat("g1", 1_100_000, 21.0, 0.9),
             GeoMarketStat("near", 950_000, 20.5, 0.9), GeoMarketStat("far", 100_000, 40.0, 0.5),
             GeoMarketStat("alike", 1_000_000, 20.0, None), GeoMarketStat("others", 1_000_000, 20.0, 0.9)]


def test_geo_decisive_split_vs_experiment():
    c = ctx([camp(spend=80_000)], entity_geo=_geo_rows(10_000))        # roas 0.5 vs avg 2.375
    card = one(geo(c), family="geo", type="geo")
    avg = 190_000 / 80_000
    assert card.direction == "down" and card.entity_id == "c1" and card.evidence["geo"] == "g-weak"
    assert card.stake == pytest.approx(20_000 * (avg - 0.5))
    c = ctx([camp(spend=80_000)], entity_geo=_geo_rows(24_000), geo_market=GEO_TABLE)   # 1.2 vs avg 2.55
    card = one(geo(c), family="experiment", type="experiment")
    assert card.stake == pytest.approx(20_000 * 0.2) and card.evidence["days"] == 14
    assert card.evidence["controls"] == ["near", "alike"]              # not g1 (served), not the bucket
    assert not [x for x in geo(ctx([camp(spend=80_000)], entity_geo=_geo_rows(40_000)))
                if x.entity_id]                                        # 2.0 vs 2.75: not weak enough


def test_geo_qualification_and_buckets_are_not_geos():
    assert not geo(ctx([camp(spend=80_000)], entity_geo=_geo_rows(10_000, name="others")))
    assert not geo(ctx([camp(spend=80_000)], entity_geo=_geo_rows(10_000, weak_spend=10_000)))  # < 15% of spend
    rows = [EntityGeoStat("c1", "g1", 30_000, 90_000), EntityGeoStat("c1", "g-weak", 20_000, 5_000)]
    assert not geo(ctx([camp()], entity_geo=rows))                     # fewer than 3 rows
    losing = [EntityGeoStat("c1", "g1", 30_000, 20_000), EntityGeoStat("c1", "g2", 30_000, 20_000),
              EntityGeoStat("c1", "g-weak", 20_000, 1_000)]
    assert not geo(ctx([camp()], entity_geo=losing))                   # campaign average below breakeven
    assert not geo(ctx([camp(status="PAUSED")], entity_geo=_geo_rows(10_000)))
    assert not geo(ctx([camp(pure=False)], entity_geo=_geo_rows(10_000)))


def test_control_geos_distance():
    c = ctx(geo_market=GEO_TABLE)
    assert control_geos(c, "g-weak", set()) == ["near", "g1"]
    assert control_geos(c, "g-weak", {"alike", "near"}) == ["g1", "far"]
    assert control_geos(c, "g-weak", {"near", "g1"}) == ["alike", "far"]   # missing availability reads 0.85
    assert control_geos(c, "unknown", set()) == []


def test_geo_expand_classic_vs_verdict():
    table = [GeoMarketStat("big", 6_000_000, 30.0, 0.95), GeoMarketStat("low", 2_000_000, 20.0, 0.95),
             GeoMarketStat("low-oos", 1_500_000, 20.0, 0.84), GeoMarketStat("low-unknown", 1_000_000, 20.0, None),
             GeoMarketStat("tiny", 100_000, 5.0, 0.95), GeoMarketStat("others", 2_000_000, 10.0, 0.95)]
    total = sum(z.total_sales for z in table)
    blend = sum(z.own_share_pct * z.total_sales for z in table) / total
    classic = geo(ctx(geo_market=table))
    card = one(classic, family="geo")
    assert card.title == "Target Geo (expand): low" and card.entity_id is None and card.segment is None
    assert card.stake == pytest.approx(2_000_000 * (blend - 20.0) / 100, abs=1)
    verdict = geo(ctx(geo_market=table, rule_profile="verdict"))
    assert {c.evidence["geo"] for c in verdict} == {"low", "low-oos", "low-unknown"}
    assert len({c.id for c in verdict}) == 3
    near = [GeoMarketStat("a", 1_000_000, 30.0, 0.95), GeoMarketStat("b", 1_000_000, 25.0, 0.95)]
    assert not geo(ctx(geo_market=near))                               # 2.5 pp under the blend


# ------------------------------------------------------------------ collision

def _col(eid, geo_="g1", spend=20_000.0, sales=40_000.0, pids=("p1",), keyword="alpha"):
    return CollisionRow(keyword, eid, geo_, spend, sales, frozenset(pids) if pids else None, SEG)


def test_same_geo_overlap_collides_best_converter_leads():
    rows = [_col("a", sales=30_000), _col("b", sales=80_000), _col("c", spend=30_000, sales=30_000),
            _col("a", "g2", spend=50_000, sales=50_000), _col("b", "g2", spend=11_000, sales=30_000)]
    c = ctx([camp("a"), camp("b"), camp("c")], collisions=rows)
    col = find_collisions(c)[0]
    assert col.waste == pytest.approx(20_000 + 30_000 + 50_000)
    card = one(collision(c), family="collision", type="keyword")
    assert card.evidence["cids"][0] == "b" and card.evidence["keep"] == "b"
    assert card.evidence["geo"] == "g1" and card.evidence["geos_up"] == 2 and card.evidence["n_campaigns"] == 3
    assert card.entity_id == "c" and card.direction == "down"
    assert card.stake == pytest.approx(100_000 * 0.15)
    assert card.id == one(collision(ctx([camp("a"), camp("b"), camp("c")], collisions=rows))).id


def test_collisions_need_shared_auction_shared_product_and_spend():
    live = [camp("a"), camp("b")]
    assert not collision(ctx(live, collisions=[_col("a", "g1"), _col("b", "g2")]))             # geo fleet
    assert not collision(ctx(live, collisions=[_col("a", pids=("p1",)), _col("b", pids=("p2",))]))  # shelf occupation
    assert collision(ctx(live, collisions=[_col("a", pids=("p1", "p3")), _col("b", pids=("p3",))]))
    assert collision(ctx(live, collisions=[_col("a", pids=None), _col("b", pids=("p2",))]))    # cannot judge: keep
    assert not collision(ctx(live, collisions=[_col("a"), _col("b", spend=9_999)]))
    assert not collision(ctx(live, collisions=[_col("a", "others"), _col("b", "others")]))
    assert not collision(ctx(live, collisions=[_col("a", keyword="not targeted"), _col("b", keyword="not targeted")]))
    assert not collision(ctx([camp("a"), camp("b", status="PAUSED")], collisions=[_col("a"), _col("b")]))
    three = [_col("a", pids=("p1",)), _col("b", pids=("p1",)), _col("c", pids=("p9",))]
    card = one(collision(ctx(live + [camp("c")], collisions=three)))
    assert card.evidence["n_campaigns"] == 2 and "c" not in card.evidence["cids"]
    single = [_col("a", None), _col("b", None)]                        # single-auction platform
    assert one(collision(ctx(live, collisions=single))).evidence["geo"] is None


# ------------------------------------------------------- playbook over a run

def test_run_families_subset_and_unknown():
    c = ctx([camp(roas=0.5, spend=40_000)])
    assert [x.family for x in run_families(c, ["efficiency"]).cards] == ["efficiency-cut"]
    assert run_families(c, ["growth"]).cards == []
    with pytest.raises(ValueError):
        run_families(c, ["nope"])
