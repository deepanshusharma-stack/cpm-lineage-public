"""Explained share, jury, verdict, confidence, sizing and the end-to-end attributor."""

import math
from datetime import date, timedelta

import numpy as np
import pytest

from cpm_lineage.attribution import (CHANNEL_SIGN, LEVERS, TIER2, AttributionConfig, LeverPanel, Prior, attribute,
                                     build_lever_panel, channel_split, confidence, demand_evidence,
                                     explained_share, fit_prior, jury_label, move_skill, size_move, verdict)
from cpm_lineage.schema import Window, WindowPair
from cpm_lineage.windows import trailing_windows

D0 = date(2026, 1, 1)


# --------------------------------------------------------------------------- pieces

def test_explained_share_move_basis():
    ex = explained_share([-0.15, -0.05, 0.02], -0.2)
    assert ex["basis"] == "move" and not ex["small_move"] and ex["denom"] == -0.2
    assert ex["net"] == pytest.approx(90.0) and ex["gross"] == pytest.approx(110.0)
    assert ex["net"] + ex["residual"] == pytest.approx(100.0, abs=1e-9)


def test_small_move_guard_switches_to_activity_basis():
    ex = explained_share([0.20, -0.18], 0.02)
    assert ex["small_move"] and ex["basis"] == "activity"
    assert ex["denom"] == pytest.approx(0.38)                       # copysign(gross, dln)
    assert explained_share([0.20, -0.18], -0.02)["denom"] == pytest.approx(-0.38)
    # thresholds: |dln| must be < 0.08 and gross > 2 x |dln|
    assert not explained_share([0.20, -0.12], 0.08)["small_move"]
    assert explained_share([0.20, -0.121], 0.079)["small_move"]
    assert not explained_share([0.03, 0.01], 0.02)["small_move"]    # gross == 2 x |dln|: not strictly more
    assert explained_share([0.031, 0.01], 0.02)["small_move"]
    assert math.isfinite(explained_share([0.0, 0.0], 0.0)["net"])


def test_jury_label_table():
    assert jury_label(0.05, 0.05) == "confirm" and jury_label(-0.3, -0.06) == "confirm"
    assert jury_label(0.05, -0.05) == "contest"
    assert jury_label(0.05, 0.049) == "solo" and jury_label(0.3, 0.0) == "solo"
    assert jury_label(0.049, 0.9) is None
    assert jury_label(0.9, 0.9, thin=True, has_prior=True) == "account-est"
    assert jury_label(0.9, 0.9, thin=True, has_prior=False) is None


def test_verdict_one_fixture_per_branch():
    base = dict(thin=False, small_move=False, net_explained_pct=80.0, top_label="confirm", skill=0.3)
    assert verdict(**{**base, "thin": True})[0] == "thin-data"
    assert verdict(**{**base, "small_move": True})[0] == "unexplained"
    assert verdict(**{**base, "net_explained_pct": 19.4})[0] == "unexplained"
    assert verdict(**{**base, "net_explained_pct": -19.4})[0] == "unexplained"
    assert verdict(**{**base, "net_explained_pct": 19.6})[0] == "confirmed"       # rounds to 20
    assert verdict(**{**base, "top_label": "contest"})[0] == "unexplained"
    assert verdict(**{**base, "skill": -0.26})[0] == "unexplained"
    assert verdict(**{**base, "skill": -0.25})[0] == "directional"                # not below the floor, not >= 0
    assert verdict(**base)[0] == "confirmed"
    assert verdict(**{**base, "skill": 0.0})[0] == "confirmed"
    assert verdict(**{**base, "skill": -0.01})[0] == "directional"
    assert verdict(**{**base, "top_label": "solo"})[0] == "directional"
    assert verdict(**{**base, "skill": None})[0] == "directional"
    # precedence: thin beats everything
    assert verdict(thin=True, small_move=True, net_explained_pct=0.0, top_label="contest", skill=-9.0)[0] == "thin-data"
    assert all(isinstance(verdict(**base)[1], str) for _ in range(1))


def test_confidence_thresholds():
    assert confidence("confirmed", 60, 0.25) == "High"
    assert confidence("confirmed", 59, 0.25) == "Medium" and confidence("confirmed", 60, 0.26) == "Medium"
    assert confidence("directional", 90, 0.0) == "Medium"
    assert confidence("unexplained", 90, 0.0) == "Low" and confidence("thin-data", 90, 0.0) == "Low"
    assert confidence("directional", 34, 0.0) == "Low" and confidence("directional", 35, 0.0) == "Medium"
    assert confidence("directional", 90, 0.46) == "Low" and confidence("directional", 90, 0.45) == "Medium"


def test_move_skill_weights_by_channel_move():
    assert move_skill({"icr": 0.5, "asp": None, "cpm": -0.5}, {"icr": 0.1, "asp": 5.0, "cpm": -0.3}) == pytest.approx(
        (0.1 * 0.5 + 0.3 * -0.5) / 0.4)
    assert move_skill({"icr": None, "asp": None, "cpm": None}, {"icr": 1.0}) is None


def test_channel_split():
    # lever worth 50% of the move: icr 40, cpm 10 (sign-flipped), asp 2 (dropped: < 5)
    chips = channel_split({"icr": -0.08, "cpm": 0.02, "asp": -0.004}, -0.2)
    assert chips == [{"ch": "icr", "pct": 40}, {"ch": "cpm", "pct": 10}]
    assert channel_split({"icr": -0.004, "cpm": 0.0, "asp": 0.0}, -0.2) == []          # lever under 3%
    # channel >= 5% but under 15% of the largest is dropped
    assert channel_split({"icr": -0.2, "cpm": 0.012, "asp": 0.0}, -0.2) == [{"ch": "icr", "pct": 100}]
    # none qualifies -> keep the largest
    assert channel_split({"icr": -0.008, "cpm": 0.0, "asp": 0.0}, -0.2) == [{"ch": "icr", "pct": 4}]


def test_demand_evidence_stories():
    rng = np.random.default_rng(0)
    n = 60
    x = rng.uniform(1.0, 2.0, n)
    icr = np.exp(0.8 * x + rng.normal(scale=0.05, size=n))
    assert demand_evidence(x, icr)["story"] == "demand-led"
    assert demand_evidence(x, 1.0 / icr)["story"] == "dilution"
    assert demand_evidence(x, np.exp(rng.normal(size=n)))["story"] == "no-link"
    assert demand_evidence(x[:13], icr[:13]) is None
    # sign flips between halves -> unstable (overall |r| still above the bar)
    flip = np.concatenate([np.exp(3.0 * x[:30]), np.exp(4.95 - 0.3 * x[30:])])
    ev = demand_evidence(x, flip)
    assert ev["story"] == "unstable" and not ev["stable"]
    # yesterday's lever drives today's conversion -> leading
    lead = np.exp(np.concatenate([[1.5], x[:-1]]))
    assert demand_evidence(x, lead)["story"] == "leading"
    assert demand_evidence(np.full(n, 1.0), icr) is None                # undefined correlation
    assert demand_evidence(np.log(x) - 5.0, icr, in_logs=True)["story"] == "demand-led"   # negative logs allowed


def test_size_move_steps_and_bounds():
    beta = {"icr": 0.0, "asp": 0.0, "cpm": 0.3}                         # +1 sd of bid raises CPM -> lowers ROAS
    s = size_move("bid", beta, sd=20.0, current=100.0, roas=3.0, spend=1000.0)
    assert s["direction"] == "down" and s["to"] == pytest.approx(90.0) and s["unit"] == "relative"
    assert s["d_roas"] == pytest.approx(3.0 * (math.exp(0.3 * 10.0 / 20.0) - 1.0))
    assert s["value"] == pytest.approx(s["d_roas"] * 1000.0) and s["estimator"] == "lever-elasticity"
    # position: one slot toward the better side, floored at the top slot
    pos = {"icr": -0.4}
    assert size_move("position", pos, 2.0, 5.0, 3.0, 1.0)["to"] == 4.0
    assert size_move("position", pos, 2.0, 1.0, 3.0, 1.0) is None       # already at the top: draw 0
    assert size_move("position", pos, 2.0, 1.4, 3.0, 1.0)["to"] == 1.0
    # shares: 5 points, clamped
    av = {"icr": 0.5}
    assert size_move("own_availability", av, 0.1, 0.80, 3.0, 1.0)["to"] == pytest.approx(0.85)
    assert size_move("own_availability", av, 0.1, 0.97, 3.0, 1.0)["to"] == 1.0
    assert size_move("own_availability", av, 0.1, 0.996, 3.0, 1.0) is None          # draw <= 0.005
    assert size_move("price_gap", {"icr": -0.5}, 0.1, 1.0, 3.0, 1.0)["to"] == pytest.approx(0.95)
    # suppressions
    assert size_move("bid", {"cpm": 0.0005}, 20.0, 100.0, 3.0, 1.0) is None         # |nbz| < 1e-3
    assert size_move("bid", {"cpm": 0.01}, 20.0, 100.0, 3.0, 1.0) is None           # |dROAS| < 0.02
    assert size_move("bid", {"cpm": 2.0}, 20.0, 100.0, 3.0, 1.0) is None            # > 0.6 x ROAS: implausible
    assert size_move("log_category_impressions", {"icr": 1.0}, 1.0, 10.0, 3.0, 1.0) is None   # no unit
    assert size_move("bid", beta, 20.0, float("nan"), 3.0, 1.0) is None


# --------------------------------------------------------------------------- end to end

def _panel(n=90, seed=0, bid_jump=0.0, noise=0.03, levers=("bid", "position", "own_paid_sov", "own_availability",
                                                           "log_category_impressions"), offset=None):
    """Synthetic entity: ln CPM responds to bid, ln ICR to availability and position."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    bid = 100.0 + rng.normal(scale=6.0, size=n) + np.where(t >= n - 30, bid_jump, 0.0)
    pos = np.clip(4.0 + rng.normal(scale=0.5, size=n), 1.0, None)
    sov = np.clip(0.3 + rng.normal(scale=0.04, size=n), 0.01, 0.99)
    av = np.clip(0.9 + rng.normal(scale=0.03, size=n), 0.0, 1.0)
    cat = 12.0 + rng.normal(scale=0.1, size=n)
    cpm = np.exp(np.log(100.0) + 0.9 * np.log(bid / 100.0) + rng.normal(scale=noise, size=n))
    icr = np.exp(np.log(0.004) + 1.0 * (av - 0.9) - 0.05 * (pos - 4.0) + rng.normal(scale=noise, size=n))
    asp = np.exp(np.log(250.0) + rng.normal(scale=noise / 3, size=n))
    impr = np.full(n, 50_000.0)
    units = icr * impr
    cols = {"bid": bid, "position": pos, "own_paid_sov": sov, "own_availability": av,
            "log_category_impressions": cat}
    if offset:
        for k, v in offset.items():
            cols[k] = cols[k] + v
    return LeverPanel(dates=[D0 + timedelta(days=int(i)) for i in t], spend=cpm * impr / 1000.0,
                      sales=units * asp, impressions=impr, units=units,
                      levers={k: cols[k] for k in levers})


def _windows(p, days=30):
    return trailing_windows(p.dates[-1], days)


def test_layer1_sums_to_log_roas_move_exactly():
    p = _panel(bid_jump=40.0)
    a = attribute(p, _windows(p))
    assert a.status == "ok"
    assert sum(CHANNEL_SIGN[ch] * v for ch, v in a.layer1.items()) == pytest.approx(
        math.log(a.comparison_roas / a.baseline_roas), abs=1e-9)
    assert a.dln == pytest.approx(math.log(a.comparison_roas / a.baseline_roas), abs=1e-9)


def test_bid_jump_is_attributed_to_bid_through_cpm_and_confirmed():
    p = _panel(bid_jump=40.0)
    a = attribute(p, _windows(p))
    assert a.top_driver == "bid" and a.verdict == "confirmed" and a.dln < -0.2
    d = a.driver("bid")
    assert d.jury == "confirm" and d.contrib < 0 and d.pct > 70 and d.channels[0]["ch"] == "cpm"
    assert a.drivers[0].lever == "bid" and not d.na
    assert a.betas["cpm"]["bid"] >= 0 and a.betas["icr"]["position"] <= 0 and a.betas["icr"]["own_availability"] >= 0
    assert a.confidence["level"] == "High" and a.days == 60 and not a.short and not a.thin
    assert a.move_skill is not None and a.move_skill >= 0
    # contributions + residual account for the whole move
    tot = sum(x.contrib for x in a.drivers)
    assert 100.0 * tot / a.dln == pytest.approx(a.explained["net"], abs=0.06)
    assert a.explained["net"] + a.explained["residual"] == pytest.approx(100.0, abs=0.11)
    # sizing: lowering the bid raises ROAS
    assert a.sizes["bid"]["direction"] == "down" and d.sizing == a.sizes["bid"]
    assert all(lv not in TIER2 for lv in a.sizes)
    # all eleven levers reported; the uncovered ones are n/a and sorted last
    assert [x.lever for x in a.drivers if x.na] and {x.lever for x in a.drivers} == set(LEVERS)
    na_seen = False
    for x in a.drivers:
        na_seen = na_seen or x.na
        assert x.na == na_seen
    assert a.as_dict()["drivers"][0]["lever"] == "bid"


def test_contribution_is_beta_times_dz_summed_over_channels():
    p = _panel(bid_jump=25.0, seed=3)
    a = attribute(p, _windows(p))
    w = _windows(p)
    b, c = p.window_mask(w.prior), p.window_mask(w.current)
    for lever in a.betas["icr"]:
        v = p.levers[lever]
        dz = (v[c].mean() - v[b].mean()) / v[b | c].std()     # z-scores use the compared days only
        expect = sum(CHANNEL_SIGN[ch] * a.betas[ch][lever] * dz for ch in a.betas)
        assert a.driver(lever).contrib == pytest.approx(expect, abs=1e-9)


def test_small_move_is_unexplained_with_activity_basis():
    p = _panel(bid_jump=0.0, seed=1, noise=0.15)
    a = attribute(p, _windows(p))
    if abs(a.dln) < 0.08 and a.explained["small_move"]:
        assert a.verdict == "unexplained" and a.explained["basis"] == "activity"
    assert a.verdict in ("unexplained", "directional", "confirmed")
    assert a.verdict != "confirmed" or abs(a.explained["net"]) >= 20


def test_thin_without_and_with_prior():
    p = _panel(bid_jump=40.0, levers=("bid", "position"))
    a = attribute(p, _windows(p))
    assert a.thin and a.verdict == "thin-data" and a.confidence["level"] == "Low" and a.sizes == {}
    assert all(d.contrib == 0 for d in a.drivers) and all(d.jury is None for d in a.drivers)
    prior = fit_prior(_panel(bid_jump=40.0, seed=9))
    a = attribute(p, _windows(p), prior=prior)
    assert a.verdict == "thin-data" and a.driver("bid").jury == "account-est"
    assert a.betas["cpm"]["bid"] == pytest.approx(prior.beta["cpm"]["bid"])       # account elasticity, as is
    assert a.driver("bid").contrib < 0 and a.lambdas == {}


def test_prior_shares_the_coordinate_system():
    acct = _panel(n=120, seed=7, bid_jump=30.0)
    prior = fit_prior(acct)
    assert set(prior.levers) == {"bid", "position", "own_paid_sov", "own_availability", "log_category_impressions"}
    assert prior.beta["cpm"]["bid"] > 0 and prior.days == 120
    assert Prior.from_mapping(prior.as_dict()).beta == prior.beta
    p = _panel(bid_jump=40.0, seed=2)
    a = attribute(p, _windows(p), prior=prior)
    w = _windows(p)
    b, c = p.window_mask(w.prior), p.window_mask(w.current)
    dz = (p.levers["bid"][c].mean() - p.levers["bid"][b].mean()) / prior.sd["bid"]
    assert a.driver("bid").contrib == pytest.approx(
        sum(CHANNEL_SIGN[ch] * a.betas[ch]["bid"] * dz for ch in a.betas), abs=1e-9)
    assert a.top_driver == "bid"


def test_tier_routing_and_short_mode():
    young = _panel(n=20)
    a = attribute(young, _windows(young, 10))
    assert a.status == "insufficient-data" and a.verdict == "thin-data" and all(d.na for d in a.drivers)
    assert a.layer1 and a.baseline_roas is not None
    mature = _panel(n=30, bid_jump=40.0)
    w = WindowPair(Window(D0, D0 + timedelta(days=14)), Window(D0 + timedelta(days=15), D0 + timedelta(days=29)))
    a = attribute(mature, w)
    assert a.status == "ok" and a.short and set(a.lambdas.values()) == {16.0}
    assert a.confidence["level"] == "Low"                                        # < 35 days
    assert attribute(young, _windows(young, 10), short=True).status == "ok"       # explicit override skips routing


def test_zero_sale_days_count_in_layer1_but_not_in_the_model():
    p = _panel(bid_jump=40.0)
    p.units[5] = 0.0
    p.sales[5] = 0.0
    a = attribute(p, _windows(p))
    p.units[35] = 0.0
    p.sales[35] = 0.0
    a = attribute(p, _windows(p))
    assert a.days == 59                      # the panel is clipped to the two 30-day windows; one zero-sale day drops
    w = _windows(p)
    c = p.window_mask(w.current)
    assert a.comparison_roas == pytest.approx(p.sales[c].sum() / p.spend[c].sum())
    b = p.window_mask(w.prior)
    assert a.baseline_roas == pytest.approx(p.sales[b].sum() / p.spend[b].sum())   # includes the zero-sale day


def test_empty_window_is_reported_not_raised():
    p = _panel()
    far = WindowPair(Window(date(2020, 1, 1), date(2020, 1, 30)), Window(D0, D0 + timedelta(days=29)))
    a = attribute(p, far, short=False)
    assert a.status == "no-comparison" and a.verdict == "thin-data" and len(a.drivers) == 11


def test_position_bound_flips_for_higher_is_better_platforms():
    p = _panel(bid_jump=10.0)
    a = attribute(p, _windows(p), position_lower_is_better=False)
    assert a.betas["icr"]["position"] >= 0


def test_suppressed_lever_is_na_and_listed():
    p = _panel(bid_jump=40.0)
    p.levers["own_paid_sov"][:60] = np.nan     # half of the compared days
    a = attribute(p, _windows(p))
    d = a.driver("own_paid_sov")
    assert a.suppressed == ["own_paid_sov"] and d.na and d.suppressed and d.contrib == 0.0


def test_synthetic_cpm_spike_and_account_prior(synthetic_dataset, windows_30):
    d = synthetic_dataset
    feeds = dict(market_rows=d.market, shelf_rows=d.shelf, targeting_units=d.targeting_units)
    prior = fit_prior(build_lever_panel(d.perf, **feeds))
    assert prior is not None and prior.beta["cpm"]["bid"] >= 0
    a = attribute(build_lever_panel(d.perf, entity_id="cmp-generic", **feeds), windows_30, prior=prior)
    assert a.top_driver == "bid" and a.verdict in ("confirmed", "directional") and a.dln < -0.1
    assert a.driver("bid").channels[0]["ch"] == "cpm" and a.driver("bid").contrib < 0
    assert sum(CHANNEL_SIGN[ch] * v for ch, v in a.layer1.items()) == pytest.approx(
        math.log(a.comparison_roas / a.baseline_roas), abs=1e-9)
    young = attribute(build_lever_panel(d.perf, entity_id="cmp-young", **feeds), windows_30, prior=prior)
    assert young.status == "insufficient-data"
    reach = attribute(build_lever_panel(d.perf, entity_id="cmp-reach", **feeds), windows_30, prior=prior)
    assert reach.status == "insufficient-data" and reach.days == 0
    brand = attribute(build_lever_panel(d.perf, entity_id="cmp-brand", **feeds), windows_30, prior=prior)
    assert brand.top_driver == "own_availability" and brand.driver("own_availability").contrib < 0


# --------------------------------------------------------------------------- regressions

def test_panel_is_clipped_to_the_compared_windows():
    p = _panel(bid_jump=40.0)
    one_day = WindowPair(Window(p.dates[-2], p.dates[-2]), Window(p.dates[-1], p.dates[-1]))
    a = attribute(p, one_day)
    # a one-day-versus-one-day comparison has two model days: no move model, no verdict, no sizes
    assert a.status == "insufficient-data" and a.verdict == "thin-data" and a.days == 2
    assert a.confidence["level"] == "Low" and a.sizes == {} and all(d.na for d in a.drivers)
    week = WindowPair(Window(p.dates[-14], p.dates[-8]), Window(p.dates[-7], p.dates[-1]))
    assert attribute(p, week).status == "insufficient-data"
    full = attribute(p, one_day, clip=False)                       # opt-out fits on the whole panel
    assert full.days == 90


def test_panel_without_lever_columns_degrades_to_thin_data():
    base = _panel(bid_jump=40.0)
    bare = LeverPanel(dates=base.dates, spend=base.spend, sales=base.sales, impressions=base.impressions,
                      units=base.units)
    a = attribute(bare, _windows(bare))
    assert a.verdict == "thin-data" and a.thin and a.n_live == 0 and len(a.drivers) == 11
    assert a.layer1 and a.dln == pytest.approx(math.log(a.comparison_roas / a.baseline_roas))
    partial = LeverPanel(dates=base.dates, spend=base.spend, sales=base.sales, impressions=base.impressions,
                         units=base.units, levers={"bid": base.levers["bid"]})
    a = attribute(partial, _windows(partial))
    assert a.verdict == "thin-data" and a.driver("position").na


def test_explained_share_falls_back_on_a_flat_move():
    flat = explained_share([0.3], 0.0)
    assert flat["net"] == pytest.approx(100.0) and flat["gross"] == pytest.approx(100.0)
    assert flat["residual"] == pytest.approx(0.0) and flat["small_move"]
    tiny = explained_share([0.3, -0.1], 5e-5)
    assert tiny["net"] == pytest.approx(100.0)
    assert explained_share([0.0], 0.0)["net"] == 0.0               # nothing moved, nothing explained
    assert explained_share([0.1], 0.2)["net"] == pytest.approx(50.0)
