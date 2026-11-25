from datetime import datetime, timedelta

from cpm_lineage.scorecard import (MET, MISSED, PENDING, Baseline, InMemoryBaselineStore,
                                   ScorecardConfig, arm, budget_roas_checkpoint, grade,
                                   grade_checkpoint, register_grader, waste_bleed_checkpoint)

NOW = datetime(2026, 3, 20, 9, 0)

CHECKPOINTS = [
    {"kind": "availability_floor", "desc": "availability back above the floor", "target": 0.85},
    waste_bleed_checkpoint(1000.0),
    budget_roas_checkpoint(5000.0, 3.0),
    {"kind": "rival_share", "desc": "rival held", "brand": "rival-b", "baseline_share": 20.0},
    {"kind": "tier_share", "desc": "premium tier share", "tier": "premium", "baseline": 10.0},
    {"kind": "discount_hold", "desc": "discount held", "baseline": 12.0},
]
GOOD = {"availability": 0.90, "waste": 400.0, "spend": 5300.0, "roas": 2.8,
        "share": {"rival-b": 20.6}, "tier_share": {"premium": 10.5}, "discount": 13.0}
BAD = {"availability": 0.70, "waste": 1100.0, "spend": 5100.0, "roas": 1.0,
       "share": {}, "tier_share": {"premium": 10.1}}


def baseline(age_days):
    return Baseline(ts=NOW - timedelta(days=age_days), checkpoints=tuple(CHECKPOINTS), vals={})


def statuses(rows):
    return [r.status for r in rows]


def test_good_case():
    rows = grade(baseline(10), GOOD, NOW)
    assert statuses(rows) == [MET, MET, MET, MISSED, PENDING, MET]
    assert rows[2].then == {"spend": 5000.0, "roas": 3.0} and rows[2].now == {"spend": 5300.0, "roas": 2.8}
    assert rows[0].desc == "availability back above the floor" and rows[1].kind == "waste_bleed"


def test_bad_case_old_baseline():
    assert statuses(grade(baseline(10), BAD, NOW)) == [MISSED, MISSED, PENDING, PENDING, MISSED, PENDING]


def test_bad_case_fresh_baseline_is_too_soon_to_miss():
    rows = grade(baseline(1), BAD, NOW)
    assert statuses(rows) == [MISSED, PENDING, PENDING, PENDING, PENDING, PENDING]
    assert rows[1].note == "too soon to judge"
    assert statuses(grade(baseline(4.99), BAD, NOW))[1] == PENDING
    assert statuses(grade(baseline(5), BAD, NOW))[1] == MISSED


def test_budget_roas_thresholds():
    cp = budget_roas_checkpoint(1000.0, 4.0)
    g = lambda spend, roas: grade_checkpoint(cp, {"spend": spend, "roas": roas}, 10).status
    assert g(1050.0, 3.6) == MET
    assert g(1049.0, 9.0) == PENDING           # spend not yet raised 5%
    assert g(1050.0, 3.59) == MISSED           # raised, return sagged
    assert g(None, 3.6) == PENDING and g(1050.0, None) == PENDING
    assert grade_checkpoint(budget_roas_checkpoint(1000.0, 4.0, spend_key="s", roas_key="r"),
                            {"s": 2000.0, "r": 4.0}, 0).status == MET


def test_other_kind_thresholds():
    w = waste_bleed_checkpoint(1000.0)
    assert grade_checkpoint(w, {"waste": 500.0}, 10).status == MET
    assert grade_checkpoint(w, {"waste": 999.0}, 10).status == PENDING
    assert grade_checkpoint(w, {"waste": 1000.0}, 10).status == MISSED
    t = {"kind": "tier_share", "tier": "p", "baseline": 10.0}
    assert grade_checkpoint(t, {"tier_share": {"p": 11.0}}, 10).status == MET
    assert grade_checkpoint(t, {"tier_share": {"p": 10.25}}, 10).status == PENDING
    assert grade_checkpoint(t, {"tier_share": {"p": 10.24}}, 10).status == MISSED
    r = {"kind": "rival_share", "brand": "b", "baseline_share": 20.0}
    assert grade_checkpoint(r, {"share": {"b": 20.49}}, 10).status == MET
    assert grade_checkpoint(r, {"share": {"b": 20.5}}, 10).status == MISSED
    d = {"kind": "discount_hold", "baseline": 12.0}
    assert grade_checkpoint(d, {"discount": 13.5}, 10).status == MET
    assert grade_checkpoint(d, {"discount": 10.4}, 10).status == MISSED
    assert grade_checkpoint({"kind": "availability_floor", "target": 0.85}, {"availability": 0.85}, 0).status == MET
    cfg = ScorecardConfig(bleed_met_ratio=0.8)
    assert grade_checkpoint(w, {"waste": 800.0}, 10, cfg).status == MET


def test_unknown_kind_custom_grader_and_broken_grader():
    assert grade_checkpoint({"kind": "nope"}, {}, 10).status == PENDING
    graders = {"always": lambda cp, cur, cfg: (1, 2, MET, "ok"),
               "boom": lambda cp, cur, cfg: 1 / 0}
    assert grade_checkpoint({"kind": "always"}, {}, 0, graders=graders).status == MET
    assert grade_checkpoint({"kind": "boom"}, {}, 0, graders=graders).status == PENDING
    register_grader("test_only_kind", graders["always"])
    assert grade_checkpoint({"kind": "test_only_kind"}, {}, 0).now == 2
    assert grade(None, GOOD, NOW) == []


def test_arm_protects_a_young_baseline():
    store = InMemoryBaselineStore()
    assert not arm(store, "acct", "all", [], {}, NOW)                         # nothing to arm
    assert arm(store, "acct", "all", CHECKPOINTS, {"spend": 1.0}, NOW)
    assert not arm(store, "acct", "all", CHECKPOINTS[:1], {}, NOW + timedelta(days=2, hours=23))
    assert len(store.get("acct", "all").checkpoints) == 6                     # the yardstick survived re-runs
    assert arm(store, "acct", "other-scope", CHECKPOINTS[:1], {}, NOW)        # scopes are independent
    assert arm(store, "acct", "all", CHECKPOINTS[:2], {}, NOW + timedelta(days=3))
    assert len(store.get("acct", "all").checkpoints) == 2

    class Broken(InMemoryBaselineStore):
        def put(self, account, scope, baseline):
            raise OSError("disk full")

    assert not arm(Broken(), "acct", "all", CHECKPOINTS, {}, NOW)


def test_arm_then_grade_round_trip():
    store = InMemoryBaselineStore()
    arm(store, "acct", "all", [budget_roas_checkpoint(5000.0, 3.0)], {"spend": 5000.0, "roas": 3.0}, NOW)
    rows = grade(store.get("acct", "all"), {"spend": 6000.0, "roas": 2.9}, NOW + timedelta(days=7))
    assert statuses(rows) == [MET]
