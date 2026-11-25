"""Bid-ceiling guardrail, position-bidder audit, rule inventory and rendering."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from cpm_lineage.cpm import (
    CpmConfig, GuardrailPolicy, audit_guardrails, ceiling_bid, ceiling_multiplier, decision_counts,
    decision_rates, guardrail_band, guardrail_policy, live_state, position_seen, rule_text,
    ruleset_members,
)
from cpm_lineage.cpm.rules import (
    NO_RULE, UNRESOLVED_BID, Rule, RuleSchema, Ruleset, RulesetMember, assemble, position_conditions,
    select_rulesets, stats, when_text,
)
from cpm_lineage.schema import BidDecision, BidRecord, Entity, PerfRow, Window

D0 = date(2026, 3, 1)
A = Window(D0 + timedelta(days=7), D0 + timedelta(days=13))
B = Window(D0, D0 + timedelta(days=6))
NOON = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)


def pt(target, gp=None, gt="percentage", **kw):
    cond = {"position_targeting": True, "position_target": target, "min_bid": 50, "max_bid": 900}
    if gp is not None:
        cond.update(guardrail_percentage=gp, guardrail_type=gt)
    cond.update(kw)
    return {"rules": [cond]}


def dec(rule, bid, n, *, pos=1.0, cap=False, tmin=1, tmax=3, kw="alpha", ts=NOON, new_bid=100.0):
    return [
        BidDecision(ts + timedelta(minutes=i), kw, new_bid, rule_id=rule, bid_id=bid, observed_position=pos,
                    target_position_min=tmin, target_position_max=tmax, at_ceiling=cap, success=True)
        for i in range(n)
    ]


def perf(day, ent, kw, spend, impr=1000.0, adv="adv-1"):
    return PerfRow(D0 + timedelta(days=day), ent, spend, impr, advertiser_id=adv, targeting_unit=kw,
                   targeting_type="keyword")


# ---------------------------------------------------------------- ceiling

def test_ceiling_is_base_times_one_plus_guardrail():
    pol = guardrail_policy(pt(1, 300)["rules"][0])
    assert pol == GuardrailPolicy("percentage_of_base", 300.0, 900.0, 50.0)
    assert ceiling_multiplier(pol) == pytest.approx(4.0)                 # 300% -> 4.0x base
    assert ceiling_bid(250, pol) == pytest.approx(1000.0)                # max bid 900 is NOT a cap here
    assert ceiling_bid(250, pol, CpmConfig(percentage_ignores_max_bid=False)) == pytest.approx(900.0)
    assert ceiling_bid(None, pol) is None and ceiling_bid(0, pol) is None
    absolute = guardrail_policy(pt(1)["rules"][0])
    assert absolute.kind == "absolute_cap" and ceiling_multiplier(absolute) is None
    assert ceiling_bid(250, absolute) == pytest.approx(900.0)
    zero = guardrail_policy(pt(1, 0)["rules"][0])
    assert zero.kind == "absolute_cap"                                   # zero percentage is not a guardrail


def test_guardrail_bands_at_edges():
    assert guardrail_band(50) == "<=50%" and guardrail_band(50.1) == "51-100%"
    assert guardrail_band(100) == "51-100%" and guardrail_band(200) == "101-200%"
    assert guardrail_band(300) == "201-300%" and guardrail_band(300.5) == ">300%"
    assert guardrail_band(None) == "absolute cap" and guardrail_band(0) == "absolute cap"
    assert guardrail_band(80, "absolute") == "absolute cap"


def test_position_seen_range_and_sentinel():
    assert position_seen(1) and position_seen(60)
    assert not position_seen(0) and not position_seen(61) and not position_seen(120) and not position_seen(None)


# ---------------------------------------------------------------- counters

def test_decision_counts_expose_both_hit_definitions():
    ds = (dec("r", "b", 2, pos=1, tmin=2, tmax=4)       # better than needed
          + dec("r", "b", 3, pos=3, tmin=2, tmax=4)     # inside the range
          + dec("r", "b", 1, pos=5, tmin=2, tmax=4)     # found, worse than target
          + dec("r", "b", 4, pos=120, tmin=2, tmax=4, cap=True))   # not found
    c = decision_counts(ds)["r"]
    assert (c.n, c.seen, c.hit_or_better, c.in_band, c.overshoot, c.cap) == (10, 6, 5, 3, 2, 4)
    r = decision_rates([c])
    assert r["hit"] == pytest.approx(0.5) and r["in_band"] == pytest.approx(0.3) and r["seen"] == pytest.approx(0.6)
    no_pos = decision_counts(ds, position_reported=False)["r"]
    assert no_pos.seen is None and no_pos.hit_or_better is None and no_pos.cap == 4
    assert decision_rates([]) == {"n": 0, "cap": None, "hit": None, "seen": None, "in_band": None, "overshoot": None}


def test_decision_counts_use_local_day_and_log_start():
    late = datetime(2026, 3, 7, 23, 30, tzinfo=timezone.utc)             # the day before A in UTC
    ds = dec("r", "b", 3, ts=late)
    assert decision_counts(ds, window=A) == {}
    assert decision_counts(ds, window=A, tz=120)["r"].n == 3            # local time is already the 8th
    assert decision_counts(ds, window=A, tz=120, log_from=date(2026, 3, 9)) == {}


# ---------------------------------------------------------------- audit

@pytest.fixture()
def world():
    rulesets = [
        Ruleset("hot", "Hot one", "keyword", advertiser_id="adv-1"),
        Ruleset("blind", "Blind one", "keyword", advertiser_id="adv-1"),
        Ruleset("abs", "Capped", "campaign", advertiser_id="adv-1"),
        Ruleset("other", "Other advertiser", "keyword", advertiser_id="adv-2"),
        Ruleset("arch", "Archived", "keyword", is_archived=True, advertiser_id="adv-1"),
        Ruleset("plain", "No position rule", "keyword", advertiser_id="adv-1"),
        Ruleset("tmpl", "Template", "keyword", is_template=True, advertiser_id="adv-1"),
    ]
    rules = [
        Rule("h1", "hot", conditions=pt(1, 300)), Rule("h2", "hot", conditions=pt(1, 120)),
        Rule("b1", "blind", conditions=pt({"min": 1, "max": 3}, 50)),
        Rule("a1", "abs", conditions=pt(2)),
        Rule("o1", "other", conditions=pt(1, 80)),
        Rule("x1", "arch", conditions=pt(1, 80)), Rule("t1", "tmpl", conditions=pt(1, 80)),
        Rule("p1", "plain", conditions={"rules": [{"name": "roas", "operator": "less_than", "value": 2}]}),
        Rule("bad", "hot", conditions="{not json"),
    ]
    bids = [
        BidRecord("c1", "Alpha", live_bid=300, base_bid=100, bid_id="k1"),
        BidRecord("c1", "beta", live_bid=100, base_bid=100, bid_id="k2"),
        BidRecord("c2", "alpha", live_bid=150, base_bid=100, bid_id="k3"),
        BidRecord("c2", "neg", live_bid=150, base_bid=100, bid_id="k4", is_negative=True),
        BidRecord("c3", "gamma", live_bid=50, base_bid=100, bid_id="k5"),
        BidRecord("c9", "alpha", live_bid=80, base_bid=40, bid_id="k9"),
    ]
    members = [
        RulesetMember("hot", "k1"), RulesetMember("hot", "k2"), RulesetMember("hot", "k1"),   # duplicate row
        RulesetMember("blind", "k1"), RulesetMember("blind", "k3"), RulesetMember("blind", "k4"),
        RulesetMember("abs", "c3"),
        RulesetMember("other", "k9"),
        RulesetMember("arch", "k2"), RulesetMember("plain", "k2"),
    ]
    decisions = (
        dec("h1", "k1", 9, pos=5, cap=True, tmax=1) + dec("h1", "k1", 15, pos=1, tmax=1)
        + dec("h1", "k2", 6, pos=5, tmax=1)                                   # h1: n 30, cap 9, hit 15, seen 30
        + dec("h2", "k2", 2, pos=5, cap=True, tmax=1) + dec("h2", "k2", 2, pos=1, tmax=1)
        + dec("h2", "k2", 6, pos=120, tmax=1)                                 # h2: n 10, cap 2, hit 2, seen 4
        + dec("b1", "k3", 7, pos=120, cap=True) + dec("b1", "k3", 3, pos=2)   # seen 30%, cap 70%
        + dec("o1", "k9", 10, pos=1)
        + dec("h1", "k1", 50, pos=120, cap=True, ts=NOON - timedelta(days=7))  # window B: ignored
    )
    rows = [
        perf(8, "c1", "alpha", 400), perf(9, "c1", "alpha", 100), perf(1, "c1", "alpha", 200),
        perf(8, "c1", "beta", 50), perf(8, "c2", "alpha", 30), perf(2, "c3", "gamma", 10),
        perf(8, "c9", "alpha", 77, adv="adv-2"), perf(8, "c1", "unheld", 999),
    ]
    return rulesets, rules, members, bids, decisions, rows


def test_audit_rows_rates_and_flags(world):
    out = audit_guardrails(*world, A, B)
    by = {r["ruleset_id"]: r for r in out["rows"]}
    assert set(by) == {"hot", "blind", "abs", "other"}                   # archived / template / plain excluded
    assert [r["ruleset_id"] for r in out["rows"]] == ["hot", "blind", "other", "abs"]   # A spend desc
    hot = by["hot"]
    assert hot["guardrail_pct"] == 300 and hot["ceiling_multiplier"] == pytest.approx(4.0)   # max across rules
    assert hot["rule_ids"] == ["h1", "h2"] and hot["target"] == "<= 1" and by["blind"]["target"] == "1–3"
    assert hot["a"]["spend"] == pytest.approx(550) and hot["b"]["spend"] == pytest.approx(200)
    assert hot["a"]["cpm"] == pytest.approx(1000 * 550 / 3000, abs=1e-9)
    assert (hot["n_keywords"], hot["n_entities"], hot["n_live"]) == (2, 1, 2)
    assert hot["overlap"] == 1 and by["blind"]["overlap"] == 1           # shared pair counted on both
    assert hot["up"] == pytest.approx(2.0)                               # median of 3.0 and 1.0
    assert hot["n_decisions"] == 40
    assert hot["cap"] == pytest.approx(0.275) and hot["seen"] == pytest.approx(0.85)   # decision-weighted
    assert hot["hit"] == pytest.approx(17 / 40)
    assert not hot["blind"] and not hot["hot"]                           # cap 27.5% is below 30%
    blind = by["blind"]
    assert len(blind["members"]) == 2                                    # negative bid excluded
    assert blind["seen"] == pytest.approx(0.3) and blind["cap"] == pytest.approx(0.7)
    assert blind["blind"] and not blind["hot"]                           # blind is never hot
    absr = by["abs"]
    assert absr["band"] == "absolute cap" and absr["ceiling_multiplier"] is None
    assert absr["cap"] is None and not absr["blind"] and not absr["hot"]
    assert absr["n_live"] == 0 and absr["b"]["spend"] == pytest.approx(10) and absr["up"] == pytest.approx(0.5)
    assert hot["band"] == "201-300%" and blind["band"] == "<=50%" and by["other"]["band"] == "51-100%"


def test_hot_fires_at_thresholds():
    def run(n_cap, n_hit, n=20):
        rs = [Ruleset("r", "R", "keyword", advertiser_id="adv-1")]
        rules = [Rule("r1", "r", conditions=pt(1, 100))]
        bids = [BidRecord("c1", "alpha", 100, 100, bid_id="k1")]
        ds = (dec("r1", "k1", n_cap, pos=5, cap=True, tmax=1) + dec("r1", "k1", n_hit, pos=1, tmax=1)
              + dec("r1", "k1", n - n_cap - n_hit, pos=5, tmax=1))
        return audit_guardrails(rs, rules, [RulesetMember("r", "k1")], bids, ds, [], A)["rows"][0]

    assert run(6, 9)["hot"]                    # cap 30%, hit 45%
    assert not run(5, 9)["hot"]                # cap 25%
    assert not run(6, 10)["hot"]               # hit 50% is not below 50%
    assert run(20, 0)["hot"]


def test_band_rollup_counts_a_shared_pair_once(world):
    out = audit_guardrails(*world, A, B)
    bands = {b["band"]: b for b in out["bands"]}
    assert list(bands) == ["<=50%", "51-100%", "201-300%", "absolute cap"]   # empty bands omitted
    # (c1, alpha) is held by 'hot' (300%) and 'blind' (50%): it counts under the more permissive one
    assert bands["201-300%"]["pairs"] == 2 and bands["201-300%"]["a"]["spend"] == pytest.approx(550)
    assert bands["<=50%"]["pairs"] == 1 and bands["<=50%"]["a"]["spend"] == pytest.approx(30)
    assert bands["201-300%"]["cap"] == pytest.approx(0.275)
    tot = out["total"]
    assert tot["n"] == 4 and tot["pairs"] == 5
    assert tot["a"]["spend"] == pytest.approx(sum(b["a"]["spend"] for b in out["bands"]), abs=1e-9)
    assert tot["a"]["spend"] == pytest.approx(550 + 30 + 77)             # 'unheld' keyword never enters
    assert sum(b["pairs"] for b in out["bands"]) == tot["pairs"]


def test_filtering_to_one_advertiser_leaks_nothing(world):
    out = audit_guardrails(*world, A, B, advertiser_id="adv-2")
    assert [r["ruleset_id"] for r in out["rows"]] == ["other"]
    assert out["total"]["a"]["spend"] == pytest.approx(77) and out["total"]["pairs"] == 1
    assert [b["band"] for b in out["bands"]] == ["51-100%"]
    assert out["total"]["n_decisions"] == 10


def test_umbrella_owner_resolved_by_spend(world):
    rulesets, rules, members, bids, decisions, rows = world
    rulesets = [Ruleset("other", "Shared", "keyword", advertiser_id="umbrella")] + rulesets[:3]
    out = audit_guardrails(rulesets, rules, members, bids, decisions, rows, A, B,
                           umbrella_advertisers=["umbrella"])
    assert {r["ruleset_id"]: r["owner"] for r in out["rows"]}["other"] == "adv-2"


def test_ruleset_member_drill(world):
    rulesets, rules, members, bids, decisions, rows = world
    out = audit_guardrails(*world, A, B)
    hot = next(r for r in out["rows"] if r["ruleset_id"] == "hot")
    drill = ruleset_members(hot, decisions, rows, A, B, entities=[Entity("c1", name="Camp one")])
    assert [m["keyword"] for m in drill["members"]] == ["alpha", "beta"]
    alpha, beta = drill["members"]
    assert alpha["up"] == pytest.approx(3.0) and alpha["entity_name"] == "Camp one"
    assert alpha["n"] == 24 and alpha["cap"] == pytest.approx(9 / 24)
    assert beta["n"] == 16 and beta["cap"] == pytest.approx(2 / 16)      # summed across both rules
    assert beta["seen"] == pytest.approx(10 / 16)
    assert alpha["a"]["spend"] == pytest.approx(500) and alpha["b"]["spend"] == pytest.approx(200)


def test_live_state_summary():
    now = datetime(2026, 3, 10, 12, 0)
    states = [
        {"ruleset_id": "r", "bid_id": "k1", "geo": "g1", "position": 2, "bid": 100, "checked_at": now},
        {"ruleset_id": "r", "bid_id": "k1", "geo": "g2", "position": 120, "bid": 300, "checked_at": now - timedelta(days=1)},
        {"ruleset_id": "r", "bid_id": "k2", "geo": "g1", "position": 4, "bid": 200, "checked_at": now - timedelta(days=2)},
        {"ruleset_id": "r", "bid_id": "k3", "geo": "g1", "position": 1, "bid": 1, "checked_at": now - timedelta(days=4)},
    ]
    out = live_state(states, now)["r"]
    assert out["bids"] == 2 and out["geos"] == 2 and out["found"] == pytest.approx(2 / 3)
    assert out["position_median"] == pytest.approx(3.0) and out["bid_median"] == pytest.approx(200)


# ---------------------------------------------------------------- rules

def test_rule_text_step_rule_and_position_rule():
    conds = {"rules": [
        {"name": "ad_rank", "operator": "greater_than", "value": 20, "days": 7},
        {"name": "time_range", "value": {"allowed_days": [], "start_time": "07:30", "end_time": "22:30"}},
    ]}
    acts = [{"name": "increase_bid", "params": {"percentage": 20, "guardrail_percentage": 140}}]
    assert rule_text(conds, acts) == "IF ad_rank > 20 AND daily 07:30–22:30 THEN increase bid 20% guardrail 140%"
    cond = pt({"min": 1, "max": 5}, 50, min_bid=200, max_bid=5000,
              time_range={"allowed_days": ["sunday", "saturday"], "start_time": "07:00", "end_time": "23:00"})
    assert rule_text(cond) == "target position 1–5, bid 200–5000, CPM guardrail 50%, sat,sun 07:00–23:00"
    assert rule_text(cond, "[]") == rule_text(cond)                      # the condition IS the configuration


def test_rule_text_lookback_suffix_and_operators():
    look = {"rules": [{"metric": "roas", "operator": "less_than", "value": 2, "days": 7}]}
    assert rule_text(look) == "roas < 2 (last 7d)"                       # lookback metric
    live = {"rules": [{"name": "daily_budget", "operator": "greater_than_or_equal_to", "value": 500, "days": 7}]}
    assert rule_text(live) == "daily_budget >= 500"                      # live metric: no suffix
    between = {"rules": [{"name": "spend", "operator": "in_between", "value": [100, 250.5], "days": 3}]}
    assert rule_text(between) == "spend between 100 and 250.5 (last 3d)"
    custom = RuleSchema(live_metrics=frozenset({"roas"}))
    assert rule_text(look, schema=custom) == "roas < 2"
    assert rule_text(None, [{"name": "pause_campaign"}]) == "pause campaign"
    assert when_text({"allowed_days": ["monday", "wednesday"], "start_time": "13:00", "end_time": "14:00"}) \
        == "mon,wed 13:00–14:00"
    assert when_text({}) == "daily 00:00–23:59"


def test_rule_text_never_raises():
    assert rule_text("{garbage", None) == NO_RULE
    assert rule_text('{"rules": [{"name": "x"}]}', "][") == NO_RULE
    assert isinstance(rule_text({"rules": [{"name": "time_range", "value": 7}]}), str)
    assert isinstance(rule_text(12345, object()), str)
    assert position_conditions("{garbage") == [] and position_conditions(None) == []


def test_assemble_inventory_and_stats():
    rulesets = [
        Ruleset("z", "zeta", "keyword", is_archived=True),
        Ruleset("k", "Beta kw", "keyword"),
        Ruleset("c", "alpha camp", "campaign"),
        Ruleset("i", "Idle", "campaign", is_active=False),
        Ruleset("e", "Empty", "keyword"),
        Ruleset("t", "Template", "keyword", is_template=True),
        Ruleset("d", "Deleted", "keyword", is_deleted=True),
    ]
    bids = [BidRecord("c1", "Alpha ", live_bid=120, bid_id="b1", match_type="exact"),
            BidRecord("c3", "beta", live_bid=80, bid_id="b2")]
    entities = [Entity("c1", name="Zed"), Entity("c2", name="Able"), Entity("c3", name="Mid")]
    members = [RulesetMember("c", "c1"), RulesetMember("c", "c2"), RulesetMember("c", "c1"),
               RulesetMember("k", "b1"), RulesetMember("k", "b2"), RulesetMember("k", "missing"),
               RulesetMember("z", "b1")]
    rules = [Rule("r1", "k", conditions=pt(1, 50)), Rule("r2", "k", name="sys_generated", conditions=pt(9)),
             Rule("r3", "c", conditions="{garbage")]
    inv = assemble(rulesets, members, rules, bids=bids, entities=entities)
    assert [r["id"] for r in inv] == ["c", "k", "e", "i", "z"]            # active, inactive, archived; type; name
    camp, kw, empty = inv[0], inv[1], inv[2]
    assert [c["name"] for c in camp["campaigns"]] == ["Able", "Zed"]     # de-duplicated, sorted by name
    assert camp["config"] == [NO_RULE]
    assert kw["config"] == ["target position <= 1, bid 50–900, CPM guardrail 50%"]   # system rule hidden
    assert [k["keyword"] for k in kw["keywords"]] == [UNRESOLVED_BID, "alpha", "beta"]
    assert kw["keywords"][1]["entity_name"] == "Zed" and kw["keywords"][1]["match_type"] == "exact"
    assert kw["n_entities"] == 2 and camp["n_entities"] == 2
    assert empty["campaigns"] == [] and empty["keywords"] == [] and empty["config"] == []
    assert stats(inv) == {"n": 5, "n_campaign_rulesets": 2, "n_keyword_rulesets": 3, "n_entities": 3, "n_keywords": 4}
    assert [r.id for r in select_rulesets(rulesets, "active")] == ["k", "c", "e"]
    assert [r.id for r in select_rulesets(rulesets, "inactive")] == ["z", "i"]
    with pytest.raises(ValueError):
        select_rulesets(rulesets, "everything")
