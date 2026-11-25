from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import KW, REACH, RECO, SEG, camp, ctx, kw, none, one, tel  # noqa: E402

from cpm_lineage.actions.families import efficiency, run_families, verdict_scoreboard  # noqa: E402
from cpm_lineage.config import Economics  # noqa: E402

CUT, SCALE = "efficiency-cut", "efficiency-scale"


# ---------------------------------------------------------------- classic cut

def test_classic_cut_fires_with_stake_and_change():
    cards = efficiency(ctx([camp(roas=0.5, spend=40_000)], telemetry=[tel(bud=2000, spd=1000, exh=None)]))
    c = one(cards, family=CUT, type="budget")
    assert c.direction == "down" and c.change.pct == -20 and c.change.frm == 2000
    assert c.change.to == pytest.approx(1600)
    assert c.stake == pytest.approx(40_000 * (1 - 0.5))
    assert c.status == "pending" and c.rule_profile == "classic"
    assert c.evidence["util"] == pytest.approx(50.0)


def test_classic_cut_thresholds():
    assert not efficiency(ctx([camp(roas=0.9, spend=40_000)]))          # needs r < 0.9 x be
    assert efficiency(ctx([camp(roas=0.899, spend=40_000)]))
    assert not efficiency(ctx([camp(roas=0.5, spend=24_999)]))          # spend floor
    assert efficiency(ctx([camp(roas=0.5, spend=25_000)]))
    assert not efficiency(ctx([camp(roas=0.5, spend=40_000, s7=0.0)]))  # not running any more
    assert not efficiency(ctx([camp(roas=None, spend=40_000)]))         # no attribution: cannot judge


def test_classic_cut_rebased_by_breakeven():
    c = ctx([camp(roas=1.5, spend=40_000)], be=2.0)
    card = one(efficiency(c), family=CUT)
    assert card.stake == pytest.approx(40_000 * (2.0 - 1.5) / 2.0)
    assert not efficiency(ctx([camp(roas=1.5, spend=40_000)]))


def test_cut_cooldown_and_recent_half_confirmation():
    base = [camp(roas=0.5, spend=40_000)]
    assert not efficiency(ctx(base, recent_applied=frozenset({("cut", "c1")})))
    assert efficiency(ctx(base, recent_applied=frozenset({("cut", "other")})))
    assert not efficiency(ctx(base, telemetry=[tel(recent=1.0, exh=None)]))   # already fixed mid-window
    assert efficiency(ctx(base, telemetry=[tel(recent=0.99, exh=None)]))


def test_cut_certainty_gate():
    base = [camp(roas=0.5, spend=40_000)]
    assert not efficiency(ctx(base, telemetry=[tel(hi=1.0, exh=None)]))       # optimistic bound breaks even
    assert efficiency(ctx(base, telemetry=[tel(hi=0.99, exh=None)]))
    assert efficiency(ctx(base, telemetry=[tel(hi=None, exh=None)]))          # no interval: gate not applied
    # deflated bound: 1.5 raw x 0.6 = 0.9 < be -> still a cut
    assert efficiency(ctx([camp(roas=0.5, spend=40_000, ad_type=RECO)], telemetry=[tel(hi=1.5, exh=None)]))


def test_currency_unit_scales_money_thresholds():
    small = Economics(currency_unit=0.01)
    assert efficiency(ctx([camp(roas=0.5, spend=250.0)], economics=small))
    assert not efficiency(ctx([camp(roas=0.5, spend=249.0)], economics=small))


# -------------------------------------------------------------- classic scale

def test_classic_scale_size_and_stake():
    c = ctx([camp(roas=4.0, spend=50_000)], telemetry=[tel(bud=1000, exh=18 * 60, im=900_000)])
    assert not efficiency(c)                                                  # exhaust after 12 h
    c = ctx([camp(roas=4.0, spend=50_000)], telemetry=[tel(bud=1000, exh=600, im=900_000)])
    card = one(efficiency(c), family=SCALE, type="budget")
    assert card.direction == "up" and card.change.pct == 50                   # 100*(12/10)*0.9 -> clamp 50
    assert card.change.to == pytest.approx(1500)
    assert card.stake == pytest.approx(50_000 * (4.0 - 1.0) * 0.5)
    assert card.evidence["oob_h"] == pytest.approx(14.0)
    assert card.evidence["missed_impr"] == pytest.approx(900_000 * 840 / 600)


def test_classic_scale_band_and_stake_cap():
    t = [tel(exh=600)]
    assert not efficiency(ctx([camp(roas=2.99, spend=50_000)], telemetry=t))
    assert efficiency(ctx([camp(roas=3.0, spend=50_000)], telemetry=t))
    assert not efficiency(ctx([camp(roas=3.0, spend=9_999)], telemetry=t))
    card = one(efficiency(ctx([camp(roas=11.0, spend=50_000)], telemetry=t)), family=SCALE)
    assert card.stake == pytest.approx(50_000 * (6.0 - 1.0) * 0.5)            # ROAS capped at 6 x be
    # >= 12x: ceiling holds unless own-brand share is known and low
    assert not efficiency(ctx([camp(roas=12.0, spend=50_000)], telemetry=t))
    assert not efficiency(ctx([camp(roas=12.0, spend=50_000, own=0.5)], telemetry=t))
    assert efficiency(ctx([camp(roas=12.0, spend=50_000, own=0.49)], telemetry=t))


def test_scale_certainty_and_binding_gates():
    good = [camp(roas=4.0, spend=50_000)]
    assert not efficiency(ctx(good, telemetry=[tel(exh=600, lo=1.99)]))       # one lucky week
    assert efficiency(ctx(good, telemetry=[tel(exh=600, lo=2.0)]))
    assert not efficiency(ctx(good, telemetry=[tel(exh=600, bud=1000, spd=400)]))   # low utilisation
    assert not efficiency(ctx(good, telemetry=[tel(exh=600, nd=20, exh_d=7)]))      # < 40% of days
    assert not efficiency(ctx(good, telemetry=[tel(exh=600, bud=None)]))            # missing cap
    assert not efficiency(ctx(good))                                                # no telemetry


def test_deflation_reaches_the_scale_decision():
    t = [tel(exh=600)]
    card = one(efficiency(ctx([camp(roas=6.0, spend=50_000, ad_type=RECO)], telemetry=t)), family=SCALE)
    assert card.evidence["roas"] == pytest.approx(3.6)
    assert not efficiency(ctx([camp(roas=4.0, spend=50_000, ad_type=RECO)], telemetry=t))   # 2.4 deflated
    assert not efficiency(ctx([camp(roas=6.0, spend=50_000, ad_type=RECO)],
                              telemetry=[tel(exh=600, lo=3.0)]))                             # 1.8 deflated bound


def test_spending_budget_held_campaign_stays_actionable_and_dead_get_nothing():
    t = [tel(exh=600)]
    assert efficiency(ctx([camp(roas=4.0, spend=50_000, status="BUDGET_HELD", s3=100.0)], telemetry=t))
    assert not efficiency(ctx([camp(roas=4.0, spend=50_000, status="BUDGET_HELD", s3=0.0)], telemetry=t))
    dead = camp(roas=0.2, spend=90_000, status="PAUSED")
    rows = [kw("rival x", spend=60_000, roas=0.1, role="competitor")]
    for prof in ("classic", "verdict"):
        assert run_families(ctx([dead], keywords=rows, telemetry=t, rule_profile=prof)).cards == []


def test_healthy_portfolio_emits_nothing():
    cs = [camp("a", roas=2.2, spend=80_000), camp("b", roas=1.6, spend=60_000),
          camp("r", roas=3.0, spend=50_000, ad_type=RECO), camp("f", roas=None, spend=70_000, ad_type=REACH)]
    rows = [kw("alpha", "a", 40_000, 2.2), kw("beta", "a", 30_000, 2.3), kw("gamma", "b", 30_000, 1.6)]
    t = [tel("a", exh=None, roas=2.0), tel("b", exh=None, roas=1.5)]
    assert run_families(ctx(cs, keywords=rows, telemetry=t)).cards == []


def test_determinism():
    def build():
        cs = [camp("a", roas=0.5, spend=80_000), camp("b", roas=4.0, spend=60_000)]
        rows = [kw("alpha", "a", 40_000, 0.2, role="competitor"), kw("beta", "b", 30_000, 4.0)]
        return ctx(cs, keywords=rows, telemetry=[tel("b", exh=500)], rule_profile="verdict")
    a, b = run_families(build()), run_families(build())
    assert [(c.id, c.status) for c in a.cards] == [(c.id, c.status) for c in b.cards]
    assert a.cards and a.board == b.board


# -------------------------------------------------------------- keyword pause

def test_classic_pause_rules():
    c = ctx([camp(roas=2.0)], keywords=[
        kw("rival a", spend=10_000, roas=0.9, role="competitor"),      # < min(fl, be), spend at floor
        kw("rival b", spend=9_999, roas=0.1, role="competitor"),       # under conquest spend floor
        kw("gen a", spend=25_000, roas=0.35),                          # <= 0.35 x floor
        kw("gen b", spend=25_000, roas=0.36),
        kw("gen c", spend=24_999, roas=0.1),
        kw("brand a", spend=25_000, roas=1.04, role="own_brand"),      # brand floor 3x -> 1.05 bar
        kw("brand b", spend=25_000, roas=1.06, role="own_brand")])
    card = one(efficiency(c), type="keyword")
    assert card.family == CUT and card.direction == "down" and card.entity_id == "c1"
    assert {k.kw for k in card.keywords} == {"rival a", "gen a", "brand a"}
    assert card.stake == pytest.approx(10_000 * 0.1 + 25_000 * 0.65)
    assert card.evidence["n_kws"] == 3


def test_classic_pause_bucket_rescue():
    filler = kw("rival big", spend=100_000, roas=0.68, role="competitor")
    c = ctx([camp(roas=2.0)], keywords=[filler, kw("rival ok", spend=30_000, roas=0.70, role="competitor"),
                                        kw("rival bad", spend=30_000, roas=0.10, role="competitor")])
    card = one(efficiency(c), type="keyword")
    assert [k.kw for k in card.keywords] == ["rival bad"]              # 0.70 is normal for its kind
    # a uniformly wasteful bucket (avg < 0.5 x floor) cannot bless its members
    c2 = ctx([camp(roas=2.0)], keywords=[kw("rival big", spend=100_000, roas=0.40, role="competitor"),
                                         kw("rival ok", spend=30_000, roas=0.45, role="competitor")])
    assert {k.kw for k in one(efficiency(c2), type="keyword").keywords} == {"rival big", "rival ok"}


def test_verdict_pause_only_absolute_waste():
    rows = [kw("gen top", spend=200_000, roas=4.0), kw("gen mid", spend=20_000, roas=1.9),
            kw("gen low", spend=20_000, roas=1.0), kw("gen thin", spend=9_999, roas=0.2),
            kw("rival", spend=20_000, roas=0.59, role="competitor"),
            kw("rival ok", spend=20_000, roas=0.62, role="competitor")]
    c = ctx([camp(roas=3.5, spend=5_000)], keywords=rows, rule_profile="verdict")
    card = one(efficiency(c), type="keyword")
    # generic bar = min(0.5 x ~3.6, be) = 1.0; competition bar = min(0.6 x 1.0, 1.0) = 0.6
    assert {k.kw for k in card.keywords} == {"gen low", "rival"}


# ------------------------------------------------------------ verdict ladder

def _ladder(roas, *, suff=1.0, kws=None, tel_kw=None, **kw_):
    kws = kws if kws is not None else [kw("anchor", "bench", 400_000, 2.0), kw("lead", "c1", 50_000, roas)]
    cs = [camp("c1", roas=roas, spend=60_000), camp("bench", roas=2.05, spend=5_000)]
    t = tel("c1", suff=suff, exh=None, bud=1000, bud_l=1200, **(tel_kw or {}))
    return ctx(cs, keywords=kws, telemetry=[t], rule_profile="verdict", **kw_)


def test_ladder_threshold_is_bucket_average_and_board_records_everyone():
    c = _ladder(2.0)
    board = verdict_scoreboard(c)
    thr = (400_000 * 2.0 + 50_000 * 2.0) / 450_000
    assert board["c1"] == {"r": 2.0, "thr": pytest.approx(thr), "cap": 1200, "segment": SEG,
                           "bucket": "generic", "spend": 60_000}
    assert "bench" not in board                                         # under the judging spend floor
    assert verdict_scoreboard(ctx([camp()], keywords=[kw("a")])) == {}  # classic: empty
    assert run_families(c).board == board


def test_ladder_raise_needs_sufficiency():
    hot = _ladder(3.0, suff=0.5)
    thr = verdict_scoreboard(hot)["c1"]["thr"]
    card = one(efficiency(hot), family=SCALE)
    assert card.change.pct == 50 and card.change.frm == 1200           # need = 1/0.5 - 1 = 1 -> 90 -> clamp
    assert card.stake == pytest.approx(60_000 * (3.0 - thr) * 0.5, abs=1)
    assert none(efficiency(_ladder(3.0, suff=0.95)), family=SCALE) is None
    mild = one(efficiency(_ladder(3.0, suff=0.8)), family=SCALE)
    assert mild.change.pct == 22
    # inside the +10% margin: no raise even when starved
    assert none(efficiency(_ladder(2.15, suff=0.5)), family=SCALE) is None


def test_ladder_below_bar_ships_a_choice_pair():
    rows = [kw("anchor", "bench", 400_000, 2.0), kw("lead", "c1", 30_000, 1.6),
            kw("drag", "c1", 20_000, 1.0), kw("fine", "c1", 10_000, 1.9)]
    cards = efficiency(_ladder(1.4, kws=rows))
    bidc, budc = one(cards, type="bid"), one(cards, type="budget")
    thr = budc.evidence["threshold"]
    assert bidc.family == budc.family == CUT
    assert bidc.tension_group == budc.tension_group == "c1"
    assert bidc.tension_with == budc.id and budc.tension_with == bidc.id
    needed = 1 - 1.4 / thr
    assert budc.change.pct == -round(needed * 100) and budc.title == "Decrease Budget"
    assert budc.change.frm == 1200
    assert budc.stake == pytest.approx(60_000 * needed, abs=1)
    assert [k.kw for k in bidc.keywords] == ["lead", "drag"]            # roas < 0.9 x thr, by spend
    wneed = (30_000 * (1 - 1.6 / thr) + 20_000 * (1 - 1.0 / thr)) / 50_000
    assert bidc.stake == pytest.approx(50_000 * wneed, abs=1)
    assert bidc.change.pct == -round(max(0.10, min(wneed, 0.30)) * 100)


def test_ladder_deep_band_is_not_linked_and_exit_needs_absolute_loss():
    rows = [kw("anchor", "bench", 400_000, 2.0), kw("drag", "c1", 50_000, 0.6)]
    cards = efficiency(_ladder(0.8, kws=rows))
    bidc = one(cards, type="bid")
    budc = one(cards, type="budget", title="Decrease Budget (step 1 of exit)")
    assert budc.change.pct == -30 and budc.tension_group is None and bidc.tension_group is None
    # 45% below the bar but still above breakeven: proportional step, no exit
    cards = efficiency(_ladder(1.02, kws=[kw("anchor", "bench", 400_000, 2.0), kw("drag", "c1", 50_000, 1.02)]))
    budc = one(cards, type="budget", title="Decrease Budget")
    assert budc.change.pct == -30 and budc.evidence["exit"] is False
    # exit path outside the deep band: budget cut only, the bid cut is skipped
    rows = [kw("anchor", "bench", 400_000, 1.5), kw("drag", "c1", 50_000, 0.8)]
    cards = efficiency(_ladder(0.8, kws=rows))
    one(cards, type="budget", title="Decrease Budget (step 1 of exit)")
    # the ladder's cut replaces the classic cut: one budget cut per campaign, unique card ids
    assert [c.title for c in cards if c.type == "budget"] == ["Decrease Budget (step 1 of exit)"]
    assert len({c.id for c in cards}) == len(cards)
    none(cards, type="bid")


def test_ladder_dead_zone():
    rows = [kw("anchor", "bench", 400_000, 2.0), kw("lead", "c1", 30_000, 2.2), kw("drag", "c1", 20_000, 1.5)]
    cards = efficiency(_ladder(1.9, kws=rows, suff=1.0))
    none(cards, type="budget")
    none(cards, type="experiment")
    assert [k.kw for k in one(cards, type="bid").keywords] == ["drag"]
    cards = efficiency(_ladder(1.9, kws=rows, suff=0.6))
    exp = one(cards, family="experiment", type="experiment")
    assert exp.stake == pytest.approx(6_000) and exp.direction == "none"
    assert one(cards, type="bid").tension_group is None


def test_ladder_console_cut_and_probe_guards():
    rows = [kw("anchor", "bench", 400_000, 2.0), kw("drag", "c1", 50_000, 1.4)]
    cards = efficiency(_ladder(1.4, kws=rows, tel_kw={"bud_p3": 1500}))     # cap already lowered < 3 d ago
    none(cards, type="budget")
    one(cards, type="bid")
    cards = efficiency(_ladder(1.4, kws=rows, tel_kw={"bud_p3": 1200.4}))   # within tolerance
    one(cards, type="budget")
    cards = efficiency(_ladder(1.4, kws=rows, recent_applied=frozenset({("biddown", "c1")})))
    none(cards, type="bid")
    assert one(cards, type="budget").tension_group is None


def test_ladder_new_user_and_volume_protection():
    anchor = [kw(f"a{i}", "bench", 50_000, 2.0, nu=0.0, vol=10.0) for i in range(8)]
    rows = anchor + [kw("acq", "c1", 20_000, 1.0, nu=50.0, vol=1.0),        # new-user top: never cut
                     kw("head", "c1", 20_000, 1.7, nu=0.0, vol=9_000.0),    # top volume: needs < 0.8 x thr
                     kw("plain", "c1", 20_000, 1.7, nu=0.0, vol=5.0)]       # < 0.9 x thr -> drag
    cards = efficiency(_ladder(1.4, kws=rows))
    assert [k.kw for k in one(cards, type="bid").keywords] == ["plain"]


def test_verdict_campaign_without_keywords_falls_to_classic_scale_sized_by_sufficiency():
    c = ctx([camp(roas=6.0, spend=50_000, ad_type=RECO)], telemetry=[tel(suff=0.5, exh=None)],
            rule_profile="verdict")
    card = one(efficiency(c), family=SCALE)
    assert card.change.pct == 50 and card.evidence["sizing"] == "sufficiency"
    assert verdict_scoreboard(c) == {}


def test_budgets_and_bids_share_one_threshold():
    from cpm_lineage.actions.families import bid
    from _helpers import kwm
    rows = [kw("anchor", "bench", 400_000, 2.0), kw("drag", "c1", 50_000, 1.4)]
    c = _ladder(1.4, kws=rows, keyword_market=[kwm("anchor", 400_000, 2.0, 4.0), kwm("drag", 50_000, 1.4, 4.0)])
    budget_thr = one(efficiency(c), type="budget").evidence["threshold"]
    bid_thr = one(bid(c), family="bid").evidence["threshold"]
    assert bid_thr == pytest.approx(budget_thr)
