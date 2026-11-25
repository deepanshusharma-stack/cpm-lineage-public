"""Prescription gate stack, ROAS ladder and drill-row calls."""

from __future__ import annotations

import itertools

import pytest

from cpm_lineage.config import AvailabilityPolicy, Economics
from cpm_lineage.prescribe import (
    GROW, HOLD, SHRINK, PrescribeBudget, PrescribeInput, PrescribeKeyword, TopAction, budget_from_probe,
    campaign_cann_rate, demand_limited, geo_row_call, keyword_row_call, prescribe, product_row_call, role_mix,
    top_actions_from,
)


def rx(**kw):
    kw.setdefault("spend", 30000.0)
    kw.setdefault("days", 30)
    return prescribe(PrescribeInput(**kw))


CAPPED = PrescribeBudget(daily=1000.0, capped=True, mroas=1.8, sat_spend=2600.0, rec_range=(1300.0, 2000.0, 1500.0))
BRAND_KW = (PrescribeKeyword("ownname bar", role="own_brand", spend=900.0, cann_rate=0.6),
            PrescribeKeyword("oat bar", role="generic", spend=100.0))
CONQ_KW = (PrescribeKeyword("rival bar", role="competitor", spend=900.0),)
CUT = TopAction(verb="Cut / negative", targets=("corn chip", "corn puff"), headline="Two keywords lose money")


# ----------------------------------------------------------------- gates 0 and A

def test_gate0_unavailable_and_status():
    assert rx(diagnosis_ok=False, roas=5.0).gate == "0a"
    p = rx(roas=5.0, status="archived")
    assert p.gate == "0b" and p.verb == "Launch replacement"
    for status in ("paused", "stopped", "completed"):
        p = rx(roas=5.0, status=status, budget=CAPPED)
        assert p.gate == "0c" and p.verb.startswith("Investigate stop")
    # platform budget hold is not a stop; adapter map is honoured
    assert rx(roas=5.0, status="on_hold", budget=CAPPED).gate == "L3.5"
    assert rx(roas=5.0, status="HALTED", status_map={"halted": "operator_paused"}).gate == "0c"


def test_gateA_reach_never_roas_judged():
    for roas in (None, 0.0, 0.4, 9.0):
        p = rx(roas=roas, reach_judged=True, budget=CAPPED)
        assert p.gate == "A" and "reach" in p.verb.lower()


# ----------------------------------------------------------------- gates B, C, C'

def test_gateB_halo_conquest_is_keep():
    p = rx(roas=0.8, total_roas=2.4, keywords=CONQ_KW)
    assert p.gate == "B" and p.verb.startswith("Keep") and "don't cut" in p.target
    # a cut as top action also triggers it on a non-conquest campaign
    assert rx(roas=0.8, total_roas=2.4, actions=(CUT,)).gate == "B"
    # non-conquest with no cut on the table: ladder decides
    assert rx(roas=0.8, total_roas=2.4).gate == "L1"


def test_gateB_thresholds():
    assert rx(roas=1.0, total_roas=2.0, keywords=CONQ_KW).gate == "B"
    assert rx(roas=1.0, total_roas=1.99, keywords=CONQ_KW).gate != "B"          # total under 2.0 is not a halo
    assert rx(roas=1.5, total_roas=2.24, keywords=CONQ_KW).gate != "B"          # total under 1.5 x direct
    assert rx(roas=1.5, total_roas=2.25, keywords=CONQ_KW).gate == "B"
    assert rx(roas=2.0, total_roas=3.0, keywords=CONQ_KW).gate == "B"
    assert rx(roas=2.01, total_roas=9.0, keywords=CONQ_KW).gate != "B"          # direct above the bar
    assert rx(roas=0.8, total_roas=None, keywords=CONQ_KW).gate == "L1"         # no indirect attribution


def test_gateC_branded_cannibalisation_holds_and_shows_incremental():
    p = rx(roas=4.0, keywords=BRAND_KW, budget=CAPPED)
    assert p.gate == "C" and p.direction == HOLD and "1.60" in p.why           # 4.0 x (1 - 0.6)
    # incremental >= 2 still scales
    kws = (PrescribeKeyword("ownname bar", role="own_brand", spend=900.0, cann_rate=0.4),)
    assert rx(roas=4.0, keywords=kws, budget=CAPPED).gate == "L3.5"
    kws = (PrescribeKeyword("ownname bar", role="own_brand", spend=900.0, cann_rate=0.5),)
    assert rx(roas=4.0, keywords=kws, budget=CAPPED).gate == "L3.5"            # incremental exactly 2.0
    # below the bar the gate does not fire
    assert rx(roas=1.5, keywords=BRAND_KW).gate == "L2"


def test_gateC_role_classified_without_name_token_and_rate_preference():
    # no brand token in the name; the per-campaign rate (0.6) wins over the account rate (0.1)
    p = rx(roas=4.0, keywords=BRAND_KW, entity_name="spring push 04", account_cann_rate=0.1, budget=CAPPED)
    assert p.gate == "C"
    kws = (PrescribeKeyword("ownname bar", role="own_brand", spend=900.0),)
    assert rx(roas=4.0, keywords=kws, account_cann_rate=0.7, budget=CAPPED).gate == "C"
    assert rx(roas=4.0, keywords=kws, budget=CAPPED).gate == "C'"
    # name-token fallback only when there is no keyword data
    assert rx(roas=4.0, entity_name="Snacks_Branded_North", budget=CAPPED).gate == "C'"
    assert rx(roas=4.0, entity_name="Snacks_Generic_North", budget=CAPPED).gate == "L3.5"


def test_role_mix_and_helpers():
    m = role_mix(BRAND_KW)
    assert m.is_branded and m.share == pytest.approx(0.9) and not m.mixed
    kws = (PrescribeKeyword("a", role="own_brand", spend=64.0), PrescribeKeyword("b", role="generic", spend=36.0),
           PrescribeKeyword("shelf", role="placement", spend=500.0, is_placement=True))
    assert role_mix(kws).mixed and role_mix(kws).dominant == "own_brand"
    kws65 = (PrescribeKeyword("a", role="own_brand", spend=65.0), PrescribeKeyword("b", role="generic", spend=35.0))
    assert not role_mix(kws65).mixed
    assert role_mix((), "q3-conquest-east").is_conquest and role_mix((), "plain").dominant is None
    assert any("Mixed campaign" in c for c in rx(roas=1.5, keywords=kws).caveats)
    two = (PrescribeKeyword("a", role="own_brand", spend=300.0, cann_rate=0.2),
           PrescribeKeyword("b", role="own_brand", spend=100.0, cann_rate=0.6),
           PrescribeKeyword("c", role="generic", spend=900.0, cann_rate=0.9))
    assert campaign_cann_rate(two, 0.05) == pytest.approx(0.3)
    assert campaign_cann_rate((), 0.05) == 0.05 and campaign_cann_rate(()) is None
    assert demand_limited((PrescribeKeyword("a", searches_per_day=49.0),))
    assert not demand_limited((PrescribeKeyword("a", searches_per_day=50.0),))
    assert not demand_limited((PrescribeKeyword("a", searches_per_day=10.0), PrescribeKeyword("b")))
    assert not demand_limited(())


# ----------------------------------------------------------------- gates 1-6

def test_gate1_surgical_cut_wins_at_any_roas():
    for roas in (0.4, 1.5, 6.0):
        p = rx(roas=roas, actions=(CUT,), budget=CAPPED)
        assert p.gate == "1" and p.direction == SHRINK and "corn chip" in p.target
        assert "clears the bar" not in p.why
    # a cut verb without targets is not surgical
    assert rx(roas=6.0, actions=(TopAction(verb="Cut"),), budget=CAPPED).gate == "L3.5"
    assert rx(roas=6.0, actions=(TopAction(verb="Launch a replacement campaign"),)).gate == "1b"


def test_gate2_availability_block_precedes_every_lever():
    p = rx(roas=6.0, availability=0.55, budget=CAPPED)
    assert p.gate == "2" and p.verb == "Fix availability first" and p.direction == HOLD
    p = rx(roas=0.5, availability=0.55, spend=30000.0, days=30)
    assert p.gate == "2" and "500/day" in p.target and p.direction == SHRINK
    assert rx(roas=6.0, availability=0.60, budget=CAPPED).gate == "L3.5"      # at the line: not blocked
    assert rx(roas=6.0, availability=55.0, budget=CAPPED).gate == "2"         # percent input is normalised
    off = AvailabilityPolicy(enabled=False)
    assert prescribe(PrescribeInput(roas=6.0, availability=0.3, budget=CAPPED, spend=1.0, days=1),
                     availability_policy=off).gate == "L3.5"


def test_availability_caution_qualifies_scale_only():
    q = "Scale in-stock products only"
    assert q in rx(roas=6.0, availability=0.70, budget=CAPPED).why                       # caution band
    assert q in rx(roas=6.0, availability=0.90, availability_prior=0.95, budget=CAPPED).why   # 5-point drop
    assert q not in rx(roas=6.0, availability=0.91, availability_prior=0.95, budget=CAPPED).why
    assert q not in rx(roas=6.0, availability=0.95, budget=CAPPED).why
    assert q not in rx(roas=1.5, availability=0.70).why                                  # a hold is not qualified


def test_gate3_burning_auto_ad_is_capped():
    p = rx(roas=0.7, auto_targeted=True, auto_burn=True)
    assert p.gate == "3" and p.direction == SHRINK
    burn = TopAction(verb="Cap / reduce", headline="Auto-targeted spend is burning")
    assert rx(roas=2.5, auto_targeted=True, actions=(burn,)).gate == "3"
    assert rx(roas=0.7, auto_targeted=True).gate == "L1"
    assert rx(roas=0.7, auto_burn=True).gate == "L1"


def test_gate4_passthrough_verbatim():
    for verb in ("Investigate tracking", "Hold the budget raise", "Audit before spending", "Hold lever changes"):
        p = rx(roas=6.0, actions=(TopAction(verb=verb, headline="because"),), budget=CAPPED)
        assert p.gate == "4" and p.verb == verb and p.why == "because"


def test_gate5_and_6():
    assert rx(roas=6.0, budget=PrescribeBudget(daily=10.0, capped=True, anomaly=True)).gate == "5"
    assert rx(roas=None).verb == "Insufficient data, investigate"
    assert rx(roas=None, state_verb="Fund from day 1").verb == "Fund from day 1"


# ----------------------------------------------------------------- ladder

def test_ladder_pause_and_trim_is_the_breakeven_slice():
    assert rx(roas=0.0).verb == "Pause"
    p = rx(roas=0.6, spend=60000.0, days=30, budget=PrescribeBudget(daily=2000.0, capped=True))
    # avg daily 2000 x ROAS 0.6 = 1200; the cut is the (1 - ROAS) slice, not an arbitrary 25%
    assert p.verb == "Trim toward break-even" and "1,200/day" in p.target and "-40%" in p.target
    assert "25%" not in p.target and p.direction == SHRINK
    p = rx(roas=0.6, budget=PrescribeBudget(ladder="bid-constrained"))
    assert p.verb == "Fix efficiency before bidding up" and p.direction == SHRINK and "raise" not in p.verb.lower()
    p = rx(roas=0.6, actions=(TopAction(verb="Restore availability"),))
    assert p.verb == "Fix availability first" and p.gate == "L1"


def test_ladder_trim_respects_breakeven_and_currency():
    econ = Economics(breakeven=1.5)
    p = prescribe(PrescribeInput(roas=1.2, spend=30000.0, days=30), economics=econ)
    assert p.gate == "L1" and "800/day" in p.target and "-20%" in p.target
    p = prescribe(PrescribeInput(roas=0.5, spend=30.0, days=30), economics=Economics(currency_unit=0.001))
    assert "0/day" in p.target or "1/day" in p.target       # rounding step scales with the currency unit


def test_ladder_middle_band_holds():
    for roas in (1.0, 1.5, 1.99):
        p = rx(roas=roas, budget=CAPPED)
        assert p.gate == "L2" and p.direction == HOLD
    assert rx(roas=0.99).gate == "L1" and rx(roas=2.0, budget=CAPPED).gate == "L3.5"


def test_ladder_curve_ceiling_holds():
    assert rx(roas=5.0, budget=PrescribeBudget(daily=1000.0, capped=True, mroas=0.9)).gate == "L3.1"
    assert rx(roas=5.0, budget=PrescribeBudget(daily=1000.0, capped=True, mroas=1.6, sat_spend=1000.0)).gate == "L3.1"
    assert rx(roas=5.0, budget=PrescribeBudget(daily=1000.0, capped=True, mroas=1.0, sat_spend=1001.0)).gate == "L3.5"


def test_ladder_capped_scales_to_range_with_step_size():
    full = rx(roas=5.0, budget=CAPPED, confidence="high")
    assert full.verb.startswith("Scale") and "2,000/day" in full.target and "curve-fit" in full.target
    thin = rx(roas=5.0, budget=PrescribeBudget(daily=1000.0, capped=True, mroas=1.2, sat_spend=2600.0,
                                               rec_range=(1300.0, 2000.0, 1500.0)))
    assert "1,300/day" in thin.target and "small step" in thin.why
    lowc = rx(roas=5.0, budget=CAPPED, confidence="Low")
    assert "1,300/day" in lowc.target
    at_bar = rx(roas=5.0, budget=PrescribeBudget(daily=1000.0, capped=True, mroas=1.5, sat_spend=2600.0,
                                                 rec_range=(1300.0, 2000.0)))
    assert "2,000/day" in at_bar.target
    # the 2x fallback is never labelled curve-fit
    fb = rx(roas=5.0, budget=PrescribeBudget(daily=1000.0, capped=True, rec_range=(1300.0, 2000.0, 1500.0)))
    assert "curve-fit" not in fb.target and "heuristic 1.3x cap" in fb.target and "1,300/day" in fb.target
    assert all(p.direction == GROW for p in (full, thin, lowc, fb))


def test_ladder_platform_held_tops_up():
    b = PrescribeBudget(daily=1000.0, capped=True, mroas=1.8, sat_spend=2600.0, rec_range=(1300.0, 2000.0), held_days=3)
    assert rx(roas=5.0, budget=b).verb == "Top up daily budget"
    b2 = PrescribeBudget(daily=1000.0, capped=True, mroas=1.8, sat_spend=2600.0, rec_range=(1300.0, 2000.0), held_days=2)
    assert rx(roas=5.0, budget=b2).verb.startswith("Scale")


def test_ladder_demand_bid_volume_branches():
    thin = (PrescribeKeyword("a", searches_per_day=20.0, spend=10.0),)
    p = rx(roas=5.0, keywords=thin, budget=CAPPED)
    assert p.verb == "Add keywords" and p.gate == "L3.2"
    bid = PrescribeBudget(daily=1000.0, ladder="bid-constrained", util=0.5)
    p = rx(roas=5.0, budget=bid, clearing_p50=140.0)
    assert p.verb == "Raise bids (not budget)" and "140.00" in p.target and "position" not in p.why
    p = rx(roas=5.0, budget=PrescribeBudget(daily=1000.0, ladder="bid-constrained", position=4.2))
    assert "position 4.2" in p.why                              # rank appears only when known
    p = rx(roas=5.0, budget=bid, actions=(TopAction(verb="Scale winners", self_competed=True),))
    assert p.verb == "Consolidate overlapping campaigns first"
    assert rx(roas=5.0, budget=PrescribeBudget(ladder="volume-ceiling")).gate == "L3.4"


def test_ladder_uncapped_terminals():
    assert rx(roas=5.0, budget=PrescribeBudget(daily=1000.0, mroas=1.5)).verb == "Scale carefully"
    p = rx(roas=5.0, budget=PrescribeBudget(daily=1000.0, mroas=1.49))
    assert p.verb.startswith("Maintain") and p.direction == HOLD
    assert rx(roas=5.0).verb.startswith("Maintain")


# ----------------------------------------------------------------- invariants

GROW_WORDS = ("scale", "raise", "top up", "add keywords", "fund")
SHRINK_WORDS = ("cut", "trim", "pause", "cap the", "fix efficiency")


def test_direction_never_contradicts_verb_across_scenarios():
    roas_v = (None, 0.0, 0.5, 1.0, 1.7, 2.0, 4.0, 12.0)
    total_v = (None, 2.5)
    budgets = (None, CAPPED, PrescribeBudget(daily=1000.0, capped=True, mroas=0.8),
               PrescribeBudget(daily=1000.0, ladder="bid-constrained"), PrescribeBudget(ladder="volume-ceiling"),
               PrescribeBudget(daily=5.0, capped=True, anomaly=True))
    avail = (None, 0.5, 0.7, 0.95)
    kws = ((), BRAND_KW, CONQ_KW)
    acts = ((), (CUT,), (TopAction(verb="Hold the budget raise"),))
    n = 0
    for roas, total, b, av, kw, ac in itertools.product(roas_v, total_v, budgets, avail, kws, acts):
        p = rx(roas=roas, total_roas=total, budget=b, availability=av, keywords=kw, actions=ac)
        n += 1
        assert p.verb and p.target and p.why and p.as_tuple() == (p.verb, p.target, p.why)
        v = p.verb.lower()
        if p.direction == GROW:
            assert not any(w in v for w in SHRINK_WORDS), p
            assert roas is not None and roas >= 2.0 and (av is None or av >= 0.6)
        if p.direction == SHRINK:
            assert not any(w in v for w in GROW_WORDS), p
        if roas is not None and roas < 1.0 and p.gate not in ("4",):
            assert p.direction != GROW, p                 # a sub-breakeven campaign is never grown
        if av == 0.5 and p.gate.startswith("L"):
            raise AssertionError("availability block must precede the ladder")
    assert n == 8 * 2 * 6 * 4 * 3 * 3


def test_synthetic_campaigns(synthetic_dataset, windows_30):
    """Reach placement, capped winner and bleeding conquest campaign from the synthetic account."""
    ds = synthetic_dataset
    cur = windows_30.current

    def facts(eid):
        rows = [r for r in ds.perf if r.entity_id == eid and cur.contains(r.date)]
        sp, sa = sum(r.spend for r in rows), sum(r.sales for r in rows)
        return rows, sp, (sa / sp if sp else None)

    cap = ds.profile.capability("featured_listing")
    _, sp, _ = facts("cmp-reach")
    p = prescribe(PrescribeInput(roas=0.0, spend=sp, days=30, reach_judged=cap.judged_on != "roas" or not cap.sales_attributed))
    assert p.gate == "A"

    _, sp, roas = facts("cmp-winner")
    held = sum(1 for d in ds.budget_days if d.entity_id == "cmp-winner" and cur.contains(d.date) and d.platform_held)
    daily = next(e.daily_budget for e in ds.entities if e.entity_id == "cmp-winner")
    p = prescribe(PrescribeInput(roas=roas, spend=sp, days=30, status="BUDGET_HELD", status_map=ds.profile.status_map,
                                 budget=PrescribeBudget(daily=daily, capped=True, held_days=held,
                                                        rec_range=(1.3 * daily, 2.0 * daily))))
    assert p.direction == GROW and p.gate == "L3.5" and "heuristic" in p.target
    assert p.verb == ("Top up daily budget" if held >= 3 else "Scale: raise the cap")

    rows, sp, roas = facts("cmp-conquest")
    by = {}
    for r in rows:
        by[r.targeting_unit] = by.get(r.targeting_unit, 0.0) + r.spend
    roles = {t.value: t.role for t in ds.targeting_units}
    kws = tuple(PrescribeKeyword(k, role=roles.get(k, "generic"), spend=v) for k, v in by.items())
    assert role_mix(kws).is_conquest
    p = prescribe(PrescribeInput(roas=roas, spend=sp, days=30, keywords=kws,
                                 actions=(TopAction(verb="Cut / negative", targets=("rivalb bar",)),)))
    assert p.gate == "1" and "rivalb bar" in p.target


def test_coercion_helpers():
    class Act:
        verb, targets, verb_class, headline, extras = "Pause", ["k1"], "stop", "h", {"self_competed": True}
    a = top_actions_from([Act(), {"verb": "Raise", "targets": ("k2",)}])
    assert a[0].targets == ("k1",) and a[0].self_competed and a[1].verb == "Raise"
    assert rx(roas=3.0, actions=(Act(),)).gate == "1"
    b = budget_from_probe({"daily_budget": 500.0, "capped": True, "mroas_cap": 1.7, "rec_range": (650.0, 1000.0, 750.0)},
                          held_days=4)
    assert b.daily == 500.0 and b.capped and b.mroas == 1.7 and b.held_days == 4
    assert budget_from_probe(None) is None


# ----------------------------------------------------------------- drill-row calls

def kw_row(**kw):
    base = dict(h_spend=30000.0, w_spend=30000.0, h_roas=2.0, w_roas=1.5, total=0.0,
                h_bid=100.0, w_bid=100.0, h_position=3.0, w_position=3.0)
    base.update(kw)
    return base


def test_keyword_row_calls():
    assert keyword_row_call(kw_row(w_spend=0.5, h_spend=500.0)) == "already stopped"
    assert keyword_row_call(kw_row(w_roas=0.9, w_spend=20001.0)) == "cut / negative"
    assert keyword_row_call(kw_row(w_roas=0.9, w_spend=20000.0)) == "hold"              # spend must exceed the floor
    assert keyword_row_call(kw_row(total=-0.03, w_bid=111.0)) == "cap bid"
    assert keyword_row_call(kw_row(total=-0.03, w_bid=110.0)) == "hold"                 # exactly 1.1x is not a step
    assert keyword_row_call(kw_row(total=-0.03, w_bid=111.0, w_roas=None)) == "cap bid"
    assert keyword_row_call(kw_row(total=-0.03, w_bid=111.0, w_roas=2.5)) == "hold"
    assert keyword_row_call(kw_row(total=-0.03, w_position=3.4)) == "push rank"
    assert keyword_row_call(kw_row(total=-0.02, w_position=3.4)) == "hold"              # inside the band
    assert keyword_row_call(kw_row(total=0.03, w_roas=2.0)) == "scale"
    assert keyword_row_call(kw_row(total=0.03, w_roas=1.99)) == "hold"
    # platforms where a higher position value is better
    assert keyword_row_call(kw_row(total=-0.03, w_position=3.4), lower_is_better=False) == "hold"
    assert keyword_row_call(kw_row(total=-0.03, w_position=2.5), lower_is_better=False) == "push rank"
    # the money floor follows the currency unit
    assert keyword_row_call(kw_row(w_roas=0.9, w_spend=300.0), economics=Economics(currency_unit=0.01)) == "cut / negative"


def test_keyword_row_call_on_drill_items(synthetic_dataset):
    from cpm_lineage.drill import keyword_drill
    res = keyword_drill(synthetic_dataset.perf, "cmp-conquest")
    calls = {i.key: keyword_row_call(i) for i in res.items}
    assert calls and calls.get("rivalb bar") == "cut / negative"


def test_product_and_geo_row_calls():
    p = dict(h_spend=30000.0, w_spend=30000.0, w_roas=1.5, total=0.0, w_availability=0.9)
    assert product_row_call({**p, "w_spend": 0.0, "w_availability": 0.02}) == "possibly delisted, verify"
    assert product_row_call({**p, "w_spend": 0.0}) == "already stopped"
    assert product_row_call({**p, "w_availability": 0.69}) == "fix availability / gate"
    assert product_row_call({**p, "w_availability": 0.70}) == "hold"
    assert product_row_call({**p, "w_roas": 0.5}) == "cut spend"
    assert product_row_call({**p, "w_roas": 2.4, "total": 0.05}) == "scale"
    assert product_row_call({**p, "w_availability": None}) == "hold"
    assert geo_row_call(0.80, 0.86) == "fix availability (trails rivals)"
    assert geo_row_call(0.80, 0.85) == "monitor"
    assert geo_row_call(0.59, 0.60) == "restock / gate"
    assert geo_row_call(0.59) == "restock / gate"
    assert geo_row_call(0.60) == "monitor" and geo_row_call(None, 0.9) == "monitor"


def test_a_zero_percent_trim_is_a_hold_not_an_action():
    p = prescribe(PrescribeInput(roas=0.998, spend=30_000.0, days=30))
    assert p.verb == "Hold & monitor" and "-0%" not in (p.target or "")
    real = prescribe(PrescribeInput(roas=0.9, spend=30_000.0, days=30))
    assert real.verb == "Trim toward break-even" and "(-10%)" in real.target
