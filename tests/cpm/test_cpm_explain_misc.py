"""CPM-move classifier, share of voice, restatement detection, ownership guard."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cpm_lineage.cpm import (
    CpmConfig, InMemorySnapshotStore, PeriodReading, apply_ownership_guard, assign_campaign_owner,
    brand_ad_shares, classify_cpm_move, explain_cpm_moves, first_seen, leaked_rows, restated,
    restated_days, share_of_voice, take_snapshot,
)
from cpm_lineage.cpm.restatement import TOTAL_KEY
from cpm_lineage.schema import MarketRow, PerfRow, Window

D0 = date(2026, 3, 1)


def R(cpm, pos=3.0, sov=0.2, roas=None, shares=None):
    return PeriodReading(cpm=cpm, impressions=1000, position=pos, sov=sov, roas=roas, brand_shares=shares or {})


def test_classifier_four_kinds_first_match_wins():
    ref = R(100, shares={"~own": 0.2, "rival": 0.10})
    assert classify_cpm_move(ref, R(120, pos=2.69))["kind"] == "bought position"      # improved by 0.3
    assert classify_cpm_move(ref, R(120, sov=0.26))["kind"] == "bought position"      # +5 points of share
    out = classify_cpm_move(R(100, roas=4.0, shares={"rival": 0.10}), R(120, roas=3.0, shares={"rival": 0.13}))
    assert out["kind"] == "rival pressure" and "rival ad share 10% -> 13%" in out["why"]
    assert "(returns fell)" in out["why"] and out["returns_fell"]
    assert classify_cpm_move(ref, R(120, pos=3.31))["kind"] == "worse spot"           # no share condition
    assert classify_cpm_move(ref, R(120))["kind"] == "auction price"
    # bought position outranks rival pressure and keeps the rival text
    both = classify_cpm_move(R(100, shares={"rival": 0.1}), R(120, pos=2.0, shares={"rival": 0.3}))
    assert both["kind"] == "bought position" and "rival ad share" in both["why"]


def test_classifier_thresholds():
    ref = R(100, shares={"rival": 0.10})
    assert classify_cpm_move(ref, R(104.9)) is None                    # rise must be above 5%
    assert classify_cpm_move(ref, R(105.1)) is not None
    assert classify_cpm_move(ref, R(90)) is None                       # a fall is never explained
    assert classify_cpm_move(ref, R(120, pos=2.71))["kind"] == "auction price"
    assert classify_cpm_move(ref, R(120, pos=3.29))["kind"] == "auction price"
    assert classify_cpm_move(ref, R(120, sov=0.249))["kind"] == "auction price"
    assert classify_cpm_move(ref, R(120, shares={"rival": 0.129}))["kind"] == "auction price"
    assert classify_cpm_move(ref, R(120, shares={"rival": 0.131}))["kind"] == "rival pressure"
    # returns fell only below 80% of reference ROAS
    assert not classify_cpm_move(R(100, roas=4.0), R(120, roas=3.2))["returns_fell"]
    assert classify_cpm_move(R(100, roas=4.0), R(120, roas=3.19))["returns_fell"]
    assert classify_cpm_move(None, R(120)) is None
    assert classify_cpm_move(PeriodReading(cpm=100, impressions=0), R(120)) is None


def test_classifier_degrades_without_position_or_share():
    out = classify_cpm_move(PeriodReading(100, 1000), PeriodReading(130, 1000))
    assert out["kind"] == "auction price" and out["position_delta"] == 0.0 and "ROAS" not in out["why"]
    # platform where a higher position number is better
    hi = classify_cpm_move(R(100, pos=3.0), R(120, pos=3.4), lower_is_better=False)
    assert hi["kind"] == "bought position"


def test_synthetic_cpm_spike_is_rival_pressure(synthetic_dataset, synthetic_events, windows_30):
    ds = synthetic_dataset
    out = explain_cpm_moves(ds.perf, windows_30.prior, windows_30.current, market_rows=ds.market, profile=ds.profile)
    spike = synthetic_events["cpm_spike"]
    hit = [d for d in out if d["keyword"] == spike.targeting_unit]
    assert len(hit) == 1
    assert hit[0]["kind"] == "rival pressure" and hit[0]["rival"] == "rival-b"
    assert hit[0]["pct"] == pytest.approx(0.6, abs=0.08) and hit[0]["returns_fell"]
    assert abs(hit[0]["position_delta"]) < 0.3
    # without the market feed the same move can only be read as a dearer slot
    bare = explain_cpm_moves(ds.perf, windows_30.prior, windows_30.current, profile=ds.profile)
    assert [d["kind"] for d in bare if d["keyword"] == spike.targeting_unit] == ["auction price"]


def test_share_of_voice_is_sum_over_sum_and_like_for_like():
    rows = [
        MarketRow(D0, "alpha", "own", True, ad_impressions=10), MarketRow(D0, "alpha", "x", False, ad_impressions=90),
        MarketRow(D0 + timedelta(days=1), "alpha", "own", True, ad_impressions=500),
        MarketRow(D0 + timedelta(days=1), "alpha", "x", False, ad_impressions=500),
        MarketRow(D0, "gone", "own", True, ad_impressions=1000),
    ]
    w = Window(D0, D0 + timedelta(days=1))
    assert share_of_voice(rows, w, ["alpha"]) == pytest.approx(510 / 1100)          # not mean(0.1, 0.5)
    later = Window(D0 + timedelta(days=1), D0 + timedelta(days=1))
    assert share_of_voice(rows, w) == pytest.approx(1510 / 2100)
    assert share_of_voice(rows, w, like_for_like=later) == pytest.approx(510 / 1100)  # 'gone' left the feed
    assert share_of_voice(rows, Window(D0 + timedelta(days=5), D0 + timedelta(days=6))) is None
    shares = brand_ad_shares(rows, w, ["alpha"])
    assert shares == {"x": pytest.approx(590 / 1100), "~own": pytest.approx(510 / 1100)}


def test_restatement_first_reading_wins_and_tolerance():
    store = InMemorySnapshotStore()
    day = "2026-03-01"
    assert take_snapshot(store, date(2026, 3, 2), {TOTAL_KEY: {"all": {day: (1000.0, 50000)}}})
    assert not take_snapshot(store, date(2026, 3, 2), {TOTAL_KEY: {"all": {day: (9.0, 9)}}})   # idempotent per day
    assert take_snapshot(store, date(2026, 3, 3), {TOTAL_KEY: {"all": {day: (1200.0, 60000)}}})
    first = first_seen(store.load())
    assert first[("all", day)] == ("2026-03-02", 1000.0, 50000.0)
    assert not restated(first[("all", day)], (1010.0, 50000))            # exactly 1%: not restated
    assert restated(first[("all", day)], (1010.5, 50000))
    assert restated(first[("all", day)], (1000.0, 50600))                # impressions alone
    assert not restated(None, (1.0, 1)) and not restated((1.0, 1.0), None)
    assert not restated((0.2, 0.0), (0.209, 0.0), cfg=CpmConfig())      # floor of 1 unit, not 1% of 0.2
    assert restated((0.2, 0.0), (0.22, 0.0))
    out = restated_days(first, {"all": {date(2026, 3, 1): (1200.0, 60000)}})
    assert list(out) == [("all", day)] and out[("all", day)]["first_taken"] == "2026-03-02"


def test_ownership_guard_majority_of_days():
    rows = [PerfRow(D0 + timedelta(days=i), "c1", 10, 100, advertiser_id="adv-1") for i in range(5)]
    rows += [PerfRow(D0 + timedelta(days=i), "c1", 999, 100, advertiser_id="adv-2") for i in range(2)]
    rows += [PerfRow(D0, "c2", 5, 100, advertiser_id="adv-2")]
    rows += [PerfRow(D0 - timedelta(days=400 + i), "c1", 1, 1, advertiser_id="adv-2") for i in range(20)]  # outside lookback
    owners = assign_campaign_owner(rows)
    assert owners == {"c1": "adv-1", "c2": "adv-2"}                      # days, not spend, decide
    kept = apply_ownership_guard(rows, owners)
    assert sum(r.spend for r in kept if r.entity_id == "c1") == pytest.approx(50)
    assert len(leaked_rows(rows, owners)) == 22
    assert assign_campaign_owner(rows, authoritative={"c1": "adv-2"})["c1"] == "adv-2"
    assert assign_campaign_owner(rows, lookback_days=1000)["c1"] == "adv-2"


def test_ownership_guard_is_a_no_op_on_clean_data(perf_rows):
    assert len(apply_ownership_guard(perf_rows)) == len(perf_rows)
