"""End-to-end checks: the orchestration API finds the planted ground truth.

The synthetic generator plants five events (see ``adapters.synthetic``).
``diagnose`` must report each of them as a typed finding carrying the right
entity, targeting unit, geo and product; ``plan`` must turn the four
actionable ones into cards and survive the pipeline and the verdict spine.
"""

from __future__ import annotations

import math

import pytest

from cpm_lineage import (Diagnosis, EngineConfig, Economics, Plan, RunCheck, as_dataset, diagnose,
                         plan, render_report)
from cpm_lineage.actions import PipelineConfig
from cpm_lineage.engine import FINDING_KINDS, OrchestrationConfig
from cpm_lineage.schema import ACTIONABLE_STATUSES, AppliedAction
from cpm_lineage.store import ActionLedger
from cpm_lineage.verdict import STATES

NO_HOLDOUT = PipelineConfig(holdout_pct=0.0)


@pytest.fixture(scope="module")
def diagnosis(synthetic_dataset) -> Diagnosis:
    return diagnose(synthetic_dataset)


@pytest.fixture(scope="module")
def classic_plan(synthetic_dataset) -> Plan:
    return plan(synthetic_dataset, pipeline_cfg=NO_HOLDOUT)


def _one(diagnosis: Diagnosis, kind: str):
    rows = diagnosis.of_kind(kind)
    assert rows, f"no {kind} finding"
    return rows[0]


# ------------------------------------------------------------------ diagnose

def test_every_planted_event_is_reported_exactly_once(diagnosis):
    kinds = [f.kind for f in diagnosis.findings]
    assert sorted(kinds) == sorted(FINDING_KINDS)          # five events, five findings, no noise


def test_cpm_spike_is_found_and_explained(diagnosis, synthetic_events):
    ev = synthetic_events["cpm_spike"]
    f = _one(diagnosis, "cpm_rise")
    assert f.targeting_unit == ev.targeting_unit and f.entity_ids[0] == ev.entity_ids[0]
    assert f.size == pytest.approx(ev.magnitude - 1.0, abs=0.08)       # planted 1.6x
    assert f.evidence["kind"] == "rival pressure" and f.evidence["rival"] == "rival-b"
    e = diagnosis.entities[ev.entity_ids[0]]
    assert e.layer1.worst == "cpm"                                     # exact identity: CPM is the drag
    assert e.attribution.top_driver == "bid" and e.attribution.verdict == "confirmed"
    lead = e.recommendation.actions[0]
    assert lead.action_type == "BID_CAP" and ev.targeting_unit in lead.targets


def test_availability_drop_is_localised_to_product_and_geo(diagnosis, synthetic_events):
    ev = synthetic_events["availability_drop"]
    f = _one(diagnosis, "availability_drop")
    assert (f.geo, f.product_id) == (ev.geo, ev.product_id)
    assert set(ev.entity_ids) <= set(f.entity_ids)
    assert f.evidence["product_before"] > 0.9 and f.evidence["product_after"] < 0.7
    assert diagnosis.availability.rows[0].geo == ev.geo                # the worst geo leads the drill


def test_budget_capped_winner_is_found_and_sized(diagnosis, synthetic_events):
    ev = synthetic_events["budget_capped_winner"]
    f = _one(diagnosis, "budget_bound_winner")
    assert f.entity_ids == ev.entity_ids
    e = diagnosis.entities[ev.entity_ids[0]]
    assert e.probe.capped and e.raise_verdict.raise_ok and e.roas > 4
    low, high, rec = e.raise_verdict.rec_range
    assert e.probe.daily_budget < low <= rec <= high                   # a raise, inside its own range
    assert diagnosis.telemetry[ev.entity_ids[0]].is_bound()
    assert e.recommendation.actions[0].action_type == "BUDGET_RAISE"
    assert e.prescription.direction == "grow"
    assert [x for x in diagnosis.entities.values() if x.raise_verdict and x.raise_verdict.raise_ok] == [e]


def test_bleeding_keyword_is_found(diagnosis, synthetic_events):
    ev = synthetic_events["bleeding_keyword"]
    f = _one(diagnosis, "bleeding_keyword")
    assert f.targeting_unit == ev.targeting_unit and f.entity_ids == ev.entity_ids
    assert f.evidence["roas"] < 0.5 and f.size == pytest.approx(f.evidence["spend"] * (1 - f.evidence["roas"]), rel=1e-3)
    e = diagnosis.entities[ev.entity_ids[0]]
    assert e.priority == "p0" and e.bleed["worst"][0] == ev.targeting_unit and e.bleed["pct"] >= 40
    assert any(ev.targeting_unit in a.targets for a in e.recommendation.actions)
    assert diagnosis.leaks.leaks[0].keyword == ev.targeting_unit
    assert diagnosis.pareto.rows[0].key == ev.entity_ids[0]            # also the largest drag on account ROAS


def test_own_overlap_is_found_with_the_right_keeper(diagnosis, synthetic_events):
    ev = synthetic_events["own_overlap"]
    f = _one(diagnosis, "own_collision")
    assert f.targeting_unit == ev.targeting_unit and set(f.entity_ids) == set(ev.entity_ids)
    assert f.evidence["keep"] == "cmp-generic" and f.entity_ids[-1] == "cmp-overlap"
    assert f.size > 0 and set(f.evidence["geos"]) == {"geo-north", "geo-south", "geo-east", "geo-west"}


def test_exact_decompositions_hold_end_to_end(diagnosis):
    l1 = diagnosis.layer1
    total = sum(c.log_points for c in l1.channels.values())
    assert total == pytest.approx(math.log(l1.roas_comparison / l1.roas_baseline), abs=1e-9)
    mix = diagnosis.ad_type_mix
    assert mix.mix_total + mix.rate_total == pytest.approx(l1.roas_comparison - l1.roas_baseline, abs=1e-9)
    for e in diagnosis.entities.values():
        if e.layer1.roas_baseline > 0 and e.layer1.roas_comparison > 0:
            s = sum(c.log_points for c in e.layer1.channels.values())
            assert s == pytest.approx(math.log(e.layer1.roas_comparison / e.layer1.roas_baseline), abs=1e-9)
    if diagnosis.composition is not None:
        assert diagnosis.composition.residual == pytest.approx(0.0, abs=1e-9)


def test_edge_entities_take_the_right_path(diagnosis):
    assert "cmp-paused" not in diagnosis.entities                      # operator-paused, no window spend
    reach = diagnosis.entities["cmp-reach"]
    assert reach.attribution is None and reach.prescription.code == "reach"   # never judged on ROAS
    young = diagnosis.entities["cmp-young"]
    assert young.tier in ("cold", "warm") and young.state is not None and young.prescription.code == "state"
    assert young.state.preflight is not None
    assert diagnosis.market is not None and diagnosis.market.status == "ok"


def test_diagnose_is_deterministic_and_scopable(synthetic_dataset, diagnosis):
    again = diagnose(synthetic_dataset)
    assert [(f.kind, f.summary) for f in again.findings] == [(f.kind, f.summary) for f in diagnosis.findings]
    one = diagnose(synthetic_dataset, entity_ids=["cmp-winner"], options=OrchestrationConfig(rca=False))
    assert list(one.entities) == ["cmp-winner"] and one.market is None


def test_perf_rows_alone_degrade_without_error(synthetic_dataset):
    d = diagnose(synthetic_dataset.perf)                               # no shelf, market, budget or offtake feed
    assert {f.kind for f in d.findings} == {"cpm_rise", "bleeding_keyword", "own_collision"}
    assert d.availability is None and d.budget_moves is None and d.market is None
    assert any("shelf" in n for n in d.notes) and any("budget" in n for n in d.notes)
    assert d.of_kind("cpm_rise")[0].evidence["kind"] == "auction price"    # no market feed: no rival read
    p = plan(synthetic_dataset.perf, pipeline_cfg=NO_HOLDOUT)
    assert all(c.type != "budget" or c.direction != "up" for c in p.actionable)   # nothing provably budget-bound


def test_economics_rebase_the_bleed_bar(synthetic_dataset):
    cfg = EngineConfig(economics=Economics(breakeven=2.0))             # thin margin: ROAS 2 breaks even
    d = diagnose(synthetic_dataset, config=cfg, options=OrchestrationConfig(rca=False))
    base = diagnose(synthetic_dataset, options=OrchestrationConfig(rca=False))
    assert len(d.of_kind("bleeding_keyword")) > len(base.of_kind("bleeding_keyword"))


# ---------------------------------------------------------------------- plan

def _cards(p: Plan, **match):
    return [c for c in p.actions if all(getattr(c, k) == v for k, v in match.items())]


def test_plan_acts_on_the_four_actionable_events(classic_plan, synthetic_events):
    p = classic_plan
    win = [c for c in _cards(p, entity_id="cmp-winner", type="budget", direction="up") if c.is_actionable]
    assert win and win[0].change.to > win[0].change.frm and win[0].ev_day > 0
    bleed = [c for c in _cards(p, entity_id="cmp-conquest") if "rivalb bar" in c.keyword_values()]
    assert bleed and bleed[0].direction == "down" and bleed[0].is_actionable
    coll = _cards(p, family="collision")
    assert coll and coll[0].keyword_values() == ["energy bar"] and coll[0].evidence["keep"] == "cmp-generic"
    assert coll[0].entity_id == "cmp-overlap" and coll[0].is_actionable       # the overlap is real, not partitioned
    spike = [c for c in _cards(p, family="bid") if "protein bar" in c.keyword_values()]
    assert spike and spike[0].entity_id == "cmp-generic"
    assert not [c for c in p.actions if c.entity_id == "cmp-paused"]          # dead entities get nothing


def test_plan_cards_are_scored_typed_and_consistent(classic_plan):
    p = classic_plan
    assert isinstance(p, Plan) and p.generator == "classic" and p.created == "2026-03-31"
    assert sum(p.verdict_states.values()) == len(p.actions)
    for c in p.actions:
        assert c.verdict and c.verdict["state"] in STATES
        if c.status in ACTIONABLE_STATUSES and c.extras.get("minted_by") != "verdict":
            assert 25 <= c.confidence <= 90 and c.wave in (1, 2, 3) and c.estimator
    for eid in {c.entity_id for c in p.actions}:
        sides = {c.direction for c in p.actionable if c.entity_id == eid and c.type == "budget"}
        assert not {"up", "down"} <= sides                                    # no contradictory budget moves
    untracked = [c for c in p.actions if c.id == "untracked-cmp-reach"]
    assert untracked and "spine" in p.telemetry                               # the spine's own sweep ran


def test_plan_is_deterministic(synthetic_dataset, classic_plan):
    assert plan(synthetic_dataset, pipeline_cfg=NO_HOLDOUT).as_dict() == classic_plan.as_dict()


@pytest.mark.parametrize("profile", ["classic", "marginal", "verdict"])
def test_every_rule_profile_runs_and_keeps_the_winner(synthetic_dataset, profile):
    p = plan(synthetic_dataset, rule_profile=profile, pipeline_cfg=NO_HOLDOUT)
    assert p.generator == profile
    up = [c for c in p.actionable if c.entity_id == "cmp-winner" and c.direction == "up"]
    assert up, f"{profile}: the capped winner lost its raise"
    if profile == "marginal":
        assert "marginal_gate" in p.telemetry["gates"]
    if profile == "verdict":
        assert len(p.actions) >= len(plan(synthetic_dataset, pipeline_cfg=NO_HOLDOUT).actions)


def test_holdout_keeps_entities_untouched(synthetic_dataset):
    p = plan(synthetic_dataset)                                               # default 15% holdout
    assert p.holdout
    assert not [c for c in p.actions if c.entity_id in p.holdout]
    # Donor cuts and spine sweeps respect it too: hold out the entity the rebalancer would
    # pick as its donor, and it must fund the raise from someone else or report a shortfall.
    kw = dict(config=EngineConfig(economics=Economics(breakeven=1.2)), rule_profile="verdict",
              budget_neutral=True, check=RunCheck("roas", ">=", 150, "pct"))
    free = plan(synthetic_dataset, holdout=[], **kw)
    donors = [c for c in free.actions if c.extras.get("rebalance_donor")]
    assert free.holdout == [] and donors and donors[0].direction == "down"
    assert [c for c in free.actions if c.id == "untracked-cmp-reach"]         # the spine sweep, when free
    held = [donors[0].entity_id, "cmp-reach"]
    v = plan(synthetic_dataset, holdout=held, **kw)
    assert sorted(v.holdout) == sorted(held)
    assert not [c for c in v.actions if c.entity_id in held]
    assert "rebalance" in v.telemetry and "budget-neutral" in v.guard_note


def test_roas_check_vetoes_raises_below_the_fence(synthetic_dataset):
    strict = plan(synthetic_dataset, pipeline_cfg=NO_HOLDOUT, check=RunCheck("roas", ">=", 900, "pct"))
    assert "check" in strict.guard_note or strict.check == {"metric": "roas", "op": ">=", "value": 900, "mode": "pct"}
    assert not [c for c in strict.actionable if c.type == "budget" and c.direction == "up"]


def test_applied_history_locks_and_cools_down(synthetic_dataset):
    today = synthetic_dataset.latest_date
    applied = [AppliedAction("experiment", "cmp-winner", today)]
    p = plan(synthetic_dataset, pipeline_cfg=NO_HOLDOUT, applied=applied)
    assert not [c for c in p.actionable if c.entity_id == "cmp-winner" and (c.verdict or {}).get("state") == "ACT"]


def test_plan_is_saved_to_the_ledger_and_feeds_back(synthetic_dataset):
    ledger = ActionLedger()
    p = plan(synthetic_dataset, pipeline_cfg=NO_HOLDOUT, ledger=ledger, account="adv-001")
    saved = ledger.load("adv-001", synthetic_dataset.profile.name)
    assert saved and saved[0]["id"] == p.id
    assert len(ledger.ledger_rows("adv-001", synthetic_dataset.profile.name, p.id)) > 0
    assert ledger.win_rates("adv-001", synthetic_dataset.profile.name, today=synthetic_dataset.latest_date) == {}


def test_report_renders_both_halves(diagnosis, classic_plan):
    text = render_report(diagnosis, classic_plan)
    assert "DIAGNOSIS" in text and "FINDINGS (5)" in text and "PLAN" in text
    for word in ("protein bar", "sku-a2", "cmp-winner", "rivalb bar", "energy bar"):
        assert word in text


def test_as_dataset_accepts_every_input_shape(synthetic_adapter, synthetic_dataset):
    assert as_dataset(synthetic_dataset) is synthetic_dataset
    assert as_dataset(synthetic_adapter) is synthetic_dataset
    rows_only = as_dataset(synthetic_dataset.perf[:500])
    assert rows_only.entities and not rows_only.shelf
    with pytest.raises(TypeError):
        as_dataset([1, 2, 3])
    with pytest.raises(ValueError):
        diagnose([])


# ------------------------------------------------------------- feedback loop

def test_plan_apply_pair_and_feed_back(synthetic_dataset):
    """plan -> ledger -> mark applied -> pair realised outcomes -> win rates -> next plan."""
    from dataclasses import replace
    from datetime import timedelta

    from cpm_lineage.outcomes import pair_outcomes
    from cpm_lineage.store import OUTCOMES

    d = synthetic_dataset
    name, cut = d.profile.name, d.latest_date - timedelta(days=20)
    early = replace(d, perf=[r for r in d.perf if r.date <= cut],
                    budget_days=[b for b in d.budget_days if b.date <= cut],
                    market=[m for m in d.market if m.date <= cut],
                    shelf=[s for s in d.shelf if s.date <= cut],
                    offtake=[o for o in d.offtake if o.date <= cut])
    ledger = ActionLedger()
    first = plan(early, pipeline_cfg=NO_HOLDOUT, ledger=ledger, account="adv-001")
    assert first.created == cut.isoformat()
    applied_ids = [c.id for c in first.actionable]
    assert applied_ids
    for cid in applied_ids:
        assert ledger.mark("adv-001", name, first.id, cid, "applied", today=cut)
    vetoed = [c for c in first.actions if c.status == "vetoed"]
    for c in vetoed:
        assert not ledger.mark("adv-001", name, first.id, c.id, "applied", today=cut)   # cannot apply a veto

    report = pair_outcomes(ledger, "adv-001", name, d.perf, today=d.latest_date, offtake=d.offtake)
    assert report.paired > 0 and set(report.outcomes) <= set(OUTCOMES)
    rates = ledger.win_rates("adv-001", name, today=d.latest_date)
    assert rates and all(0.0 <= r <= 1.0 and n >= 1 for r, n in rates.values())

    history = ledger.applied_actions("adv-001", name)
    assert {a.entity_id for a in history} >= {c.entity_id for c in first.actionable if c.entity_id}
    second = plan(d, pipeline_cfg=NO_HOLDOUT, ledger=ledger, account="adv-001", applied=history)
    assert second.id != first.id and len(ledger.load("adv-001", name)) == 2
    assert sum(second.verdict_states.values()) == len(second.actions)
