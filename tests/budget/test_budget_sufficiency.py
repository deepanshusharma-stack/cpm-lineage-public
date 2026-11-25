from datetime import date, timedelta

import pytest

from cpm_lineage.budget import (BudgetConfig, bound_method_for_profile, budget_allocation,
                                build_budget_telemetry, collapse_entity_days, day_sufficiency,
                                derive, filter_rows_by_tags, is_budget_bound, is_oob_day,
                                summarise_oob, utilisation)
from cpm_lineage.config import BudgetBoundPolicy, Economics
from cpm_lineage.schema import BudgetDayRow, BudgetTelemetry, Entity, Window
from cpm_lineage.windows import reporting_window

D0 = date(2026, 3, 1)


def day(i, eid="e1", budget=1000.0, spend=1000.0, sales=3000.0, **kw):
    return BudgetDayRow(date=D0 + timedelta(days=i), entity_id=eid, daily_budget=budget,
                        spend=spend, sales=sales, **kw)


def test_derive_pinned_values():
    assert derive(1000, 0.2) == pytest.approx((5000, 4000))
    assert derive(1000, 1.0) == pytest.approx((1000, 0))
    assert derive(1000, None) == pytest.approx((1000, 0))
    assert derive(1000, 0.0)[1] == pytest.approx(99000)      # 100x clamp
    assert derive(1000, 1.7) == pytest.approx((1000, 0))     # never above fully served


def test_reporting_window_examples():
    w = reporting_window(date(2026, 7, 23))
    assert (w.start, w.end) == (date(2026, 7, 1), date(2026, 7, 19))
    w = reporting_window(date(2026, 8, 3))
    assert w.end == date(2026, 8, 2) and (w.end - w.start).days == 20
    assert reporting_window(date(2026, 7, 19)).end == date(2026, 7, 12)   # a week-end day is still in progress


def test_oob_day_needs_flag_and_spend_at_threshold():
    assert is_oob_day(day(0, spend=500.0, exhausted_at_minute=600))        # exactly half the cap
    assert not is_oob_day(day(0, spend=499.9, exhausted_at_minute=600))    # flag at low spend is not proof
    assert not is_oob_day(day(0, spend=1000.0))                            # no flag
    assert day_sufficiency(day(0, spend=40.0, exhausted_at_minute=600, live_minutes=100,
                               expected_live_minutes=1000)) == 1.0


def test_day_sufficiency_fallbacks():
    assert day_sufficiency(day(0, exhausted_at_minute=600, live_minutes=300, expected_live_minutes=1000)) == 0.3
    assert day_sufficiency(day(0, exhausted_at_minute=600, expected_live_minutes=1000)) == 0.6
    assert day_sufficiency(day(0, exhausted_at_minute=600, live_minutes=2000, expected_live_minutes=1000)) == 1.0
    assert day_sufficiency(day(0, exhausted_at_minute=600, live_minutes=300)) is None
    cfg = BudgetConfig(exhaust_minute_origin=360.0)
    assert day_sufficiency(day(0, exhausted_at_minute=600, expected_live_minutes=960), cfg) == 0.25


def test_collapse_sums_additive_and_maxes_entity_fields():
    rows = [day(0, budget=800.0, spend=300.0, sales=900.0, live_minutes=200),
            day(0, budget=1000.0, spend=200.0, sales=100.0, exhausted_at_minute=500, live_minutes=350)]
    (r,) = collapse_entity_days(rows)
    assert (r.daily_budget, r.spend, r.sales) == (1000.0, 500.0, 1000.0)
    assert (r.exhausted_at_minute, r.live_minutes) == (500, 350)


def _allocation():
    rows = []
    for i in range(10):   # e1: every day out of budget, served 20% of the day
        rows.append(day(i, "e1", exhausted_at_minute=400, live_minutes=200, expected_live_minutes=1000))
    for i in range(10):   # e2: sufficient half the days
        kw = dict(exhausted_at_minute=500, live_minutes=500, expected_live_minutes=1000) if i % 2 else {}
        rows.append(day(i, "e2", budget=2000.0, spend=1900.0, sales=1900.0, **kw))
    for i in range(10):   # e3: placeholder cap
        rows.append(day(i, "e3", budget=50.0, spend=50.0, sales=10.0,
                        exhausted_at_minute=300, live_minutes=100, expected_live_minutes=1000))
    for i in range(5):    # e4: ended, spent early in the window only
        rows.append(day(i, "e4", spend=900.0, exhausted_at_minute=300, live_minutes=500,
                        expected_live_minutes=1000))
    for i in range(10):   # e5: no exhaustion telemetry at all
        rows.append(day(i, "e5", spend=990.0))
    rows.append(day(3, "e5", spend=0.0))   # zero-spend duplicates are ignored after collapse
    ents = [Entity("e1", name="one", status="ACTIVE", tags=(("team", "a"), ("goal", "x")), owner="acct"),
            Entity("e2", status="ACTIVE", tags=(("team", "b"), ("goal", "x")), owner="acct"),
            Entity("e3", status="ACTIVE", tags=(("other", "a"),)),
            Entity("e4", status="ENDED", owner="acct"),
            Entity("e5", status="ACTIVE", owner="someone-else")]
    return rows, ents


def test_budget_allocation_rows_and_summary():
    rows, ents = _allocation()
    rep = budget_allocation(rows, Window(D0, D0 + timedelta(days=9)), ents)
    by = {r.entity_id: r for r in rep.rows}
    assert [r.entity_id for r in rep.rows][0] == "e1"                     # sorted by extra desc
    assert by["e1"].sufficiency == pytest.approx(0.2)
    assert (by["e1"].required, by["e1"].extra) == pytest.approx((5000.0, 4000.0))
    assert by["e1"].roas == pytest.approx(3.0) and by["e1"].oob_days == 10
    assert by["e2"].sufficiency == pytest.approx(0.75)                   # non-exhausted days count 1.0
    assert by["e2"].extra == pytest.approx(2000 / 0.75 - 2000)
    assert by["e3"].anomaly and by["e3"].extra == 0.0
    assert not by["e4"].live and by["e1"].live
    assert by["e5"].sufficiency is None and by["e5"].extra == 0.0 and by["e5"].days == 10
    s = summarise_oob(rep.rows)
    assert s["n"] == 2 and s["extra_per_day"] == pytest.approx(by["e1"].extra + by["e2"].extra)
    assert s["extra_projected"] == pytest.approx(30 * s["extra_per_day"])
    assert s["current"] == pytest.approx(3000.0)
    assert rep.tag_groups["team"] == ("team/a", "team/b")


def test_placeholder_cap_scales_with_currency():
    rows, ents = _allocation()
    rep = budget_allocation(rows, Window(D0, D0 + timedelta(days=9)), ents,
                            economics=Economics(currency_unit=0.1))
    assert not {r.entity_id: r for r in rep.rows}["e3"].anomaly           # 50 >= 100 * 0.1


def test_owner_guard_drops_only_known_foreign_entities():
    rows, ents = _allocation()
    rep = budget_allocation(rows, Window(D0, D0 + timedelta(days=9)), ents, owner="acct")
    ids = {r.entity_id for r in rep.rows}
    assert "e5" not in ids and "e3" in ids                                # unknown owner stays
    rep = budget_allocation(rows, Window(D0, D0 + timedelta(days=9)), ents, owner="nobody")
    assert "e5" in {r.entity_id for r in rep.rows}                        # guard needs one match to arm


def test_tag_filter_modes():
    rows, ents = _allocation()
    rep = budget_allocation(rows, Window(D0, D0 + timedelta(days=9)), ents).rows
    assert len(filter_rows_by_tags(rep, None)) == len(rep)
    assert {r.entity_id for r in filter_rows_by_tags(rep, ["team/a", "team/b"], "any")} == {"e1", "e2"}
    assert {r.entity_id for r in filter_rows_by_tags(rep, ["team/a", "goal/x"], "all")} == {"e1"}
    assert {r.entity_id for r in filter_rows_by_tags(rep, ["other/a"], "any")} == {"e3"}   # same name, other group
    with pytest.raises(ValueError):
        filter_rows_by_tags(rep, ["team/a"], "none")


# --- telemetry and the budget-bound policy ----------------------------------------

def test_telemetry_formulas():
    rows = []
    for i in range(10):
        sales = 2000.0 if i < 5 else 4000.0
        budget = 1000.0 if i < 8 else 1500.0
        kw = dict(exhausted_at_minute=600.0, live_minutes=300.0, expected_live_minutes=1000.0) if i >= 7 else \
            dict(expected_live_minutes=1000.0)
        rows.append(day(i, budget=budget, spend=1000.0, sales=sales, impressions=10.0, **kw))
    rows.append(day(0, "small", spend=100.0))
    t = build_budget_telemetry(rows, days=10)
    assert "small" not in t                                    # at or below the evidence floor
    b = t["e1"]
    assert b.n_days == 10 and b.spend == 10000.0 and b.impressions == 100.0
    assert b.avg_budget == pytest.approx(1100.0) and b.avg_spend == 1000.0
    assert b.roas == pytest.approx(3.0) and b.roas_recent == pytest.approx(4.0)
    sd = (sum((x - 3.0) ** 2 for x in [2.0] * 5 + [4.0] * 5) / 9) ** 0.5
    assert b.roas_sd == pytest.approx(sd) and b.roas_se == pytest.approx(sd / 10 ** 0.5)
    assert b.roas_lo == pytest.approx(3.0 - 1.28 * b.roas_se) and b.roas_hi == pytest.approx(3.0 + 1.28 * b.roas_se)
    assert b.exhaust_minute == 600.0 and b.exhaust_days == 3
    assert b.budget_latest == 1500.0 and b.budget_before_recent == 1000.0
    assert b.sufficiency_3d == pytest.approx(0.3)
    assert b.capped_days == 8 and b.active_days == 10 and b.held_days is None
    assert utilisation(b) == pytest.approx(100 * 1000 / 1100) and utilisation(None) == 0.0


def test_telemetry_sufficiency_none_without_served_time_feed():
    t = build_budget_telemetry([day(i, spend=1000.0) for i in range(10)], days=10)
    assert t["e1"].sufficiency_3d is None and t["e1"].roas_se is not None
    t = build_budget_telemetry([day(i, spend=2000.0) for i in range(4)], days=10)
    assert t["e1"].roas_se is None and t["e1"].roas_lo is None       # fewer than 5 days


def _tel(**kw):
    base = dict(entity_id="e", n_days=10, avg_budget=1000.0, avg_spend=600.0)
    base.update(kw)
    return BudgetTelemetry(**base)


def test_bound_sufficiency_threshold():
    assert is_budget_bound(_tel(sufficiency_3d=0.899), rule_profile="verdict")
    assert not is_budget_bound(_tel(sufficiency_3d=0.90), rule_profile="verdict")
    assert not is_budget_bound(_tel(sufficiency_3d=None), rule_profile="verdict")
    assert not is_budget_bound(None) and not is_budget_bound(_tel(avg_budget=None, sufficiency_3d=0.1))


def test_bound_exhaustion_thresholds():
    ok = dict(exhaust_minute=719.0, avg_spend=500.0, exhaust_days=4)
    assert is_budget_bound(_tel(**ok), rule_profile="classic")
    assert not is_budget_bound(_tel(**{**ok, "exhaust_minute": 720.0}), rule_profile="classic")
    assert not is_budget_bound(_tel(**{**ok, "avg_spend": 499.0}), rule_profile="marginal")
    assert not is_budget_bound(_tel(**{**ok, "exhaust_days": 3}), rule_profile="classic")
    assert is_budget_bound(_tel(exhaust_minute=100.0, avg_spend=500.0), method="exhaustion")


def test_bound_other_methods_and_profiles():
    assert is_budget_bound(_tel(capped_days=6, active_days=10), method="capped_days")
    assert not is_budget_bound(_tel(capped_days=5, active_days=10), method="capped_days")
    assert is_budget_bound(_tel(held_days=3), method="held_days")
    assert not is_budget_bound(_tel(held_days=2), BudgetBoundPolicy(), method="held_days")
    assert bound_method_for_profile("verdict") == "sufficiency"
    with pytest.raises(ValueError):
        bound_method_for_profile("v9")


def test_synthetic_winner_is_bound_and_needs_budget(budget_days, entities, synthetic_profile):
    end = max(r.date for r in budget_days)
    rep = budget_allocation(budget_days, Window(end - timedelta(days=20), end), entities,
                            status_map=synthetic_profile.status_map)
    top = rep.rows[0]
    assert top.entity_id == "cmp-winner" and top.live and top.extra > top.avg_budget
    assert top.sufficiency is not None and top.sufficiency < 0.6
    tel = build_budget_telemetry(budget_days, entities=entities)
    assert is_budget_bound(tel["cmp-winner"])
    assert is_budget_bound(tel["cmp-winner"], method="capped_days")
    others = [e for e in tel if e != "cmp-winner"]
    assert others and not any(is_budget_bound(tel[e]) for e in others)
