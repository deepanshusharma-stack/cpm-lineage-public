"""Outcome labels, tide, ledger pairing and the difference-in-differences evaluator."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from cpm_lineage.config import Economics
from cpm_lineage.outcomes import (
    OutcomeConfig, counterfactual_verdict, daily_by_entity, damp, detect_taken, did_status, early_call,
    evaluate, is_stacked, outcome_verdict, pair_measure, pair_outcomes, tide, window_roas,
)
from cpm_lineage.schema import ActionCard, AppliedAction, ChangeEvent, OfftakeRow, PerfRow
from cpm_lineage.store import ActionLedger, ExpectedChange, LoggedAction, MemoryBackend, SQLiteBackend

A, P = "adv-001", "generic"
D0 = date(2026, 3, 1)


def prow(day, entity="e1", spend=100.0, roas=2.0, kw=None):
    return PerfRow(date=D0 + timedelta(days=day), entity_id=entity, spend=spend, impressions=1000.0,
                   sales=spend * roas, targeting_unit=kw)


def series(entity, before_roas, after_roas, days=14, spend=100.0, after_spend=None, upto=None):
    upto = days if upto is None else upto
    return ([prow(i, entity, spend, before_roas) for i in range(-days, 0)]
            + [prow(i, entity, spend if after_spend is None else after_spend, after_roas) for i in range(0, upto)])


# --------------------------------------------------------------- labels

def test_damp():
    assert damp(1.69) == pytest.approx(1.3) and damp(9.0) == 2.0 and damp(0.01) == 0.5
    assert damp(None) == 1.0 and damp(0) == 1.0 and damp(2.0) == pytest.approx(2 ** 0.5)


def test_raise_and_cut_labels():
    assert outcome_verdict("up", 3.0, 4.0, 7) == "Off Track"
    assert outcome_verdict("up", 3.4, 4.0, 7) == "On Track"       # exactly 85% of expectation holds
    assert outcome_verdict("up", 3.39, 4.0, 7) == "Off Track"
    assert outcome_verdict("down", 4.0, 4.0, 7) == "On Track"
    assert outcome_verdict("down", 3.8, 4.0, 7) == "On Track"     # exactly 95%
    assert outcome_verdict("down", 3.79, 4.0, 7) == "Review"      # a cut that misses is never Off Track


def test_tide_moves_the_expectation():
    assert outcome_verdict("up", 3.0, 3.0, 7, 1.0) == "On Track"
    assert outcome_verdict("up", 3.0, 3.0, 7, 1.69) == "Off Track"            # expectation 3.9, bar 3.315
    assert outcome_verdict("up", 3.32, 3.0, 7, 1.69) == "On Track"


def test_cut_needs_revenue_retention():
    assert outcome_verdict("down", 4.0, 4.0, 7, 1.0, 800.0, 1000.0) == "On Track"
    assert outcome_verdict("down", 4.0, 4.0, 7, 1.0, 600.0, 1000.0) == "On Track"   # 60% exactly is retained
    assert outcome_verdict("down", 4.0, 4.0, 7, 1.0, 500.0, 1000.0) == "Review"
    assert outcome_verdict("down", 4.0, 4.0, 7, 1.0, 0.0, 1000.0) == "Review"
    assert outcome_verdict("down", 4.0, 4.0, 7, 1.0, 500.0, None) == "On Track"


def test_flat_prediction_and_guards():
    assert outcome_verdict("none", 2.05, 2.0, 7) == "On Track"
    assert outcome_verdict("none", 2.3, 2.0, 7) == "On Track"     # 15% band inclusive
    assert outcome_verdict("none", 3.0, 2.0, 7) == "Review"
    assert outcome_verdict(None, 1.0, 2.0, 7) == "Review"
    assert outcome_verdict("none", 2.05, 2.0, 1) == "Too early"
    assert outcome_verdict("up", 5.0, 2.0, 2) == "Too early"
    assert outcome_verdict("up", 5.0, 2.0, 3) == "On Track"
    assert outcome_verdict("none", 2.0, 0.0, 7) == "No signal"
    assert outcome_verdict("up", 0, 0, 7) == "No signal"          # a zero baseline never judges
    assert outcome_verdict("up", 1.0, 0, 7) == "Review"
    assert outcome_verdict("up", None, 2.0, 7) == "No signal"
    assert outcome_verdict("down", 2.0, None, 7) == "No signal"


def test_counterfactual_labels():
    assert counterfactual_verdict("down", 1.8, 2.0) == "cf-right"             # it kept falling: the cut was right
    assert counterfactual_verdict("down", 1.9, 2.0) == "cf-wrong"             # 95% exactly did not fall
    assert counterfactual_verdict("up", 2.0, 2.0) == "cf-right"
    assert counterfactual_verdict("up", 1.99, 2.0) == "cf-wrong"
    assert counterfactual_verdict("up", 2.0, 2.0, 1.69) == "cf-wrong"         # the tide lifted the bar
    assert counterfactual_verdict("up", None, 2.0) == "cf-nosignal"
    assert counterfactual_verdict("down", 2.0, 0.0) == "cf-nosignal"


def test_early_call_only_names_losers():
    assert early_call("up", 1.0, 4.0, 4) == "Off Track (early)"
    assert early_call("down", 1.0, 4.0, 4) == "Review (early)"
    assert early_call("up", 2.0, 4.0, 4) is None                  # exactly half is not under half
    assert early_call("up", 9.0, 4.0, 4) is None                  # winners are never called early
    assert early_call("up", 1.0, 4.0, 2) is None and early_call("up", 1.0, 4.0, 7) is None
    assert early_call("up", 1.0, 4.0, 3) and early_call("up", 1.0, 4.0, 6)
    assert early_call("up", 1.0, 0.0, 4) is None


# ---------------------------------------------------------- measurement

def test_pair_measure_is_horizon_exact_with_edge_guard():
    daily = daily_by_entity(series("e1", 2.0, 3.0, days=7) + [prow(-8, "e1", 1e6, 9.0), prow(7, "e1", 1e6, 9.0)])
    m = pair_measure(daily["e1"], D0, 7)
    assert m.roas_before == pytest.approx(2.0) and m.roas_after == pytest.approx(3.0)
    assert m.spend_before == m.spend_after == 700.0 and m.revenue_after == pytest.approx(2100.0)
    short = daily_by_entity(series("e1", 2.0, 3.0, days=7, upto=6))["e1"]
    assert pair_measure(short, D0, 7) is None                     # data stops a day short: stays due
    assert pair_measure({}, D0, 7) is None and pair_measure(None, D0, 7) is None
    feed = OutcomeConfig(edge_guard="feed")
    assert pair_measure(short, D0, 7, feed, feed_latest=D0 + timedelta(days=5)) is None
    paused = pair_measure(short, D0, 7, feed, feed_latest=D0 + timedelta(days=6))
    assert paused is not None and paused.spend_after == 600.0
    stopped = daily_by_entity([prow(i, "e1") for i in range(-7, 0)])["e1"]
    assert pair_measure(stopped, D0, 7, feed, feed_latest=D0 + timedelta(days=30)).roas_after is None


def test_tide_is_a_same_length_ratio():
    off = ([OfftakeRow(date=D0 + timedelta(days=i), brand="b", is_own=False, sales=100.0, segment="s")
            for i in range(-7, 0)]
           + [OfftakeRow(date=D0 + timedelta(days=i), brand="own", is_own=True, sales=150.0, segment="s")
              for i in range(0, 7)]
           + [OfftakeRow(date=D0, brand="b", is_own=False, sales=1e9, segment="other"),
              OfftakeRow(date=D0 + timedelta(days=7), brand="b", is_own=False, sales=1e9, segment="s")])
    assert tide(off, "s", D0, 7) == pytest.approx(1.5)
    assert tide(off, None, D0, 7) == 1.0 and tide(off, "missing", D0, 7) == 1.0 and tide([], "s", D0, 7) == 1.0
    assert tide(off, "s", D0 + timedelta(days=30), 7) == 1.0      # no sales on either side


def test_stacked_guard():
    applied = [AppliedAction("budget", "e1", D0), AppliedAction("bid", "e1", D0 + timedelta(days=7)),
               AppliedAction("bid", "e2", D0 + timedelta(days=1))]
    assert is_stacked("e1", D0, 7, applied) is True               # d1 is inclusive
    assert is_stacked("e1", D0, 6, applied) is False
    assert is_stacked("e1", D0 + timedelta(days=7), 7, applied) is False
    assert is_stacked("e1", D0, 7, [AppliedAction("budget", "e1", D0), AppliedAction("bid", "e1", D0)]) is False


# -------------------------------------------------------------- pairing

def acard(cid, entity, direction="up", status="pending", family="growth", segment="s"):
    return ActionCard(id=cid, family=family, type="budget", title="t", segment=segment, entity_id=entity,
                      direction=direction, status=status, stake=100.0, confidence=70)


@pytest.fixture(params=["memory", "sqlite"])
def ledger(request):
    backend = MemoryBackend() if request.param == "memory" else SQLiteBackend(":memory:")
    return ActionLedger(backend, clock=lambda: datetime(2026, 3, 1, 8, 0, 0))


def test_pair_outcomes_end_to_end(ledger):
    cards = [acard("won", "e1"), acard("lost", "e2"), acard("cut", "e3", "down"),
             acard("stack", "e4"), acard("stack2", "e4"), acard("short", "e5"),
             acard("cf", "e6", "down", status="superseded"), acard("nomeasure", None)]
    plan = {"id": "p1", "created": "2026-03-01", "actions": cards}
    ledger.save_plan(A, P, plan)
    ledger.record_emit(A, P, plan)
    for cid in ("won", "lost", "cut", "stack", "short", "nomeasure"):
        assert ledger.mark(A, P, "p1", cid, "applied", today=D0)
    ledger.mark(A, P, "p1", "stack2", "applied", today=D0 + timedelta(days=3))
    rows = (series("e1", 2.0, 2.0) + series("e2", 2.0, 1.0) + series("e3", 2.0, 2.0, after_spend=20.0)
            + series("e4", 2.0, 2.0) + series("e5", 2.0, 2.0, upto=5) + series("e6", 2.0, 1.0))
    today = D0 + timedelta(days=8)
    own = OutcomeConfig(edge_guard="entity")        # e5's own data stops short of the horizon
    rep = pair_outcomes(ledger, A, P, rows, today=today, cfg=own)
    got = ledger.paired(A, P, "p1")
    assert got["won"]["outcome"] == "On Track" and got["lost"]["outcome"] == "Off Track"
    assert got["cut"]["outcome"] == "Review"                      # efficiency held but 80% of revenue went
    assert got["stack"]["outcome"] == "Unreadable" and got["stack"]["roas_after"] is None
    assert got["cf"]["outcome"] == "cf-right"
    assert "short" not in got and "nomeasure" not in got and "stack2" not in got
    assert rep.paired == 5 and rep.deferred == 1 and int(rep) == 5
    assert rep.outcomes == {"On Track": 1, "Off Track": 1, "Review": 1, "Unreadable": 1, "cf-right": 1}
    assert got["won"]["roas_before"] == pytest.approx(2.0) and got["won"]["tide"] == 1.0
    # idempotent: the deferred row is retried, nothing is paired twice
    again = pair_outcomes(ledger, A, P, rows, today=today, cfg=own)
    assert again.paired == 0 and again.deferred == 1
    # only readable realised rows train confidence
    rate, n = ledger.win_rates(A, P, today=today)["growth"]
    assert n == 3 and rate == pytest.approx(1 / 3)
    assert ledger.cf_calibration(A, P, today=today) == {"growth": (1.0, 1)}
    # once the data arrives the deferred row pairs
    later = pair_outcomes(ledger, A, P, rows + [prow(5, "e5"), prow(6, "e5")], today=today, cfg=own)
    assert later.paired == 1 and ledger.paired(A, P, "p1")["short"]["outcome"] == "On Track"


def test_unmeasurable_rows_do_not_starve_the_pairing_queue(ledger):
    # eight cuts on campaigns that then stop serving, and one measurable raise applied later
    early, late = D0 - timedelta(days=30), D0
    cards = [acard(f"cut{i}", f"dead{i}", "down") for i in range(8)] + [acard("raise", "live")]
    plan = {"id": "p1", "created": "2026-01-15", "actions": cards}
    ledger.save_plan(A, P, plan)
    ledger.record_emit(A, P, plan)
    for i in range(8):
        assert ledger.mark(A, P, "p1", f"cut{i}", "applied", today=early)
    assert ledger.mark(A, P, "p1", "raise", "applied", today=late)
    rows = series("live", 2.0, 3.0)
    for i in range(8):
        rows += [PerfRow(date=early - timedelta(days=k), entity_id=f"dead{i}", spend=100.0, impressions=1000.0,
                         sales=50.0) for k in range(1, 8)]              # spend before the cut, none after
    today = D0 + timedelta(days=8)
    # default guard: the feed has reached the horizon, so stopped entities resolve instead of deferring
    rep = pair_outcomes(ledger, A, P, rows, today=today, cfg=OutcomeConfig(pairing_cap=20))
    assert rep.paired == 20 and rep.deferred == 0                    # the queue drains: nothing stays due forever
    rep = pair_outcomes(ledger, A, P, rows, today=today, cfg=OutcomeConfig(pairing_cap=20))
    got = ledger.paired(A, P, "p1")
    assert got["raise"]["outcome"] == "On Track" and rep.deferred == 0
    assert all(got[f"cut{i}"]["outcome"] == "No signal" for i in range(8))


def test_pairing_cap_counts_paired_rows_not_deferred_ones(ledger):
    early = D0 - timedelta(days=30)
    cards = [acard(f"cut{i}", f"dead{i}", "down") for i in range(8)] + [acard("raise", "live")]
    plan = {"id": "p1", "created": "2026-01-15", "actions": cards}
    ledger.save_plan(A, P, plan)
    ledger.record_emit(A, P, plan)
    for i in range(8):
        ledger.mark(A, P, "p1", f"cut{i}", "applied", today=early)
    ledger.mark(A, P, "p1", "raise", "applied", today=D0)
    today = D0 + timedelta(days=8)
    # under the entity guard the 24 dead rows (8 cards x 3 horizons) defer; a cap of 1 must still reach the raise
    cfg = OutcomeConfig(edge_guard="entity", pairing_cap=1)
    rep = pair_outcomes(ledger, A, P, series("live", 2.0, 3.0), today=today, cfg=cfg)
    assert rep.paired == 1 and rep.deferred >= 16
    assert ledger.paired(A, P, "p1")["raise"]["outcome"] == "On Track"


def test_pairing_uses_the_market_tide(ledger):
    plan = {"id": "p1", "created": "2026-03-01", "actions": [acard("a", "e1"), acard("b", "e2", segment=None)]}
    ledger.save_plan(A, P, plan)
    ledger.record_emit(A, P, plan)
    ledger.mark(A, P, "p1", "a", "applied", today=D0)
    ledger.mark(A, P, "p1", "b", "applied", today=D0)
    off = ([OfftakeRow(date=D0 + timedelta(days=i), brand="x", is_own=False, sales=100.0, segment="s")
            for i in range(-7, 0)]
           + [OfftakeRow(date=D0 + timedelta(days=i), brand="x", is_own=False, sales=169.0, segment="s")
              for i in range(0, 7)])
    rows = series("e1", 3.0, 3.0) + series("e2", 3.0, 3.0)
    pair_outcomes(ledger, A, P, rows, today=D0 + timedelta(days=7), offtake=off)
    got = ledger.paired(A, P, "p1")
    assert got["a"]["tide"] == pytest.approx(1.69) and got["a"]["outcome"] == "Off Track"
    assert got["b"]["tide"] == 1.0 and got["b"]["outcome"] == "On Track"      # no segment: no tide


def test_pairing_cap_bounds_each_set(ledger):
    cards = [acard(f"a{i}", f"e{i}") for i in range(5)] + [acard(f"c{i}", f"x{i}", status="vetoed") for i in range(5)]
    plan = {"id": "p1", "created": "2026-03-01", "actions": cards}
    ledger.save_plan(A, P, plan)
    ledger.record_emit(A, P, plan)
    rows = []
    for i in range(5):
        ledger.mark(A, P, "p1", f"a{i}", "applied", today=D0)
        rows += series(f"e{i}", 2.0, 2.0) + series(f"x{i}", 2.0, 2.0)
    rep = pair_outcomes(ledger, A, P, rows, today=D0 + timedelta(days=7), cfg=OutcomeConfig(pairing_cap=2))
    assert rep.paired == 4
    assert sum(1 for r in ledger.ledger_rows(A, P) if r["paired_at"]) == 4


def test_pairing_on_synthetic_data(ledger, synthetic_dataset):
    d0 = synthetic_dataset.latest_date - timedelta(days=14)
    plan = {"id": "p1", "created": d0.isoformat(),
            "actions": [acard("w", "cmp-winner", "up", segment="snack-bars"),
                        acard("c", "cmp-conquest", "down", status="vetoed", segment="snack-bars")]}
    clock_ledger = ActionLedger(ledger.backend, clock=lambda: datetime(d0.year, d0.month, d0.day))
    clock_ledger.save_plan(A, P, plan)
    clock_ledger.record_emit(A, P, plan)
    clock_ledger.mark(A, P, "p1", "w", "applied", today=d0)
    rep = pair_outcomes(clock_ledger, A, P, synthetic_dataset.perf, today=synthetic_dataset.latest_date + timedelta(days=1),
                        offtake=synthetic_dataset.offtake)
    got = clock_ledger.paired(A, P, "p1")
    assert rep.paired == 4                                         # 7 and 14 day rows, realised + counterfactual
    assert got["w"]["horizon"] == 14 and got["w"]["outcome"] in ("On Track", "Off Track")
    assert got["w"]["roas_before"] > 3 and 0.5 < got["w"]["tide"] < 2.0
    assert got["c"]["outcome"].startswith("cf-")


# ---------------------------------------------------------------- DiD

def entry(entity="e1", day=0, expected=(), budget=None):
    return LoggedAction(date=D0 + timedelta(days=day), account=A, platform=P, entity_id=entity,
                        budget_daily_at=budget, expected=tuple(expected))


def did_rows(treated=(2.0, 3.0), control=(2.0, 2.0), spend=200.0, issue_day_roas=50.0):
    rows = []
    for i in range(-14, 15):
        if i == 0:
            rows += [prow(0, "e1", spend, issue_day_roas), prow(0, "c1", spend, issue_day_roas)]
            continue
        rows.append(prow(i, "e1", spend, treated[0] if i < 0 else treated[1]))
        rows.append(prow(i, "c1", spend, control[0] if i < 0 else control[1]))
        rows.append(prow(i, "c2", spend, control[0] if i < 0 else control[1]))
    return rows


def test_did_sign_and_issue_day_excluded():
    today = D0 + timedelta(days=14)
    r = evaluate([entry()], did_rows(), today=today)[0]
    assert (r.age, r.window, r.roas_pre, r.roas_post) == (14, 14, pytest.approx(2.0), pytest.approx(3.0))
    assert r.did == 1.0 and r.control_delta == 0.0 and r.status == "landed" and r.spend_post == 2800.0
    r = evaluate([entry()], did_rows(treated=(2.0, 3.0), control=(2.0, 3.0)), today=today)[0]
    assert r.did == 0.0 and r.control_delta == 1.0 and r.status == "flat"    # the whole account moved
    r = evaluate([entry()], did_rows(treated=(2.0, 2.0), control=(2.0, 3.0)), today=today)[0]
    assert r.did == -1.0 and r.status == "worsened"


def test_did_control_excludes_the_treated_entity():
    # the treated entity is 100x the rest: left in its own baseline it would cancel itself out
    rows = [r for r in did_rows(spend=100.0) if r.entity_id != "e1"]
    rows += [prow(i, "e1", 10_000.0, 2.0 if i < 0 else 3.0) for i in range(-14, 15) if i != 0]
    r = evaluate([entry()], rows, today=D0 + timedelta(days=14))[0]
    assert r.did == 1.0 and r.control_delta == 0.0
    only = evaluate([entry()], rows, today=D0 + timedelta(days=14), control_entity_ids=["e1"])[0]
    assert only.did is None and only.status == "no-data"          # no control left: unmeasurable is not flat
    one = evaluate([entry()], rows, today=D0 + timedelta(days=14), control_entity_ids=["c1", "e1"])[0]
    assert one.did == 1.0


def test_did_status_boundaries():
    assert did_status(0.11, 2.0, 5000.0, 1000.0) == "landed"
    assert did_status(0.10, 2.0, 5000.0, 1000.0) == "flat"
    assert did_status(-0.10, 2.0, 5000.0, 1000.0) == "flat"
    assert did_status(-0.11, 2.0, 5000.0, 1000.0) == "worsened"
    assert did_status(0.5, 2.0, 999.99, 1000.0) == "no-data"
    assert did_status(0.5, 2.0, 1000.0, 1000.0) == "landed"
    assert did_status(None, 2.0, 5000.0, 1000.0) == "no-data"
    assert did_status(0.5, None, 5000.0, 1000.0) == "no-data"


def test_did_age_window_and_money_threshold():
    rows = did_rows()
    young = evaluate([entry()], rows, today=D0 + timedelta(days=6))[0]
    assert young.status == "too-early" and young.did is None
    week = evaluate([entry()], rows, today=D0 + timedelta(days=7))[0]
    assert week.window == 7 and week.spend_post == 1400.0 and week.status == "landed"
    old = evaluate([entry()], rows, today=D0 + timedelta(days=60))[0]
    assert old.window == 14
    assert evaluate([entry()], rows, today=D0 + timedelta(days=7),
                    economics=Economics(currency_unit=2.0))[0].status == "no-data"   # 1400 < 2 x 1000
    stopped = [r for r in rows if not (r.entity_id == "e1" and r.date > D0)]
    assert evaluate([entry()], stopped, today=D0 + timedelta(days=14))[0].status == "no-data"
    order = evaluate([entry(day=-3), entry(day=0), entry("c1", day=-1)], rows, today=D0 + timedelta(days=14))
    assert [r.entry.date for r in order] == [D0, D0 - timedelta(days=1), D0 - timedelta(days=3)]


def test_did_one_bad_entry_does_not_stop_the_rest():
    bad = LoggedAction(date=D0, account=A, platform=P, entity_id="e1", expected=("not a change",))  # type: ignore[arg-type]
    out = evaluate([bad, entry("c1", day=-1)], did_rows(), today=D0 + timedelta(days=14))
    assert out[0].status == "error" and out[0].error and out[1].status in ("flat", "landed", "worsened")


def ev(day, field, old, new, entity="e1", **kw):
    return ChangeEvent(datetime(2026, 3, 1, 12) + timedelta(days=day), entity, field, old, new, **kw)


def test_taken_canonical_budget_direction():
    up = entry(expected=[ExpectedChange("budget", "up", verb="raise")])
    assert detect_taken(up, 14, change_events=[ev(1, "daily_budget", 100, 150, source="console")]) == [
        {"field": "daily_budget", "source": "console", "targeting_unit": None, "count": 1,
         "basis": "change_log", "old": 100, "new": 150}]
    assert detect_taken(up, 14, change_events=[ev(1, "daily_budget", 150, 100)]) == []         # wrong direction
    assert len(detect_taken(up, 14, change_events=[ev(1, "daily_budget", "n/a", "auto")])) == 1  # unparseable counts
    assert detect_taken(up, 14, change_events=[ev(14, "daily_budget", 100, 150)]) == []        # outside [d0, d0+W)
    assert len(detect_taken(up, 14, change_events=[ev(0, "daily_budget", 100, 150)])) == 1
    assert detect_taken(up, 14, change_events=[ev(1, "daily_budget", 100, 150, entity="other")]) == []
    assert detect_taken(up, 14, change_events=[ev(1, "daily_budget", 100, 150, verified=False)]) == []
    no_budget = entry(expected=[ExpectedChange("bid", "down")])
    assert detect_taken(no_budget, 14, change_events=[ev(1, "daily_budget", 100, 150)]) == []
    twice = detect_taken(up, 14, change_events=[ev(1, "daily_budget", 100, 120, source="console"),
                                                ev(2, "daily_budget", 120, 150, source="console")])
    assert twice[0]["count"] == 2 and (twice[0]["old"], twice[0]["new"]) == (120, 150)


def test_taken_canonical_keyword_bid_status():
    cut = entry(expected=[ExpectedChange("keyword", "down", targets=("Rival Bar",))])
    assert len(detect_taken(cut, 14, change_events=[ev(1, "keyword_removed", None, None, targeting_unit="rival bar")])) == 1
    assert len(detect_taken(cut, 14, change_events=[ev(1, "negative_keyword_added", None, None,
                                                       targeting_unit="rival bar")])) == 1
    assert detect_taken(cut, 14, change_events=[ev(1, "keyword_removed", None, None, targeting_unit="other")]) == []
    assert detect_taken(cut, 14, change_events=[ev(1, "bid", 5, 4)]) == []                     # no bid expected
    bid = entry(expected=[ExpectedChange("bid", "down")])
    assert len(detect_taken(bid, 14, change_events=[ev(1, "bid", 5, 4)])) == 1
    assert detect_taken(bid, 14, change_events=[ev(1, "bid", 5, 4, automated=True)]) == []     # automation re-bids
    assert detect_taken(bid, 14, change_events=[ev(1, "status", "ACTIVE", "PAUSED")]) == []
    status = entry(expected=[ExpectedChange("status", "up")])
    assert len(detect_taken(status, 14, change_events=[ev(1, "status", "PAUSED", "ACTIVE")])) == 1
    many = [ev(i, "bid", 5, 4, source=f"s{i}") for i in range(1, 8)]
    assert len(detect_taken(bid, 14, change_events=many)) == 4                                 # at most four lines


def test_taken_fallback_budget_inference():
    up = entry(expected=[ExpectedChange("budget", "up")], budget=500.0)
    assert detect_taken(up, 14, current_budget=600.0)[0]["basis"] == "inferred"
    assert detect_taken(up, 14, current_budget=501.0) == []       # within one currency unit
    assert detect_taken(up, 14, current_budget=501.5)[0]["new"] == 501.5
    assert detect_taken(up, 14, current_budget=400.0) == []       # moved the other way
    assert detect_taken(up, 14, current_budget=501.5, economics=Economics(currency_unit=2.0)) == []
    hold = entry(expected=[ExpectedChange("budget", None, verb="hold the raise")], budget=500.0)
    assert detect_taken(hold, 14, current_budget=600.0) == []     # no committed direction: nothing to infer
    # canonical evidence is authoritative when it finds something
    both = detect_taken(up, 14, current_budget=600.0, change_events=[ev(1, "daily_budget", 500, 600)])
    assert [t["basis"] for t in both] == ["change_log"]


def test_taken_fallback_keyword_share():
    def rows(post_target):
        out = []
        for i in range(-14, 15):
            if i == 0:
                continue
            out.append(prow(i, "e1", 40.0 if i < 0 else post_target, kw="rival bar"))
            out.append(prow(i, "e1", 60.0, kw="other"))
        return out
    cut = entry(expected=[ExpectedChange("keyword", "down", targets=("rival bar",))])
    assert detect_taken(cut, 14, rows=rows(10.0))[0]["field"] == "keyword_spend_share"   # 40% -> 14%
    assert detect_taken(cut, 14, rows=rows(30.0)) == []                                  # 40% -> 33%
    scale = entry(expected=[ExpectedChange("keyword", "up", targets=("rival bar",))])
    assert len(detect_taken(scale, 14, rows=rows(100.0))) == 1                           # 40% -> 62.5%
    assert detect_taken(scale, 14, rows=rows(45.0)) == []                                # under 1.2x
    ghost = entry(expected=[ExpectedChange("keyword", "down", targets=("never served",))])
    assert detect_taken(ghost, 14, rows=rows(10.0)) == []                                # pre-share too small
    r = evaluate([cut], rows(10.0) + did_rows()[1:], today=D0 + timedelta(days=14))[0]
    assert r.taken and r.taken[0]["basis"] == "inferred"


def test_window_roas():
    daily = daily_by_entity([prow(0, spend=100.0, roas=2.0), prow(1, spend=300.0, roas=4.0), prow(2, spend=0.0)])
    assert window_roas(daily["e1"], D0, D0 + timedelta(days=2)) == (pytest.approx(3.5), 400.0)
    assert window_roas(daily["e1"], D0 + timedelta(days=2), D0 + timedelta(days=3)) == (None, 0.0)
