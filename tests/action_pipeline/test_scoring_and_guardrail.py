"""Confidence, expected value, run guardrail, equity gate and marginal sizing."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _pl import bound, budget, card, ctx  # noqa: E402

from cpm_lineage.actions.guardrail import (  # noqa: E402
    annotate_equity, apply_guardrail, equity_gate, roas_fence, sov_headroom, spend_base,
)
from cpm_lineage.actions.pipeline import plan_actions, plan_rank  # noqa: E402
from cpm_lineage.actions.pipeline_config import (  # noqa: E402
    EntityState, PipelineConfig, RunCheck, SegmentStat, reason_code,
)
from cpm_lineage.actions.scoring import (  # noqa: E402
    apply_marginal, confidence, expected_value, marginal_roas, marginal_roas_by_entity, score_cards,
)
from cpm_lineage.adapters.synthetic import synthetic_profile  # noqa: E402
from cpm_lineage.config import Economics  # noqa: E402


# ---------------------------------------------------------------- confidence

def test_confidence_is_the_sum_of_its_parts():
    c = budget("c1", "up", roas=4.0, spend=60000.0)
    assert confidence(c, ctx()) == 40 + 15 + 3 + 15 == c.confidence
    assert dict(c.conf_parts) == {"30-day evidence window": 15.0, "spend depth": 3.0,
                                  "effect clarity: far from breakeven": 15.0}
    assert confidence(budget("c1", "up"), ctx(lookback_days=29)) == 40 + 8 + 3 + 15


@pytest.mark.parametrize("roas,pts", [(1.5, 15), (1.49, 8), (1.25, 8), (1.24, 3), (0.5, 15), (0.76, 3)])
def test_effect_clarity_bands(roas, pts):
    assert confidence(budget("c1", "down", roas=roas, spend=0.0), ctx()) == 40 + 15 + pts


def test_effect_clarity_rebases_on_economics():
    econ = Economics(breakeven=2.0)
    assert confidence(budget("c1", "up", roas=2.1, spend=0.0), ctx(economics=econ)) == 40 + 15 + 3
    assert confidence(budget("c1", "up", roas=2.1, spend=0.0), ctx()) == 40 + 15 + 15


@pytest.mark.parametrize("family,type_,direction,pts", [
    ("efficiency-cut", "keyword", "down", 12), ("bid", "bid", "up", 8), ("experiment", "experiment", "none", 5),
    ("geo", "geo", "none", 10), ("collision", "keyword", "down", 12), ("collision", "watch", "none", 12),
    ("defense", "watch", "none", 4), ("growth", "keyword", "none", 0),
])
def test_type_constants(family, type_, direction, pts):
    c = card(family, type_, "c1", direction)
    c.segment = None
    assert confidence(c, ctx()) == 40 + 15 + pts


def test_spend_depth_is_currency_scaled_and_capped():
    assert confidence(card("bid", "bid", "c1", spend=19999.0), ctx()) == 63
    assert confidence(card("bid", "bid", "c1", spend=20000.0), ctx()) == 64
    assert confidence(card("bid", "bid", "c1", spend=9e9), ctx()) == 73
    assert confidence(card("bid", "bid", "c1", spend=200.0), ctx(economics=Economics(currency_unit=0.01))) == 64


def test_availability_penalties_and_profile_switch():
    def run(avail, direction="up", **kw):
        return confidence(card("bid", "bid", "c1", direction), ctx(segments={"seg": SegmentStat(availability=avail)}, **kw))

    assert run(0.95) == 63
    assert run(0.85) == 63 and run(0.84) == 58 and run(0.75) == 58 and run(0.74) == 55
    assert run(None, "up") == 55 and run(None, "down") == 63
    assert run(0.10, rule_profile="verdict") == 63 and run(None, rule_profile="verdict") == 63


def test_young_winrate_and_precedent():
    def run(**kw):
        c = card("bid", "bid", "c1")
        return confidence(c, ctx(**kw)), c

    young = {"c1": EntityState("c1", live_days=13)}
    assert run(entities=young)[0] == 53
    assert run(entities={"c1": EntityState("c1", live_days=14)})[0] == 63
    assert run(entities={"c1": EntityState("c1")}, telemetry={})[0] == 63       # unknown age: no penalty
    assert run(winrates={"bid": (0.9, 5)})[0] == 71
    assert run(winrates={"bid": (0.9, 4)})[0] == 63
    assert run(winrates={"bid": (0.0, 50)})[0] == 53
    score, c = run(precedent={("bid", "c1"): {"n": 4, "won": 4, "line": "x"}})
    assert score == 71 and c.precedent["n"] == 4
    assert run(precedent={("bid", "c1"): {"n": 4, "won": 0}})[0] == 55
    assert run(precedent={("bid", "c2"): {"n": 4, "won": 4}})[0] == 63


def test_confidence_is_bounded_with_components():
    rng = random.Random(5)
    for _ in range(300):
        c = budget("c1", rng.choice(["up", "down"]), roas=rng.choice([None, 0.1, 1.0, 9.0]), spend=rng.choice([0, 1e7]))
        k = ctx(lookback_days=rng.choice([7, 30]), winrates={c.family: (rng.random(), rng.randint(0, 9))},
                precedent={(c.family, "c1"): {"n": rng.randint(0, 3), "won": rng.randint(0, 1)}},
                entities={"c1": EntityState("c1", live_days=rng.choice([3, 90]))},
                segments={"seg": SegmentStat(availability=rng.choice([None, 0.2, 0.8, 0.99]))})
        assert 25 <= confidence(c, k) <= 90 and c.conf_parts
    low = card("growth", "keyword", "c1", "up")
    assert confidence(low, ctx(lookback_days=7, segments={}, entities={"c1": EntityState("c1", live_days=1)},
                               winrates={"growth": (0.0, 9)}, precedent={("growth", "c1"): {"n": 2, "won": 0}})) == 25


# ---------------------------------------------------------------- expected value

def test_ev_formulas():
    up = budget("c1", "up", frm=1000, pct=20, roas=4.0)
    assert expected_value(up, ctx()) == pytest.approx(200 * 4.0 * 0.5)
    assert (up.est_day, up.p_success, up.ev_day, up.ev_kind, up.ev_density) == (800, 0.5, 400, "money", 2.0)
    assert up.estimator == "average_roas"
    down = budget("c1", "down", frm=1000, pct=20, roas=0.4)
    assert expected_value(down, ctx(winrates={"efficiency-cut": (0.8, 5)})) == pytest.approx(200 * 0.6 * 0.8)
    assert (down.est_day, down.p_success, down.ev_day, down.ev_density) == (120, 0.8, 96, 0.48)
    assert expected_value(budget("c1", "down", roas=0.4), ctx(winrates={"efficiency-cut": (0.8, 4)})) == pytest.approx(60)
    assert expected_value(budget("c1", "down", roas=1.7), ctx()) == 0.0          # no bleed above 1


def test_ev_bleed_uses_literal_one_unless_configured():
    econ = Economics(breakeven=2.0)
    assert expected_value(budget("c1", "down", roas=1.5), ctx(economics=econ)) == 0.0
    k = ctx(economics=econ, cfg=PipelineConfig(holdout_pct=0, ev_use_effective_breakeven=True))
    assert expected_value(budget("c1", "down", roas=1.5), k) == pytest.approx(200 * 0.5 * 0.5)


def test_ev_prefers_marginal_then_leak_then_stake():
    m = budget("c1", "up", roas=4.0, mroas=1.0)
    assert expected_value(m, ctx()) == pytest.approx(100.0) and m.estimator == "marginal_fd"
    kw = card("efficiency-cut", "keyword", "c1", "down", keywords=["k"], stake=99999, leak=3000.0)
    assert expected_value(kw, ctx()) == pytest.approx(3000 / 30 * 0.5)
    assert (kw.ev_kind, kw.estimator, kw.ev_density) == ("money", "leak", None)
    opp = card("sov-gap", "watch", None, stake=900000)
    assert expected_value(opp, ctx(lookback_days=0)) == pytest.approx(450000)
    assert (opp.ev_kind, opp.estimator) == ("opportunity", "stake")
    unsized = card("growth", "budget", "c1", "up", stake=300, roas=3.0)
    expected_value(unsized, ctx())
    assert unsized.ev_kind == "opportunity" and unsized.ev_day == 5


def test_opportunity_ev_sorts_behind_money():
    money, opp = budget("c1", "up"), card("sov-gap", "watch", None, stake=9e9, title="opp")
    score_cards([money, opp], ctx())
    assert opp.ev_day > money.ev_day
    assert sorted([opp, money], key=plan_rank)[0] is money
    # goal bias reorders only: status still dominates
    cut, scale = budget("c2", "down", roas=0.5, title="cut"), budget("c1", "up", title="scale")
    score_cards([cut, scale], ctx())
    assert sorted([cut, scale], key=plan_rank)[0] is scale
    assert sorted([cut, scale], key=lambda c: plan_rank(c, "roas"))[0] is cut
    cut.status = "vetoed"
    assert sorted([cut, scale], key=lambda c: plan_rank(c, "roas"))[0] is scale
    cut.status = "something-else"
    assert plan_rank(cut)[0] == 3


# ---------------------------------------------------------------- guardrail

def test_roas_floor_fence():
    assert roas_fence(RunCheck("roas", ">=", 200, "pct")) == 2.0
    assert roas_fence({"metric": "direct_roas", "op": ">", "value": 2.5}) == 2.5
    assert roas_fence(RunCheck("ad_spend", "<=", 5)) is None

    def run(check, **kw):
        cards = [budget("c1", "up", roas=1.99, title="low"), budget("c2", "up", roas=2.0, title="at"),
                 budget("c3", "up", roas=0.5, family="defense", title="def"),
                 budget("c3", "down", roas=0.5, title="cut"), budget("c4", "up", roas=None, title="unknown"),
                 card("bid", "bid", "c1", "up", keywords=["k"], title="bid", roas=0.3),
                 budget("c5", "up", roas=1.0, title="judged", threshold=0.8)]
        cards[1].status = "tension"
        res = apply_guardrail(cards, check, ctx(**kw))
        return res, {c.title for c in cards if reason_code(c) == "guardrail"}

    res, vetoed = run(RunCheck("roas", ">=", 200, "pct"))
    assert vetoed == {"low", "bid", "judged"} and res.vetoed == 3 and res.enforced == "roas_floor"
    assert "enforced on scale-ups" in res.note
    _, vetoed = run(RunCheck("roas", ">=", 2.01))
    assert "at" in vetoed                                    # tension cards are fenced too
    _, vetoed = run(RunCheck("roas", ">=", 2.0), rule_profile="verdict")
    assert vetoed == {"low"}                                 # bar-judged cards are not judged twice


def test_guardrail_disclosures():
    cards = [budget("c1", "up", roas=0.1)]
    assert apply_guardrail(cards, None, ctx()).note == ""
    assert apply_guardrail(cards, RunCheck(), ctx()).enforced == "none"
    r = apply_guardrail(cards, RunCheck("roas", ">="), ctx())
    assert r.enforced == "recorded" and "recorded only" in r.note
    r = apply_guardrail(cards, RunCheck("ad_sov", ">=", 30), ctx())
    assert r.enforced == "not_enforced" and "NOT machine-enforced" in r.note
    r = apply_guardrail(cards, RunCheck("roas", "<=", 3), ctx())
    assert r.enforced == "not_enforced"
    assert cards[0].status == "pending"


def _ceiling_cards():
    strong = budget("c1", "up", roas=4.0, title="strong")       # +200/day, density 2.0
    weak = budget("c2", "up", roas=2.0, title="weak")           # +200/day, density 1.0
    return strong, weak


def test_spend_ceiling_drops_the_weakest_raise_first():
    k = ctx()
    assert spend_base(k) == 3000.0
    strong, weak = _ceiling_cards()
    score_cards([strong, weak], k)
    res = apply_guardrail([strong, weak], RunCheck("ad_spend", "<=", 3250), k)
    assert (strong.status, weak.status, res.vetoed, res.enforced) == ("pending", "vetoed", 1, "spend_ceiling")
    strong, weak = _ceiling_cards()
    score_cards([weak, strong], k)
    apply_guardrail([weak, strong], RunCheck("ad_spend", "<=", 3400), k)
    assert strong.status == weak.status == "pending"            # exactly at the cap fits
    strong, weak = _ceiling_cards()
    score_cards([strong, weak], k)
    apply_guardrail([strong, weak], RunCheck("ad_spend", "<=", 100, "pct"), k)
    assert strong.status == weak.status == "vetoed"


def test_spend_ceiling_nets_concurrent_cuts_and_survives_unscored_cards():
    k = ctx()
    strong, weak = _ceiling_cards()
    cut = budget("c3", "down", roas=0.5, title="cut")           # -200/day
    unstated = card("growth", "budget", "c3", "up", title="unstated")
    cards = [strong, weak, cut, unstated]                       # not scored: must not crash on ordering
    apply_guardrail(cards, RunCheck("ad_spend", "<=", 107, "pct"), k)
    assert [c.status for c in cards] == ["pending"] * 4         # 3000 + 200 <= 3210
    cut.status = "vetoed"
    res = apply_guardrail(cards, RunCheck("ad_spend", "<=", 107, "pct"), k)
    assert res.vetoed == 1
    assert spend_base(ctx(telemetry={})) == 1.0
    assert spend_base(ctx(scope_entity_ids=frozenset({"c1"}))) == 1000.0


# ---------------------------------------------------------------- equity

def _equity_ctx(own_share=0.4, total=(1000.0, 1000.0), own=(290.0, 300.0), **kw):
    k = ctx(segments={"seg": SegmentStat(availability=0.95, total_sales=total[0], total_sales_prior=total[1],
                                         own_sales=own[0], own_sales_prior=own[1], sov=20.0, own_spend=30000.0)}, **kw)
    k.entities = {"c1": EntityState("c1", status="active", own_brand_share=own_share, live_days=60),
                  "c2": EntityState("c2", status="active", own_brand_share=0.1, live_days=60)}
    return k


def test_equity_gate_fires_on_brand_carriers_in_a_share_losing_segment():
    def run(k):
        cards = [budget("c1", "down", title="cut"), card("efficiency-cut", "keyword", "c1", "down", keywords=["k"], title="kw"),
                 budget("c2", "down", title="generic"), card("bid", "bid", "c1", "down", keywords=["k"], title="bid")]
        cards.append(budget("c1", "up", family="defense", title="up"))
        return equity_gate(cards, k), {c.title for c in cards if reason_code(c) == "equity_gate"}

    assert run(_equity_ctx()) == (2, {"cut", "kw"})
    assert run(_equity_ctx(own_share=0.39))[0] == 0
    assert run(_equity_ctx(own=(299.0, 300.0)))[0] == 0                       # -0.1pp: not losing share
    assert run(_equity_ctx(own=(297.0, 300.0)))[0] == 2                       # -0.3pp
    assert run(_equity_ctx(total=(800.0, 1000.0), own=(200.0, 300.0)))[0] == 0  # freefall: gate stands down
    assert run(_equity_ctx(total=(905.0, 1000.0), own=(250.0, 300.0)))[0] == 2  # -9.5% is not freefall
    assert run(ctx())[0] == 0                                                 # no segment totals: fail open


def test_equity_gate_counts_in_telemetry():
    res = plan_actions([budget("c1", "down", roas=0.5)], _equity_ctx())
    assert res.telemetry["gates"]["equity_gate"] == 1 and res.cards[0].status == "vetoed"


def test_sov_headroom_proxy():
    k = _equity_ctx()
    up = budget("c1", "up")                                      # +200 on own daily 1000 -> x = 0.2
    h = sov_headroom(up, k)
    assert h == {"dsov_pp": round(20 * 0.8 * 0.2, 2), "bucket": "brand bucket protected", "own_share": 40}
    assert sov_headroom(budget("c1", "down"), k)["bucket"] == "brand bucket at risk"
    assert sov_headroom(budget("c1", "down"), k)["dsov_pp"] == -3.2
    assert sov_headroom(budget("c2", "up"), k)["bucket"] == "generic/conquest bucket"
    assert sov_headroom(card("bid", "bid", "c1", "up"), k) is None
    assert sov_headroom(up, ctx()) is None
    cards = [up, card("bid", "bid", "c1")]
    annotate_equity(cards, k)
    assert cards[0].sov_headroom and cards[1].sov_headroom is None and cards[0].status == "pending"


# ---------------------------------------------------------------- marginal sizing

def test_marginal_roas_finite_difference():
    pts = [(100.0 * i, 200.0 * i) for i in range(1, 9)]
    assert marginal_roas(pts) == pytest.approx(2.0)
    assert marginal_roas(pts[:7]) is None                        # fewer than 8 spending days
    assert marginal_roas(pts + [(0.0, 50.0)] * 5) == pytest.approx(2.0)   # zero-spend days ignored
    assert marginal_roas([(100.0, 1.0)] * 4 + [(200.0, 9.0)] * 4) is None  # gap must exceed 100
    assert marginal_roas([(100.0, 1.0)] * 4 + [(200.0, 9.0)] * 4, min_spend_gap=99.0) == pytest.approx(0.08)
    assert marginal_roas([(100.0 * i, 1000.0 - 50 * i) for i in range(1, 9)]) == 0.0   # floored at zero


def test_marginal_roas_by_entity_sums_to_entity_day(make_perf_row):
    rows = []
    for i in range(10):
        rows += [make_perf_row(day=i, entity_id="a", spend=100.0 * (i + 1), sales=150.0 * (i + 1))] * 2
        rows.append(make_perf_row(day=i, entity_id="b", spend=5.0, sales=50.0))
    out = marginal_roas_by_entity(rows, ctx())
    assert out == {"a": pytest.approx(1.5)}
    assert marginal_roas_by_entity(rows, ctx(economics=Economics(currency_unit=100.0))) == {}


@pytest.mark.parametrize("m,status,pct,to", [
    (0.79, "vetoed", 20, 1200.0), (0.8, "pending", 10, 1100.0), (1.49, "pending", 10, 1100.0),
    (1.5, "pending", 20, 1200.0),
])
def test_marginal_gate_on_raises(m, status, pct, to):
    c = budget("c1", "up", pct=20)
    k = ctx()
    score_cards([c], k)
    n = apply_marginal([c], {"c1": m}, k)
    assert (c.status, c.change.pct, c.change.to) == (status, pct, pytest.approx(to))
    assert c.evidence["mroas"] == m and n == (1 if status == "vetoed" else 0)
    assert c.estimator == "marginal_fd"
    if status == "pending":                                      # EV is recomputed on the resized step
        assert c.est_day == round((to - 1000.0) * m)
    if pct == 10:
        assert c.change.pct_requested == 20 and "halved" in c.trigger


def test_marginal_resizing_details():
    k = ctx()
    small = budget("c1", "up", pct=8)
    apply_marginal([small], {"c1": 1.0}, k)
    assert small.change.pct == 5                                  # floor of a halved raise
    deep, shallow, already = budget("c1", "down", pct=20), budget("c2", "down", pct=20), budget("c3", "down", pct=40)
    apply_marginal([deep, shallow, already], {"c1": 0.49, "c2": 0.5, "c3": 0.1}, k)
    assert (deep.change.pct, deep.change.to) == (-25, pytest.approx(750.0))
    assert shallow.change.pct == -20 and already.change.pct == -45
    defense = budget("c1", "up", family="defense")
    vetoed, nodata = budget("c2", "up"), budget("c3", "up")
    vetoed.status = "vetoed"
    apply_marginal([defense, vetoed, nodata], {"c1": 0.1, "c2": 0.1}, k)
    assert "mroas" not in defense.evidence and "mroas" not in vetoed.evidence and nodata.status == "pending"
    rebased = budget("c1", "up")
    apply_marginal([rebased], {"c1": 1.5}, ctx(economics=Economics(breakeven=2.0)))
    assert rebased.status == "vetoed" and reason_code(rebased) == "marginal_gate"


def test_marginal_reading_is_deflated_for_retargeting():
    k = ctx(profile=synthetic_profile(), telemetry={"c1": bound("c1", ad_type="recommendation"),
                                                    "c2": bound("c2", ad_type="keyword_listing")})
    a, b = budget("c1", "up"), budget("c2", "up")
    apply_marginal([a, b], {"c1": 1.2, "c2": 1.2}, k)
    assert a.evidence["mroas"] == 0.72 and a.status == "vetoed"
    assert b.evidence["mroas"] == 1.2 and b.status == "pending"
