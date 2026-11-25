"""Scrubs, vetoes and the deterministic conflict table."""

from __future__ import annotations

import dataclasses
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _pl import NO_HOLDOUT, TODAY, bound, budget, card, ctx, slack  # noqa: E402

from cpm_lineage.actions.pipeline import (  # noqa: E402
    donor_exclusions, holdout_ids, plan_actions, scope_telemetry, scrub, under_test_entities,
    veto_capability, veto_under_test, veto_unfundable,
)
from cpm_lineage.actions.pipeline_config import (  # noqa: E402
    EntityState, PipelineConfig, SegmentStat, reason_code,
)
from cpm_lineage.actions.resolve import (  # noqa: E402
    apply_contested_check, contested_ratio, merge_duplicates, resolve, resolve_bid_conflicts,
    resolve_budget_conflicts, supply_gate,
)
from cpm_lineage.adapters.synthetic import synthetic_profile  # noqa: E402
from cpm_lineage.schema import AppliedAction, KeywordRef  # noqa: E402


# ---------------------------------------------------------------- holdout

def test_holdout_is_deterministic_and_rotates():
    ids = [f"e{i}" for i in range(40)]
    a = holdout_ids(ids, date(2026, 3, 5))
    assert a == holdout_ids(reversed(ids), date(2026, 3, 28)) == sorted(a)
    assert 0 < len(a) < 40
    assert holdout_ids(ids, date(2026, 3, 5), pct=0) == []
    assert holdout_ids(ids, date(2026, 3, 5), pct=100) == sorted(ids)
    months = {tuple(holdout_ids(ids, date(2026, m, 1))) for m in range(1, 13)}
    assert len(months) > 1
    weeks = {tuple(holdout_ids(ids, date(2026, 3, 2) + timedelta(days=7 * w), period="week")) for w in range(8)}
    assert len(weeks) > 1


def test_held_entities_receive_nothing():
    ids = [f"e{i}" for i in range(40)]
    c = ctx(entities=ids, cfg=PipelineConfig())
    cards = [card("efficiency-cut", "keyword", e, "down", keywords=["k"]) for e in ids]
    res = plan_actions(cards, c)
    assert res.holdout and res.holdout == holdout_ids(ids, TODAY)
    assert not {x.entity_id for x in res.cards} & set(res.holdout)
    assert res.telemetry["holdout_excluded"] == len(res.holdout) == res.telemetry["holdout"]


def test_ineligible_entities_never_enter_the_draw():
    ents = {f"e{i}": EntityState(f"e{i}", eligible=False) for i in range(40)}
    c = ctx(cfg=PipelineConfig(holdout_pct=100.0))
    c.entities = ents
    res = plan_actions([], c)
    assert res.holdout == []


# ---------------------------------------------------------------- scrubs

def test_dead_entities_receive_nothing():
    ents = {
        "gone": EntityState("gone", status="removed", spend_3d=500.0),
        "held0": EntityState("held0", status="budget_held", spend_3d=0.0),
        "held1": EntityState("held1", status="budget_held", spend_3d=50.0),
        "live": EntityState("live", status="active", spend_3d=0.0),
    }
    c = ctx(entities=list(ents))
    c.entities = ents
    cards = [card("bid", "bid", e, "down", keywords=["k"]) for e in ents] + [card("sov-gap", "watch", None)]
    kept, n_held, n_dead = scrub(cards, c)
    assert {x.entity_id for x in kept} == {"held1", "live", None}
    assert (n_held, n_dead) == (0, 2)
    kept, n_held, _ = scrub(cards, c, held=["live"])
    assert n_held == 1 and "live" not in {x.entity_id for x in kept}


def test_under_test_vetoes_everything_but_watch():
    applied = [AppliedAction("experiment", "c1", TODAY - timedelta(days=14)),
               AppliedAction("experiment", "c2", TODAY - timedelta(days=15)),
               AppliedAction("budget", "c3", TODAY, family="experiment"),
               AppliedAction("budget", "c4", TODAY, family="defense")]
    ut = under_test_entities(applied, TODAY)
    assert ut == {"c1", "c3"}
    assert donor_exclusions(applied, TODAY) == {"c1", "c3", "c4"}
    c = ctx(under_test=ut)
    cards = [budget("c1", "down"), card("defense", "watch", "c1"), budget("c2", "down")]
    assert veto_under_test(cards, c) == 1
    assert [x.status for x in cards] == ["vetoed", "pending", "pending"]
    assert reason_code(cards[0]) == "under_test"


def test_scope_telemetry_empty_scope_keeps_all():
    tel = {"a": 1, "b": 2}
    assert scope_telemetry(tel, None) == tel == scope_telemetry(tel, [])
    assert scope_telemetry(tel, ["b", "z"]) == {"b": 2}


def test_budget_raises_must_be_executable():
    unstated = card("growth", "budget", "c1", "up")                 # no frm / to
    no_tel = budget("zz", "up")
    not_bound = budget("c2", "up")
    ok = budget("c3", "up")
    cut = budget("c2", "down", title="cut")                          # cuts are never gated on sufficiency
    c = ctx(telemetry={"c2": slack("c2"), "c3": bound("c3")})
    assert veto_unfundable([unstated, no_tel, not_bound, ok, cut], c) == 3
    assert [reason_code(x) for x in (unstated, no_tel, not_bound, ok, cut)] == [
        "unstated_budget", "not_budget_bound", "not_budget_bound", None, None]
    assert ok.status == cut.status == "pending"


def test_budget_bound_definition_follows_the_rule_profile():
    # exhausts early on most days, but served 95% of expected live time in the last 3 days
    tel = dataclasses.replace(bound("c1"), sufficiency_3d=0.95)
    classic, verdict = budget("c1", "up"), budget("c1", "up")
    veto_unfundable([classic], ctx(telemetry={"c1": tel}))
    veto_unfundable([verdict], ctx(telemetry={"c1": tel}, rule_profile="verdict"))
    assert (classic.status, verdict.status) == ("pending", "vetoed")
    at, below = budget("c1", "up"), budget("c1", "up")
    veto_unfundable([at], ctx(telemetry={"c1": dataclasses.replace(tel, sufficiency_3d=0.90)}, rule_profile="verdict"))
    veto_unfundable([below], ctx(telemetry={"c1": dataclasses.replace(tel, sufficiency_3d=0.89)}, rule_profile="verdict"))
    assert (at.status, below.status) == ("vetoed", "pending")


def test_capability_playbook_binds():
    prof = synthetic_profile()
    ents = {"kw": EntityState("kw", ad_type="keyword_listing"), "reco": EntityState("reco", ad_type="recommendation"),
            "reach": EntityState("reach", ad_type="featured_listing"), "unk": EntityState("unk")}
    c = ctx(entities=list(ents), profile=prof, telemetry={})
    c.entities = ents
    cards = [card(f, t, e, title=f"{e}-{t}") for e in ents for f, t in
             (("growth", "budget"), ("bid", "bid"), ("efficiency-cut", "keyword"), ("defense", "watch"))]
    veto_capability(cards, c)
    pending = {x.title for x in cards if x.status == "pending"}
    assert pending == {"kw-budget", "kw-bid", "kw-keyword", "kw-watch", "reco-budget", "reco-watch",
                       "reach-watch", "unk-budget", "unk-bid", "unk-keyword", "unk-watch"}
    assert all(reason_code(x) == "capability_playbook" for x in cards if x.status == "vetoed")
    # telemetry ad type takes precedence over the entity record
    c2 = ctx(entities=["kw"], profile=prof, telemetry={"kw": bound("kw", ad_type="featured_listing")})
    b = budget("kw", "up")
    veto_capability([b], c2)
    assert b.status == "vetoed"


# ---------------------------------------------------------------- supply gate

def test_supply_gate_threshold_and_exemptions():
    def run(avail, **kw):
        cards = [budget("c1", "up"), budget("c2", "up", family="defense"), budget("c3", "down"),
                 card("bid", "bid", "c1", "up", keywords=["k"])]
        n = supply_gate(cards, ctx(segments={"seg": SegmentStat(availability=avail)}, **kw))
        return n, [x.status for x in cards]

    assert run(0.74) == (2, ["vetoed", "pending", "pending", "vetoed"])
    assert run(74.0) == (2, ["vetoed", "pending", "pending", "vetoed"])     # percent scale accepted
    assert run(0.75)[0] == 0
    assert run(None)[0] == 0                                                 # unmeasured passes
    assert run(0.10, rule_profile="verdict")[0] == 0                         # availability gates nothing
    assert run(0.10, cfg=PipelineConfig(supply_gate_enabled=False))[0] == 0


def test_unarmed_supply_gate_is_disclosed():
    res = plan_actions([budget("c1", "up")], ctx(segments={}))
    assert "NOT armed" in res.guard_note
    assert "NOT armed" not in plan_actions([budget("c1", "up")], ctx()).guard_note
    assert "NOT armed" not in plan_actions([budget("c1", "up")], ctx(segments={}, rule_profile="verdict")).guard_note


# ---------------------------------------------------------------- merge / bid

def test_duplicate_merge_keeps_higher_stake_first_seen_on_ties():
    a = budget("c1", "down", family="geo", stake=100, title="a")
    b = budget("c1", "down", family="geo", stake=900, title="b")
    d = budget("c1", "down", family="geo", stake=900, title="d")
    other = budget("c2", "down", title="o")
    out = merge_duplicates([a, b, d, other])
    assert [x.title for x in out] == ["b", "o"]
    assert a.status == d.status == "merged" and a.superseded_by == b.id
    assert b.trigger.count(" · also: ") == 2
    a2, b2 = budget("c1", "down", stake=100, title="a"), budget("c1", "down", family="geo", stake=900, title="b")
    out = merge_duplicates([a2, b2], PipelineConfig(merge_keeper="first_seen"))
    assert [x.title for x in out] == ["a"]


def test_merge_never_folds_a_higher_priority_family_into_a_lower_one():
    defend = budget("c1", "up", family="defense", stake=500, title="defend")
    scale = budget("c1", "up", family="efficiency-scale", stake=5000, title="scale")
    cut = budget("c1", "down", family="efficiency-cut", stake=1000, title="cut")
    out = resolve([scale, defend, cut], ctx())
    assert defend.status == "pending" and scale.status == "merged" and scale.superseded_by == defend.id
    assert cut.status == "superseded" and cut.superseded_by == defend.id      # defense outranks the cut
    assert [c.title for c in out] == ["defend", "cut"]


def test_keyword_cards_on_different_terms_are_not_duplicates():
    one = card("bid", "bid", "c1", "down", title="Decrease Bid", keywords=[KeywordRef(kw="bad1", spend=10.0)])
    two = card("bid", "bid", "c1", "down", title="Decrease Bid", keywords=[KeywordRef(kw="bad2", spend=10.0)])
    same = card("bid", "bid", "c1", "down", title="Decrease Bid again", keywords=[KeywordRef(kw="BAD1 ", spend=5.0)])
    out = merge_duplicates([one, two, same])
    assert [c.keywords[0].kw for c in out] == ["bad1", "bad2"] and same.status == "merged"


def test_merge_keeps_the_choice_pair_intact():
    plain = budget("c1", "down", stake=900, title="plain cut")
    paired = budget("c1", "down", stake=900, title="ladder cut")
    bid = card("efficiency-cut", "bid", "c1", "down", title="first response",
               keywords=[KeywordRef(kw="k", spend=1.0)])
    paired.tension_group = bid.tension_group = "c1"
    paired.tension_with, bid.tension_with = bid.id, paired.id
    out = merge_duplicates([plain, paired, bid])
    assert [c.title for c in out] == ["plain cut", "first response"]
    assert plain.tension_group == "c1" and plain.tension_with == bid.id and bid.tension_with == plain.id


def test_no_duplicate_pending_entity_type_direction():
    rng = random.Random(3)
    fams = ["efficiency-cut", "efficiency-scale", "growth", "defense", "sov-gap", "geo"]
    cards = [budget(f"c{rng.randint(1, 3)}", rng.choice(["up", "down"]), family=rng.choice(fams),
                    stake=rng.randint(1, 5000), title=f"t{i}") for i in range(40)]
    out = resolve(cards, ctx())
    keys = [(c.entity_id, c.type, c.direction) for c in out if c.status in ("pending", "tension")]
    assert len(keys) == len(set(keys))
    assert all(c.status != "merged" for c in out)


def test_one_term_one_direction_per_entity():
    up = card("bid", "bid", "c1", "up", keywords=["Protein Bar"], title="up", spend=500.0)
    down = card("bid", "bid", "c1", "down", keywords=["protein bar"], title="down", spend=900.0)
    other_term = card("bid", "bid", "c1", "up", keywords=["energy bar"], title="o", spend=1.0)
    other_ent = card("bid", "bid", "c2", "up", keywords=["protein bar"], title="e", spend=1.0)
    assert resolve_bid_conflicts([up, down, other_term, other_ent]) == 1
    assert up.status == "superseded" and up.superseded_by == down.id
    assert reason_code(up) == "bid_conflict" and "900" in up.veto_reason and "500" in up.veto_reason
    assert down.status == other_term.status == other_ent.status == "pending"
    # ties favour the earlier card
    a = card("bid", "bid", "c1", "up", keywords=["k"], title="a", spend=5.0)
    b = card("bid", "bid", "c1", "down", keywords=["k"], title="b", spend=5.0)
    resolve_bid_conflicts([a, b])
    assert (a.status, b.status) == ("pending", "superseded")


# ---------------------------------------------------------------- budget conflicts

def test_cut_outranks_scale_and_allies_stay():
    cut = budget("c1", "down", stake=1000, title="cut")
    scale = budget("c1", "up", stake=1000, title="scale")
    ally = card("experiment", "experiment", "c1", stake=10, title="pause-test", pauses=True)
    kw = card("efficiency-cut", "keyword", "c1", "down", keywords=["k"], title="kw")
    bid = card("bid", "bid", "c1", "up", keywords=["k"], title="bid")
    assert resolve_budget_conflicts([cut, scale, ally, kw, bid]) == (1, 0)
    assert scale.status == "superseded" and scale.superseded_by == cut.id
    assert "efficiency-cut outranks efficiency-scale" in scale.veto_reason
    assert cut.status == ally.status == kw.status == bid.status == "pending"


def test_defense_raise_beats_cut_and_pausing_experiment():
    up = budget("c1", "up", family="defense", stake=500, title="up")
    cut = budget("c1", "down", stake=500, title="cut")
    pause = card("experiment", "experiment", "c1", stake=10, title="pause", pauses=True)
    plain = card("experiment", "experiment", "c1", stake=10, title="plain")
    resolve_budget_conflicts([cut, up, pause, plain])
    assert (up.status, cut.status, pause.status, plain.status) == ("pending", "superseded", "superseded", "pending")


def test_priority_tie_resolves_to_list_order():
    g = budget("c1", "up", family="growth", title="g")
    s = budget("c1", "down", family="sov-gap", title="s")
    resolve_budget_conflicts([s, g])
    assert (s.status, g.status) == ("pending", "superseded")


def test_stake_ratio_valve_abstains_at_threshold():
    def run(loser_stake):
        cut, up = budget("c1", "down", stake=1000, title="cut"), budget("c1", "up", stake=loser_stake, title="up")
        resolve_budget_conflicts([cut, up])
        return cut, up

    cut, up = run(3000)                      # exactly 3x: the table still decides
    assert (cut.status, up.status) == ("pending", "superseded")
    cut, up = run(3001)
    assert cut.status == up.status == "tension"
    assert cut.tension_group == up.tension_group == "c1"
    assert cut.tension_with == up.id and up.tension_with == cut.id
    assert "exactly ONE of the 2" in cut.veto_reason and reason_code(up) == "tension"
    cut, up = run(50000)
    assert cut.status == up.status == "tension"
    # the winner's stake is floored at 1 in the ratio
    z, big = budget("c1", "down", stake=0, title="z"), budget("c1", "up", stake=4, title="big")
    resolve_budget_conflicts([z, big])
    assert z.status == "tension"


def test_three_way_conflict_is_one_group():
    cut = budget("c1", "down", stake=100, title="cut")
    up1 = budget("c1", "up", family="growth", stake=5000, title="g")
    up2 = budget("c1", "up", family="sov-gap", stake=50, title="s")
    assert resolve_budget_conflicts([cut, up1, up2]) == (0, 3)
    assert {c.tension_group for c in (cut, up1, up2)} == {"c1"}
    assert cut.tension_with == up1.id and up1.tension_with == up2.tension_with == cut.id


def test_ally_stake_does_not_trip_the_valve_unless_configured():
    def run(cfg):
        cut = budget("c1", "down", stake=100, title="cut")
        ally = card("experiment", "experiment", "c1", stake=9000, title="pause", pauses=True)
        up = budget("c1", "up", stake=100, title="up")
        resolve_budget_conflicts([cut, ally, up], cfg)
        return [x.status for x in (cut, ally, up)]

    assert run(None) == ["pending", "pending", "superseded"]
    assert run(PipelineConfig(valve_counts_allies=True)) == ["tension"] * 3


def test_never_pending_up_and_down_budget_on_one_entity():
    rng = random.Random(11)
    fams = ["efficiency-cut", "efficiency-scale", "growth", "defense", "sov-gap", "geo", "collision", "unknown"]
    for _ in range(60):
        cards = [budget(f"c{rng.randint(1, 3)}", rng.choice(["up", "down"]), family=rng.choice(fams),
                        stake=rng.choice([0, 10, 1000, 100000]), title=f"t{i}") for i in range(rng.randint(2, 9))]
        cards.append(card("experiment", "experiment", "c1", stake=rng.choice([1, 5000]), title="p", pauses=True))
        out = resolve(cards, ctx())
        for e in ("c1", "c2", "c3"):
            pend = [c for c in out if c.status == "pending" and c.entity_id == e
                    and (c.type == "budget" or c.evidence.get("pauses"))]
            sides = {"down" if (c.direction == "down" or c.type == "experiment") else "up" for c in pend}
            assert len(sides) <= 1
            groups = {c.tension_group for c in out if c.status == "tension" and c.entity_id == e}
            assert groups <= {e}


# ---------------------------------------------------------------- contested days

def test_contested_ratio_and_partition_veto():
    d = [date(2026, 3, 1) + timedelta(days=i) for i in range(10)]
    assert contested_ratio({}) is None
    assert contested_ratio({"a": d[:5], "b": d[5:]}) == 0.0
    assert contested_ratio({"a": d, "b": d[:3]}) == 0.3
    hit = card("collision", "keyword", "c1", "down", keywords=["k"], title="hit")
    turn = card("collision", "keyword", "c2", "down", keywords=["k"], title="turn")
    nodata = card("collision", "keyword", "c3", "down", keywords=["k"], title="nodata")
    n = apply_contested_check([hit, turn, nodata], {hit.id: {"a": d, "b": d[:3]}, turn.id: {"a": d, "b": d[:2]}})
    assert n == 1 and (hit.status, turn.status, nodata.status) == ("pending", "vetoed", "pending")
    assert reason_code(turn) == "partitioned" and turn.evidence["contested_ratio"] == 0.2
