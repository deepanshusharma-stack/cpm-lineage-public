"""Verdict spine: lanes, walk order, shaping, detectors, probes, contested days."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cpm_lineage.config import Economics
from cpm_lineage.conflicts import (
    ConflictConfig, Override, grain_rank, is_cut, resolve_action_conflict, resolve_data_conflict,
)
from cpm_lineage.schema import ActionCard, AppliedAction, BudgetTelemetry, Change, KeywordRef, Window
from cpm_lineage.verdict import (
    STAGE_ORDER, STATES, SpineContext, VerdictConfig, apply_spine, contested_days, contested_ratio,
    demote, detect_families, graduate, open_entities, owned_entities, partition_collisions, probes,
    recent_bid_downs, reversibility_class, risk_bar, run_spine, verdict_states,
)


def tel(eid, n_days=30, spend=20_000.0, impressions=50_000.0, budget=1000.0, roas=2.0, roas_recent=2.0,
        ad_type="keyword_listing"):
    return BudgetTelemetry(entity_id=eid, n_days=n_days, spend=spend, impressions=impressions,
                           avg_budget=budget, roas=roas, roas_recent=roas_recent, ad_type=ad_type)


def card(cid="c1", entity="e1", family="efficiency-scale", type="budget", direction="up", confidence=80,
         status="pending", segment="seg", **kw):
    c = ActionCard(id=cid, family=family, type=type, title=kw.pop("title", "Move"), segment=segment,
                   entity_id=entity, direction=direction, trigger="because", targets="do it",
                   evidence={"roas": 2.0}, status=status, confidence=confidence)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def ctx(**kw):
    kw.setdefault("telemetry", {"e1": tel("e1")})
    return SpineContext(**kw)


# ---------------------------------------------------------------- lanes

def test_clean_card_acts_and_every_card_is_typed():
    cards = [card(), card("c2", status="superseded"), card("c3", status="vetoed", veto_reason="upstream"),
             card("c4", status="applied"), card("c5", status="skipped")]
    apply_spine(cards, ctx())
    lanes = [c.verdict["lane"] for c in cards]
    assert lanes == ["ACT", "NO-ACTION(conflict)", "NO-ACTION(gate)", "NO-ACTION(applied)", "NO-ACTION(skipped)"]
    assert cards[0].status == "pending" and cards[0].verdict["why"] == "because"
    assert cards[2].verdict["why"] == "upstream"
    assert all(c.verdict["state"] in STATES for c in cards)
    assert verdict_states(cards) == {"ACT": 1, "NO-ACTION": 4}


def test_demote_first_claim_wins():
    c = card()
    assert demote(c, "HOLD(price-event)", "x") is True
    assert c.status == "vetoed" and c.veto_reason == "HOLD(price-event): x"
    assert c.verdict == {"state": "HOLD", "why": "x", "lane": "HOLD(price-event)"}
    assert demote(c, "OBSERVE(insufficient)", "y") is False
    assert c.verdict["lane"] == "HOLD(price-event)"


def test_quarantine_names_a_contaminated_read_and_threshold():
    def book(n):
        t = {f"p{i}": tel(f"p{i}", n_days=1, spend=10.0, impressions=5.0) for i in range(n)}
        return t
    c = card(entity="p0")
    out = apply_spine([c], ctx(telemetry=book(8)))
    assert c.verdict["lane"] == "OBSERVE(quarantine)" and c.status == "vetoed"
    assert out["quarantine"] == 1 and out["insufficient"] == 0
    c = card(entity="p0")
    apply_spine([c], ctx(telemetry=book(7)))
    assert c.verdict["lane"] == "OBSERVE(insufficient)"          # 7 is one short of the cohort size
    c = card(entity="p0")
    apply_spine([c], ctx(telemetry=book(8), days=13))
    assert c.verdict["lane"] == "OBSERVE(insufficient)"          # short windows never quarantine


def test_quarantine_buckets_by_ad_type():
    t = {f"p{i}": tel(f"p{i}", n_days=1, spend=10.0, ad_type="a" if i < 5 else "b") for i in range(10)}
    c = card(entity="p0")
    apply_spine([c], ctx(telemetry=t))
    assert c.verdict["lane"] != "OBSERVE(quarantine)"


def test_walk_order_is_load_bearing():
    t = {f"p{i}": tel(f"p{i}", n_days=1, spend=10.0, impressions=5.0) for i in range(8)}
    a, b = card(entity="p0"), card(entity="p0")
    apply_spine([a], ctx(telemetry=t))
    names = dict(STAGE_ORDER)
    swapped = [("evidence", names["evidence"]), ("integrity", names["integrity"])] + [
        s for s in STAGE_ORDER if s[0] not in ("evidence", "integrity")]
    apply_spine([b], ctx(telemetry=t), stages=swapped)
    assert a.verdict["lane"] == "OBSERVE(quarantine)"
    assert b.verdict["lane"] == "OBSERVE(insufficient)"
    assert [n for n, _ in STAGE_ORDER] == ["integrity", "lifecycle", "evidence", "identity", "action_lock",
                                           "automation", "risk_bars", "terminals"]


def test_placeholder_budget_only_distrusts_budget_cards():
    t = {"e1": tel("e1", budget=99.0)}
    bud, bid = card(), card("c2", type="bid", direction="up")
    out = apply_spine([bud, bid], ctx(telemetry=t))
    assert bud.verdict["lane"] == "OBSERVE(bad-budget-field)" and bid.verdict["lane"] == "ACT"
    assert out["bud_distrust"] == 1
    ok = card()
    apply_spine([ok], ctx(telemetry={"e1": tel("e1", budget=100.0)}))
    assert ok.verdict["lane"] == "ACT"
    # the threshold is money: it scales with the currency unit
    scaled = card()
    apply_spine([scaled], ctx(telemetry={"e1": tel("e1", budget=100.0)}, economics=Economics(currency_unit=2.0)))
    assert scaled.verdict["lane"] == "OBSERVE(bad-budget-field)"


def test_launch_grace_holds_cuts_only_and_expires():
    def run(nd, direction="down"):
        c = card(family="efficiency-cut", direction=direction, confidence=90)
        apply_spine([c], ctx(telemetry={"e1": tel("e1", n_days=nd)}, book_max_live_days=60))
        return c
    held = run(3)
    assert held.verdict["lane"] == "HOLD(launch-grace)" and "EXPIRES" in held.verdict["why"]
    assert run(4).verdict["lane"] == "HOLD(launch-grace)"
    assert run(5).verdict["lane"] == "ACT"
    assert run(6).verdict["lane"] == "ACT"
    assert run(3, "up").verdict["lane"] == "ACT"


def test_evidence_floor_binds_and_spares_experiments_watches_and_portfolio_cards():
    t = {"e1": tel("e1", impressions=999.0)}
    thin = card()
    exp = card("c2", family="experiment", type="experiment", direction="none")
    watch = card("c3", type="watch", direction="none")
    portfolio = card("c4", entity=None)
    out = apply_spine([thin, exp, watch, portfolio], ctx(telemetry=t))
    assert thin.verdict["lane"] == "OBSERVE(insufficient)" and out["insufficient"] == 1
    assert exp.verdict["lane"] == "ACT" and watch.verdict["lane"] == "ACT" and portfolio.verdict["lane"] == "ACT"
    at = card()
    apply_spine([at], ctx(telemetry={"e1": tel("e1", impressions=1000.0)}))
    assert at.verdict["lane"] == "ACT"


@pytest.mark.parametrize("cur,pri,held", [(20.0, 12.0, True), (12.0, 20.0, True), (19.9, 12.0, False),
                                          (None, 12.0, False)])
def test_price_event_threshold(cur, pri, held):
    c = card()
    context = ctx(price_index={"seg": (cur, pri)})
    apply_spine([c], context)
    assert (c.verdict["lane"] == "HOLD(price-event)") is held
    assert ("seg" in context.price_events) is held


def test_price_event_spares_defense_and_other_segments_fall_through_to_lock():
    media, defense, other, exp = (card(), card("c2", family="defense"), card("c3", segment="quiet", entity="e2"),
                                  card("c4", type="experiment", family="experiment", direction="none"))
    t = {"e1": tel("e1"), "e2": tel("e2")}
    apply_spine([media, defense, other, exp],
                ctx(telemetry=t, price_index={"seg": (30.0, 10.0)}, open_entities=frozenset({"e2"})))
    assert media.verdict["lane"] == "HOLD(price-event)"
    assert defense.verdict["lane"] == "ACT"
    assert other.verdict["lane"] == "HOLD(monitoring-window)"
    assert exp.verdict["lane"] == "ACT"


def test_automation_is_matched_by_lever():
    def run(automation):
        cs = [card("bud"), card("bid", type="bid", direction="down", confidence=90),
              card("kw", type="keyword", direction="down", confidence=90, keywords=[KeywordRef("k")]),
              card("w", type="watch", direction="none"), card("free", entity="e2")]
        out = apply_spine(cs, ctx(telemetry={"e1": tel("e1"), "e2": tel("e2")}, automation=automation))
        return {c.id: c.verdict["lane"] for c in cs}, cs, out
    lanes, cs, out = run({"e1": {"keyword": "position bidder"}})
    assert lanes == {"bud": "ACT", "bid": "ESCALATE(automation-owned)", "kw": "ESCALATE(automation-owned)",
                     "w": "ACT", "free": "ACT"}
    assert "position bidder" in cs[1].verdict["why"] and out["automation_conflict"] == 2
    lanes, _, _ = run({"e1": {"budget": "day-parting"}})
    assert lanes["bud"] == "ESCALATE(automation-owned)" and lanes["bid"] == "ACT" and lanes["kw"] == "ACT"
    lanes, _, out = run({})
    assert set(lanes.values()) == {"ACT"} and out["automation_conflict"] == 0


def test_risk_bars_by_reversibility():
    cfg = VerdictConfig()
    assert risk_bar(card(type="watch", direction="none"), cfg) == 25
    assert risk_bar(card(type="bid"), cfg) == 45
    assert risk_bar(card(), cfg) == 55
    assert risk_bar(card(direction="down"), cfg) == 65
    assert risk_bar(card(type="keyword", direction="down"), cfg) == 65
    assert risk_bar(card(title="Freeze Budget", direction="down"), cfg) == 30
    assert risk_bar(card(type="geo", direction="none"), cfg) == 50
    explicit = card(direction="down")
    explicit.extras["reversibility"] = "restore"
    assert reversibility_class(explicit) == "restore" and risk_bar(explicit) == 30

    def lane(confidence, **kw):
        c = card(confidence=confidence, **kw)
        apply_spine([c], ctx(book_max_live_days=60))
        return c.verdict["lane"]
    assert lane(28, type="watch", direction="none") == "ACT"
    assert lane(24, type="watch", direction="none") == "OBSERVE(below-bar)"
    assert lane(60, direction="down") == "OBSERVE(below-bar)"
    assert lane(65, direction="down") == "ACT"
    assert lane(60) == "ACT" and lane(54) == "OBSERVE(below-bar)"


def test_cold_start_reads_the_book_not_the_selection():
    young = {"e1": tel("e1", n_days=10)}
    c = card(confidence=60)
    context = ctx(telemetry=young, book_max_live_days=20)
    apply_spine([c], context)
    assert c.verdict["lane"] == "OBSERVE(below-bar)" and context.cold_start and "cold-start" in c.verdict["why"]
    c = card(confidence=60)                      # old book, young selection: no bump
    context = ctx(telemetry=young, book_max_live_days=40)
    apply_spine([c], context)
    assert c.verdict["lane"] == "ACT" and not context.cold_start
    c = card(confidence=60)                      # no book signal: falls back to the selection and still binds
    apply_spine([c], ctx(telemetry=young))
    assert c.verdict["lane"] == "OBSERVE(below-bar)"
    c = card(confidence=65)
    apply_spine([c], ctx(telemetry=young, book_max_live_days=20))
    assert c.verdict["lane"] == "ACT"
    full = SpineContext(telemetry={"e1": tel("e1", n_days=10), "old": tel("old", n_days=50)})
    scoped = full.scoped(["e1"])
    assert set(scoped.telemetry) == {"e1"} and scoped.book_max_live_days == 50
    assert set(full.scoped([]).telemetry) == {"e1", "old"}


def test_unfillable_card_degrades():
    c = card()
    c.evidence = {}
    out = apply_spine([c], ctx())
    assert c.verdict["lane"] == "OBSERVE(unfillable)" and out["unfillable"] == 1
    c = card(confidence=None)
    c.extras["reversibility"] = "watch"
    apply_spine([c], ctx())
    assert c.verdict["state"] == "OBSERVE"


def test_availability_gates_nothing():
    assert not any("avail" in name for name, _ in STAGE_ORDER)
    assert not any("avail" in f for f in SpineContext.__dataclass_fields__)


# ------------------------------------------------------------ graduate

def kw_cut(cid="k1", entity="e1"):
    return card(cid, entity=entity, family="efficiency-cut", type="keyword", direction="down", confidence=90,
                keywords=[KeywordRef("kw a")])


def test_keyword_cut_becomes_bid_probe_first():
    c = kw_cut()
    out = graduate([c], ctx())
    assert out["bid_ladder"] == 1
    assert c.type == "bid" and c.change.pct == -25 and c.ladder == "bid-down-first"
    assert "do it" in c.targets and c.status == "pending"
    # a prior applied bid-down means the original cut stands
    c = kw_cut()
    out = graduate([c], ctx(bid_down_entities=frozenset({"e1"})))
    assert out["bid_ladder"] == 0 and c.type == "keyword" and c.ladder is None


def test_one_probe_per_entity_and_budget_cuts_never_retyped():
    a, b = kw_cut("k1"), kw_cut("k2")
    bud = card("b", family="efficiency-cut", direction="down")
    out = graduate([a, b, bud], ctx())
    assert out["bid_ladder"] == 1 and a.type == "bid"
    assert b.status == "superseded" and "bid-down first" in b.veto_reason
    assert bud.type == "budget"
    existing = card("x", family="bid", type="bid", direction="down")
    c = kw_cut()
    graduate([existing, c], ctx())
    assert c.status == "superseded"


def test_staircase_caps_a_raise():
    c = card(change=Change(pct=50.0, frm=10_000.0, to=15_000.0))
    out = graduate([c], ctx())
    assert out["staircase"] == 1
    assert c.change.pct == 25 and c.change.pct_requested == 50 and c.change.to == 12_500
    ok = card(change=Change(pct=25.0, frm=100.0, to=125.0))
    assert graduate([ok], ctx())["staircase"] == 0 and ok.change.pct_requested is None


def test_bleeder_sweep_threshold_and_full_ids():
    def book(bleed_spend):
        return {"good": tel("good", spend=100_000.0, roas=3.0),
                "cmp-11": tel("cmp-11", spend=bleed_spend, roas=0.5),
                "cmp-111": tel("cmp-111", spend=bleed_spend, roas=0.5)}
    cards: list[ActionCard] = []
    out = graduate(cards, ctx(telemetry=book(5_000.0)))          # loss 2500 each of 110k book = 2.27%
    assert out["bleeder"] == 2
    assert {c.id for c in cards} == {"bleed-cmp-11", "bleed-cmp-111"}
    c = cards[0]
    assert (c.type, c.direction, c.family, c.confidence) == ("bid", "down", "efficiency-cut", 55)
    assert c.stake == 2500 and c.evidence["cum_loss"] == pytest.approx(2500.0)
    assert c.conf_parts == [("cumulative loss vs book", 15.0)]
    quiet: list[ActionCard] = []
    assert graduate(quiet, ctx(telemetry=book(3_000.0)))["bleeder"] == 0 and not quiet  # 1500/106k = 1.4%
    # exactly at 2% of book it fires: loss 2000 of a 100000 book
    exact: list[ActionCard] = []
    assert graduate(exact, ctx(telemetry={"good": tel("good", spend=96_000.0, roas=3.0),
                                           "a": tel("a", spend=4_000.0, roas=0.5)}))["bleeder"] == 1
    # an entity that already has a card is never swept
    covered = [card(entity="cmp-11", status="vetoed")]
    graduate(covered, ctx(telemetry=book(5_000.0)))
    assert {c.id for c in covered[1:]} == {"bleed-cmp-111"}


def test_bleeder_respects_breakeven():
    t = {"good": tel("good", spend=100_000.0, roas=3.0), "x": tel("x", spend=20_000.0, roas=1.5)}
    assert graduate([], ctx(telemetry=t))["bleeder"] == 0
    cards: list[ActionCard] = []
    graduate(cards, ctx(telemetry=t, economics=Economics(breakeven=2.0)))
    assert [c.id for c in cards] == ["bleed-x"] and cards[0].stake == 5000   # 20000 * (1 - 1.5/2)


def test_unowned_freeze_only_for_unowned_ramps():
    t = {"owned": tel("owned", n_days=20, spend=20_000.0, roas=0.9),
         "drift": tel("drift", n_days=20, spend=20_000.0, roas=0.9),
         "big": tel("big", spend=5_000_000.0, roas=3.0)}
    recent = {"owned": 14_000.0, "drift": 14_000.0}             # 2000/day vs 1000/day window average
    cards: list[ActionCard] = []
    out = graduate(cards, ctx(telemetry=t, spend_recent=recent, owned_entities=frozenset({"owned"})))
    assert out["unowned_freeze"] == 1 and [c.id for c in cards] == ["freeze-drift"]
    c = cards[0]
    assert c.change.pct == 0 and c.change.frm == c.change.to == 1000.0
    assert c.stake == 7000 and c.confidence == 60 and risk_bar(c) == 30
    assert graduate([], ctx(telemetry=t))["unowned_freeze"] == 0             # no recent-spend read: inert
    assert graduate([], ctx(telemetry=t, spend_recent={"drift": 10_500.0}))["unowned_freeze"] == 0  # 1.5x exactly


def test_sweeps_never_leave_the_selection():
    full = SpineContext(telemetry={"sel": tel("sel", spend=100_000.0, roas=3.0),
                                   "out": tel("out", spend=50_000.0, roas=0.2)})
    cards: list[ActionCard] = []
    graduate(cards, full.scoped(["sel"]))
    assert not cards
    graduate(cards, full)
    assert [c.entity_id for c in cards] == ["out"]


# ----------------------------------------------------------- detectors

def test_winner_went_dark_only_for_the_dark_winner():
    t = {"dark": tel("dark", n_days=10, spend=20_000.0, roas=4.0, roas_recent=None),
         "serving": tel("serving", n_days=10, spend=20_000.0, roas=4.0, roas_recent=4.0),
         "loser": tel("loser", n_days=10, spend=20_000.0, roas=1.0, roas_recent=None),
         "small": tel("small", n_days=10, spend=9_999.0, roas=4.0, roas_recent=None)}
    cards: list[ActionCard] = []
    out = detect_families(cards, ctx(telemetry=t))
    assert out == {"winner_dark": 1, "justify_or_kill": 0}
    c = cards[0]
    assert c.id == "dark-dark" and c.type == "watch" and c.confidence == 60
    assert c.stake == 20_000.0 * 3 / 10 * 7
    capped: list[ActionCard] = []
    detect_families(capped, ctx(telemetry={"d": tel("d", n_days=10, spend=20_000.0, roas=9.0, roas_recent=None)}))
    assert capped[0].stake == 20_000.0 * 5 / 10 * 7              # ROAS capped at 6


def test_justify_or_kill_only_for_untracked_ad_types():
    t = {"reach": tel("reach", spend=60_000.0, roas=0.0, ad_type="featured_listing"),
         "small": tel("small", spend=49_999.0, roas=0.0, ad_type="featured_listing"),
         "kw": tel("kw", spend=60_000.0, ad_type="keyword_listing")}
    cards: list[ActionCard] = []
    out = detect_families(cards, ctx(telemetry=t, untracked_ad_types=frozenset({"featured_listing"})))
    assert out["justify_or_kill"] == 1 and cards[0].id == "untracked-reach"
    assert "do NOT judge it on ROAS" in cards[0].targets and cards[0].stake == 60_000
    assert detect_families([], ctx(telemetry=t))["justify_or_kill"] == 0     # no capability data: inert
    have = [card(entity="reach")]
    detect_families(have, ctx(telemetry=t, untracked_ad_types=frozenset({"featured_listing"})))
    assert len(have) == 1


def test_probes_cap_at_two_richest_first_and_clear_the_spine():
    t = {f"p{i}": tel(f"p{i}", n_days=10, spend=100.0 * (i + 1), impressions=500.0) for i in range(4)}
    t["young"] = tel("young", n_days=6, spend=9_999.0, impressions=500.0)
    t["read"] = tel("read", n_days=40, spend=9_999.0, impressions=5_000.0)
    cards: list[ActionCard] = []
    context = ctx(telemetry=t)
    assert probes(cards, context) == {"probes": 2}
    assert [c.id for c in cards] == ["probe-p3", "probe-p2"]
    assert cards[0].stake == 1000 and cards[0].confidence == 40
    apply_spine(cards, context)
    assert [c.verdict["lane"] for c in cards] == ["ACT", "ACT"]   # not held by the evidence floor or the bar


# ------------------------------------------------------- contested days

def test_contested_ratio():
    d = [date(2026, 3, 1) + timedelta(days=i) for i in range(10)]
    assert contested_ratio({"a": d[:5], "b": d[5:]}) == 0.0
    assert contested_ratio({"a": d[:5], "b": d[:5]}) == 1.0
    one_of_five = contested_ratio({"a": d[:3], "b": d[2:5]})
    assert one_of_five == pytest.approx(0.2) and 0 < one_of_five < 0.3
    assert contested_ratio({"a": d}) == 0.0
    assert contested_ratio({"a": d, "b": []}) == 0.0
    assert contested_ratio({}) == 0.0
    assert contested_ratio({"a": d[:2], "b": d[1:3], "c": d[5:6]}) == pytest.approx(1 / 4)


def collision(ids):
    c = card("col", entity=None, family="collision", type="keyword", direction="down",
             keywords=[KeywordRef("energy bar")])
    c.evidence = {"entity_ids": ids, "geo": "geo-north"}
    return c


def test_partition_threshold():
    d = [date(2026, 3, 1) + timedelta(days=i) for i in range(10)]
    c = collision(["a", "b"])
    assert partition_collisions([c], lambda _c: {"a": d[:3], "b": d[2:5]}) == 1
    assert c.verdict["lane"] == "NO-ACTION(partitioned)" and c.status == "vetoed"
    assert c.evidence["contested_ratio"] == 0.2
    c = collision(["a", "b"])
    assert partition_collisions([c], lambda _c: {"a": d[:10], "b": d[:3]}) == 0      # 0.3 exactly stays
    assert c.verdict is None and c.evidence["contested_ratio"] == 0.3
    c = collision(["a", "b"])
    assert partition_collisions([c], lambda _c: None) == 0 and "contested_ratio" not in c.evidence

    def boom(_c):
        raise RuntimeError("feed down")
    assert partition_collisions([collision(["a", "b"])], boom) == 0                  # fails open
    assert partition_collisions([collision(["a"])], lambda _c: {"a": d}) == 0


def test_synthetic_own_overlap_is_a_real_collision(perf_rows, synthetic_events, windows_30):
    ev = synthetic_events["own_overlap"]
    days = contested_days(perf_rows, ev.entity_ids, ev.targeting_unit, window=windows_30.current)
    assert all(len(v) == 30 for v in days.values())
    assert contested_ratio(days) == 1.0
    c = collision(list(ev.entity_ids))
    assert partition_collisions([c], lambda _c: days) == 0 and c.status == "pending"
    one_geo = contested_days(perf_rows, ev.entity_ids, ev.targeting_unit, geo="geo-north",
                             window=Window(windows_30.current.start, windows_30.current.start))
    assert all(len(v) == 1 for v in one_geo.values())


# -------------------------------------------------------- orchestration

def test_run_spine_on_synthetic_book(synthetic_dataset, perf_rows, windows_30):
    from cpm_lineage.metrics import group_cells
    from cpm_lineage.windows import filter_rows
    cur = filter_rows(perf_rows, windows_30.current)
    cells = group_cells(cur, "entity_id")
    ents = {e.entity_id: e for e in synthetic_dataset.entities}
    telemetry = {eid: BudgetTelemetry(entity_id=eid, n_days=len({r.date for r in cur if r.entity_id == eid}),
                                      spend=c.spend, impressions=c.impressions, roas=c.roas, roas_recent=c.roas,
                                      avg_budget=ents[eid].daily_budget, ad_type=ents[eid].ad_type)
                 for eid, c in cells.items()}
    context = SpineContext(days=30, telemetry=telemetry, book_max_live_days=90,
                           untracked_ad_types=synthetic_dataset.profile.no_attribution_types())
    res = run_spine([], context)
    assert all(c.verdict and c.verdict["state"] in STATES for c in res.cards)
    assert sum(res.verdict_states.values()) == len(res.cards)
    assert {c.entity_id for c in res.cards} <= set(telemetry)
    assert set(res.telemetry) >= {"quarantine", "below_bar", "bleeder", "winner_dark", "probes", "partitioned"}
    assert isinstance(res.zero_fire_gates(), list)
    again = run_spine([], SpineContext(days=30, telemetry=telemetry, book_max_live_days=90,
                                       untracked_ad_types=synthetic_dataset.profile.no_attribution_types()))
    assert [(c.id, c.verdict["lane"]) for c in again.cards] == [(c.id, c.verdict["lane"]) for c in res.cards]


def test_run_spine_without_minting_only_gates():
    t = {"e1": tel("e1"), "bleed": tel("bleed", spend=90_000.0, roas=0.1)}
    res = run_spine([card()], ctx(telemetry=t), mint=False)
    assert len(res.cards) == 1 and res.verdict_states == {"ACT": 1}
    res = run_spine([card()], ctx(telemetry=t))
    assert {c.id for c in res.cards} == {"c1", "bleed-bleed"}


def test_history_helpers():
    today = date(2026, 3, 31)
    applied = [AppliedAction("bid", "a", today - timedelta(days=7), direction="down"),
               AppliedAction("budget", "b", today - timedelta(days=8), direction="up"),
               AppliedAction("biddown", "c", today - timedelta(days=14)),
               AppliedAction("bid", "d", today - timedelta(days=15), direction="down"),
               AppliedAction("bid", "e", today, direction="up"),
               AppliedAction("watch", None, today)]
    assert open_entities(applied, today) == {"a", "e"}
    assert recent_bid_downs(applied, today) == {"a", "c"}
    assert owned_entities(applied) == {"a", "b", "c", "d", "e"}


# ------------------------------------------------------------ conflicts

def test_most_granular_signal_wins():
    r = resolve_action_conflict([{"grain": "entity", "verb": "scale"}, {"grain": "keyword", "verb": "pause"}])
    assert r["winner"]["verb"] == "pause" and len(r["losers"]) == 1
    assert r["citation"].startswith("[CONFLICT: keyword->pause vs entity->scale;") and "action=pause]" in r["citation"]


def test_conflict_edge_cases():
    assert resolve_action_conflict([]) is None
    assert resolve_action_conflict([{"grain": "geo", "verb": None}]) is None
    one = resolve_action_conflict([{"grain": "geo", "verb": "hold"}, {"grain": "keyword"}])
    assert one["citation"] is None and one["losers"] == []
    tie = resolve_action_conflict([{"grain": "geo", "verb": "first"}, {"grain": "city", "verb": "second"}])
    assert tie["winner"]["verb"] == "first"                       # alias folds, stable on ties
    unk = resolve_action_conflict([{"grain": "mystery", "verb": "x"}, {"grain": "portfolio", "verb": "y"}])
    assert unk["winner"]["verb"] == "y" and grain_rank("mystery") == -1
    assert grain_rank("campaign") == grain_rank("entity") == 4 and grain_rank("keyword") == 5


def test_universal_overrides():
    r = resolve_action_conflict([{"grain": "keyword", "verb": "scale"},
                                 {"grain": "portfolio", "verb": "fix supply", "override": "availability"}])
    assert r["winner"]["override"] == "availability" and "supply" in r["citation"]
    r = resolve_action_conflict([{"grain": "keyword", "verb": "pause keyword"},
                                 {"grain": "segment", "verb": "keep", "override": "acquisition"}])
    assert r["winner"]["verb"] == "keep"
    r = resolve_action_conflict([{"grain": "keyword", "verb": "scale"},
                                 {"grain": "segment", "verb": "keep", "override": "acquisition"}])
    assert r["winner"]["verb"] == "scale"                         # nothing is being cut: plain granularity
    cfg = ConflictConfig(overrides=(Override("legal"),))
    r = resolve_action_conflict([{"grain": "keyword", "verb": "scale"},
                                 {"grain": "portfolio", "verb": "stop", "override": "legal"}], cfg)
    assert r["winner"]["verb"] == "stop" and "legal override" in r["citation"]
    assert is_cut("Tighten budget") and is_cut("add NEGATIVE") and not is_cut("scale")


def test_source_authority():
    rows = [{"source": "warehouse", "field": "daily_budget", "value": 500},
            {"source": "operational", "field": "daily_budget", "value": 800},
            {"source": "analytics", "field": "daily_budget", "value": None},
            {"source": "elsewhere", "field": "daily_budget", "value": 800}]
    r = resolve_data_conflict(rows)
    assert r["winner"]["source"] == "operational" and [d["source"] for d in r["disagreements"]] == ["warehouse"]
    assert "operational=800 beats warehouse=500 (lags); verify before acting" in r["citation"]
    agree = resolve_data_conflict(rows[1:])
    assert agree["citation"] is None and agree["disagreements"] == []
    assert resolve_data_conflict([{"source": "x", "field": "f", "value": None}]) is None
    custom = resolve_data_conflict(rows, ConflictConfig(source_authority=("warehouse", "operational")))
    assert custom["winner"]["source"] == "warehouse"
