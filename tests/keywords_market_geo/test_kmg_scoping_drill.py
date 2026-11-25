"""Segment scoping, pure-campaign rule, placement labels and drilldowns."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cpm_lineage.drill import (DrillConfig, auction_drill, item_shift_share, keyword_drill, product_drill,
                               scope_buckets, scope_rows)
from cpm_lineage.schema import Entity, PerfRow, Product, Window
from cpm_lineage.scoping import (LabelCampaign, ScopingConfig, classify_labels, label_delivery, label_evidence,
                                 label_options, pure_campaigns, segment_audit, segment_products)

D0 = date(2026, 1, 1)


def day(n: int) -> date:
    return D0 + timedelta(days=n)


# ----------------------------------------------------------------- segment audit / purity

CATALOGUE = [Product("a1", "own", True, "snacks"), Product("a2", "own", True, "snacks"),
             Product("b1", "own", True, "drinks"), Product("r1", "rival", False, "snacks"),
             Product("multi", "own", True, "snacks"), Product("multi", "own", True, "drinks")]


def _perf(ent, pid, spend=1000.0, sales=2000.0, n=20, status="ACTIVE", **kw):
    return PerfRow(day(n), ent, spend, 10000.0, sales=sales, product_id=pid, status=status, **kw)


def test_segment_products_from_catalogue():
    assert segment_products(CATALOGUE, "snacks") == {"a1", "a2", "multi"}
    assert segment_products(CATALOGUE, "snacks", own_only=False) == {"a1", "a2", "multi", "r1"}
    assert segment_products(CATALOGUE, "snacks", subset=["a2", "b1"]) == {"a2"}


def test_segment_audit_partitions_and_stale(synthetic_dataset, windows_30):
    d = synthetic_dataset
    a = segment_audit(d.products, d.perf, "snack-bars", windows_30.current, entities=d.entities, profile=d.profile)
    by = {c.entity_id: c for c in a.campaigns}
    assert a.dropped_stale_stopped == ("cmp-paused",) and "cmp-paused" not in by
    assert by["cmp-reach"].partition == "reach" and by["cmp-reach"].direct_roas is None
    assert by["cmp-reco"].partition == "auto"
    assert by["cmp-generic"].partition == "keyword" and by["cmp-generic"].status_source == "entity"
    assert set(a.keyword_campaign_ids) == {c for c, v in by.items() if v.partition == "keyword"}
    assert a.totals.spend == pytest.approx(sum(t.spend for t in a.by_type.values()))
    assert a.totals.n == len(a.campaigns) == sum(t.n for t in a.by_type.values())
    c = by["cmp-generic"]
    assert c.total_roas == pytest.approx((c.direct_sales + c.indirect_sales) / c.spend)
    assert c.spend_3d <= c.spend_7d <= c.spend
    assert segment_audit(d.products, d.perf, "no-such-segment").reason


def test_segment_audit_scopes_by_product_not_row_label():
    rows = [_perf("c1", "a1", segment=None), _perf("c1", "b1", spend=5000.0), _perf("c2", "b1")]
    a = segment_audit(CATALOGUE, rows, "snacks")
    assert [c.entity_id for c in a.campaigns] == ["c1"] and a.campaigns[0].spend == 1000.0
    assert a.campaigns[0].status_source == "rows" and a.campaigns[0].partition == "other"   # type unverified
    assert a.campaigns[0].indirect_sales is None            # not reported -> None, never 0
    assert segment_audit(CATALOGUE, [_perf("c9", None)], "snacks").reason


def test_pure_campaign_hard_rule():
    rows = [_perf("pure", "a1"), _perf("pure", "a2"), _perf("mixed", "a1"), _perf("mixed", "b1"),
            _perf("multi-seg", "multi"), _perf("unknown", "a1"), _perf("unknown", "zz"),
            _perf("by-entity", "a1")]
    ents = [Entity("by-entity", product_ids=("a1", "b1"))]         # full advertised set from the entity record
    a = segment_audit(CATALOGUE + [Product("zz", "own", True, None)], rows, "snacks", entities=ents)
    res = pure_campaigns(a, CATALOGUE, entities=ents, perf_rows=rows)
    assert res.pure_campaign_ids == ("pure",)
    why = {e.entity_id: (e.reason, e.detail) for e in res.excluded}
    assert why["mixed"][0] == "mixed" and "drinks" in why["mixed"][1]
    assert why["multi-seg"][0] == "mixed"                          # product maps to two segments
    assert why["unknown"] == ("mixed", "1 product(s) with no segment mapping")
    assert why["by-entity"][0] == "mixed"
    assert not res.purity_incomplete
    unscoped = pure_campaigns(a, CATALOGUE)                        # no product-level scope supplied at all
    assert unscoped.pure_campaign_ids == () and {e.reason for e in unscoped.excluded} == {"unscoped"}
    capped = pure_campaigns(a, CATALOGUE, perf_rows=rows, cfg=ScopingConfig(purity_campaign_cap=1))
    assert capped.purity_incomplete and sum(e.reason == "not-checked" for e in capped.excluded) == 4


# ----------------------------------------------------------------- placement labels

def test_label_evidence_floor_runs_within_each_campaign():
    camps = [LabelCampaign("c1", {"snack shelf": 50.0, "legacy": 40.0}, day(0)),
             LabelCampaign("c2", {"legacy": 60.0, "new shelf": 30.0}, day(95)),
             LabelCampaign("c3", {"snack shelf": 70.0}, day(0), is_active=False)]
    delivery = {("c1", "snack shelf"): (10000.0, 100000.0), ("c1", "legacy"): (100.0, 5000.0),
                ("c2", "legacy"): (500.0, 10000.0), ("c3", "snack shelf"): (2000.0, 20000.0)}
    ev = label_evidence(camps, delivery, as_of=day(100))
    assert ev["legacy"].spend == 500.0 and ev["legacy"].stray == 100.0     # stray in c1, real in c2
    assert ev["snack shelf"].spend == 12000.0 and ev["snack shelf"].cpm == pytest.approx(100.0)
    assert ev["snack shelf"].bid == 50.0 and ev["snack shelf"].n_campaigns == 2      # inactive bid ignored
    assert ev["new shelf"].new == 1 and ev["legacy"].new == 1
    verdicts = {v.label: v.verdict for v in classify_labels(ev)}
    assert verdicts == {"snack shelf": "VALID", "legacy": "VALID", "new shelf": "UNCONFIRMED"}
    order = [v.label for v in classify_labels(ev)]
    assert order[0] == "snack shelf"


def test_classify_labels_table():
    camps = [LabelCampaign("c1", {"top": None, "edge": None, "under": None}, day(0)),
             LabelCampaign("c2", {"fresh": None}, day(90)), LabelCampaign("c3", {"dead": None}, day(0)),
             LabelCampaign("c4", {"top": None, "strayed": None}, day(0))]
    delivery = {("c1", "top"): (10000.0, 1.0), ("c1", "edge"): (300.0, 1.0), ("c1", "under"): (200.0, 1.0),
                ("c4", "top"): (5000.0, 1.0), ("c4", "strayed"): (50.0, 1.0)}
    out = {v.label: v for v in classify_labels(label_evidence(camps, delivery, as_of=day(100)))}
    assert out["top"].verdict == "VALID" and out["top"].share == 1.0
    assert out["edge"].verdict == "VALID"            # 300 == 2 % of 15000
    assert out["under"].verdict == "DROP"            # passes its campaign floor, fails the global one
    assert out["strayed"].verdict == "DROP" and "stray" in out["strayed"].why
    assert out["fresh"].verdict == "UNCONFIRMED" and out["dead"].verdict == "DROP"
    assert [v.verdict for v in classify_labels(label_evidence(camps, delivery, as_of=day(100)))] == [
        "VALID", "VALID", "UNCONFIRMED", "DROP", "DROP", "DROP"]


def test_label_options_closest_valid_tier_decides():
    own = [LabelCampaign("o1", {"own label": None}, day(0))]
    peer = [LabelCampaign("p1", {"peer label": None}, day(0))]
    delivery = {("p1", "peer label"): (900.0, 1000.0)}
    opt = label_options({"own": own, "brand": [], "peer": peer}, delivery, as_of=day(100))
    assert opt.tier == "peer" and opt.decided and opt.labels[0].label == "peer label"
    fallback = label_options({"own": own, "brand": [], "peer": []}, {}, as_of=day(100))
    assert fallback.tier == "own" and not fallback.decided
    assert label_options({}, {}).tier is None
    rows = [PerfRow(day(1), "p1", 400.0, 500.0, targeting_unit="peer label"),
            PerfRow(day(2), "p1", 500.0, 500.0, targeting_unit="peer label"),
            PerfRow(day(40), "p1", 500.0, 500.0, targeting_unit="peer label")]
    assert label_delivery(rows, Window(day(0), day(14))) == delivery


# ----------------------------------------------------------------- drilldowns

def test_scope_filter_and_buckets(synthetic_dataset):
    d = synthetic_dataset
    assert scope_rows(d.perf, []) == []
    assert len(scope_rows(d.perf, "*")) == len(d.perf) == len(scope_rows(d.perf, None))
    assert {r.entity_id for r in scope_rows(d.perf, ["cmp-brand", "cmp-young"])} == {"cmp-brand", "cmp-young"}
    b = scope_buckets(scope_rows(d.perf, "cmp-paused"))
    assert b.current.end < d.latest_date                         # anchored to the scope's own last active day
    assert b.current.days == 30 and b.prior.end == b.current.start - timedelta(days=1)
    assert scope_buckets([]) is None


def test_item_shift_share_exact():
    sums = {"a": (100.0, 300.0, 50.0, 100.0), "b": (0.0, 0.0, 200.0, 900.0), "c": (80.0, 40.0, 0.0, 0.0),
            "d": (20.0, 10.0, 30.0, 90.0)}
    items, res = item_shift_share(sums)
    delta = (100 + 900 + 90) / 280 - (300 + 40 + 10) / 200
    assert sum(i.total for i in items) == pytest.approx(delta, abs=1e-9)
    assert sum(i.mix for i in items) + sum(i.rate for i in items) == pytest.approx(delta, abs=1e-9)
    assert [i.total for i in items] == sorted(i.total for i in items)
    by = {i.key: i for i in items}
    assert by["b"].h_roas is None and by["c"].w_roas is None and by["a"].w_share == pytest.approx(50 / 280)


@pytest.mark.parametrize("scope", ["cmp-generic", "cmp-conquest", None, ["cmp-overlap", "cmp-winner"]])
def test_keyword_drill_sums_to_scope_roas_change(synthetic_dataset, scope):
    d = synthetic_dataset
    res = keyword_drill(d.perf, scope, profile=d.profile, cfg=DrillConfig(keyword_top_n=999, keyword_gainers=500))
    rows = [r for r in scope_rows(d.perf, scope) if r.targeting_unit]
    cur = [r for r in rows if res.buckets.current.contains(r.date)]
    pri = [r for r in rows if res.buckets.prior.contains(r.date)]
    delta = sum(r.sales for r in cur) / sum(r.spend for r in cur) - sum(r.sales for r in pri) / sum(r.spend for r in pri)
    assert res.delta == pytest.approx(delta, abs=1e-9)
    assert res.mix_total + res.rate_total == pytest.approx(delta, abs=1e-9)
    assert sum(i.total for i in res.items) == pytest.approx(delta, abs=1e-9)
    assert len(res.items) == res.n_items


def test_keyword_drill_finds_injected_cpm_spike(synthetic_dataset):
    d = synthetic_dataset
    res = keyword_drill(d.perf, "cmp-generic", profile=d.profile)
    worst = res.items[0]
    assert worst.key == "protein bar" and worst.total < 0
    assert worst.w_bid > 1.4 * worst.h_bid                                   # bid up about 1.6x
    assert worst.w_position == pytest.approx(worst.h_position, rel=0.1)      # position flat
    assert worst.w_roas < worst.h_roas


def test_keyword_drill_losers_then_gainers_and_placements(make_perf_row, synthetic_profile):
    rows = []
    for i in range(12):                                   # 12 losers
        rows += [make_perf_row(5, spend=100, sales=300, targeting_unit=f"loser {i:02d}"),
                 make_perf_row(40, spend=100, sales=300 - 10 * (i + 1), targeting_unit=f"loser {i:02d}")]
    for i in range(7):                                    # 7 gainers
        rows += [make_perf_row(5, spend=100, sales=300, targeting_unit=f"gainer {i}"),
                 make_perf_row(40, spend=100, sales=300 + 10 * (i + 1), targeting_unit=f"gainer {i}")]
    rows += [make_perf_row(5, spend=9999, sales=0, targeting_unit="feed"),
             make_perf_row(40, spend=9999, sales=99999, targeting_unit="feed")]
    res = keyword_drill(rows, profile=synthetic_profile)
    keys = [i.key for i in res.items]
    assert len(keys) == 15 and "feed" not in keys
    assert keys[:10] == [f"loser {i:02d}" for i in range(11, 1, -1)]         # worst first
    assert keys[10:] == [f"gainer {i}" for i in range(2, 7)]                 # the five largest gainers
    assert res.n_items == 19
    assert keyword_drill([]).reason


def test_product_drill_with_availability(synthetic_dataset):
    d = synthetic_dataset
    res = product_drill(d.perf, None, shelf_rows=d.shelf)
    by = {i.key: i for i in res.items}
    assert by["sku-a2"].w_availability < by["sku-a2"].h_availability - 0.05  # injected availability drop
    assert all(0 <= i.w_availability <= 1 for i in res.items)
    assert all(i.w_availability is None for i in product_drill(d.perf, None).items)
    full = product_drill(d.perf, None, cfg=DrillConfig(product_top_n=99, product_gainers=50))
    assert sum(i.total for i in full.items) == pytest.approx(full.delta, abs=1e-9)
    assert len(product_drill(d.perf, None, cfg=DrillConfig(product_top_n=5, product_gainers=4)).items) <= 5


class _Auction:
    def keyword_intel(self, keyword):
        if keyword == "protein bar":
            return {"top_slot_p50": 200.0, "top_slot_p75": 300.4, "n": 40, "top_competitor": "rival-b",
                    "threat": "aggressive", "note": "x"}
        return None


def test_auction_drill_optional_provider(synthetic_dataset):
    d = synthetic_dataset
    rows, reason = auction_drill(d.perf, "cmp-generic")
    assert rows == [] and "provider" in reason
    rows, reason = auction_drill(d.perf, "cmp-generic", _Auction(), profile=d.profile)
    assert reason is None and rows[0].keyword == "protein bar" and len(rows) <= 8
    assert rows[0].gap == round(300.4 - rows[0].own_bid) and rows[0].extras == {"note": "x"}
    assert rows[1].top_slot_p75 is None and rows[1].gap is None
    assert [r.spend for r in rows] == sorted((r.spend for r in rows), reverse=True)
    few, _ = auction_drill(d.perf, "cmp-generic", _Auction(), cfg=DrillConfig(auction_min_spend_ref=10**9))
    assert few == []
