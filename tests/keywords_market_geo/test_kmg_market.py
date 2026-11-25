"""Share of voice, market stats, saturation screen, flywheel, competition,
price tiers, events."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cpm_lineage.market import (MS_SOV_CHAIN, OWN_LABEL, MarketConfig, ad_sov, brand_ad_shares, break_scan,
                                competition_table, daily_share, demand_seasonality, elasticity, event_dating,
                                flywheel_state, flywheel_states, geo_role_sov, house_key, keyword_top_rivals,
                                launches, market_stats, ms_sov_label, organic_share, price_tiers, rival_table,
                                saturation_verdict, sku_footprint, sov_cut, sov_summary, tier_position,
                                tier_pricing_power, tier_share_trend, tier_whitespace, weighted_availability)
from cpm_lineage.schema import MarketRow, OfftakeRow, PerfRow, ShelfRow, Window, WindowPair

D0 = date(2026, 1, 1)


def day(n: int) -> date:
    return D0 + timedelta(days=n)


def mrow(n, kw, brand, own, ad, org=0.0, **kw_):
    return MarketRow(day(n), kw, brand, own, ad, org, **kw_)


PAIR = WindowPair(Window(day(0), day(9)), Window(day(10), day(19)))


# ----------------------------------------------------------------- share of voice

def test_ad_sov_sums_then_divides():
    rows = [mrow(10, "a", "own", True, 10), mrow(10, "a", "r", False, 90),
            mrow(11, "a", "own", True, 900), mrow(11, "a", "r", False, 100)]
    assert ad_sov(rows, PAIR.current) == pytest.approx(100 * 910 / 1100)      # not the mean of 10 % and 90 %
    assert ad_sov(rows, PAIR.prior) is None                                   # no denominator -> None, never 0
    assert ad_sov([], PAIR.current) is None


def test_like_for_like_guard():
    rows = [mrow(0, "kept", "own", True, 50), mrow(0, "kept", "r", False, 50),
            mrow(0, "gone", "own", True, 400), mrow(0, "gone", "r", False, 100),     # leaves the feed
            mrow(10, "kept", "own", True, 50), mrow(10, "kept", "r", False, 50)]
    assert ad_sov(rows, PAIR.prior) == pytest.approx(75.0)
    assert ad_sov(rows, PAIR.prior, like_for_like=PAIR.current) == pytest.approx(50.0)
    assert sov_cut(rows, PAIR).ad_delta == pytest.approx(0.0)
    assert sov_cut(rows, PAIR, like_for_like=False).ad_delta == pytest.approx(-25.0)


def test_sov_cut_three_shares_and_by_role():
    rows = [mrow(10, "g", "own", True, 20, 60, unit_role="generic"), mrow(10, "g", "r", False, 80, 40,
                                                                          unit_role="generic"),
            mrow(10, "b", "own", True, 90, 10, unit_role="brand"), mrow(10, "b", "r", False, 10, 90,
                                                                        unit_role="brand")]
    cut = sov_cut(rows, PAIR)
    assert cut.ad == pytest.approx(55.0) and cut.organic == pytest.approx(35.0) and cut.overall == pytest.approx(45.0)
    assert cut.own_organic_share == pytest.approx(70 / 180)
    s = sov_summary(rows, PAIR)
    assert s.by_role["generic"].ad == pytest.approx(20.0) and s.by_role["brand"].ad == pytest.approx(90.0)
    assert s.by_role["generic"].mix == pytest.approx(50.0)
    assert sum(c.mix for c in s.by_role.values()) == pytest.approx(100.0)
    assert sov_summary(rows, PAIR, roles={"g": "own_brand", "b": "own_brand"}).by_role.keys() == {"own_brand"}
    assert sov_summary([], PAIR).reason


def test_rival_table_top_six_and_own_key():
    rows = [mrow(10, "kw", "own", True, 100)] + [mrow(10, "kw", f"r{i}", False, 10 * (i + 1)) for i in range(8)]
    shares = brand_ad_shares(rows, PAIR.current)
    assert shares[OWN_LABEL] == pytest.approx(100 / 460) and sum(shares.values()) == pytest.approx(1.0)
    tab = rival_table(rows, {"cur": PAIR.current, "pri": PAIR.prior}, ["KW"])
    assert list(tab["kw"]) == ["cur"] and len(tab["kw"]["cur"]) == 6 and OWN_LABEL in tab["kw"]["cur"]


def test_weighted_availability_velocity_then_checks():
    rows = [ShelfRow(day(10), "p1", "g", 5, 10, units=90), ShelfRow(day(10), "p2", "g", 10, 10, units=10),
            ShelfRow(day(10), "p3", "g", 0, 10, is_own=False)]
    assert weighted_availability(rows) == pytest.approx(0.5 * 0.9 + 1.0 * 0.1)
    plain = [ShelfRow(day(10), "p1", "g", 5, 10), ShelfRow(day(10), "p2", "g", 10, 30)]
    assert weighted_availability(plain) == pytest.approx(15 / 40)
    assert weighted_availability(rows, own=False) == 0.0
    assert weighted_availability([]) is None


# ----------------------------------------------------------------- labels / saturation / flywheel

@pytest.mark.parametrize("ratio, label", [
    (0.49, "over-investing: block"), (0.5, "over-investing in ads"), (0.69, "over-investing in ads"),
    (0.7, "mild under-conversion"), (0.99, "mild under-conversion"), (1.0, "healthy"), (1.5, "healthy"),
    (1.51, "highly efficient"),
])
def test_ms_sov_label_boundaries(ratio, label):
    assert ms_sov_label(ratio).startswith(label)


def test_ms_sov_label_unknown():
    assert ms_sov_label(None) is None


DECL = ([30.0, 28.0, 26.0], [30.0, 30.0, 30.0])      # ratios 1.0, 0.933, 0.867


@pytest.mark.parametrize("cost, organic, code", [
    ((10, 10, 11), None, "measure_organic_first"),
    ((10, 10, 11), 0.19, "flywheel_cold"),
    ((10, 10, 11), 0.51, "saturation_confirmed"),
    ((10, 10, 11), 0.20, "test_reduction"),
    ((10, 10, 11), 0.50, "test_reduction"),
    ((10, 10, 10.5), 0.9, "efficiency_drift"),        # exactly 1.05x is not "rising"
    ((None, None, None), 0.9, "verify_spend_source"),
    ((10, None, 11), 0.9, "verify_spend_source"),
])
def test_saturation_verdict_table(cost, organic, code):
    v = saturation_verdict(DECL[0], DECL[1], cost, organic)
    assert v.code == code and v.declining


def test_saturation_needs_both_legs_and_net_five_percent():
    assert saturation_verdict([30, 29.5, 29], [30, 30, 30]).code == "not_saturating"        # net 3.3 %
    assert saturation_verdict([30, 31, 26], [30, 30, 30]).code == "not_saturating"          # one leg only
    assert saturation_verdict([30, 28, None], [30, 30, 30]).code == "insufficient_data"
    assert saturation_verdict([30, 28, 26], [30, 30, 0]).code == "insufficient_data"
    v = saturation_verdict([30, 28, 26], [30, 30, 30], (5, 5, 6), 0.6)
    assert v.cost_rising and v.ratios == pytest.approx((1.0, 28 / 30, 26 / 30))


@pytest.mark.parametrize("share, state", [
    (0.0, "Cold"), (0.199, "Cold"), (0.20, "Warming"), (0.499, "Warming"), (0.50, "Active"), (0.699, "Active"),
    (0.70, "Self-Sustaining"), (1.0, "Self-Sustaining"),
])
def test_flywheel_state_boundaries(share, state):
    assert flywheel_state(share) == state


def test_flywheel_states_per_product_and_material_set():
    rows = [mrow(10, "k", "own", True, 900, 100, product_id="big-cold"),
            mrow(10, "k", "own", True, 100, 900, product_id="big-self"),
            mrow(10, "k", "own", True, 1, 9, product_id="tail"),
            mrow(10, "k", "own", True, 0, 0, product_id="silent"),
            mrow(10, "k", "rival", False, 500, 500, product_id="other")]
    perf = [PerfRow(day(10), "c", 200.0, 1000.0, sales=500.0, product_id="big-cold")]
    res = flywheel_states(rows, PAIR.current, perf_rows=perf)
    assert res.n_products == 3 and res.mixed_state
    assert res.distribution == {"Cold": 1, "Self-Sustaining": 2}
    by = {p.product_id: p for p in res.products}
    assert by["big-cold"].state == "Cold" and by["big-cold"].roas == pytest.approx(2.5)
    assert by["big-self"].spend == 0.0 and by["big-self"].roas is None
    assert flywheel_states(rows, PAIR.current).products[0].spend is None      # no spend feed -> None
    same = [r for r in rows if r.product_id != "big-self"]
    assert not flywheel_states(same, PAIR.current).mixed_state                # the tail is outside the 90 % set
    assert flywheel_states([mrow(10, "k", "own", True, 5, 5)], PAIR.current).reason


# ----------------------------------------------------------------- market stats

def _offtake(own_cur=300.0, cat_cur=1000.0, own_prev=200.0, cat_prev=800.0):
    return [OfftakeRow(day(15), "own", True, own_cur, units=10, segment="s1"),
            OfftakeRow(day(15), "rival", False, cat_cur - own_cur, units=40, segment="s1"),
            OfftakeRow(day(5), "own", True, own_prev, units=10, segment="s1"),
            OfftakeRow(day(5), "rival", False, cat_prev - own_prev, units=30, segment="s1")]


def test_market_stats_formulas():
    perf = [PerfRow(day(15), "c", 60.0, 1000.0, sales=100.0)]
    market = [mrow(15, "k", "own", True, 600, 400), mrow(15, "k", "rival", False, 400, 600)]
    ms = market_stats(_offtake(), PAIR, perf_rows=perf, market_rows=market)
    t = ms.total
    assert t.share_pct == pytest.approx(30.0) and t.share_prev_pct == pytest.approx(25.0)
    assert t.delta_pp == pytest.approx(5.0)
    assert t.own_growth_pct == pytest.approx(50.0) and t.segment_growth_pct == pytest.approx(25.0)
    assert t.attributable_pct == pytest.approx(25.0)
    assert ms.ad_cost_share_pct == pytest.approx(20.0)
    assert ms.ad_sov_pct == pytest.approx(60.0)
    assert ms.price_index == pytest.approx((300 / 10) / (1000 / 50))
    assert ms.ms_sov == pytest.approx(0.5) and ms.ms_sov_label == "over-investing in ads"
    assert ms.ms_sov_chain == MS_SOV_CHAIN and ms.ms_sov_chain[0] == "availability"
    assert ms.segments[0].segment == "s1" and ms.days == 10


def test_market_stats_degrades():
    assert market_stats(None, PAIR).reason
    ms = market_stats(_offtake(), PAIR)
    assert ms.reason is None and ms.ad_sov_pct is None and ms.ms_sov is None and ms.ms_sov_chain == ()
    assert ms.ad_cost_share_pct is None and ms.saturation.code == "insufficient_data"


def test_market_stats_top_segments_and_full_denominator():
    rows = []
    for i in range(6):
        rows += [OfftakeRow(day(15), "own", True, 100.0 * (i + 1), segment=f"s{i}"),
                 OfftakeRow(day(15), "rival", False, 1000.0, segment=f"s{i}")]
    ms = market_stats(rows, PAIR)
    assert [s.segment for s in ms.segments] == ["s5", "s4", "s3", "s2"]
    assert ms.total.share_pct == pytest.approx(100 * 2100 / 8100)


def test_market_stats_saturation_three_periods():
    rows, market, perf = [], [], []
    for k, (own, cost) in enumerate([(300.0, 30.0), (280.0, 30.0), (250.0, 40.0)]):
        d = 5 + 10 * k
        rows += [OfftakeRow(day(d), "own", True, own), OfftakeRow(day(d), "rival", False, 1000.0 - own)]
        market += [mrow(d, "k", "own", True, 300, 100), mrow(d, "k", "rival", False, 700, 900)]
        perf.append(PerfRow(day(d), "c", cost, 100.0))
    pair = WindowPair(Window(day(10), day(19)), Window(day(20), day(29)))
    sat = market_stats(rows, pair, perf_rows=perf, market_rows=market).saturation
    assert sat.declining and sat.cost_rising and sat.code == "test_reduction"
    assert sat.organic_share == pytest.approx(0.25)


def test_market_stats_on_synthetic(synthetic_dataset, windows_30):
    d = synthetic_dataset
    ms = market_stats(d.offtake, windows_30, perf_rows=d.perf, market_rows=d.market)
    assert ms.reason is None and 0 < ms.total.share_pct < 100
    assert ms.ms_sov == pytest.approx(ms.total.share_pct / ms.ad_sov_pct)
    assert ms.total.attributable_pct == pytest.approx(ms.total.own_growth_pct - ms.total.segment_growth_pct)
    assert organic_share(d.market, windows_30.current) == pytest.approx(ms.saturation.organic_share)


# ----------------------------------------------------------------- competition

def test_house_key():
    assert house_key("Acme Glow") == "acme" and house_key("  Acme-Kids") == "acme" and house_key("") == ""


def test_competition_table_own_row_house_and_unmapped():
    rows = [OfftakeRow(day(15), "Acme Glow", True, 200.0, list_value=250.0),
            OfftakeRow(day(15), "Acme Pure", True, 100.0, list_value=150.0),
            OfftakeRow(day(15), "Acme Kids", False, 50.0),                 # same house by first token
            OfftakeRow(day(15), "Rivalo", False, 400.0, list_value=400.0),
            OfftakeRow(day(15), "", False, 250.0),                         # unmapped stays in the denominator
            OfftakeRow(day(5), "Acme Glow", True, 100.0, list_value=100.0),
            OfftakeRow(day(5), "Rivalo", False, 400.0)]
    shelf = [ShelfRow(day(15), "p1", "g", 9, 10, brand="Acme Glow", units=30),
             ShelfRow(day(15), "p2", "g", 5, 10, brand="Acme Pure", units=10),
             ShelfRow(day(15), "p3", "g", 8, 10, brand="Rivalo", is_own=False)]
    market = [mrow(15, "k", "Acme Glow", True, 30, 10), mrow(15, "k", "Rivalo", False, 70, 90)]
    t = competition_table(rows, PAIR, shelf_rows=shelf, market_rows=market)
    assert t.own.brand == OWN_LABEL and t.own.share_pct == pytest.approx(30.0)      # 300 / 1000 incl. unmapped
    assert t.own.share_delta_pp == pytest.approx(30.0 - 20.0)
    assert t.own.discount_pct == pytest.approx(100 * (1 - 300 / 400))
    assert t.own.availability == pytest.approx((9 * 30 + 5 * 10) / (10 * 30 + 10 * 10))
    assert t.own.ad_sov_pct == pytest.approx(30.0) and t.own.organic_sov_pct == pytest.approx(10.0)
    assert t.own_rank == 2 and t.n_brands == 3 and t.unmapped_pct == pytest.approx(25.0)
    by = {r.brand: r for r in t.rows}
    assert by["Acme Kids"].is_house and not by["Rivalo"].is_house
    assert sum(1 for r in t.rows if r.is_own) == 1
    assert t.segment_growth_pct == pytest.approx(100.0)
    assert t.top_advertisers[0][0] == "Rivalo"
    assert competition_table(rows, PAIR, house_map={"Rivalo": "acme"}).rows[0].is_house
    assert competition_table([], PAIR).reason


# ----------------------------------------------------------------- price tiers

def _tier_rows(n=6):
    rows = []
    for i in range(n):
        price = 100.0 * (i + 1)
        for d, mult in ((15, 1.0), (5, 0.5)):
            rows.append(OfftakeRow(day(d), "own" if i % 2 == 0 else "rival", i % 2 == 0, 600.0 * mult,
                                   units=600.0 * mult / price, product_id=f"p{i}", geo="g1",
                                   list_value=600.0 * mult))
    return rows


def test_price_tiers_sales_weighted_terciles():
    t = price_tiers(_tier_rows(), PAIR)
    assert t.cuts == pytest.approx((200.0, 400.0))
    assert [x.n_products for x in t.tiers] == [2, 2, 2]
    assert sum(x.contribution_pct for x in t.tiers) == pytest.approx(100.0)
    assert all(x.own_share_pct == pytest.approx(50.0) and x.pp_worth == pytest.approx(12.0) for x in t.tiers)
    assert all(x.growth_pct == pytest.approx(100.0) and x.band == "contested" for x in t.tiers)
    assert t.membership["p0"] == "value" and t.membership["p5"] == "premium"


def test_price_tiers_need_six_products_and_distinct_cuts():
    assert price_tiers(_tier_rows(5), PAIR).reason
    flat = [OfftakeRow(day(15), "b", False, 100.0, units=1.0, product_id=f"p{i}", list_value=100.0)
            for i in range(8)]
    assert price_tiers(flat, PAIR).tiers == ()


@pytest.mark.parametrize("share, delta, band, trend", [
    (85, 0.31, "owned", "rising"), (84.9, 0.3, "led", "flat"), (55, -0.31, "led", "falling"),
    (25, -0.3, "contested", "flat"), (1, 0, "minority", "flat"), (0.99, None, "absent", "flat"),
])
def test_tier_position(share, delta, band, trend):
    assert tier_position(share, delta) == (band, trend)


def test_elasticity_ols():
    pts = [(float(x), 2.0 * x + 1.0) for x in range(6)]
    slope, r, n = elasticity(pts)
    assert slope == pytest.approx(2.0) and r == pytest.approx(1.0) and n == 6
    assert elasticity(pts[:5]) is None
    assert elasticity([(1.0, float(i)) for i in range(8)]) is None        # no variance on x


def test_tier_pricing_power_whitespace_trend():
    member = {"own": "value", "riv": "value"}
    rows = []
    for w in range(12):
        disc = 5.0 + w
        own_sales = 100.0 + 10.0 * w
        rows += [OfftakeRow(day(7 * w), "own", True, own_sales, units=1, product_id="own",
                            list_value=own_sales / (1 - disc / 100)),
                 OfftakeRow(day(7 * w), "riv", False, 1000.0 - own_sales, units=1, product_id="riv")]
    power = {p.tier: p for p in tier_pricing_power(rows, day(83), member)}
    assert power["value"].n == 12 and power["value"].slope == pytest.approx(1.0) and power["value"].r > 0.99
    assert power["mid"].slope is None
    trend = tier_share_trend(rows, member)
    assert trend and trend[0].tier == "value" and len(trend[0].months) >= 3
    gap_rows = [OfftakeRow(day(15), "rival", False, 600.0, units=600.0 / (100.0 * (i + 1)), product_id=f"p{i}",
                           geo="g1", list_value=600.0) for i in range(6)]
    gaps = tier_whitespace(gap_rows)
    assert {g.tier for g in gaps} == {"value", "mid", "premium"} and all(g.own_share_pct == 0 for g in gaps)
    assert tier_whitespace(_tier_rows()) == []


def test_price_tiers_on_synthetic(synthetic_dataset, windows_30):
    t = price_tiers(synthetic_dataset.offtake, windows_30)
    assert t.reason is None and t.cuts[0] < t.cuts[1]
    assert sum(x.sales for x in t.tiers) > 0


# ----------------------------------------------------------------- events

def _series(n=20, jump_at=None, jump=0.0, wobble=0.1):
    out, v = [], 30.0
    for i in range(n):
        v += wobble if i % 2 else -wobble
        if jump_at is not None and i == jump_at:
            v += jump
        out.append((day(i), v))
    return out


def test_break_scan_finds_planted_break_and_stays_quiet():
    ev = break_scan(_series(jump_at=12, jump=-4.0))
    assert ev is not None and ev.date == day(12) and ev.delta < -3.5 and ev.sigmas > 3
    assert break_scan(_series()) is None                                   # quiet window: nothing
    assert break_scan(_series(n=13, jump_at=6, jump=-4.0)) is None         # fewer than 14 points
    assert break_scan(_series(jump_at=12, jump=0.45, wobble=0.0)) is None  # under half a share point
    assert break_scan(_series(jump_at=12, jump=0.6, wobble=0.0)).sigmas == float("inf")


def test_event_dating_same_day_movers():
    share = _series(jump_at=12, jump=-4.0)
    avail = [(day(11), 95.0), (day(12), 92.5)]
    sov = [(day(11), 30.0), (day(12), 29.0)]
    rivals = {"Rivalo": [(day(11), 1000.0), (day(12), 1300.0)], "Calm": [(day(11), 1000.0), (day(12), 1100.0)]}
    out = event_dating(share, availability_series=avail, ad_sov_series=sov, rival_ad_series=rivals)
    assert [(m.kind, m.name) for m in out.movers] == [("availability", "own availability"), ("rival_ad", "Rivalo")]
    assert out.movers[1].change == pytest.approx(30.0)
    assert event_dating(_series()).event is None and event_dating(_series()).reason


def test_daily_share(synthetic_dataset):
    ser = daily_share(synthetic_dataset.offtake)
    assert len(ser) == 90 and all(0 < v < 100 for _, v in ser)


def test_launches_honesty_gate_and_trend():
    rows = [OfftakeRow(day(i), "rival", False, 1000.0, product_id="old") for i in range(100)]
    rows += [OfftakeRow(day(i), "rival", False, 500.0 if i < 85 else 3000.0, product_id="new") for i in range(60, 100)]
    rows += [OfftakeRow(day(i), "own", True, 10.0, product_id="tiny") for i in range(60, 100)]
    win = Window(day(85), day(99))
    out = launches(rows, win)
    assert [x.product_id for x in out] == ["new"]         # "old" starts with the history; "tiny" is under the floor
    assert out[0].trend == "ramping" and out[0].days_alive == 40
    assert out[0].rate_current == pytest.approx(3000.0)
    assert launches(rows, win, cfg=MarketConfig(launch_history_gate_days=60)) == []


def test_seasonality_needs_six_months():
    rows = [OfftakeRow(date(2025, m, 15), "b", False, 100.0 * m) for m in range(1, 8)]
    s = demand_seasonality(rows)
    assert s.months == tuple(f"2025-{m:02d}" for m in range(1, 7))        # last (partial) month dropped
    assert s.peak == "2025-06" and s.trough == "2025-01"
    assert demand_seasonality(rows[:5]) is None


def test_small_reads():
    rows = [mrow(10, "k", "own", True, 100, 0, geo="g1", unit_role="generic"),
            mrow(10, "k", "Rivalo", False, 1500, 0, geo="g1", unit_role="generic", organic_position=3.0),
            mrow(10, "k", "Small", False, 900, 0, geo="g1", unit_role="generic")]
    top = keyword_top_rivals(rows)
    assert top["k"].brand == "Rivalo" and top["k"].ad_share_pct == pytest.approx(60.0)
    gr = geo_role_sov(rows, PAIR)
    assert gr[0].geo == "g1" and gr[0].ad_sov_pct == pytest.approx(4.0) and gr[0].ad_sov_delta_pp is None
    fp = sku_footprint([OfftakeRow(day(1), "b", False, 100.0, product_id="x"),
                        OfftakeRow(day(1), "b", False, 300.0, product_id="y")])
    assert fp[0].n_products == 2 and fp[0].sales_per_product == pytest.approx(200.0)
