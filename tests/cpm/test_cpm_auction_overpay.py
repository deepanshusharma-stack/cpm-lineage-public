"""Auction intelligence and the three-hypothesis keyword overpay check."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from cpm_lineage.cpm import (
    AgentDay, CpmConfig, agent_day_log, auction_hints, band_reference, check_overpay, classify_threat,
    clearing_bids, competitors, geo_from_name, h1_flags, organic_trend, overpay_flag, position_band,
    render_overpay_markdown, self_floor_flag, suggested_bids, suggested_bids_by_geo, threat_class, timing,
)
from cpm_lineage.cpm.auction import CLEARING_CAVEAT, OWN_LOG_CAVEAT, daypart_of
from cpm_lineage.cpm.config import DEFAULT_DAYPARTS
from cpm_lineage.schema import BidDecision, BidRecord, PerfRow, SuggestedBid, Window

T0 = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
DAY = date(2026, 3, 10)


def d(bid, *, kw="alpha", tmin=1, ts=T0, brand=None, cap=None, ok=True, organic=None, **kw_):
    return BidDecision(ts, kw, bid, target_position_min=tmin, brand=brand, at_ceiling=cap, success=ok,
                       organic_position=organic, **kw_)


# ---------------------------------------------------------------- clearing

def test_clearing_slot_split_quantiles_and_min_obs():
    ds = [d(b, tmin=1) for b in (100, 200, 300)] + [d(b, tmin=2) for b in (400, 500)]   # slot 1: five bids
    ds += [d(b, tmin=3) for b in (10, 20, 30, 40)]                                     # slot 3: only four
    ds += [d(9999, tmin=6), d(9999, tmin=0), d(9999, ok=False), d(0)]                  # all ignored
    ds += [d(9999, ts=T0 - timedelta(days=31))]                                        # too old
    out = clearing_bids(ds, ["Alpha"])
    assert out == {"alpha": {"s1_p25": 200.0, "s1_p50": 300.0, "s1_p75": 400.0, "s1_obs": 5}}
    ds.append(d(50, tmin=5))
    out = clearing_bids(ds)
    assert out["alpha"]["s3_obs"] == 5 and out["alpha"]["s3_p50"] == 30.0
    assert clearing_bids([]) == {} and clearing_bids(ds, ["other"]) == {}
    fine = clearing_bids([d(b) for b in (1.11, 1.22, 1.33, 1.44, 1.55)], cfg=CpmConfig(bid_round_digits=2))
    assert fine["alpha"]["s1_p50"] == pytest.approx(1.33)


def test_competitors_stack_and_own_brand_exclusion():
    ds = [d(100 + i, brand="Rival One", tmin=1, cap=(i < 2)) for i in range(5)]
    ds += [d(300, brand="Rival Two", tmin=3, cap=False) for _ in range(6)]
    ds += [d(900, brand="Own-Brand Foods", tmin=1) for _ in range(9)]
    ds += [d(900, brand="Tiny", tmin=1) for _ in range(4)]
    out = competitors(ds, ["alpha"], own_brand="ownbrand")
    rows = out["alpha"]
    assert [r["brand"] for r in rows] == ["Rival Two", "Rival One"]      # highest average bid first
    one = rows[1]
    assert one == {"brand": "Rival One", "avg_bid": 102.0, "max_bid": 104.0, "slot": 1.0, "obs": 5, "ceiling_pct": 40.0}
    assert "Own-Brand Foods" in [r["brand"] for r in competitors(ds, ["alpha"])["alpha"]]


def test_threat_classes_and_precedence():
    assert classify_threat(None, 100) is None
    top = {"ceiling_pct": 20, "slot": 1, "max_bid": 150, "avg_bid": 120}
    assert threat_class(top, 100) == "active threat"
    assert threat_class({**top, "ceiling_pct": 21}, 100) == "ceiling-constrained"     # outranks active threat
    assert threat_class(top, 150) == "present"                                        # max bid not above ours
    assert threat_class(top, None) == "present"                                       # None-safe own bid
    deep = {"ceiling_pct": 0, "slot": 3, "max_bid": 500, "avg_bid": 80}
    assert threat_class(deep, 100) == "background"
    assert threat_class({**deep, "avg_bid": 100}, 100) == "present"
    assert threat_class({"ceiling_pct": None, "slot": None, "max_bid": 1, "avg_bid": 1}, 100) == "present"
    assert classify_threat(top, 100).startswith("active threat")


def test_dayparts_including_wrap_around():
    parts = DEFAULT_DAYPARTS
    assert [daypart_of(h, parts) for h in (6, 9, 10, 13, 14, 17, 18, 21, 22, 23, 0, 5)] == [
        "06-10", "06-10", "10-14", "10-14", "14-18", "14-18", "18-22", "18-22", "22-06", "22-06", "22-06", "22-06"]


def test_timing_local_time_min_obs_and_single_bucket():
    at = lambda hour, minute=0: datetime(2026, 3, 10, hour, minute, tzinfo=timezone.utc)
    ds = [d(100, ts=at(3)) for _ in range(10)]             # UTC 03:00
    ds += [d(300, ts=at(15)) for _ in range(10)]           # UTC 15:00
    ds += [d(999, ts=at(8)) for _ in range(9)]             # nine observations: dropped
    ds += [d(50, kw="solo", ts=at(3)) for _ in range(20)]  # one bucket only: omitted
    utc = timing(ds)
    assert utc["alpha"]["cheap"] == ("22-06", 100.0) and utc["alpha"]["dear"] == ("14-18", 300.0)
    assert "solo" not in utc and set(utc["alpha"]["parts"]) == {"14-18", "22-06"}
    local = timing(ds, tz=330)                             # 03:00 UTC -> 08:30 local; 15:00 -> 20:30
    assert local["alpha"]["cheap"] == ("06-10", 100.0) and local["alpha"]["dear"] == ("18-22", 300.0)
    assert timing(ds[:10]) == {}


def test_organic_trend_slope_sign():
    monday = datetime(2026, 2, 2, 12, 0, tzinfo=timezone.utc)
    ds = []
    for week, pos in enumerate((9.0, 7.0, 5.0)):
        ds += [d(100, organic=pos, ts=monday + timedelta(weeks=week, days=i % 5)) for i in range(5)]
    ds += [d(100, organic=20.0, ts=monday + timedelta(weeks=3)) for _ in range(4)]     # thin week dropped
    ds += [d(100, organic=0.0, ts=monday) for _ in range(9)]                           # not found: ignored
    ds += [d(100, kw="one", organic=3.0, ts=monday) for _ in range(8)]                 # single week: omitted
    out = organic_trend(ds)
    assert out == {"alpha": {"position": 5.0, "delta_per_week": -2.0, "weeks": 3}}     # negative = improving
    worse = organic_trend([x for x in ds if x.organic_position != 5.0])
    assert worse["alpha"]["delta_per_week"] == -2.0 and worse["alpha"]["weeks"] == 2


def test_suggested_bids_anchor_to_feed_latest_date():
    last = date(2025, 6, 30)                                # an old feed: still anchored to its own latest date
    rows = [SuggestedBid("Alpha", 100, 200, searches=500, as_of=last),
            SuggestedBid("alpha", 200, 400, searches=900, as_of=last - timedelta(days=10)),
            SuggestedBid("alpha", 1, 9999, as_of=last - timedelta(days=31)),          # stale
            SuggestedBid("alpha", 1, 0, as_of=last),                                   # no upper suggestion
            SuggestedBid("beta", 5, 10, as_of=last)]
    out = suggested_bids(rows, ["alpha"])
    assert out == {"alpha": {"lo": 150.0, "hi": 300.0, "searches": 900, "as_of": last}}
    assert len(suggested_bids(rows, cfg=CpmConfig(suggested_max_keywords=1))) == 1
    geo_rows = [SuggestedBid("alpha", 10, 300, geo="north", as_of=last)] * 3 + \
               [SuggestedBid("alpha", 10, 100, geo="south", as_of=last)] * 3 + \
               [SuggestedBid("alpha", 10, 999, geo="east", as_of=last)] * 2
    by_geo = suggested_bids_by_geo(geo_rows, "alpha")
    assert [(g["geo"], g["hi"]) for g in by_geo] == [("north", 300.0), ("south", 100.0)]   # east has 2 rows
    assert len(suggested_bids_by_geo(geo_rows, "alpha", top=1)) == 1


def test_auction_hints_combine_clearing_gap_and_caveat():
    w = Window(DAY - timedelta(days=5), DAY)
    perf = [PerfRow(DAY, "c1", 3000, 10000, targeting_unit="alpha", bid=100),
            PerfRow(DAY, "c1", 1000, 10000, targeting_unit="alpha", bid=200),
            PerfRow(DAY, "c1", 1000, 10000, targeting_unit="small"),                   # not above the floor
            PerfRow(DAY, "c2", 9000, 10000, targeting_unit="elsewhere")]
    ds = [d(b, brand="Rival", tmin=1, cap=False) for b in (100, 200, 300, 400, 500)]
    hints = auction_hints(perf, w, ds, entity_id="c1", own_brand="own")
    assert [h["keyword"] for h in hints] == ["alpha"]
    h = hints[0]
    assert h["own_bid"] == pytest.approx(125.0)            # spend-weighted
    assert h["clearing"]["s1_p75"] == 400.0 and h["gap"] == 275.0 and h["clearing"]["s1_obs"] == 5
    assert h["competitor"]["brand"] == "Rival" and h["threat"].startswith("active threat")
    assert h["caveat"] == CLEARING_CAVEAT
    own = auction_hints(perf, w, ds, entity_id="c1", pooled=False)[0]
    assert own["competitor"] is None and own["threat"] is None and own["caveat"] == OWN_LOG_CAVEAT
    from cpm_lineage.config import Economics
    assert auction_hints(perf, w, ds, entity_id="c1", economics=Economics(currency_unit=10)) == []


# ---------------------------------------------------------------- overpay H1

def agent(**kw):
    base = dict(n=20, seen=20, over=0, hit=20, cap=0, bid_median=100.0, bid_min=80.0, bid_max=120.0,
                bid_first=100.0, bid_last=100.0)
    base.update(kw)
    return AgentDay(**base)


def codes(flags):
    return [f["code"] for f in flags]


def test_h1_each_flag_at_its_boundary():
    assert h1_flags(agent()) == [] and h1_flags(None) == [] and h1_flags(AgentDay()) == []
    assert codes(h1_flags(agent(seen=11, hit=11))) == ["BLIND"] and h1_flags(agent(seen=12, hit=12)) == []
    assert codes(h1_flags(agent(over=10, hit=10))) == ["OVERSHOOT"]
    assert h1_flags(agent(over=9, hit=11)) == []
    assert h1_flags(agent(over=10, hit=10, bid_last=99.0)) == []         # the bidder did cut the bid
    assert codes(h1_flags(agent(cap=6, hit=9))) == ["CAP"]
    assert h1_flags(agent(cap=5, hit=9)) == [] and h1_flags(agent(cap=6, hit=10)) == []
    assert codes(h1_flags(agent(cap=6, hit=None))) == ["CAP"]            # hit unknown: cap rate alone
    assert codes(h1_flags(agent(bid_min=99.5, bid_max=101.0))) == ["FLAT"]
    assert h1_flags(agent(bid_min=99.0, bid_max=101.0)) == []            # exactly 2% is not flat
    assert h1_flags(agent(n=9, seen=9, hit=9, bid_min=100.0, bid_max=100.0)) == []
    # BLIND does not suppress CAP here (unlike the guardrail audit)
    assert codes(h1_flags(agent(seen=5, hit=2, cap=15))) == ["BLIND", "CAP"]
    assert all(f["hypothesis"] == "H1" for f in h1_flags(agent(seen=5, hit=2, cap=15)))


def test_h1_without_position_columns_only_cap_and_flat():
    a = agent(seen=None, over=None, hit=None, cap=20, bid_min=100.0, bid_max=100.0)
    assert codes(h1_flags(a)) == ["CAP", "FLAT"]
    assert h1_flags(agent(seen=None, over=None, hit=None, cap=None)) == []


def test_agent_day_log_counters():
    def dd(i, pos, bid, cap=False, geo="g1", day_shift=0):
        return BidDecision(T0 + timedelta(minutes=i, days=day_shift), "alpha", bid, bid_id="k1", geo=geo,
                           observed_position=pos, target_position_min=2, target_position_max=4,
                           at_ceiling=cap, success=True)
    ds = [dd(0, 1, 90), dd(1, 3, 100), dd(2, 5, 110, cap=True), dd(3, 120, 130, cap=True),
          dd(4, 2, 95, geo="g2"), dd(5, 1, 5, day_shift=1), dd(6, 1, 0)]
    log = agent_day_log(ds, DAY)
    recs = {a.geo: a for a in log[("id", "k1")]}
    g1 = recs["g1"]
    assert (g1.n, g1.seen, g1.over, g1.hit, g1.cap) == (4, 3, 1, 1, 2)
    assert (g1.bid_first, g1.bid_last, g1.bid_min, g1.bid_max, g1.bid_median) == (90, 130, 90, 130, 105)
    assert g1.position_median == 3 and (g1.target_min, g1.target_max) == (2, 4)
    assert recs["g2"].n == 1
    blind = agent_day_log(ds, DAY, position_reported=False)[("id", "k1")][0]
    assert blind.seen is None and blind.hit is None and blind.over is None


# ---------------------------------------------------------------- overpay H2 / H3

def prow(ent, kw, geo, spend, impr, pos, name=None, bid=None, day=DAY):
    return PerfRow(day, ent, spend, impr, targeting_unit=kw, targeting_type="keyword", geo=geo,
                   position=pos, entity_name=name, bid=bid)


def test_h2_self_floor_boundary_and_auction_type():
    perf = [prow("top", "alpha", "g1", 100, 1000, 1.0), prow("low", "alpha", "g1", 10, 1000, 4.0),
            prow("nopos", "alpha", "g1", 10, 1000, None)]

    def run(low_bid, **kw):
        bids = [BidRecord("top", "alpha", 500, 500, bid_id="k1"), BidRecord("low", "alpha", low_bid, 50, bid_id="k2")]
        return check_overpay(perf, DAY, ["alpha"], bids=bids, auction_type="second_price", **kw)

    out = run(80.0)                                        # 80% of the upper CPM (100)
    by = {r["entity_id"]: r for r in out["rows"]}
    assert [r["entity_id"] for r in out["rows"]] == ["top", "low", "nopos"]   # best position first, missing last
    assert codes(by["top"]["flags"]) == ["SELF_FLOOR"] and by["top"]["flags"][0]["hypothesis"] == "H2"
    assert by["low"]["flags"] == [] and out["tally"] == {"H1": 0, "H2": 1, "H3": 0}
    assert run(79.0)["tally"]["H2"] == 0
    assert self_floor_flag(100, 80) and not self_floor_flag(100, 79.99) and not self_floor_flag(None, 80)
    assert not self_floor_flag(100, 0)
    # the bidder's last bid of the day is preferred over the live bid
    decisions = [BidDecision(T0 + timedelta(minutes=i), "alpha", b, bid_id="k2", success=True)
                 for i, b in enumerate((50.0, 95.0))]
    agent_run = run(10.0, decisions=decisions)
    by = {r["entity_id"]: r for r in agent_run["rows"]}
    assert by["low"]["mode"] == "agent" and by["top"]["mode"] == "static"
    assert codes(by["top"]["flags"]) == ["SELF_FLOOR"]
    # not a second-price auction: the hypothesis is not evaluated at all
    bids = [BidRecord("top", "alpha", 500, 500), BidRecord("low", "alpha", 500, 50)]
    for kind in ("first_price", "unknown", None):
        res = check_overpay(perf, DAY, ["alpha"], bids=bids, auction_type=kind)
        assert res["tally"]["H2"] == 0 and not res["h2_checked"]
    assert "H2 not checked" in render_overpay_markdown(res)


def test_h3_band_edges_reference_and_strict_ratio():
    assert [position_band(p) for p in (1.0, 1.01, 3, 3.01, 6, 6.5, 10, 10.1, 100, 100.1, None)] == [
        "1", "2-3", "2-3", "4-6", "4-6", "7-10", "7-10", "11+", "11+", "—", "—"]
    assert overpay_flag(126, 100) and not overpay_flag(125, 100) and not overpay_flag(126, None)
    base = [{"keyword": "alpha", "cpm": c, "position": 2.0} for c in (90, 100, 110)]
    assert band_reference(base) == {("alpha", "2-3"): 100}
    assert band_reference(base[:2]) == {}                               # fewer than 3 rows: no reference
    assert band_reference(base + [{"keyword": "alpha", "cpm": None, "position": 2.0}]) == {("alpha", "2-3"): 100}

    def run(cpm_high):
        perf = [prow(f"c{i}", "alpha", f"g{i}", 100, 1000, 2.0) for i in range(4)]
        perf.append(prow("hot", "alpha", "g9", cpm_high, 1000, 2.5))
        perf.append(prow("deep", "alpha", "g9", 900, 1000, 8.0))        # another band, alone: no verdict
        return check_overpay(perf, DAY, ["alpha"], top_geos=1)

    over = run(126.0)
    assert over["geos"] == ["g9"] and [r["entity_id"] for r in over["rows"]] == ["hot", "deep"]
    hot, deep = over["rows"]
    assert hot["ref_cpm"] == pytest.approx(100.0)                       # median over ALL geos, not the shown one
    assert codes(hot["flags"]) == ["OVERPAY"] and over["tally"] == {"H1": 0, "H2": 0, "H3": 1}
    assert deep["ref_cpm"] is None and deep["flags"] == []
    assert run(125.0)["tally"]["H3"] == 0                               # strictly above 1.25


def test_check_overpay_rows_geo_fallback_and_markdown():
    perf = [prow("c1", "Alpha  One", None, 100, 1000, 999999, name="North shore push"),
            prow("c1", "alpha one", None, 50, 1000, 2.0, name="North shore push"),
            prow("c2", "alpha one", None, 10, 100, 2.0, name="No geo here"),
            prow("c3", "alpha one", "g1", 0, 100, 2.0),                 # zero spend: skipped
            prow("c4", "other", "g1", 10, 100, 2.0),                    # not requested
            prow("c5", "alpha one", "g1", 10, 100, 2.0, day=DAY - timedelta(days=1))]
    resolver = lambda name: geo_from_name(name, ["north", "north shore", "shore"])
    out = check_overpay(perf, DAY, ["alpha one"], geo_resolver=resolver)
    assert [(r["entity_id"], r["geo"]) for r in out["rows"]] == [("c1", "north shore"), ("c2", "All/unknown")]
    c1 = out["rows"][0]
    assert c1["spend"] == pytest.approx(150) and c1["cpm"] == pytest.approx(75.0)
    assert c1["position"] == pytest.approx(2.0)                         # sentinel position excluded from the mean
    assert c1["mode"] == "static" and c1["agent"] is None
    assert geo_from_name("Northern lights", ["north"]) is None          # whole words only
    text = render_overpay_markdown(out)
    assert text.splitlines()[0].startswith("| geo | keyword") and text.splitlines()[-1].startswith("H1 0 | H2")


def test_overpay_on_synthetic_day_is_structurally_sound(synthetic_dataset):
    ds = synthetic_dataset
    out = check_overpay(ds.perf, ds.latest_date, ["protein bar", "energy bar"], profile=ds.profile)
    assert out["rows"] and not out["h2_checked"]                        # first-price platform: H2 not evaluated
    assert out["tally"]["H1"] == 0 and out["tally"]["H2"] == 0          # no decision log supplied
    for r in out["rows"]:
        assert r["cpm"] == pytest.approx(1000 * r["spend"] / r["impressions"], abs=1e-9)
        assert r["mode"] == "static"
    # the same keyword in two own campaigns (planted overlap) appears as separate rows per geo
    overlap = {r["entity_id"] for r in out["rows"] if r["keyword"] == "energy bar"}
    assert {"cmp-generic", "cmp-overlap"} <= overlap
