"""Storage backends, action ledger, history store, change log and recommendation log."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from cpm_lineage.schema import ActionCard, ChangeEvent, PerfRow, ShelfRow, TargetingUnit
from cpm_lineage.store import (
    ActionLedger, ActionLog, ChangeLog, ConfigVersion, ExpectedChange, HistoryStore, LedgerConfig,
    LoggedAction, MemoryBackend, SQLiteBackend, StorageBackend, build_entity_history, diff_versions,
    geo_history, input_change_ledger, keyword_history, product_history,
)
from cpm_lineage.verdict import open_entities, recent_bid_downs

A, P = "adv-001", "generic"
T0 = date(2026, 3, 1)


@pytest.fixture(params=["memory", "sqlite-memory", "sqlite-file"])
def backend(request, tmp_path):
    if request.param == "memory":
        b = MemoryBackend()
    elif request.param == "sqlite-memory":
        b = SQLiteBackend(":memory:")
    else:
        b = SQLiteBackend(str(tmp_path / "actions.db"))
    yield b
    b.close()


def acard(cid, entity="e1", family="growth", type="budget", direction="up", status="pending", **kw):
    c = ActionCard(id=cid, family=family, type=type, title="t", segment="seg", entity_id=entity,
                   direction=direction, status=status, stake=kw.pop("stake", 100.0),
                   confidence=kw.pop("confidence", 60))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def plan(pid, cards, created="2026-03-01"):
    return {"id": pid, "created": created, "days": 30, "actions": cards}


def ledger(backend, day=T0):
    return ActionLedger(backend, clock=lambda: datetime(day.year, day.month, day.day, 9, 0, 0))


# ------------------------------------------------------------ backends

def test_backend_contract(backend):
    assert isinstance(backend, StorageBackend)
    assert backend.get("t", ("a", "b")) is None
    backend.put("t", ("a", "b"), {"x": 1, "when": date(2026, 1, 2), "tags": ("p", "q")})
    assert backend.get("t", ("a", "b")) == {"x": 1, "when": "2026-01-02", "tags": ["p", "q"]}
    assert backend.put_if_absent("t", ("a", "b"), {"x": 2}) is False
    assert backend.put_if_absent("t", ("a", "bb"), {"x": 3}) is True
    backend.put("t", ("ab", "c"), {"x": 4})
    backend.put("other", ("a", "b"), {"x": 5})
    assert [k for k, _ in backend.scan("t", ("a",))] == [("a", "b"), ("a", "bb")]
    assert [k for k, _ in backend.scan("t", ("a", "b"))] == [("a", "b")]
    assert len(backend.scan("t")) == 3 and backend.scan("missing") == []
    got = backend.get("t", ("a", "b"))
    got["x"] = 99
    assert backend.get("t", ("a", "b"))["x"] == 1                 # callers cannot mutate stored state
    assert backend.delete("t", ("a", "b")) is True and backend.delete("t", ("a", "b")) is False
    with backend.transaction():
        with backend.transaction():
            backend.put("t", ("z",), {"x": 6})
    assert backend.get("t", ("z",)) == {"x": 6}


def test_sqlite_rolls_back_and_persists(tmp_path):
    path = str(tmp_path / "s.db")
    b = SQLiteBackend(path)
    b.put("t", ("keep",), {"v": 1})
    with pytest.raises(RuntimeError):
        with b.transaction():
            b.put("t", ("gone",), {"v": 2})
            raise RuntimeError("boom")
    assert b.get("t", ("gone",)) is None
    b.close()
    again = SQLiteBackend(path)
    assert again.get("t", ("keep",)) == {"v": 1}
    again.close()


# ---------------------------------------------------------- plans / mark

def test_save_load_roundtrip_and_ordering(backend):
    led = ledger(backend)
    cards = [acard("v", status="vetoed", confidence=90), acard("lo", confidence=40), acard("hi", confidence=80),
             acard("hi2", confidence=80, stake=500.0), acard("t", status="tension", confidence=95)]
    led.save_plan(A, P, plan("p1", cards))
    led.save_plan(A, P, plan("p2", [acard("x")], created="2026-03-05"))
    led.save_plan(A, P, plan("p0", [acard("y")], created="2026-03-05"))
    plans = led.load(A, P)
    assert [p["id"] for p in plans] == ["p0", "p2", "p1"]         # created desc, then save order desc
    assert plans[0]["days"] == 30
    assert [c["id"] for c in plans[2]["actions"]] == ["hi2", "hi", "lo", "t", "v"]
    assert plans[2]["actions"][0]["entity_id"] == "e1"
    assert len(led.load(A, P, limit=1)) == 1 and led.load("someone-else", P) == []
    with pytest.raises(ValueError):
        led.save_plan(A, P, {"actions": []})


def test_mark_apply_skip_and_refusals(backend):
    led = ledger(backend)
    led.save_plan(A, P, plan("p1", [acard("a"), acard("s", status="superseded"), acard("v", status="vetoed"),
                                    acard("m", status="merged"), acard("k")]))
    assert led.mark(A, P, "p1", "a", "applied", today=T0) is True
    assert led.mark(A, P, "p1", "s", "applied") is False          # a conflict loser is never executed
    assert led.mark(A, P, "p1", "v", "applied") is False
    assert led.mark(A, P, "p1", "m", "applied") is False
    assert led.mark(A, P, "p1", "a", "vetoed") is False           # not an operator status
    assert led.mark(A, P, "p1", "nope", "applied") is False
    assert led.mark(A, P, "p1", "k", "skipped", reason="not now") is True
    by_id = {c["id"]: c for c in led.load(A, P)[0]["actions"]}
    assert by_id["a"]["status"] == "applied" and by_id["a"]["applied_at"] == "2026-03-01"
    assert by_id["k"]["status"] == "skipped" and by_id["k"]["skip_reason"] == "not now"
    assert by_id["s"]["status"] == "superseded"
    assert led.accept_rates(A, P, today=T0) == {"growth": (0.5, 2)}
    assert led.accept_rates(A, P, today=T0 + timedelta(days=91)) == {}     # the window is honoured
    assert led.mark(A, P, "p1", "a", "pending") is True
    assert {c["id"]: c for c in led.load(A, P)[0]["actions"]}["a"]["applied_at"] is None


def test_exactly_one_apply_per_tension_group(backend):
    led = ledger(backend)
    led.save_plan(A, P, plan("p1", [acard("up", status="tension", tension_group="e1"),
                                    acard("down", status="tension", tension_group="e1", direction="down"),
                                    acard("free", entity="e2", status="tension", tension_group="e2")]))
    assert led.mark(A, P, "p1", "up", "applied") is True
    assert led.mark(A, P, "p1", "down", "applied") is False
    assert led.mark(A, P, "p1", "free", "applied") is True
    assert led.mark(A, P, "p1", "up", "pending") is True          # releasing the choice frees the group
    assert led.mark(A, P, "p1", "down", "applied") is True


# ----------------------------------------------------------- the ledger

def test_emit_due_pair_and_win_rate(backend):
    led = ledger(backend)
    p = plan("p1", [acard("a"), acard("s", status="superseded"), acard("noent", entity=None)])
    led.save_plan(A, P, p)
    assert led.record_emit(A, P, p) == 9                          # every card, three horizons each
    assert led.record_emit(A, P, p) == 0                          # insert-if-absent
    assert led.due_pairs(A, P, today=T0 + timedelta(days=40)) == []          # nothing applied yet
    led.mark(A, P, "p1", "a", "applied", today=T0)
    led.mark(A, P, "p1", "noent", "applied", today=T0)
    assert [r["horizon"] for r in led.due_pairs(A, P, today=T0 + timedelta(days=6))] == []
    due = led.due_pairs(A, P, today=T0 + timedelta(days=8))
    assert [(r["action_id"], r["horizon"], r["d0"]) for r in due] == [("a", 7, "2026-03-01")]   # 30-day row not due
    assert [r["horizon"] for r in led.due_pairs(A, P, today=T0 + timedelta(days=30))] == [7, 14, 30]
    assert all(r["entity_id"] for r in led.due_pairs(A, P, today=T0 + timedelta(days=99)))      # no-entity row never due

    assert led.store_pair(A, P, "p1", "a", 7, outcome="On Track", tide=1.1, roas_before=2.0, roas_after=2.2)
    assert led.store_pair(A, P, "p1", "a", 7, outcome="Off Track") is False  # pairing is permanent
    assert led.store_pair(A, P, "p1", "zzz", 7, outcome="On Track") is False
    assert led.win_rates(A, P, today=T0 + timedelta(days=8)) == {"growth": (1.0, 1)}
    assert led.win_rates(A, P, horizon=14, today=T0 + timedelta(days=8)) == {}
    assert led.win_rates(A, P, today=T0 + timedelta(days=91)) == {}          # emitted more than 90 days ago
    assert [r["horizon"] for r in led.due_pairs(A, P, today=T0 + timedelta(days=8))] == []
    assert led.paired(A, P, "p1") == {"a": {"horizon": 7, "tide": 1.1, "roas_before": 2.0, "roas_after": 2.2,
                                            "outcome": "On Track", "paired_at": "2026-03-01T09:00:00"}}
    led.store_pair(A, P, "p1", "a", 14, outcome="Review")
    assert led.paired(A, P, "p1")["a"]["horizon"] == 14           # widest elapsed horizon wins


def test_win_rates_only_count_readable_realised_outcomes(backend):
    led = ledger(backend)
    cards = [acard(f"c{i}", entity=f"e{i}") for i in range(6)]
    p = plan("p1", cards)
    led.save_plan(A, P, p)
    led.record_emit(A, P, p)
    for c in cards:
        led.mark(A, P, "p1", c.id, "applied", today=T0)
    for c, outcome in zip(cards, ["On Track", "Review", "Off Track", "Unreadable", "No signal", "Too early"]):
        led.store_pair(A, P, "p1", c.id, 7, outcome=outcome)
    rate, n = led.win_rates(A, P, today=T0)["growth"]
    assert n == 3 and rate == pytest.approx(1 / 3)


def test_counterfactual_set_and_calibration(backend):
    led = ledger(backend)
    cards = [acard("s", status="superseded"), acard("t", status="tension"), acard("v", status="vetoed"),
             acard("p"), acard("s2", status="superseded", entity=None),
             acard("a", entity="e9"), acard("n", status="vetoed", entity="e8")]
    p = plan("p1", cards)
    led.save_plan(A, P, p)
    led.record_emit(A, P, p)
    led.mark(A, P, "p1", "a", "applied", today=T0)
    cf = led.due_pairs(A, P, cf=True, today=T0 + timedelta(days=7))
    assert sorted(r["action_id"] for r in cf) == ["n", "s", "t", "v"]        # pending, applied and entity-less excluded
    assert all(r["counterfactual"] and r["d0"] == "2026-03-01" and r["horizon"] == 7 for r in cf)
    for aid, outcome in [("s", "cf-right"), ("t", "cf-wrong"), ("v", "cf-nosignal"), ("n", "cf-right")]:
        led.store_pair(A, P, "p1", aid, 7, outcome=outcome)
    assert led.cf_calibration(A, P, today=T0) == {"growth": (0.5, 4)}        # nosignal stays in the denominator
    strict = ActionLedger(backend, LedgerConfig(cf_count_nosignal=False), clock=lambda: datetime(2026, 3, 1))
    rate, n = strict.cf_calibration(A, P, today=T0)["growth"]
    assert n == 3 and rate == pytest.approx(2 / 3)
    assert led.win_rates(A, P, today=T0) == {}                    # counterfactual labels never train confidence


def test_unapply_moves_a_row_back_to_the_counterfactual_set(backend):
    led = ledger(backend)
    p = plan("p1", [acard("t", status="tension", tension_group="e1")])
    led.save_plan(A, P, p)
    led.record_emit(A, P, p)
    later = T0 + timedelta(days=10)
    assert len(led.due_pairs(A, P, cf=True, today=later)) == 1
    led.mark(A, P, "p1", "t", "applied", today=T0)
    assert led.due_pairs(A, P, cf=True, today=later) == [] and len(led.due_pairs(A, P, today=later)) == 1
    led.mark(A, P, "p1", "t", "pending")
    assert led.due_pairs(A, P, today=later) == [] and len(led.due_pairs(A, P, cf=True, today=later)) == 1


def test_refresh_emit_syncs_demotions_and_late_cards_but_not_paired_rows(backend):
    led = ledger(backend)
    a, b = acard("a"), acard("b", entity="e2")
    p = plan("p1", [a, b])
    led.save_plan(A, P, p)
    led.record_emit(A, P, p)
    led.mark(A, P, "p1", "b", "applied", today=T0)
    led.store_pair(A, P, "p1", "b", 7, outcome="On Track")
    a.status = "vetoed"                                           # demoted by the spine after the first emit
    b.status = "vetoed"
    late = acard("bleed-e3", entity="e3", family="efficiency-cut", type="bid", direction="down")
    p2 = plan("p1", [a, b, late])
    assert led.refresh_emit(A, P, p2) > 0
    rows = {(r["action_id"], r["horizon"]): r for r in led.ledger_rows(A, P, "p1")}
    assert rows[("a", 7)]["status_at_emit"] == "vetoed"
    assert rows[("bleed-e3", 30)]["family"] == "efficiency-cut"
    assert rows[("b", 7)]["status_at_emit"] == "pending"          # paired rows are immutable
    assert rows[("b", 14)]["status_at_emit"] == "vetoed"
    assert len(rows) == 9


def test_applied_actions_feed_the_spine_gates(backend):
    led = ledger(backend)
    led.save_plan(A, P, plan("p1", [acard("bd", type="bid", direction="down"), acard("bu", entity="e2"),
                                    acard("no", entity="e3")]))
    led.save_plan(A, P, plan("p2", [acard("x", entity="e4")], created="2026-03-02"))
    led.mark(A, P, "p1", "bd", "applied", today=T0)
    led.mark(A, P, "p1", "bu", "applied", today=T0 - timedelta(days=9))
    led.mark(A, P, "p2", "x", "applied", today=T0)
    applied = led.applied_actions(A, P)
    assert {(a.entity_id, a.kind, a.direction) for a in applied} == {
        ("e1", "bid", "down"), ("e2", "budget", "up"), ("e4", "budget", "up")}
    assert open_entities(applied, T0) == {"e1", "e4"}
    assert recent_bid_downs(applied, T0) == {"e1"}
    assert {a.entity_id for a in led.applied_actions(A, P, exclude_plan="p2")} == {"e1", "e2"}
    assert {a.entity_id for a in led.applied_actions(A, P, plans=1)} == {"e4"}


def test_ledger_accepts_plan_objects(backend):
    from cpm_lineage.actions.pipeline import Plan
    led = ledger(backend)
    p = Plan(id="obj", created="2026-03-01", generator="verdict", actions=[acard("a")], economics={}, days=30,
             holdout=[], telemetry={})
    led.save_plan(A, P, p)
    assert led.record_emit(A, P, p) == 3
    assert led.load(A, P)[0]["generator"] == "verdict"


# -------------------------------------------------------------- history

def hrow(day, entity="e1", kw="protein bar", geo="geo-north", product="sku-1", spend=100.0, sales=250.0, **kw_):
    return PerfRow(date=T0 + timedelta(days=day), entity_id=entity, spend=spend, impressions=1000.0, sales=sales,
                   targeting_unit=kw, geo=geo, product_id=product, **kw_)


def test_entity_history_formulas():
    from cpm_lineage.adapters.base import PlatformProfile
    profile = PlatformProfile(placement_targets=frozenset({"feed"}))
    rows = [hrow(0, indirect_sales=50.0, new_users=2.0), hrow(1, entity="e2", indirect_sales=50.0, new_users=3.0),
            hrow(1, kw="Protein Bar ", geo="geo-south", product="sku-2", indirect_sales=0.0, new_users=0.0),
            hrow(2, kw="feed"), hrow(2, kw="zero", spend=0.0, sales=0.0), hrow(2, geo="others", kw=None),
            hrow(-200)]
    shelf = [ShelfRow(date=T0, product_id="sku-1", available_count=8, check_count=10),
             ShelfRow(date=T0 - timedelta(days=60), product_id="sku-1", available_count=0, check_count=10)]
    h = build_entity_history(rows, account=A, platform=P, profile=profile, shelf_rows=shelf,
                             targeting_units=[TargetingUnit("protein bar", role="generic", relevance=0.8)])
    k = keyword_history(h, " PROTEIN BAR")
    assert k["spend"] == 300.0 and k["roas"] == 2.5 and k["n_days"] == 2 and k["n_entities"] == 2
    assert k["new_users"] == 5.0 and k["cost_per_new_user"] == 60.0
    assert k["halo"] == pytest.approx(100 / 850, abs=1e-4) and k["role"] == "generic" and k["relevance"] == 0.8
    assert k["last_seen"] == "2026-03-02"
    assert keyword_history(h, "feed") is None and keyword_history(h, "zero") is None
    assert keyword_history(h, "never seen") is None               # cold, not zero
    assert geo_history(h, "others") is None and geo_history(h, "geo-south")["spend"] == 100.0
    assert product_history(h, "sku-1")["availability"] == 0.8     # latest 30 days only
    assert "availability" not in product_history(h, "sku-2")
    assert h["window_days"] == 90 and h["anchor"] == "2026-03-03"


def test_entity_history_degrades_without_optional_fields(perf_rows, synthetic_dataset):
    h = build_entity_history(perf_rows, profile=synthetic_dataset.profile,
                             targeting_units=synthetic_dataset.targeting_units,
                             market_rows=synthetic_dataset.market, shelf_rows=synthetic_dataset.shelf)
    assert h["keywords"] and h["products"] and h["geos"]
    assert "others" not in h["geos"]
    bleed = keyword_history(h, "rivalb bar")
    assert bleed["roas"] < 1.0 and bleed["n_days"] == 90
    total = sum(r.spend for r in perf_rows if r.product_id)
    assert sum(p["spend"] for p in h["products"].values()) == pytest.approx(total, rel=1e-6)
    bare = build_entity_history([hrow(0)])
    assert "halo" not in bare["keywords"]["protein bar"] and "new_users" not in bare["keywords"]["protein bar"]
    assert build_entity_history([])["keywords"] == {}


def test_history_store_freshness(backend):
    now = datetime(2026, 3, 10, 12, 0, 0)
    store = HistoryStore(backend, clock=lambda: now)
    assert store.load(A, P) is None
    calls = []

    def rebuild():
        calls.append(1)
        return build_entity_history([hrow(0)], account=A, platform=P)
    doc = store.load(A, P, rebuild=rebuild)
    assert doc["built_at"] == "2026-03-10T12:00:00" and keyword_history(doc, "protein bar")
    store.load(A, P, now=now + timedelta(hours=24), rebuild=rebuild)
    assert len(calls) == 1                                        # 24 h old is still fresh
    stale_at = now + timedelta(hours=24, seconds=1)
    assert store.is_fresh(doc, stale_at) is False
    assert store.load(A, P, now=stale_at)["built_at"] == "2026-03-10T12:00:00"   # no rebuild: stale copy returned
    fresh = store.load(A, P, now=stale_at, rebuild=rebuild)
    assert len(calls) == 2 and fresh["built_at"] == "2026-03-11T12:00:01"


# ----------------------------------------------------------- change log

def ver(entity, day, hour=9, **kw):
    return ConfigVersion(entity_id=entity, synced_at=datetime(2026, 3, day, hour), name=f"name-{entity}", **kw)


def test_diff_versions_netting_grouping_and_platform_states():
    since = date(2026, 3, 5)
    versions = [
        ver("e1", 1, daily_budget=100.0, status="ACTIVE", bid=None),
        ver("e1", 3, daily_budget=120.0, status="ACTIVE"),            # before `since`: only seeds the baseline
        ver("e1", 5, 9, daily_budget=150.0, status="ACTIVE"),         # 120 -> 150 ...
        ver("e1", 5, 15, daily_budget=200.0, status="ACTIVE"),        # ... -> 200 nets to 120 -> 200
        ver("e1", 6, daily_budget=200.0, status="BUDGET_HELD"),       # platform-set: ignored
        ver("e1", 7, daily_budget=200.0, status="ACTIVE", bid=5.0),   # bid null -> 5: ignored
        ver("e2", 4, daily_budget=50.0, status="ACTIVE"),
        ver("e2", 6, 9, daily_budget=80.0, status="ACTIVE"),
        ver("e2", 6, 18, daily_budget=50.0, status="ACTIVE"),         # same-day revert: nothing
        ver("e2", 8, daily_budget=50.0, status="PAUSED"),             # operator pause: kept
        ver("e3", 4, daily_budget=10.0), ver("e3", 5, daily_budget=20.0), ver("e3", 6, daily_budget=10.0),
        ver("e3", 7, daily_budget=20.0), ver("e3", 8, daily_budget=10.0), ver("e3", 9, daily_budget=20.0),
    ]
    status_map = {"ACTIVE": "active", "BUDGET_HELD": "budget_held", "PAUSED": "operator_paused"}
    ev = diff_versions(versions, since, status_map)
    got = {(e["entity_id"], e["field"], e["frm"], e["to"]): e for e in ev}
    assert set(got) == {("e1", "daily_budget", 120.0, 200.0), ("e2", "status", "ACTIVE", "PAUSED"),
                        ("e3", "daily_budget", 10.0, 20.0), ("e3", "daily_budget", 20.0, 10.0)}
    rhythm = got[("e3", "daily_budget", 10.0, 20.0)]
    assert rhythm["n"] == 3 and rhythm["date"] == date(2026, 3, 9)    # grouped, latest date kept
    assert got[("e3", "daily_budget", 20.0, 10.0)]["n"] == 2
    assert got[("e1", "daily_budget", 120.0, 200.0)]["name"] == "name-e1"
    assert [e["date"] for e in ev] == sorted(e["date"] for e in ev)


def test_input_change_ledger_before_after_and_shortlist():
    d = date(2026, 3, 10)

    def rows(entity, before, after, roas=2.0):
        out = []
        for i in range(-7, 9):
            sp = before if i < 0 else after
            out.append(PerfRow(date=d + timedelta(days=i), entity_id=entity, spend=sp, impressions=1.0,
                               sales=sp * roas))
        return out
    events = [
        {"entity_id": "big", "field": "daily_budget", "frm": 100.0, "to": 200.0, "date": d, "n": 1},
        {"entity_id": "big", "field": "bid", "frm": 5.0, "to": 6.0, "date": d, "n": 1},
        {"entity_id": "tiny", "field": "daily_budget", "frm": 100.0, "to": 105.0, "date": d, "n": 1},
        {"entity_id": "stat", "field": "status", "frm": "ACTIVE", "to": "PAUSED", "date": d, "n": 1},
        {"entity_id": "mid", "field": "daily_budget", "frm": 100.0, "to": 110.0, "date": d, "n": 1},
        {"entity_id": "small", "field": "daily_budget", "frm": 100.0, "to": 150.0, "date": d, "n": 1},
    ]
    perf = (rows("big", 100.0, 200.0) + rows("tiny", 100.0, 190.0) + rows("stat", 80.0, 0.0)
            + rows("mid", 100.0, 150.0) + rows("small", 100.0, 110.0))
    out = input_change_ledger(events, perf)
    first = out["events"][0]
    assert first["entity_id"] == "big" and first["spend_before"] == 100.0 and first["spend_after"] == 200.0
    assert first["moved"] == pytest.approx(700.0) and first["roas_before"] == pytest.approx(2.0)
    assert [e["moved"] for e in out["events"]] == sorted((e["moved"] for e in out["events"]), reverse=True)
    # one per entity; the 5% budget nudge is not material even though spend moved; 10% exactly is
    assert [e["entity_id"] for e in out["top"]] == ["big", "stat", "mid"]
    assert "not causal" in out["method"]
    lonely = input_change_ledger([{"entity_id": "ghost", "field": "bid", "frm": 1.0, "to": 2.0, "date": d}], [])
    assert lonely["events"][0]["moved"] == 0.0 and lonely["events"][0]["spend_before"] is None


def test_change_log_store(backend):
    log = ChangeLog(backend)
    ev = [ChangeEvent(datetime(2026, 3, 5, 10), "e1", "daily_budget", 100.0, 150.0, source="console", verified=True),
          ChangeEvent(datetime(2026, 3, 6, 10), "e1", "bid", 5.0, 6.0, automated=True, targeting_unit="protein bar"),
          ChangeEvent(datetime(2026, 3, 7, 10), "e2", "status", "ACTIVE", "PAUSED")]
    assert log.append(A, P, ev) == 3 and log.append(A, P, ev) == 0
    assert log.events(A, P) == ev
    assert log.events(A, P, entity_id="e1", start=date(2026, 3, 6), end=date(2026, 3, 7)) == [ev[1]]
    assert log.events(A, P, entity_id="e") == []
    vs = [ver("e1", 1, daily_budget=100.0), ver("e1", 6, daily_budget=150.0)]
    assert log.record_versions(A, P, vs) == 2 and log.record_versions(A, P, vs) == 0
    assert log.versions(A, P) == vs
    changes = log.changes_since(A, P, date(2026, 3, 2))
    assert [(c["field"], c["frm"], c["to"]) for c in changes] == [("daily_budget", 100.0, 150.0)]


# ------------------------------------------------------ recommendation log

def test_action_log_dedupes_and_skips(backend):
    log = ActionLog(backend)
    e = LoggedAction(date=T0, account=A, platform=P, entity_id="e1", roas_at=2.0, budget_daily_at=500.0,
                     expected=tuple(ExpectedChange("budget", "up", verb=f"raise {i}") for i in range(5)))
    assert log.log(e) is True and log.log(e) is False             # one per entity per day
    assert log.log(LoggedAction(date=T0, account=A, platform=P, entity_id="*")) is False
    assert log.log(LoggedAction(date=T0, account=A, platform=P, entity_id=None)) is False
    assert log.log(LoggedAction(date=T0, account=A, platform=P, entity_id="e2", status="degraded")) is False
    later = LoggedAction(date=T0 + timedelta(days=1), account=A, platform=P, entity_id="e1",
                         expected=(ExpectedChange("keyword", "down", targets=("a", "b")),))
    assert log.log(later) is True
    got = log.entries(A, P)
    assert [x.date for x in got] == [T0 + timedelta(days=1), T0]
    assert got[0] == later and len(got[1].expected) == 3 and got[1].budget_daily_at == 500.0
    assert log.entries(A, "other-platform") == []


# ------------------------------------------------------------- operator-state regressions

def test_resaving_a_regenerated_plan_keeps_operator_decisions(backend):
    led = ledger(backend)
    led.save_plan(A, P, plan("p1", [acard("a"), acard("b", entity="e2"), acard("c", entity="e3")]))
    assert led.mark(A, P, "p1", "a", "applied", today=T0)
    assert led.mark(A, P, "p1", "b", "skipped", reason="not now")
    # the same plan generated again: fresh cards, no operator columns, one of them now vetoed
    led.save_plan(A, P, plan("p1", [acard("a", status="vetoed"), acard("b", entity="e2"), acard("c", entity="e3")]))
    got = {c["id"]: c for c in led.load(A, P)[0]["actions"]}
    assert got["a"]["status"] == "applied" and got["a"]["applied_at"] == T0.isoformat()
    assert got["b"]["status"] == "skipped" and got["b"]["skip_reason"] == "not now"
    assert got["c"]["status"] == "pending"
    assert [x.card_id for x in led.applied_actions(A, P)] == ["a"]
    # a loaded copy round-trips and can still be un-applied
    loaded = led.load(A, P)[0]
    led.save_plan(A, P, loaded)
    assert {c["id"]: c["status"] for c in led.load(A, P)[0]["actions"]}["a"] == "applied"
    assert led.mark(A, P, "p1", "b", "pending") and led.applied_actions(A, P)[0].card_id == "a"


def test_unappliable_cards_cannot_be_laundered_through_pending_or_skipped(backend):
    led = ledger(backend)
    cards = [acard("sup", status="superseded"), acard("veto", entity="e2", status="vetoed"),
             acard("ten", entity="e3", status="tension")]
    led.save_plan(A, P, plan("p1", cards))
    for cid in ("sup", "veto"):
        assert led.mark(A, P, "p1", cid, "pending") is False
        assert led.mark(A, P, "p1", cid, "skipped") is False
        assert led.mark(A, P, "p1", cid, "applied", today=T0) is False
    stored = {c["id"]: c["status"] for c in led.load(A, P)[0]["actions"]}
    assert stored["sup"] == "superseded" and stored["veto"] == "vetoed"
    # un-applying restores the status the pipeline gave the card, not a blanket "pending"
    assert led.mark(A, P, "p1", "ten", "applied", today=T0) and led.mark(A, P, "p1", "ten", "pending")
    assert {c["id"]: c["status"] for c in led.load(A, P)[0]["actions"]}["ten"] == "tension"
