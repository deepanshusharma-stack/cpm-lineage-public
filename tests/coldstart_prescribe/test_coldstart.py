"""Cold-start shrinkage, pre-flight, regime guard and state-mode diagnosis."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from cpm_lineage.coldstart import (
    CALL_BRAND_DEFENSE, CALL_KNOWN_BLEEDER, CALL_NEUTRAL, CALL_NO_HISTORY, CALL_PROVEN_WINNER,
    CALL_THIN_HISTORY, CALL_THIN_PROMISING, AccountHistory, ClearingBid, ColdStartConfig, GeoHistory, OwnUnit,
    Preflight, ProductHistory, UnitHistory, blend_local, confidence_from_weight, daily_weighted_bid,
    detect_regime, diagnose_state, geo_signal, history_from_rows, preflight, preflight_keywords,
    preflight_products, shrink, siblings,
)
from cpm_lineage.config import Economics
from cpm_lineage.liveness import dead_for_window, stale_stopped, stopped_since_window
from cpm_lineage.schema import ChangeEvent

D0 = date(2026, 3, 31)


# ----------------------------------------------------------------- shrink / blend / siblings

def test_shrink_formula_exact():
    v, w = shrink(3.0, 1.0, 10, 1)
    assert w == pytest.approx(14 / 28, abs=1e-12)
    assert v == pytest.approx(0.5 * 3.0 + 0.5 * 1.0, abs=1e-12)


def test_shrink_campaign_evidence_beats_prior():
    # near-zero replicated in five campaigns must not hide behind the prior
    many, _ = shrink(0.01, 0.9, 2, 5)
    none, _ = shrink(0.01, 0.9, 2, 0)
    assert many < 0.45 < none


def test_shrink_edges():
    assert shrink(None, 1.2, 30, 3) == (1.2, 0.0)
    v, w = shrink(2.5, None, 14, 0)
    assert v == 2.5 and w == pytest.approx(0.5)
    assert shrink(2.5, 1.0, 0, 0) == (1.0, 0.0)
    cfg = ColdStartConfig(shrink_k=7.0, camp_evidence=0.0)
    assert shrink(2.0, 0.0, 7, 9, cfg)[1] == pytest.approx(0.5)


def test_blend_local_pinned_values():
    assert blend_local(7000, 0.2, 0.7) == pytest.approx(0.311, abs=1e-3)
    assert blend_local(100, 0.2, 0.7) == pytest.approx(0.676, abs=1e-3)
    assert blend_local(0, 0.2, 0.7) == 0.7
    assert blend_local(5000, None, 0.7) == 0.7
    assert blend_local(5000, 0.2, None) == 0.2
    # tau scales with the currency unit
    assert blend_local(70, 0.2, 0.7, economics=Economics(currency_unit=0.01)) == pytest.approx(0.311, abs=1e-3)


@pytest.mark.parametrize("a,b,expected", [
    ("granola", "granol", True),                # typo-prefix heads, both >= 5
    ("gran", "granola", False),                 # prefix too short to trust
    ("oatzo paste", "oatzo", True),             # one remainder empty
    ("oat cookie box", "oat cookie", True),     # shorter remainder (>= 4) inside the longer
    ("oat flap jack", "oat flapjack", True),      # remainders equal once concatenated
    ("oat cookie", "oat bar", False),           # different remainders
    ("oat bar", "rye bar", False),              # different heads
    ("oat bar", "oat bar", False),              # identical is not a sibling
    ("", "oat bar", False),                     # empty
    ("oat bar", "oat bars", False),             # shorter remainder under 4 characters
    ("Oat  Cookie", "oat cookie box", True),    # case and whitespace insensitive
])
def test_siblings(a, b, expected):
    assert siblings(a, b) is expected
    assert siblings(b, a) is expected


def test_confidence_bands_at_thresholds():
    assert confidence_from_weight(0.7) == "high"
    assert confidence_from_weight(0.6999) == "med"
    assert confidence_from_weight(0.4) == "med"
    assert confidence_from_weight(0.3999) == "low"


# ----------------------------------------------------------------- keyword pre-flight

COHORTS = {"generic": {"n": 20, "p25": 1.2, "p50": 1.8, "p75": 2.6},
           "own_brand": {"n": 20, "p25": 4.0, "p50": 7.0, "p75": 9.0}}


def _card(pf: Preflight, kw: str):
    return next(c for c in pf.cards if c.kw == kw)


def test_preflight_calls_and_order():
    hist = {
        "oat bar": UnitHistory(roas=3.5, n_days=40, n_camps=3, role="generic"),
        "rye bar": UnitHistory(roas=3.5, n_days=4, n_camps=1, role="generic"),
        "corn chip": UnitHistory(roas=0.3, n_days=40, n_camps=2, role="generic", halo=0.05),
        "corn puff": UnitHistory(roas=0.3, n_days=40, n_camps=2, role="generic", halo=0.4),
        "ownname bar": UnitHistory(roas=25.0, n_days=60, n_camps=2, role="own_brand"),
        "seed mix": UnitHistory(roas=1.7, n_days=60, n_camps=2, role="generic"),
        "nut mix": UnitHistory(roas=None, n_days=3, n_camps=1, role="generic"),
    }
    pf = preflight_keywords(list(hist) + ["brand new term"], hist, COHORTS)
    assert _card(pf, "oat bar").call == CALL_PROVEN_WINNER
    assert _card(pf, "rye bar").call == CALL_THIN_PROMISING        # 4 days, 1 campaign: a lead, not proof
    assert _card(pf, "corn chip").call == CALL_KNOWN_BLEEDER
    assert "negative" in _card(pf, "corn chip").actions[0]
    assert "cap, don't kill" in _card(pf, "corn puff").actions[0]  # halo spare
    assert _card(pf, "ownname bar").call == CALL_BRAND_DEFENSE
    assert _card(pf, "seed mix").call == CALL_NEUTRAL
    assert _card(pf, "nut mix").call == CALL_THIN_HISTORY
    assert _card(pf, "brand new term").call == CALL_NO_HISTORY
    assert _card(pf, "brand new term").confidence == "low"
    order = [c.call for c in pf.cards]
    assert order.index(CALL_KNOWN_BLEEDER) < order.index(CALL_PROVEN_WINNER) < order.index(CALL_THIN_HISTORY) \
        < order.index(CALL_NO_HISTORY) < order.index(CALL_NEUTRAL) < order.index(CALL_BRAND_DEFENSE)
    assert pf.n_keywords == 8 and pf.n_with_history == 6 and pf.coverage_pct == 75
    assert [c.kw for c in pf.winners] == ["oat bar"]
    assert [c.kw for c in pf.brand_defense] == ["ownname bar"]


def test_preflight_winner_and_bleeder_bars_at_threshold():
    def call(roas, cohorts=None):
        h = {"k": UnitHistory(roas=roas, n_days=10_000, n_camps=2)}     # w ~ 1: judge ~ roas
        return preflight_keywords(["k"], h, cohorts).cards[0].call
    assert call(2.01) == CALL_PROVEN_WINNER
    assert call(1.95) == CALL_NEUTRAL
    assert call(1.0) == CALL_NEUTRAL
    assert call(0.98) == CALL_KNOWN_BLEEDER
    # cohort bars widen both ends
    assert call(1.7, {"generic": {"n": 12, "p25": 0.5, "p50": 1.0, "p75": 1.6}}) == CALL_PROVEN_WINNER
    assert call(1.5, {"generic": {"n": 12, "p25": 1.55, "p50": 1.8, "p75": 2.5}}) == CALL_KNOWN_BLEEDER
    # a cohort under the minimum n is ignored: absolute bars only
    assert call(1.7, {"generic": {"n": 11, "p25": 0.5, "p50": 1.0, "p75": 1.6}}) == CALL_NEUTRAL


def test_preflight_proven_needs_replication_or_days():
    def call(nd, nc):
        return preflight_keywords(["k"], {"k": UnitHistory(roas=6.0, n_days=nd, n_camps=nc)}).cards[0].call
    assert call(9, 1) == CALL_THIN_PROMISING
    assert call(10, 1) == CALL_PROVEN_WINNER
    assert call(3, 2) == CALL_PROVEN_WINNER


def test_preflight_brand_defense_bar():
    def call(roas, role):
        h = {"k": UnitHistory(roas=roas, n_days=10_000, n_camps=3, role=role)}
        return preflight_keywords(["k"], h).cards[0].call
    assert call(15.01, "own_brand") == CALL_BRAND_DEFENSE
    assert call(14.9, "own_brand") == CALL_PROVEN_WINNER
    assert call(30.0, "generic") == CALL_PROVEN_WINNER
    # live role classifier overrides the stored role
    h = {"k": UnitHistory(roas=30.0, n_days=10_000, n_camps=3, role="generic")}
    assert preflight_keywords(["k"], h, roles={"k": "own_brand"}).cards[0].call == CALL_BRAND_DEFENSE


def test_preflight_local_blend_differentiates_campaigns():
    hist = {"oat bar": UnitHistory(roas=2.6, n_days=60, n_camps=3)}
    weak = preflight_keywords([OwnUnit("oat bar", spend=9000, sales=1800)], hist).cards[0]
    tiny = preflight_keywords([OwnUnit("oat bar", spend=400, sales=80)], hist).cards[0]
    assert tiny.blended_roas is None and tiny.call == CALL_PROVEN_WINNER     # below the blend minimum
    assert weak.blended_roas == pytest.approx((9000 * 0.2 + 2000 * weak.shrunk_roas) / 11000, abs=1e-9)
    assert weak.call == CALL_KNOWN_BLEEDER
    assert any("blended" in a for a in weak.actions)
    at_min = preflight_keywords([OwnUnit("oat bar", spend=500, sales=100)], hist).cards[0]
    assert at_min.blended_roas is not None


def test_preflight_bid_addendum():
    hist = {"oat bar": UnitHistory(roas=2.6, n_days=60, n_camps=3)}
    clearing = {"oat bar": ClearingBid(p50=120.0, p75=150.0)}
    under = preflight_keywords([OwnUnit("oat bar", spend=10, sales=30, bid=100.0)], hist, clearing=clearing).cards[0]
    assert any("underbidding" in a for a in under.actions)
    day0 = preflight_keywords(["oat bar"], hist, clearing=clearing).cards[0]
    assert any("opening bid" in a for a in day0.actions)
    over = preflight_keywords([OwnUnit("oat bar", bid=130.0)], hist, clearing=clearing).cards[0]
    assert not any("bid" in a.lower() for a in over.actions)


def test_preflight_sibling_conflict_withholds_winner():
    hist = {"oat cookie": UnitHistory(roas=4.0, n_days=50, n_camps=2),
            "oat cookie box": UnitHistory(roas=0.4, n_days=50, n_camps=2),
            "rye bar": UnitHistory(roas=4.0, n_days=50, n_camps=2)}
    pf = preflight_keywords(list(hist), hist)
    win, bleed = _card(pf, "oat cookie"), _card(pf, "oat cookie box")
    assert win.sibling_conflict == "oat cookie box" and bleed.sibling_conflict == "oat cookie"
    assert [c.kw for c in pf.winners] == ["rye bar"]
    assert bleed in pf.bleeders and win in pf.cards


def test_preflight_empty_degrades():
    pf = preflight_keywords([], {})
    assert pf.status == "empty" and pf.reason and pf.coverage_pct == 0


# ----------------------------------------------------------------- products and geos

def test_product_preflight():
    hist = {"p1": ProductHistory(roas=4.0, n_days=60, n_camps=3, availability=0.95),
            "p2": ProductHistory(roas=0.4, n_days=60, n_camps=2, availability=0.5),
            "p3": ProductHistory(roas=1.5, n_days=60, n_camps=1, availability=0.5),
            "p4": ProductHistory(roas=1.6, n_days=60, n_camps=1)}
    cards, winners, bleeders = preflight_products(["p3", "p1", "p2", "unknown"], hist)
    assert [c.product_id for c in cards] == ["p2", "p1", "p3"]
    assert [c.product_id for c in winners] == ["p1"] and [c.product_id for c in bleeders] == ["p2"]
    assert "fix supply before funding" in bleeders[0].actions[0]
    assert any("Warning" in a for a in cards[2].actions)
    # solo proven product is not a reallocation winner
    cards, winners, _ = preflight_products(["p1"], hist)
    assert cards[0].solo and winners == []
    assert preflight_products(None, hist) == ([], [], [])
    assert preflight_products(["p1"], {}) == ([], [], [])


def test_product_prior_is_account_median():
    hist = {"a": ProductHistory(roas=1.0, n_days=14, n_camps=0),
            "b": ProductHistory(roas=3.0, n_days=14, n_camps=0),
            "c": ProductHistory(roas=5.0, n_days=14, n_camps=0)}
    cards, _, _ = preflight_products(["a"], hist)
    assert cards[0].shrunk_roas == pytest.approx(0.5 * 1.0 + 0.5 * 3.0, abs=1e-12)


def test_geo_signal():
    sig = geo_signal({"g1": GeoHistory(roas=3.0), "g2": 2.0, "g3": {"roas": 0.6}, "g4": 0.4, "g5": 1.4},
                     availability={"g4": 0.5}, tiers={"g1": "Scale"})
    assert [d["geo"] for d in sig.strong] == ["g1", "g2", "g5"]
    assert [d["geo"] for d in sig.weak] == ["g4", "g3"]
    assert sig.weak[0]["availability"] == 0.5 and sig.strong[0]["tier"] == "Scale"
    assert "g1" in sig.note
    assert geo_signal({"g1": 2.0}) is None
    assert geo_signal(None) is None


# ----------------------------------------------------------------- synthetic account

def test_synthetic_young_campaign_preflight(synthetic_dataset):
    ds = synthetic_dataset
    hist = history_from_rows(ds.perf, exclude_entity_id="cmp-young",
                             roles={t.value: t.role for t in ds.targeting_units},
                             is_search_keyword=ds.profile.is_search_keyword,
                             is_aggregate_geo=ds.profile.is_aggregate_geo)
    assert "others" not in hist.geos and "vegan protein bar" not in hist.units
    assert hist.units["energy bar"].n_camps == 2                    # the planted own-overlap keyword
    assert hist.units["rivalb bar"].roas < 0.5                      # the planted bleeding keyword
    pf = preflight(["vegan protein bar", "rivalb bar", "energy bar", "ownbrand bar"], hist,
                   product_ids=["sku-a3", "sku-a2"])
    calls = {c.kw: c.call for c in pf.cards}
    assert calls["rivalb bar"] == CALL_KNOWN_BLEEDER
    assert calls["energy bar"] == CALL_PROVEN_WINNER
    assert calls["vegan protein bar"] == CALL_NO_HISTORY
    assert pf.cards[0].kw == "rivalb bar" and pf.coverage_pct == 75
    assert {c.product_id for c in pf.product_winners} == {"sku-a2", "sku-a3"}
    assert pf.geo_signal is not None and len(pf.geo_signal.strong) == 3 and not pf.geo_signal.weak

    young = [r for r in ds.perf if r.entity_id == "cmp-young"]
    n_act = len({r.date for r in young if r.spend > 0})
    out = diagnose_state("cmp-young", "ACTIVE", n_act, pf, status_map=ds.profile.status_map,
                         own_roas=sum(r.sales for r in young) / sum(r.spend for r in young))
    assert out.status == "ok" and out.mode == "state" and out.verdict == "state-only"
    assert out.tier == "warm" and out.dln_roas == 0.0 and out.short and not out.drivers
    assert out.priority == "p0" and out.confidence == "Medium"
    assert out.top3[0].kind == "bleed" and out.top3[0].targets == ("rivalb bar",)
    assert any("noisy" in f for f in out.facts)


# ----------------------------------------------------------------- regime guard

def test_regime_change_log_and_post_change_days():
    ev = [ChangeEvent(datetime(2026, 3, 30, 9), "c1", "daily_budget", 100, 60, source="operator", verified=True),
          ChangeEvent(datetime(2026, 3, 10, 9), "c1", "bid", 5, 6, verified=True)]
    r = detect_regime(D0, ev, entity_id="c1")
    assert r.source == "change_log" and r.post_change_days == 1 and r.prior_regime and "PRIOR regime" in r.warning
    r2 = detect_regime(D0 + timedelta(days=1), ev, entity_id="c1")
    assert r2.post_change_days == 2 and r2.prior_regime
    r3 = detect_regime(D0 + timedelta(days=2), ev, entity_id="c1")
    assert r3.post_change_days == 3 and not r3.prior_regime and r3.warning is None


def test_regime_ignores_automated_bids_unverified_and_old():
    ev = [ChangeEvent(datetime(2026, 3, 30, 9), "c1", "bid", 5, 6, automated=True),
          ChangeEvent(datetime(2026, 3, 30, 9), "c1", "bid", 5, 6, source="Position-Bidder"),
          ChangeEvent(datetime(2026, 3, 30, 9), "c1", "status", "a", "b", verified=False),
          ChangeEvent(datetime(2026, 3, 30, 9), "c2", "status", "a", "b"),
          ChangeEvent(datetime(2026, 2, 20, 9), "c1", "status", "a", "b"),
          ChangeEvent(datetime(2026, 3, 30, 9), "c1", "name", "a", "b")]
    assert detect_regime(D0, ev, entity_id="c1", automated_sources={"position-bidder"}) is None
    # an automated budget change still counts
    ev.append(ChangeEvent(datetime(2026, 3, 29, 9), "c1", "daily_budget", 1, 2, automated=True))
    assert detect_regime(D0, ev, entity_id="c1", automated_sources={"position-bidder"}).post_change_days == 2


def test_regime_source_order_and_bid_telemetry():
    bids = {D0 - timedelta(days=5): 100.0, D0 - timedelta(days=4): 124.0, D0 - timedelta(days=1): 155.0, D0: 150.0}
    r = detect_regime(D0, None, None, bids)
    assert r.source == "bid_telemetry" and r.changed_on == D0 - timedelta(days=1) and r.prior_regime
    assert detect_regime(D0, None, None, {D0 - timedelta(days=1): 100.0, D0: 124.9}) is None
    assert detect_regime(D0, None, None, {D0 - timedelta(days=1): 100.0, D0: 75.0}).source == "bid_telemetry"
    declared = [(D0 - timedelta(days=10), "moved budget in the platform console")]
    assert detect_regime(D0, None, declared, bids).source == "declared"
    ev = [ChangeEvent(datetime(2026, 3, 1, 9), "c1", "status", "a", "b")]
    assert detect_regime(D0, ev, declared, bids).source == "change_log"
    assert detect_regime(D0) is None


def test_daily_weighted_bid_and_synthetic_regime(synthetic_dataset, make_perf_row):
    rows = [make_perf_row(day=0, spend=100, bid=10.0), make_perf_row(day=0, spend=300, bid=20.0),
            make_perf_row(day=1, spend=50, bid=None)]
    series = daily_weighted_bid(rows)
    assert list(series.values()) == [pytest.approx(17.5)]
    ds = synthetic_dataset
    # the planted bid change was made by the automated bidder: the change log ignores it ...
    assert detect_regime(ds.latest_date, ds.change_events, entity_id="cmp-generic",
                         automated_sources=ds.profile.automated_sources or {"position-bidder"}) is None
    # ... but bid telemetry sees the step
    kw = [r for r in ds.perf if r.entity_id == "cmp-generic" and r.targeting_unit == "protein bar"]
    r = detect_regime(ds.latest_date, None, None, daily_weighted_bid(kw))
    assert r is not None and r.source == "bid_telemetry" and not r.prior_regime


# ----------------------------------------------------------------- state mode

def _pf(n_bleed=0, n_win=0, n_def=0, n_pbleed=0, n_pwin=0, cov_hist=None, extra_unknown=0):
    hist, kws = {}, []
    for i in range(n_bleed):
        hist[f"bleed{i} term"] = UnitHistory(roas=0.2, n_days=60, n_camps=2)
    for i in range(n_win):
        hist[f"win{i} term"] = UnitHistory(roas=5.0, n_days=60, n_camps=2)
    for i in range(n_def):
        hist[f"own{i} term"] = UnitHistory(roas=30.0, n_days=60, n_camps=2, role="own_brand")
    kws = list(hist) + [f"new{i} term" for i in range(extra_unknown)]
    ph = {f"pb{i}": ProductHistory(roas=0.2, n_days=60, n_camps=2) for i in range(n_pbleed)}
    ph.update({f"pw{i}": ProductHistory(roas=6.0, n_days=60, n_camps=2) for i in range(n_pwin)})
    return preflight(kws, AccountHistory(units=hist, products=ph), product_ids=list(ph) or None)


def test_state_top3_stop_loss_then_reserved_winner():
    out = diagnose_state("c", "ACTIVE", 5, _pf(n_bleed=3, n_win=3, n_def=1, n_pbleed=1, n_pwin=1))
    assert [a.kind for a in out.top3] == ["bleed", "bleed", "win"]
    assert out.top3[2].verb.startswith("Hold top slot")            # brand defense takes the reserved slot
    assert out.top3[0].sizing == "half-step cap, recheck in 7 days"
    assert out.priority == "p0" and out.tier == "cold"
    out = diagnose_state("c", "ACTIVE", 5, _pf(n_bleed=1, n_win=3))
    assert [a.kind for a in out.top3] == ["bleed", "win", "win"] and out.top3[1].verb == "Fund from day 1"
    out = diagnose_state("c", "ACTIVE", 5, _pf(n_bleed=2, n_pbleed=1))
    assert [a.lever for a in out.top3] == ["keyword", "keyword", "product"]
    out = diagnose_state("c", "ACTIVE", 5, _pf(n_win=1, n_pwin=2))
    assert [a.verb for a in out.top3] == ["Fund from day 1", "Allocate budget"] and out.priority == "p2"


def test_state_confidence_bands_and_regime_override():
    assert diagnose_state("c", "ACTIVE", 5, _pf(n_win=4, extra_unknown=1)).confidence == "High"      # 80%
    assert diagnose_state("c", "ACTIVE", 5, _pf(n_win=3, extra_unknown=1)).confidence == "Medium"    # 75%
    assert diagnose_state("c", "ACTIVE", 5, _pf(n_win=2, extra_unknown=3)).confidence == "Medium"    # 40%
    assert diagnose_state("c", "ACTIVE", 5, _pf(n_win=1, extra_unknown=2)).confidence == "Low"       # 33%
    ev = [ChangeEvent(datetime(2026, 3, 30, 9), "c", "bid", 5, 6)]
    regime = detect_regime(D0, ev)
    out = diagnose_state("c", "ACTIVE", 5, _pf(n_win=4), regime=regime, untracked=["an offline campaign"],
                         window_facts=["custom window: spend flat"])
    assert out.confidence == "Low" and out.facts[0] == regime.warning
    assert any("Coverage caveat" in f for f in out.facts) and "custom window: spend flat" in out.facts
    old = detect_regime(D0, [ChangeEvent(datetime(2026, 3, 20, 9), "c", "bid", 5, 6)])
    assert diagnose_state("c", "ACTIVE", 5, _pf(n_win=4), regime=old).confidence == "High"


def test_state_excludes_stale_stopped():
    out = diagnose_state("c", "ended", 5, _pf(n_win=1), spend_7d=0.0)
    assert out.status == "excluded" and out.top3 == []
    assert diagnose_state("c", "ended", 5, _pf(n_win=1), spend_7d=120.0).status == "ok"
    assert diagnose_state("c", "budget_held", 5, _pf(n_win=1), spend_7d=0.0).status == "ok"   # soft state
    assert diagnose_state("c", "ACTIVE", 5, None).confidence == "Low"


# ----------------------------------------------------------------- liveness rules used by this layer

def test_liveness_truth_table():
    # hard-dead: stale only without recent spend
    assert stale_stopped("ended", 0.0) and not stale_stopped("ended", 50.0)
    # soft-dead: follows the 3-day rule
    assert stale_stopped("budget_held", 0.0, 0.0) and not stale_stopped("budget_held", 0.0, 10.0)
    assert not stale_stopped("active", 0.0, 0.0)
    # retrospective: any in-window spend keeps the entity
    assert not dead_for_window("ended", 500.0, 0.0) and dead_for_window("ended", 0.0, 0.0)
    assert stopped_since_window("ended", 500.0, 0.0) and not stopped_since_window("active", 500.0, 10.0)
