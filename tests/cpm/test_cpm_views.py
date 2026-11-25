"""CPM views: ratio-of-sums algebra, window compare, CPM per position,
overview, tracker, category matrix, single keyword view."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cpm_lineage.cpm import (
    CpmConfig, category_matrix, cpm_by_position, daily_tracker, keyword_view, overview,
    position_slot, top_keywords, window_compare,
)
from cpm_lineage.cpm.tracking import KW_REST, OTHER
from cpm_lineage.cpm.rules import Rule, Ruleset, RulesetMember, assemble
from cpm_lineage.schema import BidRecord, MarketRow, PerfRow, Window

D0 = date(2026, 3, 1)


def row(day, kw, spend, impr, *, ent="c1", adv="adv-1", pos=None, sales=0.0, ttype="keyword", bid=None):
    return PerfRow(
        date=D0 + timedelta(days=day), entity_id=ent, spend=spend, impressions=impr, sales=sales,
        advertiser_id=adv, targeting_unit=kw, targeting_type=ttype, position=pos, bid=bid,
    )


A = Window(D0 + timedelta(days=7), D0 + timedelta(days=13))
B = Window(D0, D0 + timedelta(days=6))


def test_window_compare_totals_shares_and_ratio_of_sums():
    rows = [
        row(8, "alpha", 100, 1000), row(9, "alpha", 300, 1000),       # A: cpm 200 (not mean of 100, 300 -> same) 
        row(8, "beta", 50, 2000), row(8, "gamma", 10, 100),
        row(8, None, 40, 4000, ttype="placement"),
        row(1, "alpha", 100, 2000), row(2, "beta", 100, 1000), row(1, "gamma", 500, 1000),
    ]
    out = window_compare(rows, A, B, n=2)
    assert [t["keyword"] for t in out["top"]] == ["alpha", "beta"]      # by A spend, not B
    assert out["A"]["spend"] == pytest.approx(500)                      # includes ~kw and ~other
    assert out["B"]["spend"] == pytest.approx(700)
    assert set(out["rest"]) == {KW_REST, OTHER}
    assert out["rest"][OTHER]["a"]["spend"] == pytest.approx(40)
    assert out["rest"][KW_REST]["b"]["spend"] == pytest.approx(500)
    alpha = out["top"][0]
    assert alpha["a"]["cpm"] == pytest.approx(1000 * 400 / 2000, abs=1e-9)
    assert alpha["share_a"] == pytest.approx(400 / 500)
    assert alpha["share_b"] == pytest.approx(100 / 700)
    assert out["share_a"] == pytest.approx(450 / 500)
    assert out["top_a"]["cpm"] == pytest.approx(1000 * 450 / 4000, abs=1e-9)
    assert out["A"]["days"] == 2                                        # max over groups, never summed


def test_window_compare_clamps_n():
    rows = [row(8, f"k{i}", 10 + i, 100) for i in range(60)]
    assert len(window_compare(rows, A, B, n=500)["top"]) == 50
    assert len(window_compare(rows, A, B, n=0)["top"]) == 1
    assert len(window_compare(rows, A, B)["top"]) == CpmConfig().top_n


def test_cpm_by_position_only_observed_positions():
    rows = [
        row(8, "alpha", 300, 1000, pos=1.4), row(9, "alpha", 100, 1000, pos=1.9),
        row(8, "alpha", 60, 1000, pos=3.2),
        row(8, "alpha", 999, 1000, pos=999999),          # sentinel: excluded from the table
        row(8, "alpha", 20, 500, pos=44),
        row(1, "alpha", 50, 1000, pos=5.0),
    ]
    table = cpm_by_position(rows, A, B)
    assert [p["position"] for p in table] == [1, 3, 5, 21]              # 2 and 4 never seen: never interpolated
    assert table[-1]["label"] == "21+"
    p1 = table[0]
    assert p1["a"]["cpm"] == pytest.approx(1000 * 400 / 2000, abs=1e-9)
    assert p1["share_a"] == pytest.approx(2000 / 3500)
    assert sum(p["share_a"] for p in table) == pytest.approx(1.0, abs=1e-9)
    p5 = table[2]
    assert p5["a"]["cpm"] is None and p5["share_a"] == 0 and p5["share_b"] == pytest.approx(1.0)
    assert position_slot(0.5) is None and position_slot(21.9) == 21 and position_slot(None) is None


def test_overview_share_against_all_spend_and_empty_advertisers():
    rows = [
        row(0, "alpha", 100, 1000, pos=2), row(1, "alpha", 300, 1000, pos=4),
        row(0, None, 600, 10000, ttype="placement"),
        row(0, "beta", 0, 0, adv="adv-2"),
    ]
    out = overview(rows, registered=["adv-1", "adv-2", "adv-3"])
    assert [a["advertiser_id"] for a in out["advertisers"]] == ["adv-1"]
    assert set(out["empty"]) == {"adv-2", "adv-3"}
    adv = out["advertisers"][0]
    kw = adv["keywords"][0]
    assert kw["keyword"] == "alpha"
    assert kw["share"] == pytest.approx(400 / 1000)                     # ALL ad types in the denominator
    assert kw["total"]["cpm"] == pytest.approx(1000 * 400 / 2000, abs=1e-9)
    assert kw["total"]["position"] == pytest.approx(3.0, abs=1e-9)      # impression weighted
    assert out["total"]["share"] == pytest.approx(0.4)
    assert out["months"] == ["2026-03"] and out["last"] == D0 + timedelta(days=1)


def test_daily_tracker_columns_baseline_and_sums():
    start = D0 + timedelta(days=10)
    rows, market = [], []
    for d in range(0, 13):
        rows.append(row(d, "alpha", 100 + d, 1000))
        rows.append(row(d, "beta", 50, 2000))
        rows.append(row(d, None, 10, 5000, ttype="placement"))
        rows.append(row(d, "alpha", 10, 1000, adv="adv-2", ent="c9"))
        market.append(MarketRow(D0 + timedelta(days=d), "alpha", "own", True, ad_impressions=10 + d))
        market.append(MarketRow(D0 + timedelta(days=d), "alpha", "rival", False, ad_impressions=90))
    ref = Window(D0, D0 + timedelta(days=2))
    out = daily_tracker(rows, tracker_start=start, references={"ref": ref}, market_rows={"adv-1": market})
    assert len(out["days"]) == 7 and out["days"][0] == start             # at least a week of columns
    assert out["day_n"] == 3                                             # days 10, 11, 12 have data
    assert out["base_days"] == [start - timedelta(days=i) for i in range(7, 0, -1)]
    assert [a["advertiser_id"] for a in out["advertisers"]] == ["adv-1", "adv-2"]  # by baseline spend
    s = out["advertisers"][0]["series"]
    base_spend = sum(100 + d + 50 + 10 for d in range(3, 10))
    assert s["all"]["base"]["spend"] == pytest.approx(base_spend)
    assert s["all"]["base"]["cpm"] == pytest.approx(1000 * base_spend / (7 * 8000), abs=1e-9)
    assert s["top"]["base"]["spend"] == pytest.approx(sum(150 + d for d in range(3, 10)))
    assert s["k:alpha"]["ref"]["spend"] == pytest.approx(100 + 101 + 102)
    assert (D0 + timedelta(days=1)).isoformat() not in s["all"]          # reference days only inside the label
    assert s["k:alpha"]["base"]["sov"] == pytest.approx(
        sum(10 + d for d in range(3, 10)) / sum(100 + d for d in range(3, 10)), abs=1e-9)  # own/total summed
    tot = out["total"]["all"]["base"]
    assert tot["spend"] == pytest.approx(base_spend + 70)
    assert tot["cpm"] == pytest.approx(1000 * (base_spend + 70) / (7 * 9000), abs=1e-9)
    only = daily_tracker(rows, tracker_start=start, only=["adv-2"])
    assert only["total"]["all"]["base"]["spend"] == pytest.approx(70)


def test_category_matrix_folds_small_categories():
    rows = [
        row(0, "alpha", 30000, 100000), row(1, "alpha", 30000, 200000),
        row(0, "beta", 100, 1000), row(0, "gamma", 50, 1000),
        row(-5, "alpha", 500, 10000),
    ]
    out = category_matrix(
        rows, start=D0, reference=Window(D0 - timedelta(days=10), D0 - timedelta(days=1)),
        keyword_category={"alpha": "bars", "beta": "drinks"},
    )
    adv = out["advertisers"][0]
    assert [c["category"] for c in adv["categories"]] == ["bars", "Other keywords"]
    other = adv["categories"][1]
    assert other["mtd"]["spend"] == pytest.approx(150)                   # folded 'drinks' + unmapped 'gamma'
    assert adv["categories"][0]["mtd"]["cpm"] == pytest.approx(1000 * 60000 / 300000, abs=1e-9)
    assert adv["categories"][0]["reference"]["spend"] == pytest.approx(500)
    assert adv["total"]["mtd"]["spend"] == pytest.approx(60150)
    assert out["total"]["mtd"]["spend"] == pytest.approx(60150)
    assert out["days"] == [D0, D0 + timedelta(days=1)]


def test_keyword_view_campaign_merge_and_period_days():
    rows = [
        row(8, "alpha", 100, 1000, ent="c1", pos=2.0, bid=120), row(9, "alpha", 100, 1000, ent="c1", pos=2.0, bid=80),
        row(8, "beta", 999, 1000, ent="c1"),
    ]
    bids = [
        BidRecord("c1", "alpha", live_bid=150, base_bid=100, bid_id="b1"),
        BidRecord("c2", "Alpha", live_bid=90, base_bid=90, bid_id="b2"),      # no spend: kept with zero cells
        BidRecord("c3", "alpha", live_bid=90, base_bid=90, bid_id="b3"),      # sibling's campaign, never spent here
    ]
    inventory = assemble(
        [Ruleset("rs1", "Hold top", "keyword"), Ruleset("rs2", "Elsewhere", "keyword")],
        [RulesetMember("rs1", "b1"), RulesetMember("rs2", "b3")],
        [Rule("r1", "rs1", conditions={"rules": [{"position_targeting": True, "position_target": 1}]})],
        bids=bids,
    )
    out = keyword_view(rows, " ALPHA ", A, B, bids=bids, rulesets=inventory, foreign_entity_ids=["c3"])
    assert [c["entity_id"] for c in out["campaigns"]] == ["c1", "c2"]
    c1, c2 = out["campaigns"]
    assert c1["bid_a"] == pytest.approx(100) and c1["rulesets"] == [("Hold top", "keyword", "Active")]
    assert c2["a"]["spend"] == 0 and c2["rulesets"] == []
    assert [r["name"] for r in out["rulesets"]] == ["Hold top"]
    assert out["A"]["days"] == 2 and out["A"]["cpm"] == pytest.approx(100.0)
    assert out["B"]["cpm"] is None                                        # empty window: None, never 0
    assert [p["position"] for p in out["positions"]] == [2]


def test_synthetic_views_are_consistent(synthetic_dataset, windows_30):
    ds = synthetic_dataset
    out = window_compare(ds.perf, windows_30.current, windows_30.prior, profile=ds.profile)
    cur = [r for r in ds.perf if windows_30.current.contains(r.date)]
    assert out["A"]["spend"] == pytest.approx(sum(r.spend for r in cur), rel=1e-12)
    assert out["A"]["cpm"] == pytest.approx(1000 * sum(r.spend for r in cur) / sum(r.impressions for r in cur), abs=1e-9)
    parts = sum(t["a"]["spend"] for t in out["top"]) + sum(v["a"]["spend"] for v in out["rest"].values())
    assert parts == pytest.approx(out["A"]["spend"], abs=1e-6)
    assert out["top"][0]["keyword"] == top_keywords(ds.perf, 1, window=windows_30.current, profile=ds.profile)[0]
    spike = [r for r in ds.perf if r.targeting_unit == "protein bar"]
    table = cpm_by_position(spike, windows_30.current, windows_30.prior, profile=ds.profile)
    both = [p for p in table if p["a"]["cpm"] and p["b"]["cpm"]]
    assert both and all(p["a"]["cpm"] > 1.4 * p["b"]["cpm"] for p in both)   # dearer at the same position
