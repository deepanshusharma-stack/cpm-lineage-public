"""Guardrail audit: id types, counter coverage and the profile's position range."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from cpm_lineage.adapters.base import PlatformProfile, PositionSemantics
from cpm_lineage.cpm.guardrails import DecisionCounts, audit_guardrails, decision_rates, ruleset_members
from cpm_lineage.cpm.rules import Rule, Ruleset, RulesetMember
from cpm_lineage.schema import BidDecision, BidRecord, PerfRow, Window

D = date(2025, 1, 1)
A = Window(D, D + timedelta(days=6))


def cond(pct=50):
    return {"rules": [{"position_targeting": True, "position_target": {"min": 1, "max": 3}, "min_bid": 10,
                       "max_bid": 500, "guardrail_percentage": pct, "guardrail_type": "percentage"}]}


BIDS = [BidRecord("c1", "milk", 150, 100, bid_id=7)]
# a fully blind bidder pinned at its ceiling: the position is never found
DECISIONS = [BidDecision(datetime(2025, 1, 2, 12), "milk", 150, rule_id=10, bid_id=7, entity_id="c1", success=True,
                         observed_position=120, target_position_min=1, target_position_max=3, at_ceiling=True)
             for _ in range(10)]
PERF = [PerfRow(D, "c1", 100, 1000, targeting_unit="milk", targeting_type="keyword", position=2)]


@pytest.mark.parametrize("rs_id, rule_id, rule_rs, member_bid", [(1, 10, 1, 7), ("1", "10", "1", "7"),
                                                                  (1, "10", 1, "7"), ("1", 10, "1", 7)])
def test_blind_rule_is_flagged_whatever_the_id_types(rs_id, rule_id, rule_rs, member_bid):
    out = audit_guardrails([Ruleset(rs_id, "rs")], [Rule(rule_id, rule_rs, conditions=cond())],
                           [RulesetMember(rs_id, member_bid)], BIDS, DECISIONS, PERF, A)
    row = out["rows"][0]
    assert row["n_decisions"] == 10 and row["blind"] is True and row["no_decisions"] is False
    members = ruleset_members(row, DECISIONS, PERF, A)["members"]
    assert [(m["n"], m["seen"]) for m in members] == [(10, 0.0)]


def test_rule_set_without_matched_decisions_is_marked_unmeasured():
    out = audit_guardrails([Ruleset(1, "rs")], [Rule(10, 1, conditions=cond())], [RulesetMember(1, 7)], BIDS, [],
                           PERF, A)
    row = out["rows"][0]
    assert row["n_decisions"] == 0 and row["no_decisions"] is True and not row["blind"] and not row["hot"]


def test_rates_are_not_diluted_by_groups_that_lack_the_counter():
    r = decision_rates([DecisionCounts(n=10, cap=10, seen=10, hit_or_better=0),
                        DecisionCounts(n=90, cap=None, seen=90, hit_or_better=None)])
    assert r["n"] == 100 and r["cap"] == pytest.approx(1.0) and r["hit"] == pytest.approx(0.0)
    assert r["seen"] == pytest.approx(1.0)
    assert decision_rates([DecisionCounts(n=10, cap=5), DecisionCounts(n=30, cap=3)])["cap"] == pytest.approx(0.2)


def test_audit_honours_the_profile_position_range():
    profile = PlatformProfile(position=PositionSemantics(valid_max=10))
    perf = [PerfRow(D, "c1", 100, 1000, targeting_unit="milk", targeting_type="keyword", position=50)]
    out = audit_guardrails([Ruleset(1, "rs")], [Rule(10, 1, conditions=cond())], [RulesetMember(1, 7)], BIDS,
                           DECISIONS, perf, A, profile=profile)
    row = out["rows"][0]
    assert row["a"]["position"] is None                           # 50 is outside the profile's 1..10
    members = ruleset_members(row, DECISIONS, perf, A, profile=profile)["members"]
    assert members[0]["a"]["position"] is None


def test_self_floor_check_is_skipped_when_bids_are_not_per_thousand():
    from cpm_lineage.cpm.overpay import check_overpay as overpay_check

    perf = [PerfRow(D, "c1", 100.0, 1000.0, targeting_unit="milk", targeting_type="keyword", position=1, geo="g"),
            PerfRow(D, "c2", 50.0, 1000.0, targeting_unit="milk", targeting_type="keyword", position=2, geo="g")]
    cpm = PlatformProfile(auction_type="second_price")
    per_imp = PlatformProfile(auction_type="second_price", bid_unit="per_impression")
    ok = overpay_check(perf, D, ["milk"], profile=cpm)
    assert ok["h2_checked"] is True and ok["h2_skip_reason"] is None
    skipped = overpay_check(perf, D, ["milk"], profile=per_imp)
    assert skipped["h2_checked"] is False and "per thousand" in skipped["h2_skip_reason"]
    first = overpay_check(perf, D, ["milk"], profile=PlatformProfile(auction_type="first_price"))
    assert first["h2_checked"] is False and "second-price" in first["h2_skip_reason"]
