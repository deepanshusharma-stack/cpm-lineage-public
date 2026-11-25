from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import KW, REACH, RECO, SEG, SHELF, camp, ctx, kw, tel  # noqa: E402

from cpm_lineage.actions.config import ActionsConfig  # noqa: E402
from cpm_lineage.actions.primitives import (RunCheck, apply_playbook, bucket_of, budget_bound,  # noqa: E402
                                            campaign_role, check_roas, cooldown_register,
                                            decision_roas, keyword_bench, keyword_demand,
                                            keyword_threshold, live_campaigns, live_keywords,
                                            parse_floors, raise_pct_from_exhaust,
                                            raise_pct_from_sufficiency, roas_floor)
from cpm_lineage.config import Economics  # noqa: E402
from cpm_lineage.schema import AppliedAction, make_card  # noqa: E402


def test_economics_rebase_and_brand_floor():
    c = ctx()
    assert c.be == 1.0
    assert roas_floor(c, "generic") == 1.0
    assert roas_floor(c, "own_brand") == 3.0 and roas_floor(c, "sibling_brand") == 3.0
    c2 = ctx(economics=Economics(breakeven=2.0, ltv_mult=4.0))
    assert c2.be == pytest.approx(0.5)
    assert roas_floor(c2, "competitor") == pytest.approx(0.5)
    assert ctx(economics=Economics(breakeven=0.0)).be == pytest.approx(0.1)


def test_check_pct_mode_is_a_multiple():
    assert check_roas(RunCheck("roas", ">=", 200, "pct")) == 2.0
    assert check_roas(RunCheck("roas", ">=", 2.5, "abs")) == 2.5
    assert check_roas(RunCheck("ad_spend", "<=", 5, "abs")) is None
    assert check_roas(None) is None
    assert roas_floor(ctx(check=RunCheck("roas", ">=", 200, "pct")), "own_brand") == 6.0


def test_buckets():
    assert bucket_of("own_brand") == bucket_of("sibling_brand") == "branded"
    assert bucket_of("competitor") == "competition"
    assert bucket_of(None) == bucket_of("generic") == bucket_of("something") == "generic"


def test_keyword_threshold_rules():
    v = ctx(rule_profile="verdict")
    assert keyword_threshold(v, "generic", 3.0) == 3.0          # no check: the bucket average is the bar
    assert keyword_threshold(v, "generic", 0.6) == 1.0          # breakeven is a hard floor
    assert keyword_threshold(v, "generic", None) == 1.0
    chk = ctx(rule_profile="verdict", check=RunCheck("roas", ">=", 2.0))
    assert keyword_threshold(chk, "own_brand", 5.0) == 2.0      # capped by the check, no brand multiplier
    assert keyword_threshold(chk, "generic", 1.5) == 1.5
    fl = ctx(rule_profile="verdict", operator_floors={"generic": 4.0})
    assert keyword_threshold(fl, "generic", 2.0) == 4.0         # stated floor overrides the bench
    assert keyword_threshold(fl, "competitor", 2.0) == 2.0
    both = ctx(rule_profile="verdict", operator_floors={"generic": 4.0}, check=RunCheck("roas", ">=", 3.0))
    assert keyword_threshold(both, "generic", 2.0) == 3.0       # still capped by the check


def test_parse_floors():
    assert parse_floors("branded 4x, generic 3, comp: 2.5") == {"branded": 4.0, "generic": 3.0,
                                                                "competition": 2.5}
    assert parse_floors("generic 500") == {}
    assert parse_floors("generic 0.05x") == {}
    assert parse_floors("") == {} and parse_floors(None) == {}
    assert parse_floors("keep generic terms above 2.2x please") == {"generic": 2.2}


def test_bench_is_spend_weighted_and_scoped_floor():
    rows = [kw("a", spend=10_000, roas=1.0), kw("b", spend=30_000, roas=3.0),
            kw("c", spend=5_000, roas=None), kw("r", spend=10_000, roas=0.5, role="competitor")]
    c = ctx([camp()], keywords=rows)
    bench = keyword_bench(c)
    assert bench["generic"] == (pytest.approx(2.5), 40_000)
    assert bench["competition"][0] == 0.5
    assert bench["_all"][0] == pytest.approx((10_000 + 90_000 + 5_000) / 50_000)
    scoped = ctx([camp()], keywords=rows, rule_profile="verdict", scoped=True)
    assert keyword_bench(scoped) == {}
    many = [kw(f"k{i}", spend=1_000, roas=2.0) for i in range(12)]
    assert "generic" in keyword_bench(ctx([camp()], keywords=many, rule_profile="verdict", scoped=True))
    assert "generic" not in keyword_bench(ctx([camp()], keywords=many[:11], rule_profile="verdict", scoped=True))
    assert "generic" in keyword_bench(ctx([camp()], keywords=many[:11], scoped=True))  # classic: no floor


def test_campaign_role_majority():
    c = ctx([camp()], keywords=[kw("a", spend=30_000, role="own_brand"), kw("b", spend=10_000)])
    assert campaign_role(c, "c1") == ("own_brand", pytest.approx(0.75))
    assert campaign_role(c, "zz") == (None, 0.0)


def test_keyword_demand_flags():
    rows = [kw(f"k{i}", spend=1_000, nu=float(i), vol=float(100 * i)) for i in range(8)]
    rows += [kw("brand a", role="own_brand", vol=50.0)]
    d = keyword_demand(ctx([camp()], keywords=rows, rule_profile="verdict"))
    assert d.new_user_top == {("k7", SEG), ("k6", SEG)}            # ceil(8 * 0.25) = 2
    assert d.volume_top == {("k7", SEG), ("brand a", SEG)}         # ceil(8 * 0.10) = 1 per bucket
    assert d.priority[(SEG, "generic")] == 0 and d.priority[(SEG, "branded")] == 1
    classic = keyword_demand(ctx([camp()], keywords=rows))
    assert not classic.new_user_top and not classic.volume_top


def test_budget_bound_classic_thresholds():
    c = ctx()
    assert budget_bound(c, tel(exh=719))
    assert not budget_bound(c, tel(exh=720))                        # exhaust after 12 h
    assert not budget_bound(c, tel(exh=None))
    assert budget_bound(c, tel(bud=1000, spd=500))
    assert not budget_bound(c, tel(bud=1000, spd=499))              # exhaustion not credible
    assert budget_bound(c, tel(nd=20, exh_d=8))
    assert not budget_bound(c, tel(nd=20, exh_d=7))                 # under 40% of days
    assert not budget_bound(c, tel(bud=None))                       # missing cap
    assert not budget_bound(c, None)
    assert budget_bound(c, tel(suff=1.0))                           # classic ignores sufficiency


def test_budget_bound_verdict_is_sufficiency():
    v = ctx(rule_profile="verdict")
    assert budget_bound(v, tel(suff=0.89))
    assert not budget_bound(v, tel(suff=0.90))
    assert not budget_bound(v, tel(suff=1.0, exh=300))
    assert budget_bound(v, tel(suff=None, exh=300))                 # no served-time data: exhaust fallback


def test_raise_sizing():
    c = ctx()
    assert raise_pct_from_exhaust(c, 660) == 50                     # 11h -> 100*(11/11)*0.9 = 90 -> clamp
    assert raise_pct_from_exhaust(c, 18 * 60) == 20                 # 100*(4/18)*0.9 = 20
    assert raise_pct_from_exhaust(c, 22 * 60) == 5
    assert raise_pct_from_sufficiency(c, 0.8) == 22                 # (1/0.8 - 1) * 90 = 22.5 -> 22
    assert raise_pct_from_sufficiency(c, 0.01) == 50
    assert raise_pct_from_sufficiency(c, None) == 5


def test_liveness_single_rule():
    cs = [camp("held-spending", status="BUDGET_HELD", s3=500.0),
          camp("held-silent", status="BUDGET_HELD", s3=0.0),
          camp("paused", status="PAUSED", s3=900.0),
          camp("mixed", pure=False), camp("ok")]
    c = ctx(cs, keywords=[kw("x", eid="paused"), kw("y", eid="ok"), kw("z", eid="ghost")])
    assert [x.entity_id for x in live_campaigns(c, SEG)] == ["held-spending", "ok"]
    assert [k.keyword for k in live_keywords(c)] == ["y"]


def test_decision_roas_deflates_and_abstains():
    c = ctx()
    assert decision_roas(c, camp(roas=6.0, ad_type=RECO)) == pytest.approx(3.6)
    assert decision_roas(c, camp(roas=None, direct=2.0)) == 2.0
    assert decision_roas(c, camp(roas=None)) is None
    assert decision_roas(c, camp(roas=4.0, ad_type=REACH)) is None


def test_playbook_veto():
    cs = [camp("kw", ad_type=KW), camp("reco", ad_type=RECO), camp("shelf", ad_type=SHELF),
          camp("reach", ad_type=REACH)]
    c = ctx(cs)
    cards = [make_card("bid", t, "t", entity_id=e)
             for e in ("kw", "reco", "shelf", "reach") for t in ("budget", "bid", "keyword", "watch")]
    apply_playbook(cards, c)
    ok = {(x.entity_id, x.type) for x in cards if x.status == "pending"}
    assert ok == {("kw", "budget"), ("kw", "bid"), ("kw", "keyword"), ("kw", "watch"),
                  ("reco", "budget"), ("reco", "watch"), ("shelf", "budget"), ("shelf", "watch"),
                  ("reach", "watch")}
    assert all(x.veto_reason for x in cards if x.status == "vetoed")
    free = apply_playbook([make_card("geo", "geo", "t")], c)[0]
    assert free.status == "pending"


def test_cooldown_register_windows():
    today = date(2026, 3, 31)
    acts = [AppliedAction("cut", "a", today - timedelta(days=6)),
            AppliedAction("cut", "b", today - timedelta(days=7)),
            AppliedAction("defend", "a", today - timedelta(days=13)),
            AppliedAction("defend", "b", today - timedelta(days=14)),
            AppliedAction("biddown", "a", today - timedelta(days=13)),
            AppliedAction("other", "a", today), AppliedAction("cut", None, today)]
    assert cooldown_register(acts, today) == {("cut", "a"), ("defend", "a"), ("biddown", "a")}
    assert ("cut", "b") in cooldown_register(acts, today, ActionsConfig(cut_cooldown_days=10))


def test_card_identity_includes_keywords():
    a = make_card("bid", "bid", "Increase Bid", entity_id="c1", segment=SEG, keywords=["alpha"])
    b = make_card("bid", "bid", "Increase Bid", entity_id="c1", segment=SEG, keywords=["beta"])
    a2 = make_card("bid", "bid", "Increase Bid", entity_id="c1", segment=SEG, keywords=["alpha"])
    assert a.id != b.id and a.id == a2.id and len(a.id) == 12


def test_unknown_rule_profile_rejected():
    with pytest.raises(ValueError):
        ctx(rule_profile="v9")
    assert not ctx(rule_profile="marginal").verdict
