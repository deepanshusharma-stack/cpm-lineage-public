"""Caps, annotations, waves, plan assembly, rebalancing and the synthetic end-to-end run."""

from __future__ import annotations

import copy
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _pl import NO_HOLDOUT, TODAY, bound, budget, card, ctx, slack  # noqa: E402

from cpm_lineage.actions.pipeline import (  # noqa: E402
    Plan, annotate_demand_priority, annotate_posture, annotate_term_owner, apply_polish, assign_waves,
    build_plan, coordinate, demand_priority, family_caps, pending_cap, plan_actions,
)
from cpm_lineage.actions.pipeline_config import (  # noqa: E402
    EntityState, PipelineConfig, PlanContext, RunCheck, SegmentStat, TermOwner,
)
from cpm_lineage.actions.rebalance import BoardEntry, funding_need, rebalance  # noqa: E402
from cpm_lineage.config import Economics  # noqa: E402
from cpm_lineage.schema import BudgetTelemetry, TargetingUnit  # noqa: E402


# ---------------------------------------------------------------- caps

def test_family_caps_drop_lowest_stake_and_ignore_vetoed():
    cards = [card("growth", "keyword", f"e{i}", "up", stake=10 * (i + 1), keywords=["k"], title=f"g{i}") for i in range(5)]
    vetoed = card("growth", "keyword", "v", "up", stake=9999, title="vetoed")
    vetoed.status = "vetoed"
    kept, dropped = family_caps(cards + [vetoed])
    assert dropped == 2
    assert {c.title for c in kept} == {"g2", "g3", "g4", "vetoed"}      # a vetoed card never uses a slot
    kept, dropped = family_caps(cards, PipelineConfig(family_caps={}, family_cap_default=99))
    assert dropped == 0 and len(kept) == 5


def test_caps_never_strand_half_a_choice_pair():
    cards = [card("growth", "keyword", f"e{i}", "up", stake=100 - i, title=f"g{i}") for i in range(4)]
    cards[3].tension_group = cards[0].tension_group = "pair"
    kept, dropped = family_caps(cards)
    assert dropped == 0 and len(kept) == 4
    cards[0].tension_group = None
    assert family_caps(cards)[1] == 1


def test_global_pending_cap_keeps_tension_groups_whole():
    cards = [card("bid", "bid", f"e{i}", "up", title=f"b{i}") for i in range(30)]
    done = card("geo", "geo", "x", title="vetoed")
    done.status = "vetoed"
    kept, cut = pending_cap(cards + [done], 25)
    assert cut == 5 and [c.title for c in kept] == [f"b{i}" for i in range(25)] + ["vetoed"]
    assert pending_cap(cards[:25], 25) == (cards[:25], 0)
    cards[29].tension_group = cards[2].tension_group = "g"
    kept, cut = pending_cap(cards, 25)
    assert cut == 4 and cards[29] in kept
    cards[2].tension_group, cards[28].tension_group = None, "g"         # both members beyond the cap
    assert pending_cap(cards, 25)[1] == 5


# ---------------------------------------------------------------- annotations

def test_posture_annotates_and_never_vetoes():
    def run(total, own):
        k = ctx(segments={"seg": SegmentStat(availability=0.95, total_sales=total[0], total_sales_prior=total[1],
                                             own_sales=own, own_sales_prior=own)})
        cards = [budget("c1", "up", title="up"), budget("c2", "down", title="down")]
        return annotate_posture(cards, k), cards

    n, cards = run((1000.0, 1000.0), 600.0)
    assert n == 1 and "saturation" in cards[0].posture and cards[0].posture in cards[0].trigger
    assert cards[0].status == "pending" and cards[1].posture is None
    assert run((1000.0, 1000.0), 599.0)[0] == 0
    n, cards = run((890.0, 1000.0), 100.0)
    assert n == 1 and "declining" in cards[0].posture
    assert run((900.0, 1000.0), 100.0)[0] == 0
    assert annotate_posture([budget("c1", "up")], ctx()) == 0


def test_demand_priority_ranks_role_buckets_by_volume():
    units = [TargetingUnit("Big Generic", role="generic", search_volume=9000, segment="seg"),
             TargetingUnit("own term", role="own_brand", search_volume=500, segment="seg"),
             TargetingUnit("rival term", role="competitor", search_volume=3000, segment="seg"),
             TargetingUnit("other", role="generic", search_volume=1, segment="seg2")]
    pri = demand_priority(units)
    assert pri == {("seg", "big generic"): 0, ("seg", "rival term"): 1, ("seg", "own term"): 2, ("seg2", "other"): 0}
    k = ctx(keyword_priority=pri)
    a = card("bid", "bid", "c1", "up", keywords=["Own Term"], title="a")
    b = card("bid", "bid", "c2", "up", keywords=["unknown"], title="b")
    annotate_demand_priority([a, b], k)
    assert a.kw_priority == 2 and a.evidence["bucket_sv_rank"] == 3 and b.kw_priority is None


def test_term_owner_stamp():
    owners = {"k1": TermOwner(owner="rival-b", owner_share=0.60, rival="rival-b", rival_ad_impressions=900,
                              own_ad_impressions=100, advertisers=8),
              "k2": TermOwner(owner="rival-b", owner_share=0.59, advertisers=7),
              "k3": TermOwner(owner="own-brand", owner_share=0.9, owned_by_us=True)}
    cards = [card("bid", "bid", "c1", "up", keywords=[k], title=k) for k in ("k1", "k2", "k3", "k4")]
    assert annotate_term_owner(cards, ctx(term_owners=owners)) == 2
    assert "rented attention" in cards[0].lever_field and "8 brands" in cards[0].lever_field
    assert "out-advertises" in cards[0].lever_field and cards[0].lever_field in cards[0].trigger
    assert cards[0].evidence["term_owner"] == "rival-b" and cards[0].evidence["owner_share"] == 0.60
    assert cards[1].lever_field is None and cards[1].evidence["advertisers"] == 7
    assert "we own" in cards[2].lever_field and cards[3].lever_field is None
    assert all(c.status == "pending" for c in cards)


def test_waves_and_coordination():
    cut = budget("c1", "down", title="cut")
    kw_cut = card("efficiency-cut", "keyword", "c1", "down", keywords=["shared"], title="kw-cut")
    bid_up = card("bid", "bid", "c1", "up", keywords=["Shared"], title="bid-up")
    exp = card("experiment", "experiment", "c2", title="exp")
    watch = card("defense", "watch", "c2", title="watch")
    raise_ = budget("c3", "up", title="raise")
    kw_add = card("growth", "keyword", "c3", "up", keywords=["new"], title="kw-add")
    vetoed = budget("c2", "up", title="vetoed")
    vetoed.status = "vetoed"
    cards = [cut, kw_cut, bid_up, exp, watch, raise_, kw_add, vetoed]
    assign_waves(cards)
    assert [c.wave for c in cards] == [1, 1, 2, 2, 2, 3, 3, None]
    coordinate(cards)
    assert "kw-cut (wave 1)" in cut.coordination and "bid-up (wave 2)" in cut.coordination
    assert "wave order" in cut.coordination
    assert "'shared' is also touched by bid" in kw_cut.coordination
    assert "'shared' is also touched by efficiency-cut" in bid_up.coordination
    assert "vetoed" not in exp.coordination and vetoed.coordination is None


def test_polish_guards():
    a = card("bid", "bid", "c1", "down", keywords=["protein bar"], title="a")
    a.facts, a.trigger = "ROAS 0.8 on 12,500 spend", "position 3"
    b = card("bid", "bid", "c2", "down", keywords=["energy bar"], title="b")
    b.facts = "ROAS 1.2"
    c = card("geo", "geo", "c3", title="c")
    c.facts = "ROAS 2"

    def polisher(batch):
        return {a.id: {"why": "protein bar earns 0.80 on 12500 at position 3. " + "x" * 400, "caution": "watch rank"},
                b.id: {"why": "energy bar earns 1.2 and could reach 3.5"},      # invented number
                c.id: {"why": "ROAS 2", "caution": "y" * 300}}

    assert apply_polish([a, b, c], polisher) == 2
    assert len(a.wording) == 300 and a.caution == "watch rank" and b.wording is None and len(c.caution) == 200
    d = card("bid", "bid", "c1", "down", keywords=["protein bar"], title="d")
    assert apply_polish([d], lambda batch: {d.id: {"why": "a keyword is weak"}}) == 0   # keyword name dropped
    assert apply_polish([d], lambda batch: 1 / 0) == 0 and apply_polish([d], None) == 0
    many = [card("geo", "geo", f"e{i}", title=f"m{i}") for i in range(20)]
    seen = []
    apply_polish(many, lambda batch: seen.extend(batch) or {})
    assert len(seen) == 12


# ---------------------------------------------------------------- rebalance

BOARD = [BoardEntry("d1", roas=1.0, threshold=2.0, cap=1000.0, segment="seg"),
         BoardEntry("d2", roas=0.4, threshold=2.0, cap=400.0, segment="seg"),
         BoardEntry("healthy", roas=6.0, threshold=2.0, cap=5000.0),
         BoardEntry("nocap", roas=0.1, threshold=2.0, cap=0.0),
         BoardEntry("noroas", roas=None, threshold=2.0, cap=900.0)]


def test_rebalance_nets_to_zero_worst_earner_first():
    cards = [budget("c1", "up")]                                 # +200/day
    assert funding_need(cards) == (200.0, 0.0, 200.0)
    res = rebalance(cards, BOARD, ctx())
    assert [d.entity_id for d in res.donors] == ["d2", "d1"]
    d2, d1 = res.donors
    assert (d2.change.pct, d2.change.frm, d2.change.to, d2.stake) == (-30, 400.0, pytest.approx(280.0), 120.0)
    assert (d1.change.pct, d1.change.to) == (-8, pytest.approx(920.0))
    assert (d2.family, d2.type, d2.direction, d2.confidence, d2.wave) == ("efficiency-cut", "budget", "down", 66, 3)
    assert isinstance(d2.conf_parts, list) and d2.evidence["funds"] == 120.0
    assert res.short == 0 and res.funded == pytest.approx(200.0)
    assert funding_need(cards + res.donors)[2] == pytest.approx(0.0)
    assert res.telemetry() == {"raise": 200.0, "cut": 0.0, "donors": 2, "short": 0.0}
    assert d2.id != d1.id


def test_rebalance_mints_nothing_when_cuts_already_fund_the_raises():
    assert rebalance([budget("c1", "up"), budget("c2", "down")], BOARD, ctx()).donors == []
    assert rebalance([budget("c2", "down")], BOARD, ctx()).donors == []
    assert rebalance([], BOARD, ctx()).donors == []                      # a healthy book stays untouched
    vetoed = budget("c1", "up")
    vetoed.status = "vetoed"
    assert rebalance([vetoed], BOARD, ctx()).donors == []


def test_rebalance_exclusions_demotion_and_shortfall():
    up = budget("c1", "up")
    probing = card("bid", "bid", "d2", "down", keywords=["k"])
    res = rebalance([up, probing], BOARD, ctx(), excluded=frozenset({"d1"}))
    assert res.donors == [] and res.short == 200.0                       # 200 is 4% of the healthy cap: noise
    res = rebalance([up, probing], BOARD[:2], ctx(), excluded=frozenset({"d1"}))
    assert res.donors == [] and res.short == 200.0 and "SHORT" in res.note
    res = rebalance([up], BOARD[:2], ctx(), demote=lambda c: c.entity_id == "d2")
    assert [d.entity_id for d in res.donors] == ["d1"] and res.donors[0].change.pct == -20
    has_card = budget("d2", "down", frm=400, pct=10)                      # -40: an entity with a budget card never donates
    res = rebalance([up, has_card], BOARD[:2], ctx())
    assert [d.entity_id for d in res.donors] == ["d1"] and res.donors[0].change.pct == -16
    small = budget("c1", "up", frm=100, pct=30)                           # needs 30 = 3% of d1: console noise
    res = rebalance([small], [BOARD[0]], ctx())
    assert res.donors == [] and res.short == pytest.approx(30.0)
    big = budget("c1", "up", frm=10000, pct=50)                           # never more than 30% per donor
    res = rebalance([big], BOARD[:2], ctx())
    assert [d.change.pct for d in res.donors] == [-30, -30] and res.short == pytest.approx(5000 - 420)


# ---------------------------------------------------------------- plan core

def _book():
    return [
        budget("c1", "up", roas=4.0, title="scale-c1"),
        budget("c1", "down", roas=4.0, stake=500, title="cut-c1"),
        budget("c2", "down", roas=0.5, title="cut-c2"),
        card("efficiency-cut", "keyword", "c2", "down", keywords=["waste"], title="kw-c2", leak=6000.0, spend=40000.0),
        card("bid", "bid", "c3", "up", keywords=["term"], title="bid-c3", spend=5000.0),
        card("sov-gap", "watch", None, stake=500000, title="watch"),
        budget("c3", "up", roas=3.0, title="unbound-c3"),
    ]


def _book_ctx(**kw):
    kw.setdefault("telemetry", {"c1": bound("c1"), "c2": bound("c2"), "c3": slack("c3")})
    return ctx(**kw)


def test_plan_actions_end_to_end_and_telemetry():
    res = plan_actions(_book(), _book_ctx())
    st = {c.title: c.status for c in res.cards}
    assert st == {"scale-c1": "superseded", "cut-c1": "pending", "cut-c2": "pending", "kw-c2": "pending",
                  "bid-c3": "pending", "watch": "pending", "unbound-c3": "vetoed"}
    t = res.telemetry
    assert t["families"] == {"efficiency-scale": 2, "efficiency-cut": 3, "bid": 1, "sov-gap": 1}
    assert t["gates"] == {"capability_playbook": 0, "under_test": 0, "unfundable": 1, "supply_gate": 0,
                          "guardrail": 0, "equity_gate": 0, "superseded": 1, "tension": 0}
    assert (t["capped"], t["holdout"], t["holdout_excluded"], t["dead_excluded"]) == (0, 0, 0, 0)
    for c in res.cards:                                           # every card is scored, whatever its status
        assert 25 <= c.confidence <= 90 and c.conf_parts and c.ev_day is not None
    by = {c.title: c for c in res.cards}
    assert (by["cut-c2"].wave, by["bid-c3"].wave, by["watch"].wave, by["scale-c1"].wave) == (1, 2, 2, None)
    assert "kw-c2" in by["cut-c2"].coordination


def test_gates_run_before_caps():
    cards = [budget(f"e{i}", "up", family="growth", roas=0.5 if i < 5 else 3.0, stake=1000 - i, title=f"g{i}")
             for i in range(10)]
    k = ctx(telemetry={f"e{i}": bound(f"e{i}") for i in range(10)})
    res = plan_actions(cards, k, RunCheck("roas", ">=", 1.0))
    pending = [c.title for c in res.cards if c.status == "pending"]
    assert sorted(pending) == [f"g{i}" for i in range(5, 10)] and res.dropped == 0   # cap is 5: fenced cards used no slot
    assert res.telemetry["gates"]["guardrail"] == 5 and res.guardrail.enforced == "roas_floor"


def test_supply_gate_end_to_end_keeps_defense():
    k = _book_ctx(segments={"seg": SegmentStat(availability=74.0)})
    cards = [budget("c1", "up", title="scale"), budget("c2", "up", family="defense", title="defend")]
    res = plan_actions(cards, k)
    assert {c.title: c.status for c in res.cards} == {"scale": "vetoed", "defend": "pending"}
    assert res.telemetry["gates"]["supply_gate"] == 1


def test_healthy_portfolio_emits_nothing():
    res = plan_actions([], _book_ctx())
    assert res.cards == [] and res.dropped == 0
    plan = build_plan([], _book_ctx())
    assert plan.actions == [] and plan.actionable == [] and plan.coverage_note == ""


def test_build_plan_is_deterministic_and_sorted():
    def run(**kw):
        return build_plan(_book(), _book_ctx(), check=RunCheck("roas", ">=", 150, "pct"), **kw)

    a, b = run(), run()
    assert [(c.id, c.status) for c in a.actions] == [(c.id, c.status) for c in b.actions] and a.id == b.id
    order = [c.status for c in a.actions]
    assert order == sorted(order, key=["pending", "tension", "superseded", "vetoed"].index)
    pend = [c for c in a.actions if c.status == "pending"]
    assert [c.ev_kind for c in pend] == sorted(c.ev_kind for c in pend)          # money before opportunity
    assert pend[0].ev_kind == "money" and pend[-1].ev_kind == "opportunity"
    assert (a.created, a.generator, a.days, a.holdout) == (TODAY.isoformat(), "classic", 30, [])
    assert a.economics == {"breakeven": 1.0, "ltv_mult": 1.0, "be": 1.0}
    assert "enforced on scale-ups" in a.guard_note and "economics" not in a.guard_note
    assert a.check == {"metric": "roas", "op": ">=", "value": 150, "mode": "pct"}
    assert a.n_entities == 3 and a.n_segments == 1
    d = a.as_dict()
    assert d["actions"][0]["id"] == a.actions[0].id and d["telemetry"]["gates"]["superseded"] == 1
    goal = run(goal={"metric": "roas"})
    assert goal.actions[0].family == "efficiency-cut" and goal.actions[0].goal_aligned
    assert not next(c for c in goal.actions if c.title == "watch").goal_aligned
    assert {c.title for c in goal.actions} == {c.title for c in a.actions}        # the goal only reorders


def test_build_plan_disclosures_and_stamps():
    k = _book_ctx(economics=Economics(breakeven=1.5, ltv_mult=1.2), status_source="degraded",
                  operator_floors={"generic": 2.0})
    k.entities = dict(k.entities, c2=EntityState("c2", status="active", name="Campaign Two", live_days=60))
    plan = build_plan(_book(), k, context_text="x" * 500, plan_id="p1", engine_version="abc")
    assert "effective floor 1.25" in plan.guard_note and "degraded" in plan.guard_note
    assert "generic >= 2" in plan.guard_note and len(plan.context) == 300
    assert (plan.id, plan.engine_version, plan.status_source) == ("p1", "abc", "degraded")
    c2 = next(c for c in plan.actions if c.entity_id == "c2")
    assert (c2.entity_name, c2.entity_status) == ("Campaign Two", "active")


def test_build_plan_global_cap_and_coverage_note():
    cards = [card("geo", "geo", f"e{i}", stake=100 + i, title=f"geo{i}") for i in range(40)]
    plan = build_plan(cards, ctx(cfg=PipelineConfig(holdout_pct=0, family_caps={})))
    assert len(plan.actions) == 25 and plan.telemetry["capped"] == 15 and "15 further" in plan.coverage_note
    plan = build_plan(cards, ctx())                                       # default per-family cap of 6
    assert len(plan.actions) == 6 and plan.telemetry["capped"] == 34


def test_marginal_profile_sizes_or_falls_back_loudly(make_perf_row):
    rows = [make_perf_row(day=i, entity_id="c1", spend=200.0 * (i + 1), sales=240.0 * (i + 1)) for i in range(10)]
    k = lambda: ctx(rule_profile="marginal")                              # noqa: E731
    plan = build_plan([budget("c1", "up", pct=20)], k(), day_rows=rows)
    c = plan.actions[0]
    assert plan.generator == "marginal" and c.evidence["mroas"] == 1.2 and c.change.pct == 10
    assert plan.telemetry["gates"]["marginal_gate"] == 0
    plan = build_plan([budget("c1", "up")], k(), marginal_readings={"c1": 0.3})
    assert plan.actions[0].status == "vetoed" and plan.telemetry["gates"]["marginal_gate"] == 1
    plan = build_plan([budget("c1", "up")], k())
    assert plan.generator == "classic" and "marginal sizing unavailable" in plan.guard_note
    plan = build_plan([budget("c1", "up")], ctx(), marginal_readings={"c1": 0.3})
    assert plan.actions[0].status == "pending" and "mroas" not in plan.actions[0].evidence


def test_budget_neutral_plan_nets_to_zero():
    board = [BoardEntry("d1", roas=1.0, threshold=2.0, cap=1000.0, segment="seg", name="Donor One")]
    plan = build_plan([budget("c1", "up", roas=4.0)], ctx(rule_profile="verdict"), board=board, budget_neutral=True)
    assert funding_need(plan.actions)[2] == pytest.approx(0.0)
    donor = next(c for c in plan.actions if c.extras.get("rebalance_donor"))
    assert donor.ev_day is not None and donor.entity_name == "Donor One" and donor.status == "pending"
    assert plan.telemetry["rebalance"]["donors"] == 1 and "budget-neutral" in plan.guard_note
    healthy = build_plan([], ctx(), board=board, budget_neutral=True)
    assert healthy.actions == [] and healthy.telemetry["rebalance"]["donors"] == 0
    off = build_plan([budget("c1", "up")], ctx(), board=board)
    assert len(off.actions) == 1 and "rebalance" not in off.telemetry


def test_contested_check_in_plan():
    coll = card("collision", "keyword", "c1", "down", keywords=["k"], title="coll")
    days = {coll.id: {"c1": [TODAY - timedelta(days=i) for i in range(10)], "c2": [TODAY - timedelta(days=20)]}}
    plan = build_plan([coll], ctx(rule_profile="verdict"), contested_days=days)
    assert plan.actions[0].status == "vetoed" and plan.telemetry["gates"]["partitioned"] == 1


def test_tension_pair_survives_to_the_plan():
    cut, up = budget("c1", "down", roas=0.9, stake=1000, title="cut"), budget("c1", "up", roas=0.9, family="growth",
                                                                              stake=50000, title="up")
    plan = build_plan([cut, up], ctx())
    assert {c.status for c in plan.actions} == {"tension"} and len({c.tension_group for c in plan.actions}) == 1
    assert plan.telemetry["gates"]["tension"] == 2 and len(plan.actionable) == 2
    assert all(c.wave for c in plan.actions)


def test_input_cards_drive_identical_plans_from_copies():
    cards = _book()
    p1 = build_plan(copy.deepcopy(cards), _book_ctx())
    p2 = build_plan(copy.deepcopy(cards), _book_ctx())
    assert p1.as_dict() == p2.as_dict()


# ---------------------------------------------------------------- synthetic dataset

def _synthetic_ctx(ds, **kw) -> PlanContext:
    end = ds.latest_date
    spend3: dict[str, float] = {}
    for r in ds.perf:
        if (end - r.date).days < 3:
            spend3[r.entity_id] = spend3.get(r.entity_id, 0.0) + r.spend
    ents = {e.entity_id: EntityState(e.entity_id, status=e.status, spend_3d=spend3.get(e.entity_id, 0.0),
                                     ad_type=e.ad_type, segment=e.segment, name=e.name) for e in ds.entities}
    tel = {}
    for eid in ents:
        rows = [b for b in ds.budget_days if b.entity_id == eid and (end - b.date).days < 30 and b.daily_budget]
        if not rows:
            continue
        ex = [b.exhausted_at_minute for b in rows if b.exhausted_at_minute is not None]
        last3 = [b for b in rows if (end - b.date).days < 3 and b.live_minutes is not None and b.expected_live_minutes]
        tel[eid] = BudgetTelemetry(
            entity_id=eid, n_days=len(rows), avg_budget=sum(b.daily_budget for b in rows) / len(rows),
            avg_spend=sum(b.spend for b in rows) / len(rows), exhaust_minute=sum(ex) / len(ex) if ex else None,
            exhaust_days=len(ex), ad_type=ents[eid].ad_type,
            sufficiency_3d=(sum(b.live_minutes for b in last3) / sum(b.expected_live_minutes for b in last3)) if last3 else None)
    return PlanContext(entities=ents, telemetry=tel, today=end, profile=ds.profile, cfg=NO_HOLDOUT, **kw)


def test_synthetic_ground_truth_through_the_pipeline(synthetic_dataset):
    ds = synthetic_dataset
    k = _synthetic_ctx(ds, rule_profile="verdict")
    cap = k.telemetry["cmp-winner"].avg_budget
    seg = "snack-bars"
    cards = [
        budget("cmp-winner", "up", family="growth", frm=cap, pct=30, roas=5.5, segment=seg, title="raise winner"),
        budget("cmp-generic", "up", frm=1000.0, roas=3.0, segment=seg, title="raise generic"),
        card("efficiency-cut", "keyword", "cmp-conquest", "down", keywords=["rivalb bar"], segment=seg,
             title="pause bleeder", leak=90000.0, spend=143000.0, roas=0.35),
        budget("cmp-reach", "up", segment=seg, title="raise reach"),
        card("efficiency-cut", "keyword", "cmp-reco", "down", keywords=["x"], segment=seg, title="reco keyword"),
        budget("cmp-paused", "down", segment=seg, title="cut paused"),
    ]
    plan = build_plan(cards, k)
    st = {c.title: c.status for c in plan.actions}
    assert "cut paused" not in st and plan.telemetry["dead_excluded"] == 1       # operator-paused: dead
    assert st["raise winner"] == "pending"                                       # the capped winner is fundable
    assert st["pause bleeder"] == "pending"
    assert st["raise reach"] == st["reco keyword"] == "vetoed"                   # lever does not exist there
    by = {c.title: c for c in plan.actions}
    assert by["reco keyword"].extras["reason_code"] == "capability_playbook"
    assert by["raise reach"].extras["reason_code"] in ("not_budget_bound", "capability_playbook")
    if "cmp-generic" in k.telemetry:
        assert st["raise generic"] == "vetoed"                                   # budget-sufficient: nothing to buy
    bleeder = next(c for c in plan.actions if c.title == "pause bleeder")
    assert bleeder.ev_day == 1500 and bleeder.wave == 1
    assert plan.actions[0].status == "pending" and isinstance(plan, Plan)


def test_synthetic_marginal_readings_exist_for_spending_entities(synthetic_dataset):
    from cpm_lineage.actions.scoring import marginal_roas_by_entity

    readings = marginal_roas_by_entity(synthetic_dataset.perf, _synthetic_ctx(synthetic_dataset))
    assert readings and all(v >= 0 for v in readings.values())
    assert set(readings) <= {e.entity_id for e in synthetic_dataset.entities}


def test_families_output_flows_through_the_pipeline(synthetic_dataset, windows_30):
    try:
        from cpm_lineage.actions.families import run_families
        from cpm_lineage.actions.primitives import context_from_dataset
    except ImportError:
        pytest.skip("rule families not available")
    actx = context_from_dataset(synthetic_dataset, windows_30)
    cards = run_families(actx).cards
    ents = {c.entity_id: EntityState(c.entity_id, status=c.status, spend_3d=c.spend_3d, ad_type=c.ad_type,
                                     segment=c.segment) for c in actx.campaigns}
    k = PlanContext(entities=ents, telemetry=actx.telemetry, today=synthetic_dataset.latest_date,
                    profile=synthetic_dataset.profile, cfg=NO_HOLDOUT, economics=actx.economics)
    plan = build_plan(cards, k)
    assert sum(plan.telemetry["families"].values()) == len(cards)
    live = plan.actionable
    assert all(25 <= c.confidence <= 90 and c.wave in (1, 2, 3) for c in live)
    keys = [(c.entity_id, c.type, c.direction) for c in live if c.entity_id and c.status == "pending"]
    assert len(keys) == len(set(keys))
    for eid in ents:
        sides = {c.direction for c in live if c.status == "pending" and c.entity_id == eid and c.type == "budget"}
        assert not {"up", "down"} <= sides
