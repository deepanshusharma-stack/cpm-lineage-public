"""Lever set, panel building, feature preparation and the collinearity guard."""

from datetime import date, timedelta

import numpy as np
import pytest

from cpm_lineage.attribution import (LEVERS, SHORT_KEEP, TIER1, TIER2, AttributionConfig, build_lever_panel,
                                     fold_price_cluster, prepare_levers, raw_facts, sanity, vif)
from cpm_lineage.schema import MarketRow, PerfRow, ShelfRow, TargetingUnit

D0 = date(2026, 1, 1)


def test_lever_set_shape():
    assert len(LEVERS) == 11 and len(TIER1) == 8 and len(TIER2) == 3
    assert TIER2 == {"log_category_impressions", "comp_discount", "comp_availability"}
    assert SHORT_KEEP <= set(LEVERS)


def test_panel_formulas_hand_built():
    perf = [
        PerfRow(D0, "c1", spend=100.0, impressions=1000.0, sales=300.0, units=3.0, targeting_unit="snack",
                bid=50.0, position=2.0, product_id="p1"),
        PerfRow(D0, "c1", spend=300.0, impressions=3000.0, sales=300.0, units=3.0, targeting_unit="rival x",
                bid=90.0, position=4.0, product_id="p1"),
        PerfRow(D0, "c1", spend=50.0, impressions=500.0, sales=0.0, units=0.0, targeting_unit="home feed",
                bid=10.0, position=999.0, product_id="p1"),
        PerfRow(D0, "c2", spend=999.0, impressions=1.0, sales=1.0, units=1.0, targeting_unit="snack"),
    ]
    tus = [TargetingUnit("snack", role="generic"), TargetingUnit("rival x", role="competitor"),
           TargetingUnit("home feed", role="placement", is_placement=True)]
    market = [
        MarketRow(D0, "snack", "own", True, ad_impressions=30.0, organic_impressions=10.0, total_impressions=100.0),
        MarketRow(D0, "snack", "riv", False, ad_impressions=70.0, organic_impressions=30.0, total_impressions=300.0),
        MarketRow(D0, "unrelated", "riv", False, ad_impressions=1e6, organic_impressions=1e6),
    ]
    shelf = [
        ShelfRow(D0, "p1", available_count=9.0, check_count=10.0, price=80.0, list_price=100.0, weight=3.0),
        ShelfRow(D0, "p-other", available_count=0.0, check_count=10.0, price=1.0, list_price=1.0),
        ShelfRow(D0, "r1", available_count=5.0, check_count=10.0, price=100.0, list_price=125.0, is_own=False),
    ]
    p = build_lever_panel(perf, market_rows=market, shelf_rows=shelf, targeting_units=tus, entity_id="c1")
    assert p.dates == [D0] and p.spend[0] == 450.0
    lv = {k: v[0] for k, v in p.levers.items()}
    assert lv["bid"] == pytest.approx((100 * 50 + 300 * 90 + 50 * 10) / 450)
    assert lv["position"] == pytest.approx((1000 * 2 + 3000 * 4) / 4000)        # sentinel 999 excluded
    assert lv["competitor_share"] == pytest.approx(300 / 400)                    # placement spend excluded
    assert lv["own_paid_sov"] == pytest.approx(0.3) and lv["own_organic_sov"] == pytest.approx(0.25)
    assert lv["log_category_impressions"] == pytest.approx(np.log(400.0))
    assert lv["own_availability"] == pytest.approx(0.9)                          # only the advertised product
    assert lv["comp_availability"] == pytest.approx(0.5)
    assert lv["own_discount"] == pytest.approx(0.2) and lv["comp_discount"] == pytest.approx(0.2)
    assert lv["price_gap"] == pytest.approx(0.8)


def test_panel_degrades_without_optional_feeds(make_perf_row):
    p = build_lever_panel([make_perf_row(day=i) for i in range(5)])
    assert len(p) == 5 and p.model_mask.all()
    for f in ("own_paid_sov", "own_availability", "price_gap", "log_category_impressions", "bid", "position"):
        assert np.isnan(p.levers[f]).all()
    assert (p.levers["competitor_share"] == 0).all()                              # missing -> 0, by definition
    prep = prepare_levers(p.levers)
    assert prep.live == [] and prep.thin


def test_velocity_weighted_availability(make_perf_row):
    perf = [make_perf_row(day=0)]
    d = perf[0].date
    shelf = [ShelfRow(d, "fast", available_count=0.0, check_count=10.0, weight=9.0),
             ShelfRow(d, "slow", available_count=10.0, check_count=10.0, weight=1.0),
             ShelfRow(d, "dead", available_count=10.0, check_count=10.0, weight=50.0, is_active=False)]
    p = build_lever_panel(perf, shelf_rows=shelf)
    assert p.levers["own_availability"][0] == pytest.approx((0 * 9 + 10 + 10) / (90 + 10 + 10))


def test_sanity_ranges():
    assert np.isnan(sanity("position", [0.5, 501.0, 106978.0])).all()
    assert not np.isnan(sanity("position", [1.0, 500.0])).any()
    assert np.isnan(sanity("own_availability", [-0.01, 1.01])).all()
    assert np.isnan(sanity("own_discount", [-0.51, 0.96])).all() and not np.isnan(sanity("own_discount", [-0.5, 0.95])).any()
    assert np.isnan(sanity("price_gap", [0.09, 10.1, np.inf])).all()
    assert np.isnan(sanity("bid", [0.0, -1.0])).all()


def _base(n=40, seed=0):
    rng = np.random.default_rng(seed)
    cols = {f: rng.uniform(0.2, 0.8, n) for f in LEVERS}
    cols["position"] = rng.uniform(1.0, 10.0, n)
    return cols, rng


def test_coverage_30pct_is_live_and_45pct_null_is_suppressed():
    cols, _ = _base(40)
    cols["own_paid_sov"] = np.where(np.arange(40) < 12, cols["own_paid_sov"], 0.0)      # 30% real, rest zero
    cols["own_organic_sov"] = np.where(np.arange(40) < 8, cols["own_organic_sov"], 0.0)  # 20%: not > 0.2
    cols["own_availability"] = np.where(np.arange(40) < 18, np.nan, cols["own_availability"])  # 45% null
    cols["comp_availability"] = np.where(np.arange(40) < 16, np.nan, cols["comp_availability"])  # 40% null: kept
    prep = prepare_levers(cols)
    assert prep.coverage["own_paid_sov"] == pytest.approx(0.30) and "own_paid_sov" in prep.live
    assert "own_organic_sov" not in prep.live and "own_organic_sov" not in prep.suppressed
    assert prep.suppressed == ["own_availability"] and "own_availability" not in prep.live
    assert "comp_availability" in prep.live
    assert not np.isnan(prep.matrix()).any()
    # imputation = column median of the real values
    real = cols["own_availability"][18:]
    assert prep.filled["own_availability"][0] == pytest.approx(np.median(real))
    assert np.isnan(prep.raw["own_availability"][0])


def test_true_zero_discount_counts_as_covered_but_zero_mix_does_not():
    cols, _ = _base(30)
    cols["own_discount"] = np.zeros(30)
    cols["competitor_share"] = np.zeros(30)
    prep = prepare_levers(cols)
    assert prep.coverage["own_discount"] == 1.0 and prep.coverage["competitor_share"] == 0.0
    assert "competitor_share" not in prep.live


def test_short_mode_and_thin():
    cols, _ = _base(30)
    prep = prepare_levers(cols, short=True)
    assert set(prep.live) <= SHORT_KEEP and len(prep.live) == 7 and not prep.thin
    few = {f: cols[f] for f in ("bid", "position", "own_paid_sov")}
    assert prepare_levers(few).thin and not prepare_levers({**few, "price_gap": cols["price_gap"]}).thin


def test_derived_levers():
    prep = prepare_levers({"category_impressions": [100.0, 0.0, 1000.0], "own_price": [80.0] * 3, "comp_price": [100.0] * 3})
    assert prep.raw["log_category_impressions"] == pytest.approx([np.log(100), 0.0, np.log(1000)])
    assert prep.raw["price_gap"] == pytest.approx([0.8] * 3)


def test_vif_values():
    rng = np.random.default_rng(1)
    a = rng.normal(size=60)
    near = {"a": a, "b": a + rng.normal(scale=1e-3, size=60), "c": rng.normal(size=60)}
    v = vif(near, ["a", "b", "c"])
    assert v["a"] > 1000 and v["b"] > 1000 and v["c"] < 1.5
    ind = {k: rng.normal(size=200) for k in "abc"}
    assert all(0.99 <= x < 1.1 for x in vif(ind, list("abc")).values())
    assert vif({"a": a[:7], "b": a[:7]}, ["a", "b"]) == {"a": 1.0, "b": 1.0}   # < 8 complete rows
    assert vif({"a": a}, ["a"]) == {"a": 1.0}
    holes = {"a": np.where(np.arange(60) < 55, np.nan, a), "b": a}             # only 5 complete rows
    assert vif(holes, ["a", "b"])["a"] == 1.0


def _price_cols(collinear: bool, n=60, seed=3):
    rng = np.random.default_rng(seed)
    own = rng.uniform(0.05, 0.4, n)
    comp = rng.uniform(0.05, 0.4, n)
    gap = (1 - own) / (1 - comp) if collinear else rng.uniform(0.6, 1.4, n)
    return {"own_discount": own, "comp_discount": comp, "price_gap": gap}


def test_guard_folds_only_when_all_three_live_and_collinear():
    raw = _price_cols(True)
    raw["price_gap"] = 1.5 - 2 * raw["own_discount"] + 2 * raw["comp_discount"]      # exactly linear
    live, guard = fold_price_cluster(raw, ["bid", "own_discount", "comp_discount", "price_gap"])
    assert guard["folded"] == ["own_discount", "comp_discount"] and live == ["bid", "price_gap"]
    assert guard["vif"] >= guard["threshold"] == 8.0
    # independent: all kept
    live, guard = fold_price_cluster(_price_cols(False), ["own_discount", "comp_discount", "price_gap"])
    assert guard["folded"] == [] and len(live) == 3 and guard["vif"] < 8.0
    # only two of the three live: never folded, however collinear
    two = {"own_discount": raw["own_discount"], "price_gap": 1 - raw["own_discount"]}
    live, guard = fold_price_cluster(two, ["own_discount", "price_gap"])
    assert guard["folded"] == [] and live == ["own_discount", "price_gap"]


def test_guard_threshold_is_config():
    raw = _price_cols(False)
    rng = np.random.default_rng(5)
    raw["price_gap"] = 1.0 - raw["own_discount"] + raw["comp_discount"] + rng.normal(scale=0.08, size=60)
    worst = max(vif(raw, list(raw)).values())
    assert 1.5 < worst < 1e6
    names = ["own_discount", "comp_discount", "price_gap"]
    assert fold_price_cluster(raw, names, AttributionConfig(vif_max=worst * 1.01))[1]["folded"] == []
    assert fold_price_cluster(raw, names, AttributionConfig(vif_max=worst * 0.99))[1]["folded"] == names[:2]


def test_prepare_reports_folded_levers():
    cols, _ = _base(60)
    cols["price_gap"] = 1.5 - 2 * cols["own_discount"] + 2 * cols["comp_discount"]
    prep = prepare_levers(cols)
    assert prep.folded == ["own_discount", "comp_discount"] and "price_gap" in prep.live
    assert prep.guard["checked"] == ["own_discount", "comp_discount", "price_gap"]


def test_raw_facts_thresholds():
    n = 20
    b = np.arange(n) < 10
    c = ~b
    raw = {
        "own_availability": np.where(b, 0.90, 0.83),        # exactly 7 points -> fact
        "own_paid_sov": np.where(b, 0.30, 0.24),            # 6 points -> none
        "bid": np.where(b, 100.0, 121.0),                   # +21% -> fact
        "position": np.where(b, 4.0, 4.9),                  # +22.5% < 25% -> none
        "price_gap": np.where(b, 1.0, 1.3),                 # +30% -> fact
    }
    facts = raw_facts(raw, b, c, channel_moves={"cpm": 0.2, "icr": -0.05}, suppressed=["own_availability"])
    by = {f["lever"]: f for f in facts}
    assert set(by) == {"own_availability", "bid", "price_gap"}
    assert by["own_availability"]["suppressed"] and "aligned_channel" not in by["own_availability"]
    assert by["bid"]["aligned_channel"] == "cpm" and by["bid"]["self_inflicted"] is True
    opposite = {f["lever"]: f for f in raw_facts(raw, b, c, channel_moves={"cpm": -0.2})}
    assert "self_inflicted" not in opposite["bid"] and opposite["bid"]["aligned_channel"] == "cpm"
    assert "aligned_channel" not in {f["lever"]: f for f in raw_facts(raw, b, c, channel_moves={"cpm": 0.09})}["bid"]
    # fewer than 3 real days in a window -> no fact
    sparse = {"bid": np.where(np.arange(n) < 18, np.nan, 500.0)}
    assert raw_facts(sparse, b, c) == []


def test_raw_facts_sov_split_and_displacement():
    b = np.arange(20) < 10
    c = ~b
    own = np.full(20, 300.0)
    pool = np.where(b, 1000.0, 1500.0)
    facts = raw_facts({}, b, c, extras={"own_ad_impressions": own, "ad_pool_impressions": pool},
                      impressions=np.where(b, 1000.0, 1300.0))
    kinds = {f["kind"]: f for f in facts}
    assert kinds["sov_split"]["cause"] == "rivals_scaled_in" and "displacement" in kinds
    facts = raw_facts({}, b, c, extras={"own_ad_impressions": np.where(b, 300.0, 200.0),
                                        "ad_pool_impressions": np.full(20, 1000.0)})
    assert facts[0]["cause"] == "own_pulled_back"
    assert raw_facts({}, b, c, extras={"own_ad_impressions": np.where(b, 300.0, 280.0),
                                       "ad_pool_impressions": np.full(20, 1000.0)}) == []


def test_synthetic_panel(synthetic_dataset, synthetic_events):
    d = synthetic_dataset
    p = build_lever_panel(d.perf, market_rows=d.market, shelf_rows=d.shelf, targeting_units=d.targeting_units,
                          entity_id="cmp-generic")
    assert len(p) == 90 and p.dates == sorted(p.dates)
    ev = synthetic_events["cpm_spike"]
    after = np.array([dt >= ev.start for dt in p.dates])
    assert p.levers["bid"][after].mean() > 1.15 * p.levers["bid"][~after].mean()
    assert p.levers["own_paid_sov"][after].mean() < p.levers["own_paid_sov"][~after].mean()
    brand = build_lever_panel(d.perf, shelf_rows=d.shelf, entity_id="cmp-brand")
    ev = synthetic_events["availability_drop"]
    after = np.array([dt >= ev.start for dt in brand.dates])
    assert brand.levers["own_availability"][after].mean() < brand.levers["own_availability"][~after].mean() - 0.03
    assert ((p.levers["competitor_share"] == 0)).all()
