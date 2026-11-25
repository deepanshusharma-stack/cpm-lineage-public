"""End-to-end regressions of ``plan()``: ledger round trips, the holdout, the spine and funding."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from cpm_lineage import plan
from cpm_lineage import verdict as V
from cpm_lineage.actions import PipelineConfig
from cpm_lineage.actions.families import moat_campaign, run_families
from cpm_lineage.actions.primitives import context_from_dataset, cooldown_register
from cpm_lineage.actions.rebalance import funding_need
from cpm_lineage.adapters.base import AdTypeCapability, Dataset, PlatformProfile
from cpm_lineage.schema import (AppliedAction, BudgetDayRow, CapabilityClass, Entity, OfftakeRow, PerfRow,
                                TargetingRole, TargetingUnit)
from cpm_lineage.store import ActionLedger
from cpm_lineage.windows import trailing_windows

END = date(2026, 3, 31)
NH = PipelineConfig(holdout_pct=0.0)


def days(n=60, end=END):
    return [end - timedelta(days=i) for i in range(n)][::-1]


def camp(eid, daily_spend, roas, kws=None, n=60, end=END, geo=None, status="active", pid=None, ad_type=None,
         only=None):
    """One campaign: ``(perf rows, entity)``.  ``kws`` = ``{keyword: (spend share, roas)}``."""
    rows = []
    for d in days(n, end):
        if only and not only(d):
            continue
        if kws:
            for kw, (share, r) in kws.items():
                rows.append(PerfRow(date=d, entity_id=eid, spend=daily_spend * share,
                                    impressions=daily_spend * share * 10, sales=daily_spend * share * r,
                                    targeting_unit=kw, targeting_type="keyword", geo=geo, segment="seg",
                                    position=3.0, product_id=pid, ad_type=ad_type))
        else:
            rows.append(PerfRow(date=d, entity_id=eid, spend=daily_spend, impressions=daily_spend * 10,
                                sales=daily_spend * roas, geo=geo, segment="seg", product_id=pid, ad_type=ad_type))
    ent = Entity(entity_id=eid, name=eid, status=status, daily_budget=daily_spend * 1.05, segment="seg",
                 is_pure=True, ad_type=ad_type)
    return rows, ent


def bdays(eid, daily_spend, roas, budget, exh=None, live=None, exp=None):
    return [BudgetDayRow(date=d, entity_id=eid, daily_budget=budget, spend=daily_spend, sales=daily_spend * roas,
                         impressions=daily_spend * 10, exhausted_at_minute=exh, live_minutes=live,
                         expected_live_minutes=exp) for d in days()]


def ds(*camps, budget_days=None, profile=None, units=None, offtake=None):
    perf, ents = [], []
    for rows, ent in camps:
        perf += rows
        ents.append(ent)
    return Dataset(profile=profile or PlatformProfile(), perf=perf, entities=ents, budget_days=budget_days or [],
                   targeting_units=units or [], offtake=offtake or [])


def ledger():
    return ActionLedger(clock=lambda: datetime(2026, 3, 31, 12))


# ---------------------------------------------------------------- ledger round trip

def test_regenerating_a_plan_keeps_the_operators_applied_state():
    data = ds(camp("loser", 2000, 0.5), camp("ok", 2000, 2.0))
    led = ledger()
    first = plan(data, pipeline_cfg=NH, ledger=led, account="acc")
    cut = next(c for c in first.actions if c.entity_id == "loser" and c.type == "budget")
    assert led.mark("acc", "generic", first.id, cut.id, "applied", today=END)
    again = plan(data, pipeline_cfg=NH, ledger=led, account="acc")          # same data, same options
    assert again.id == first.id
    stored = {c["id"]: c for c in led.load("acc", "generic")[0]["actions"]}
    assert stored[cut.id]["status"] == "applied" and stored[cut.id]["applied_at"] == END.isoformat()
    applied = led.applied_actions("acc", "generic")
    assert [(a.entity_id, a.card_id) for a in applied] == [("loser", cut.id)]
    assert {r["applied_at"] for r in led.ledger_rows("acc", "generic") if r["action_id"] == cut.id} == {END.isoformat()}
    # the documented loop (feeding the applied history back) does not lose it either
    plan(data, pipeline_cfg=NH, ledger=led, account="acc", applied=applied)
    assert [a.card_id for a in led.applied_actions("acc", "generic")] == [cut.id]


def test_ledger_history_arms_the_cut_cooldown():
    data = ds(camp("loser", 2000, 0.5), camp("ok", 2000, 2.0))
    led = ledger()
    first = plan(data, pipeline_cfg=NH, ledger=led, account="acc")
    cut = next(c for c in first.actions if c.entity_id == "loser" and c.type == "budget")
    led.mark("acc", "generic", first.id, cut.id, "applied", today=END)
    applied = led.applied_actions("acc", "generic")
    assert applied[0].kind == "budget" and applied[0].family == "efficiency-cut"
    later = END + timedelta(days=2)
    assert cooldown_register(applied, later) == frozenset({("cut", "loser")})
    data2 = ds(camp("loser", 2000, 0.5, end=later), camp("ok", 2000, 2.0, end=later))
    replay = plan(data2, pipeline_cfg=NH, applied=applied, spine=False)
    assert not [c for c in replay.actions if c.entity_id == "loser" and c.type == "budget" and c.status == "pending"]


@pytest.mark.parametrize("action, kind", [
    (AppliedAction("budget", "e", END, family="defense", direction="up"), "defend"),
    (AppliedAction("bid", "e", END, family="efficiency-cut", direction="down"), "biddown"),
    (AppliedAction("keyword", "e", END, family="efficiency-cut", direction="down"), "cut"),
    (AppliedAction("cut", "e", END), "cut"),
    (AppliedAction("budget", "e", END, family="growth", direction="up"), None),
    (AppliedAction("bid", "e", END, direction="up"), None),
])
def test_cooldown_kind_from_card_records(action, kind):
    reg = cooldown_register([action], END + timedelta(days=1))
    assert reg == (frozenset({(kind, "e")}) if kind else frozenset())


def test_plan_ids_differ_by_rule_profile_and_plan_key():
    data = ds(camp("win", 2000, 5.0), budget_days=bdays("win", 2000, 5.0, 2000, exh=480))
    led = ledger()
    classic = plan(data, pipeline_cfg=NH, ledger=led, account="acc", rule_profile="classic")
    marginal = plan(data, pipeline_cfg=NH, ledger=led, account="acc", rule_profile="marginal")
    assert classic.id != marginal.id
    assert {p["id"] for p in led.load("acc", "generic")} == {classic.id, marginal.id}
    assert plan(data, pipeline_cfg=NH, rule_profile="classic").id == classic.id            # deterministic
    assert plan(data, pipeline_cfg=NH, plan_key="run-2").id != classic.id
    assert plan(data, pipeline_cfg=NH, plan_id="mine").id == "mine"


# ---------------------------------------------------------------- spine

def test_alternating_day_campaigns_are_a_schedule_not_a_collision():
    a = camp("camp-a", 3000, 2.0, kws={"shared term": (1.0, 2.0)}, geo="north", pid="sku1",
             only=lambda d: d.toordinal() % 2 == 0)
    b = camp("camp-b", 3000, 1.5, kws={"shared term": (1.0, 1.5)}, geo="north", pid="sku1",
             only=lambda d: d.toordinal() % 2 == 1)
    p = plan(ds(a, b), pipeline_cfg=NH)
    col = next(c for c in p.actions if c.family == "collision")
    assert col.verdict["lane"] == "NO-ACTION(partitioned)" and col.status == "vetoed"
    assert col.evidence["contested_ratio"] == 0.0 and p.telemetry["spine"]["partitioned"] == 1


def test_verdict_profile_raises_without_served_time_telemetry():
    data = ds(camp("winner", 2000, 5.0, kws={"good": (1.0, 5.0)}), camp("other", 2000, 2.0, kws={"meh": (1.0, 2.0)}),
              budget_days=bdays("winner", 2000, 5.0, 2000, exh=480) + bdays("other", 1000, 2.0, 3000))
    for profile in ("classic", "verdict"):
        p = plan(data, pipeline_cfg=NH, rule_profile=profile)
        up = next(c for c in p.actions if c.entity_id == "winner" and c.type == "budget" and c.direction == "up")
        assert up.status == "pending" and up.verdict["state"] == "ACT", (profile, up.veto_reason)


def test_spine_sweeps_skip_dead_reach_and_leverless_entities():
    prof = PlatformProfile(ad_types={
        "Banner": AdTypeCapability("Banner", capability_class=CapabilityClass.REACH.value, sales_attributed=False,
                                   judged_on="reach"),
        "Reco": AdTypeCapability("Reco", capability_class=CapabilityClass.RETARGETING.value, targeting="auto",
                                 roas_deflator=0.6),
        "Search": AdTypeCapability("Search")})
    data = ds(camp("ok", 3000, 3.0, kws={"k": (1.0, 3.0)}, ad_type="Search"),
              camp("dead-paused", 3000, 0.4, ad_type="Search", status="paused",
                   only=lambda d: d < END - timedelta(days=14)),
              camp("banner", 1500, 0.0, ad_type="Banner"), camp("reco", 1500, 0.9, ad_type="Reco"), profile=prof)
    p = plan(data, pipeline_cfg=NH)
    assert not [c for c in p.actions if c.entity_id == "dead-paused"]
    for c in p.actions:
        if c.entity_id is not None and V.is_act(c):
            assert c.type in prof.allowed_levers(next(e.ad_type for e in data.entities if e.entity_id == c.entity_id)), c
    assert not [c for c in p.actions if c.entity_id == "banner" and c.type in ("bid", "budget", "keyword")]
    assert not [c for c in p.actions if c.entity_id == "reco" and c.type == "bid"]


def test_spine_mints_nothing_on_an_entity_under_a_running_experiment():
    data = ds(camp("ok", 3000, 3.0, kws={"k": (1.0, 3.0)}), camp("loser2", 3000, 0.95))
    running = [AppliedAction("experiment", "loser2", END - timedelta(days=10))]
    p = plan(data, pipeline_cfg=NH, applied=running)
    assert not [c for c in p.actions if c.entity_id == "loser2" and V.is_act(c) and c.type != "watch"]
    free = plan(data, pipeline_cfg=NH)                                       # without the experiment the sweep fires
    assert [c for c in free.actions if c.entity_id == "loser2" and V.is_act(c)]


def test_configured_windows_reach_the_gates():
    data = ds(camp("loser", 2000, 0.5), camp("ok", 2000, 2.0))
    recent = [AppliedAction("budget", "loser", END - timedelta(days=5))]
    locked = plan(data, pipeline_cfg=NH, applied=recent)
    assert any((c.verdict or {}).get("lane") == "HOLD(monitoring-window)" for c in locked.actions)
    short = plan(data, pipeline_cfg=NH, applied=recent, verdict_cfg=V.VerdictConfig(lock_days=2))
    assert not any((c.verdict or {}).get("lane") == "HOLD(monitoring-window)" for c in short.actions)
    test = [AppliedAction("experiment", "loser", END - timedelta(days=10))]
    held = plan(data, pipeline_cfg=NH, applied=test, spine=False)
    assert all(c.status == "vetoed" for c in held.actions if c.entity_id == "loser" and c.type != "watch")
    freed = plan(data, pipeline_cfg=PipelineConfig(holdout_pct=0.0, under_test_days=3), applied=test, spine=False)
    assert any(c.status == "pending" for c in freed.actions if c.entity_id == "loser")


# ---------------------------------------------------------------- budget-neutral funding

def _neutral_book():
    units = [TargetingUnit("good", role="generic")] + [TargetingUnit(k, role=TargetingRole.OWN_BRAND.value)
                                                        for k in ("k1", "k2", "k3")]
    budget = (bdays("winner", 8000, 6.0, 8000, exh=400, live=400, exp=1000)
              + bdays("mid1", 4000, 3.6, 8000, live=1000, exp=1000)
              + bdays("mid2", 4000, 3.7, 8000, live=1000, exp=1000)
              + bdays("mid3", 4000, 3.8, 8000, live=1000, exp=1000))
    return ds(camp("winner", 8000, 6.0, kws={"good": (1.0, 6.0)}), camp("mid1", 4000, 3.6, kws={"k1": (1.0, 3.6)}),
              camp("mid2", 4000, 3.7, kws={"k2": (1.0, 3.7)}), camp("mid3", 4000, 3.8, kws={"k3": (1.0, 3.8)}),
              budget_days=budget, units=units)


def _live_net(p):
    live = [c for c in p.actions if V.is_act(c)]
    return funding_need(live)


def test_budget_neutral_plan_is_neutral_after_the_spine_trims_the_raise():
    p = plan(_neutral_book(), pipeline_cfg=NH, rule_profile="verdict", budget_neutral=True,
             operator_floors={"generic": 3.0})
    up, down, need = _live_net(p)
    assert up == pytest.approx(2000.0) and need == pytest.approx(0.0, abs=1.0)     # the +50% ask was staircased to +25%
    assert p.telemetry["rebalance"]["raise"] == pytest.approx(up)
    assert f"raises {up:,.0f}/day, funded {down:,.0f}/day" in p.guard_note


def test_budget_neutral_mints_no_donor_when_the_spine_holds_the_raise():
    lock = [AppliedAction("bid", "winner", END - timedelta(days=3), direction="up")]
    p = plan(_neutral_book(), pipeline_cfg=NH, rule_profile="verdict", budget_neutral=True, applied=lock,
             operator_floors={"generic": 3.0})
    up, down, _need = _live_net(p)
    assert (up, down) == (0.0, 0.0)
    assert not [c for c in p.actions if c.extras.get("rebalance_donor")]
    assert "budget-neutral" not in p.guard_note


# ---------------------------------------------------------------- holdout

def test_holdout_moves_the_segment_card_to_the_next_best_candidate():
    off = []
    for i, d in enumerate(days()):
        m = 1.0 if i < 30 else 1.2
        off += [OfftakeRow(d, "own", True, 10000 * m, segment="seg"), OfftakeRow(d, "rival", False, 30000 * m, segment="seg")]
    data = ds(camp("best", 2000, 6.0), camp("second", 2000, 4.0),
              budget_days=bdays("best", 2000, 6.0, 2000, exh=480) + bdays("second", 2000, 4.0, 2000, exh=480),
              offtake=off)
    free = plan(data, pipeline_cfg=NH, holdout=[], families=["growth"])
    assert [c.entity_id for c in free.actions if c.family == "growth"] == ["best"]
    held = plan(data, pipeline_cfg=NH, holdout=["best"], families=["growth"])
    assert [c.entity_id for c in held.actions if c.family == "growth"] == ["second"] and held.holdout == ["best"]


def test_collision_cards_never_touch_a_held_out_campaign():
    book = (camp("keeper", 3000, 3.0, kws={"shared term": (1.0, 3.0)}, geo="north", pid="sku1"),
            camp("loser-big", 3000, 1.5, kws={"shared term": (1.0, 1.5)}, geo="north", pid="sku1"),
            camp("loser-small", 2000, 1.2, kws={"shared term": (1.0, 1.2)}, geo="north", pid="sku1"))
    p = plan(ds(*book), pipeline_cfg=NH, holdout=["loser-small"], families=["collision"], spine=False)
    card = next(c for c in p.actions if c.family == "collision")
    assert card.entity_id == "loser-big" and "loser-small" not in card.evidence["cids"]
    # holding the anchor loser still cards the other one
    p = plan(ds(*book), pipeline_cfg=NH, holdout=["loser-big"], families=["collision"], spine=False)
    assert [c.entity_id for c in p.actions if c.family == "collision"] == ["loser-small"]
    for q in (p,):
        assert all(c.entity_id not in q.holdout for c in q.actions)


# ---------------------------------------------------------------- families

def test_verdict_profile_emits_unique_ids_and_an_exactly_one_pair():
    data = ds(camp("loser", 2000, 0.7, kws={"bad1": (0.5, 0.6), "bad2": (0.5, 0.8)}))
    raw = run_families(context_from_dataset(data, trailing_windows(END, 30), rule_profile="verdict")).cards
    assert len({c.id for c in raw}) == len(raw)
    led = ledger()
    p = plan(data, pipeline_cfg=NH, rule_profile="verdict", ledger=led, account="acc", spine=False)
    pair = [c for c in p.actions if c.tension_group == "loser"]
    assert sorted(c.type for c in pair) == ["bid", "budget"]
    ids = {c.id for c in p.actions}
    assert all(c.tension_with in ids for c in pair)
    marks = [led.mark("acc", "generic", p.id, c.id, "applied", today=END) for c in pair]
    assert sorted(marks) == [False, True]                                    # exactly one of the pair


def test_two_bid_cards_on_different_keywords_both_reach_the_plan():
    rows, ent = camp("c", 4000, 2.0, kws={"good": (0.5, 4.0), "bad1": (0.25, 0.3), "bad2": (0.25, 0.2)})
    import dataclasses
    data = ds(([dataclasses.replace(r, position=1.0) for r in rows], ent))
    raw = run_families(context_from_dataset(data, trailing_windows(END, 30)), ["bid"]).cards
    p = plan(data, pipeline_cfg=NH, families=["bid"])
    assert len(raw) >= 2
    assert sorted(k.kw for c in p.actions for k in c.keywords) == sorted(k.kw for c in raw for k in c.keywords)


def test_own_brand_share_counts_sibling_brand_keywords():
    data = ds(camp("sib", 2000, 3.0, kws={"sister brand term": (1.0, 3.0)}),
              units=[TargetingUnit("sister brand term", role=TargetingRole.SIBLING_BRAND.value)])
    actx = context_from_dataset(data, trailing_windows(END, 30))
    assert actx.campaigns[0].own_brand_share == pytest.approx(1.0)
    assert moat_campaign(actx, "seg")[1] == pytest.approx(1.0)
