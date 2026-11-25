"""Geo quadrants, tiers, posture, drilldown, money at stake, product x geo map."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cpm_lineage.config import AvailabilityPolicy
from cpm_lineage.geo import (GeoConfig, account_posture, availability_standing, cell_decision, competitor_products,
                             geo_candidates, geo_drill, geo_quadrants, geo_roas, geo_tier, impact_sizing,
                             investment_tier, product_geo_map, quadrant, rank_label)
from cpm_lineage.schema import MarketRow, OfftakeRow, PerfRow, ShelfRow, Window, WindowPair

D0 = date(2026, 1, 1)
PAIR = WindowPair(Window(D0, D0 + timedelta(days=9)), Window(D0 + timedelta(days=10), D0 + timedelta(days=19)))
CUR, PRI = D0 + timedelta(days=15), D0 + timedelta(days=5)


# ----------------------------------------------------------------- quadrant / tier / posture

@pytest.mark.parametrize("share, nat, sov, key, posture", [
    (30, 30, 30, "Q1", "Defend"),          # both at the boundary count as high
    (30, 30, 29.9, "Q2", "Harvest"),
    (29.9, 30, 29.9, "Q3", "Convert"),     # voice is compared with the geo's OWN share
    (29.9, 30, 20, "Q4", "Build"),
    (40, 30, None, "Q2", "Harvest"),       # no impression feed -> voice low
    (0, 30, 5, "Q4", "Build"),             # zero share can never be "voice high"
])
def test_quadrant_cells(share, nat, sov, key, posture):
    k, label, post, levers = quadrant(share, nat, sov)
    assert (k, post) == (key, posture) and label.startswith(key) and len(levers) == 4


def test_quadrant_exit_variant():
    assert quadrant(10, 30, 5, -5.1)[1:3] == ("Q4 Under-Invested (Exit)", "Exit")
    assert quadrant(10, 30, 5, -5.1)[3] == ("Deprioritise spend",)
    assert quadrant(10, 30, 5, -5.0)[2] == "Build"
    assert quadrant(40, 30, 50, -50)[0] == "Q1"            # only Q4 can exit


def test_availability_standing():
    s = availability_standing([(0.90, False), (0.95, True), (0.80, False)])
    assert (s.rank, s.n_brands, s.p90) == (1, 3, 0.95)
    assert availability_standing([(0.9, True), (0.8, False)]) is None       # fewer than 3 brands
    assert availability_standing([(0.9, False)] * 3) is None                # no own row
    many = [(1.0 - i / 100, i == 20) for i in range(21)]
    s = availability_standing(many)
    assert s.rank == 21 and s.p90 == pytest.approx(0.98)                    # sorted[(21 - 1) // 10]


def test_geo_tier_first_match():
    assert geo_tier(0.84, 5.0, True, 1.0, 2.0, 0.85)[0] == "Fix first"
    assert geo_tier(0.85, 0.9, True, 1.0, 2.0, 0.85) == ("Fix first", "clean up ROAS before investing: "
                                                                      "bottom quartile of geos")
    assert geo_tier(0.85, 2.1, True, 1.0, 2.0, 0.85)[0] == "Scale"
    assert geo_tier(0.85, 2.0, True, 1.0, 2.0, 0.85)[0] == "Build"          # must exceed the median
    assert geo_tier(0.85, 2.1, False, 1.0, 2.0, 0.85)[0] == "Build"
    assert geo_tier(None, None, True, None, None, 0.85)[0] == "Build"       # every gate skipped
    assert geo_tier(0.5, 5.0, True, 1.0, 2.0, None)[0] == "Scale"           # availability policy off
    st = availability_standing([(0.5, True), (0.4, False), (0.3, False)])
    assert "geo-wide" in geo_tier(0.5, 5.0, True, 1.0, 2.0, 0.85, st)[1]


@pytest.mark.parametrize("dist, pg, growth, posture", [
    ({"Q1": 61}, 51, None, "Defending"),
    ({"Q1": 60}, 75, None, "Gaining"),
    ({"Q1": 60, "Q4": 40}, 51, 20, "Unclassified"),
    ({"Q4": 41, "Q1": 20, "Q3": 30}, 51, 15.1, "Building"),
    ({"Q4": 41, "Q1": 20, "Q3": 30}, 51, 15.0, "Unclassified"),
    ({"Q1": 30, "Q3": 21}, 49, None, "Stalling"),
    ({"Q2": 30, "Q4": 21}, 60, None, "Under Pressure"),
    ({"Q1": 50, "Q3": 0, "Q2": 50}, 24, None, "Under Pressure"),
    ({"Q1": 50, "Q2": 50}, 50, None, "Unclassified"),
])
def test_account_posture(dist, pg, growth, posture):
    assert account_posture(dist, pg, growth) == posture


def test_rank_label():
    assert [rank_label(v) for v in (25.1, 25, 10, 9.9, 3, 2.9, None)] == [
        "Category Leader", "Contender", "Contender", "Mid-Tier", "Mid-Tier", "Niche/Emerging", "Niche/Emerging"]


# ----------------------------------------------------------------- geo_quadrants end to end

def _geo_world():
    off, shelf, market, perf = [], [], [], []
    spec = {   # geo: (own cur, seg cur, own prev, seg prev, own ad, all ad, availability, roas)
        "g-lead": (500, 1000, 400, 1000, 600, 1000, 0.95, 4.0),
        "g-org": (450, 1000, 400, 1000, 100, 1000, 0.95, 3.0),
        "g-ads": (100, 1000, 120, 1000, 300, 1000, 0.95, 2.0),
        "g-low": (50, 1000, 60, 1200, 20, 1000, 0.70, 1.0),
        "others": (400, 4000, 400, 4000, 100, 1000, 0.95, 2.5),
    }
    for geo, (oc, sc, op, spv, oad, aad, av, roas) in spec.items():
        off += [OfftakeRow(CUR, "own", True, oc, geo=geo), OfftakeRow(CUR, "rival", False, sc - oc, geo=geo),
                OfftakeRow(PRI, "own", True, op, geo=geo), OfftakeRow(PRI, "rival", False, spv - op, geo=geo)]
        shelf += [ShelfRow(CUR, "p-own", geo, av * 100, 100, brand="own"),
                  ShelfRow(PRI, "p-own", geo, 95, 100, brand="own"),
                  ShelfRow(CUR, "p-r1", geo, 80, 100, is_own=False, brand="r1"),
                  ShelfRow(CUR, "p-r2", geo, 85, 100, is_own=False, brand="r2")]
        market += [MarketRow(CUR, "k", "own", True, oad, 50, geo=geo),
                   MarketRow(CUR, "k", "rival", False, aad - oad, 150, geo=geo)]
        perf.append(PerfRow(CUR, "c", 100.0, 1000.0, sales=100.0 * roas, geo=geo))
    return off, shelf, market, perf


def test_geo_quadrants_end_to_end(synthetic_profile):
    off, shelf, market, perf = _geo_world()
    res = geo_quadrants(off, PAIR, shelf_rows=shelf, market_rows=market, perf_rows=perf, profile=synthetic_profile)
    assert res.national_share_pct == pytest.approx(100 * 1500 / 8000)       # includes the aggregate bucket
    assert res.national_ad_sov_pct == pytest.approx(100 * 1120 / 5000)
    assert res.availability_floor == 0.85                                   # national share 18.75 % > 10
    by = {g.geo: g for g in res.geos}
    assert set(by) == {"g-lead", "g-org", "g-ads", "g-low"}                 # "others" is not ranked
    assert [by[g].quadrant for g in ("g-lead", "g-org", "g-ads", "g-low")] == ["Q1", "Q2", "Q3", "Q4"]
    assert by["g-low"].quadrant_label.endswith("(Exit)")                    # segment shrank 16.7 %
    assert by["g-lead"].share_trend_pp == pytest.approx(10.0)
    assert by["g-lead"].organic_sov_pct == pytest.approx(100 * 50 / 1200)
    assert by["g-lead"].availability_rank == 1 and by["g-lead"].availability_n_brands == 3
    assert by["g-low"].availability == pytest.approx(0.70) and by["g-low"].availability_delta == pytest.approx(-0.25)
    assert by["g-low"].tier == "Fix first" and by["g-low"].availability_rank == 3
    assert by["g-lead"].tier == "Scale" and by["g-org"].tier == "Scale" and by["g-ads"].tier == "Build"
    assert sum(res.quadrant_dist.values()) == pytest.approx(100.0)
    assert res.quadrant_dist["Q1"] == pytest.approx(100 * 500 / 1100)       # share of OWN SALES, not geo count
    assert res.gaining_pct == pytest.approx(50.0)
    assert res.classification.rank == "Contender" and res.classification.posture == "Unclassified"   # exactly 50 % gaining


def test_geo_quadrants_degrades(synthetic_profile):
    off, shelf, market, perf = _geo_world()
    assert geo_quadrants(None).reason
    res = geo_quadrants(off, PAIR, profile=synthetic_profile)
    assert {g.quadrant for g in res.geos} <= {"Q2", "Q4"}                   # no impressions -> voice low
    assert all(g.tier == "Build" and g.roas is None and g.availability is None for g in res.geos)
    off_policy = geo_quadrants(off, PAIR, shelf_rows=shelf, perf_rows=perf, profile=synthetic_profile,
                               availability=AvailabilityPolicy(enabled=False))
    assert off_policy.availability_floor is None
    assert {g.geo: g.tier for g in off_policy.geos}["g-low"] == "Fix first"      # now on ROAS, not supply
    three = [o for o in off if o.geo != "g-low"]
    r3 = geo_quadrants(three, PAIR, perf_rows=perf, profile=synthetic_profile)
    assert r3.roas_p25 == r3.roas_p50                                       # < 4 geos: quartile collapses


def test_geo_quadrants_own_anchor_and_rollup(synthetic_dataset):
    d = synthetic_dataset
    res = geo_quadrants(d.offtake, shelf_rows=d.shelf, market_rows=d.market, perf_rows=d.perf, profile=d.profile)
    assert {g.geo for g in res.geos} == {"geo-north", "geo-south", "geo-east", "geo-west"}
    assert all(g.ad_sov_pct is None for g in res.geos)                      # the feed has no geo grain
    assert res.national_ad_sov_pct is not None
    south = next(g for g in res.geos if g.geo == "geo-south")
    assert south.availability_delta < -0.05                                 # the injected availability drop


def test_shelf_weight_floor_and_inactive():
    shelf = [ShelfRow(CUR, "a", "g1", 10, 10, weight=9.0), ShelfRow(CUR, "b", "g1", 0, 10, weight=0.2),
             ShelfRow(CUR, "c", "g1", 0, 10, weight=50.0, is_active=False)]
    off = [OfftakeRow(CUR, "own", True, 10.0, geo="g1"), OfftakeRow(PRI, "own", True, 10.0, geo="g1")]
    res = geo_quadrants(off, PAIR, shelf_rows=shelf)
    assert res.geos[0].availability == pytest.approx(90 / (90 + 10 + 10))


# ----------------------------------------------------------------- drilldown

def test_investment_tier_table():
    assert investment_tier(0.59, 9.0, 1.0, 2.0) == "FIX-FIRST"              # overrides everything
    assert investment_tier(0.60, None, 1.0, 2.0) is None
    assert investment_tier(0.80, 2.0, 1.0, 2.0) == "SCALE"
    assert investment_tier(0.79, 2.0, 1.0, 2.0) == "BUILD"
    assert investment_tier(None, 2.0, 1.0, 2.0) == "SCALE"                  # unknown availability counts as fine
    assert investment_tier(0.9, 1.0, 1.0, 2.0) == "BUILD"
    assert investment_tier(0.9, 0.99, 1.0, 2.0) == "HOLD"
    assert investment_tier(0.3, 9.0, 1.0, 2.0, AvailabilityPolicy(enabled=False)) == "BUILD"   # block off; SCALE still needs supply


def _drill_shelf():
    rows = []
    for geo, h, w, comp, checks in [("g1", 0.9, 0.5, 0.8, 100), ("g2", 0.9, 0.8, 0.9, 300), ("g3", None, 0.9, 0.7, 50),
                                    ("others", 0.9, 0.1, 0.9, 500)]:
        if h is not None:
            rows.append(ShelfRow(PRI, "own-1", geo, h * checks, checks, price=100.0))
        rows.append(ShelfRow(CUR, "own-1", geo, w * checks, checks, price=110.0))
        rows.append(ShelfRow(CUR, "riv-1", geo, comp * 100, 100, price=90.0, is_own=False))
        rows.append(ShelfRow(CUR, "riv-x", geo, 0, 100, price=10.0, is_own=False))
    return rows


def test_geo_drill_impact_order_and_tiers(synthetic_profile):
    res = geo_drill(_drill_shelf(), PAIR, {"own-1"}, {"riv-1"}, profile=synthetic_profile,
                    roas_by_geo={"g1": 3.0, "g2": 2.0, "g3": 1.0})
    assert [r.geo for r in res.rows] == ["g1", "g2", "g3"]                  # aggregate bucket skipped
    g1, g2, g3 = res.rows
    total = 200 + 600 + 50
    assert g1.drop == pytest.approx(0.4) and g1.weight == pytest.approx(200 / total)
    assert g1.impact == pytest.approx(g1.weight * 0.4) and g1.impact > g2.impact > g3.impact
    assert g1.gap == pytest.approx(0.5 - 0.8) and g1.comp == pytest.approx(0.8)     # riv-x is not a competitor here
    assert g1.own_price == 110.0 and g1.comp_price == 90.0
    assert g3.h_own is None and g3.drop == 0.0                              # missing bucket -> drop forced to 0
    assert sum(r.weight for r in res.rows) == pytest.approx(1.0)
    assert (res.roas_p25, res.roas_p50) == (1.0, 2.0)                       # vals[3 // 4], vals[3 // 2]
    assert [r.tier for r in res.rows] == ["FIX-FIRST", "SCALE", "BUILD"]
    assert all(r.tier is None for r in geo_drill(_drill_shelf(), PAIR, {"own-1"}, {"riv-1"}).rows)
    assert geo_drill([], PAIR).reason


def test_geo_drill_top_n_and_helpers(synthetic_dataset, windows_30):
    d = synthetic_dataset
    roas = geo_roas(d.perf, windows_30.current, d.profile)
    assert "others" not in roas and len(roas) == 4
    res = geo_drill(d.shelf, windows_30, roas_by_geo=roas, profile=d.profile, cfg=GeoConfig(drill_top_n=2))
    assert len(res.rows) == 2 and res.rows[0].geo == "geo-south"            # the injected availability drop
    assert res.rows[0].drop > 0.05 and res.rows[0].gap < 0
    mk = [MarketRow(CUR, "Kw One", "r", False, 1, product_id="riv-1"),
          MarketRow(CUR, "kw one", "o", True, 1, product_id="own-1"),
          MarketRow(CUR, "other", "r", False, 1, product_id="riv-2")]
    assert competitor_products(mk, ["kw one"], {"own-1"}) == {"riv-1"}


# ----------------------------------------------------------------- money at stake

def test_geo_candidates_table():
    base = dict(geo="g", segment_sales=100000.0, share_pct=20.0)
    c = geo_candidates({**base, "share_trend_pp": -2.0, "availability": 0.69})
    assert (c[0].code, c[0].tier, c[0].kind, c[0].money) == ("share-loss-supply", "CRITICAL", "Bleed", 2000.0)
    assert geo_candidates({**base, "share_trend_pp": -2.0, "availability": 0.70})[0].code == "share-loss"
    assert geo_candidates({**base, "share_trend_pp": -2.0, "availability": 65})[0].code == "share-loss-supply"
    c = geo_candidates({**base, "share_trend_pp": 1.0, "organic_sov_pct": 25.0}, segment_growth_pct=10.1)
    assert [x.code for x in c] == ["recall-gap", "segment-growth", "run-rate"]      # tier first, then money
    assert c[0].money == pytest.approx(5000.0) and c[2].money == pytest.approx(2000.0)
    assert c[1].money == pytest.approx(100000 * 0.101 * 0.20)
    assert geo_candidates({**base, "share_trend_pp": 0.0}, segment_growth_pct=10.0) == []
    assert all("[" in x.basis for x in c)


def test_impact_sizing_bleed_not_masked_and_rollup():
    geos = [
        {"geo": "small-bleed", "segment_sales": 10000.0, "share_pct": 10.0, "share_trend_pp": -1.0,
         "organic_sov_pct": 60.0},                                   # bleed 100 vs blocked 5000
        {"geo": "big-capture", "segment_sales": 1_000_000.0, "share_pct": 10.0, "share_trend_pp": 5.0},
        {"geo": "blocked", "segment_sales": 50000.0, "share_pct": 10.0, "share_trend_pp": 0.0,
         "organic_sov_pct": 20.0},
        {"geo": "nothing", "segment_sales": 50000.0, "share_pct": 10.0, "share_trend_pp": 0.0},
    ]
    res = impact_sizing(geos)
    assert [g.geo for g in res.geos] == ["small-bleed", "blocked", "big-capture"]
    assert res.geos[0].primary.kind == "Bleed" and len(res.geos[0].candidates) == 2
    assert res.highest_impact.geo == "small-bleed"
    assert res.by_kind == {"Bleed": 100.0, "Blocked": 5000.0, "Capturable": 100000.0}    # primary money only
    assert res.tier_counts == {"CRITICAL": 1, "NEEDS ATTENTION": 1, "ON TRACK": 1}
    assert impact_sizing(None).reason and impact_sizing([geos[3]]).reason


def test_impact_sizing_accepts_quadrant_rows(synthetic_dataset, windows_30):
    d = synthetic_dataset
    q = geo_quadrants(d.offtake, windows_30, shelf_rows=d.shelf, perf_rows=d.perf, profile=d.profile)
    res = impact_sizing(q.geos, q.national_segment_growth_pct)
    assert res.highest_impact.geo == "geo-south" and res.highest_impact.primary.kind == "Bleed"


# ----------------------------------------------------------------- product x geo map

@pytest.mark.parametrize("priority, strong, roas, decision", [
    (True, False, None, "INVEST"), (True, False, 1.0, "INVEST"), (True, False, 0.99, "FIX FIRST"),
    (True, True, 0.1, "DEFEND"), (False, True, None, "HARVEST"), (False, False, 9.0, "DEPRIORITISE"),
])
def test_cell_decision(priority, strong, roas, decision):
    assert cell_decision(priority, strong, roas) == decision


def test_cell_decision_blank_without_data():
    assert cell_decision(True, False, None, has_data=False) == ""


def test_product_geo_map(synthetic_profile):
    off, market, perf = [], [], []
    sales = {"p1": 500, "p2": 400, "p3": 300, "p4": 200}
    for pid, v in sales.items():
        off.append(OfftakeRow(CUR, "own", True, float(v), product_id=pid, geo="g1"))
    off.append(OfftakeRow(CUR, "own", True, 50.0, product_id="p1", geo="g2"))
    off.append(OfftakeRow(CUR, "own", True, 9999.0, product_id="p1", geo="others"))
    market += [MarketRow(CUR, "generic kw", "own", True, 0, 100, product_id="p1", geo="g1", organic_position=12.0),
               MarketRow(CUR, "generic kw", "own", True, 0, 100, product_id="p2", geo="g1", organic_position=13.0),
               MarketRow(CUR, "generic kw", "own", True, 0, 100, product_id="p4", geo="g1", organic_position=3.0),
               MarketRow(CUR, "brand kw", "own", True, 0, 100, product_id="p3", geo="g1", organic_position=1.0,
                         unit_role="own_brand"),
               MarketRow(CUR, "generic kw", "own", True, 0, 100, product_id="p3", geo="g1", organic_position=900.0)]
    perf += [PerfRow(CUR, "c", 100.0, 1000.0, sales=250.0, product_id="p2", geo="g1"),
             PerfRow(CUR, "c", 100.0, 1000.0, sales=50.0, product_id="p3", geo="g1")]
    m = product_geo_map(off, PAIR.current, market_rows=market, perf_rows=perf, profile=synthetic_profile)
    assert m.geos == ("g1", "g2") and m.products == ("p1", "p2", "p3", "p4")
    assert m.cell("p1", "g1").decision == "DEFEND"            # priority, organic position 12 == page-one depth
    assert m.cell("p2", "g1").decision == "INVEST"            # priority, weak organic, ROAS 2.5
    assert m.cell("p3", "g1").decision == "FIX FIRST"         # priority, no valid generic position, ROAS 0.5
    assert m.cell("p4", "g1").decision == "HARVEST"           # not a priority, strong organic
    assert m.cell("p2", "g2").decision == ""                  # no data at all
    assert m.cell("p1", "g2").decision == "INVEST"
    assert product_geo_map([], PAIR.current).reason


def test_stalling_has_separate_mix_and_gaining_thresholds():
    dist = {"Q1": 40.0, "Q3": 30.0, "Q2": 20.0, "Q4": 10.0}
    assert account_posture(dist, 40.0, 5.0) == "Stalling"
    # raising only the quadrant-mix threshold must not move the gaining threshold with it
    assert account_posture(dist, 40.0, 5.0, cfg=GeoConfig(posture_stall_pct=60.0)) == "Stalling"
    assert account_posture(dist, 40.0, 5.0, cfg=GeoConfig(posture_stall_gaining_pct=30.0)) != "Stalling"
