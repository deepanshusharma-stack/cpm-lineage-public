"""Correlation pre-screen, watchdog tripwires and the unified action queue."""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from cpm_lineage.config import Economics
from cpm_lineage.queue import GROUP_BLEED, GROUP_SCALE, GROUP_STRUCTURE, QueueConfig, build_action_queue
from cpm_lineage.screen import (
    MODERATE, STRONG, WEAK, DriverRead, ScreenConfig, WeekPoint, band, pearson, prescreen, promo_read, routing,
    weekly_series,
)
from cpm_lineage.watchdog import Alert, WatchdogConfig, alert_lines, arm, sweep, tripwires


# ----------------------------------------------------------------- pre-screen

def test_pearson_guards():
    x = [1, 2, 3, 4, 5, 6]
    assert pearson(x, [2, 4, 6, 8, 10, 12]) == 1.0
    assert pearson(x, [6, 5, 4, 3, 2, 1]) == -1.0
    assert pearson(x[:5], x[:5]) is None                       # under six pairs
    assert pearson(x, [3] * 6) is None                         # zero variance
    assert pearson(x + [7], [1, 2, None, 4, 5, 6, 7]) == 1.0   # masked pair dropped, six remain
    assert pearson(x, [1, 2, None, 4, 5, 6]) is None           # five remain
    assert pearson(x, x[:5]) is None                           # unequal lengths


def test_band_thresholds():
    assert band(0.7) == STRONG and band(-0.7) == STRONG
    assert band(0.69) == MODERATE and band(0.4) == MODERATE
    assert band(0.39) == WEAK and band(0.0) == WEAK
    assert band(0.5, ScreenConfig(strong_r=0.5)) == STRONG


def test_routing_is_sign_aware():
    weak = [DriverRead("availability", 0.5, None, MODERATE)]
    assert "full step sequence" in routing(weak) and "full step sequence" in routing([])
    assert routing([DriverRead("ad share of voice", 0.8, None, STRONG)]).startswith("lead with ad share of voice")
    inv = routing([DriverRead("competitor ad activity", -0.9, None, STRONG), DriverRead("availability", 0.8, None, STRONG)])
    assert "INVERSE" in inv and "NOT a lever" in inv


def _points(n=8, **series):
    share = series.pop("share", [20, 21, 22, 23, 24, 25, 26, 27][:n])
    out = []
    for i in range(n):
        kw = {k: v[i] for k, v in series.items()}
        out.append(WeekPoint(own_offtake=share[i] * 10.0, segment_offtake=1000.0, **kw))
    return out


def test_prescreen_minimum_weeks():
    res = prescreen(_points(5, ad_sov=[1, 2, 3, 4, 5]))
    assert res.drivers == () and "below the 6-week minimum" in res.reason and res.weeks == 5
    # weeks with no segment sell-out are skipped before the count
    pts = _points(6, ad_sov=[1, 2, 3, 4, 5, 6])
    pts[2] = WeekPoint(own_offtake=0.0, segment_offtake=0.0, ad_sov=3.0)
    assert prescreen(pts).weeks == 5
    assert prescreen(_points(6, ad_sov=[1, 2, 3, 4, 5, 6])).drivers[0].r_contemp == 1.0


def test_prescreen_drivers_notes_and_order():
    pts = _points(
        ad_sov=[10, 11, 12, 13, 14, 15, 16, 17],                       # r = +1
        rival_ad_impressions=[80, 70, 65, 50, 45, 30, 20, 10],         # strongly inverse
        availability=[0.90, 0.92, 0.91, 0.94, 0.95, 0.94, 0.97, 0.98],  # strong positive
        organic_sov=[5, 5, 5, 5, 5, 5, 5, 5],                          # zero variance: skipped
        discount_gap=[1, -1, 1, -1, 1, -1, 1, -1],                     # weak
    )
    res = prescreen(pts)
    names = [d.driver for d in res.drivers]
    assert "organic share of voice" not in names and "category size" not in names   # constant series skipped
    assert names[0] == "ad share of voice" and res.drivers[0].signal == STRONG
    assert [abs(d.r_contemp) for d in res.drivers] == sorted((abs(d.r_contemp) for d in res.drivers), reverse=True)
    by = {d.driver: d for d in res.drivers}
    assert "competitive-response" in by["competitor ad activity"].note
    assert "fix supply before ads" in by["availability"].note
    assert by["discount gap"].signal == WEAK and by["discount gap"].note is None
    assert res.routing.startswith("lead with ad share of voice")
    assert len(res.series_share) == 8 and res.series_share[0] == pytest.approx(20.0)
    assert not any("own sell-out" == d.driver for d in res.drivers)
    assert any("directional" in c for c in res.caveats)


def test_prescreen_inverse_routing():
    pts = _points(rival_ad_impressions=[80, 70, 65, 50, 45, 30, 20, 10])
    res = prescreen(pts)
    assert res.drivers[0].r_contemp < -0.9 and "INVERSE" in res.routing


def test_lag_pairs_adjacent_weeks_only():
    # driver leads share by exactly one week
    drv = [1.0, 5.0, 2.0, 8.0, 3.0, 9.0, 4.0, 7.0]
    share = [20.0] + [20.0 + d for d in drv[:-1]]
    pts = [WeekPoint(own_offtake=s * 10, segment_offtake=1000.0, ad_sov=d) for s, d in zip(share, drv)]
    d = prescreen(pts).drivers[0]
    assert d.r_lag1 == 1.0 and abs(d.r_contemp) < 0.9
    # a gap drops the pair it touches instead of pairing across it: six adjacent pairs remain, still exact
    pts[3] = WeekPoint(own_offtake=share[3] * 10, segment_offtake=1000.0, ad_sov=None)
    d = prescreen(pts).drivers[0]
    assert d.r_lag1 == 1.0 and d.r_contemp is not None
    # a second gap leaves five pairs: below the minimum
    pts[5] = WeekPoint(own_offtake=share[5] * 10, segment_offtake=1000.0, ad_sov=None)
    assert prescreen(pts).drivers[0].r_lag1 is None


def test_promo_read_bought_and_kept():
    gap = [0.0, 0.0, 0.0, 6.0, 7.0, 0.0, 0.0, 0.0]
    share = [20.0, 20.0, 20.0, 23.0, 25.0, 21.0, 21.0, 20.0]
    off = [100.0] * 8
    p = promo_read(gap, share, off)
    mu = sum(gap) / 8
    assert (p.start_index, p.end_index, p.weeks) == (3, 4, 2) and p.peak_gap == 7.0 and p.ended
    assert p.bought_pp == pytest.approx(4.0, abs=1e-9)
    assert p.kept_pp == pytest.approx(62.0 / 3 - 20.0, abs=1e-9)
    assert p.cost == pytest.approx(((6 - mu) + (7 - mu)) / 100 * 100.0, abs=1e-9)


def test_promo_read_guards():
    off = [100.0] * 8
    share = [20.0] * 8
    assert promo_read([0.0] * 8, share, off) is None                              # no hot week
    assert promo_read([0.0, 0.0, 0.0, 0.9, 0.0, 0.0, 0.0, 0.0], share, off) is None   # under the 1 pp minimum
    assert promo_read([5.0, 0, 0, 0, 0, 0, 0, 0], share, off) is None             # no baseline before the push
    assert promo_read([0, 0, 0, 5.0, None, None, None, None], share, off) is None  # under six observed weeks
    assert promo_read([0, 0, 0, 5.0], share, off) is None                         # unequal lengths
    running = promo_read([0, 0, 0, 0, 0, 0, 0, 6.0], share, off)
    assert running is not None and not running.ended and running.kept_pp is None
    # threshold is mean + max(sd, 1): a wide-variance series needs a full sd
    noisy = [0.0, 4.0, 0.0, 4.0, 0.0, 4.0, 0.0, 4.5]
    assert promo_read(noisy, share, off).peak_gap == 4.5
    assert prescreen(_points(discount_gap=[0.0, 0.0, 0.0, 6.0, 7.0, 0.0, 0.0, 0.0])).promo is not None


def test_weekly_series_from_synthetic_feeds(synthetic_dataset):
    ds = synthetic_dataset
    pts = weekly_series(ds.offtake, ds.shelf, ds.market, weeks=8, segment=None)
    assert len(pts) == 8 and pts[-1].week_start == date(2026, 3, 25)
    own = sum(r.sales for r in ds.offtake if r.is_own and r.date >= date(2026, 3, 25))
    assert pts[-1].own_offtake == pytest.approx(own, abs=1e-6)
    assert all(0 < p.share < 100 and 0 < p.availability <= 1 and 0 < p.ad_sov < 100 for p in pts)
    assert all(p.rival_ad_impressions > 0 for p in pts)
    res = prescreen(pts)
    assert res.weeks == 8 and res.routing and len(res.drivers) >= 4
    # no market feed -> share-of-voice drivers degrade to None instead of 0
    bare = weekly_series(ds.offtake, weeks=8)
    assert all(p.ad_sov is None and p.rival_ad_impressions is None and p.availability is None for p in bare)
    names = {d.driver for d in prescreen(bare).drivers}
    assert "ad share of voice" not in names and "availability" not in names
    assert weekly_series([], weeks=8) == []


# ----------------------------------------------------------------- watchdog

def test_tripwires_rules_and_thresholds():
    comp = {"rows": [{"brand": "rival-b", "is_own": False, "share_delta_pp": 0.5},
                     {"brand": "rival-c", "is_own": False, "share_delta_pp": 0.49},
                     {"brand": "own-brand", "is_own": True, "share_delta_pp": 3.0},
                     {"brand": "sibling", "is_own": False, "is_house": True, "share_delta_pp": 3.0}],
            "segment_sales": 2_000_000.0}
    out = tripwires(comp)
    assert len(out) == 1 and out[0].severity == 1 and "rival-b" in out[0].line and "10,000" in out[0].line
    assert "segment sales" not in tripwires({"rows": comp["rows"]})[0].line

    assert tripwires(None, {"availability": 0.849})[0].severity == 1
    assert tripwires(None, {"availability": 0.85}) == []
    assert tripwires(None, {"availability": 84.0})[0].severity == 1                 # percent input
    # the drop alert is an "else": below the floor only the severity-1 line fires
    both = tripwires(None, {"availability": 0.80, "availability_delta": -0.10})
    assert [a.severity for a in both] == [1]
    assert tripwires(None, {"availability": 0.90, "availability_delta": -0.03})[0].severity == 2
    assert tripwires(None, {"availability": 0.90, "availability_delta": -0.029}) == []
    assert tripwires(None, {"ad_sov_delta_pp": -5.0})[0].severity == 2
    assert tripwires(None, {"ad_sov_delta_pp": -4.9}) == []

    launches = {"rows": [{"brand": "rival-d", "is_own": False, "trend": "ramping", "title": "new bites"},
                         {"brand": "rival-d", "is_own": False, "trend": "steady", "title": "old bites"},
                         {"brand": "own-brand", "is_own": True, "trend": "ramping", "title": "own new"}]}
    out = tripwires(None, None, launches)
    assert [(a.severity, "new bites" in a.line) for a in out] == [(3, True)]
    assert tripwires() == []


def test_tripwires_sorted_and_capped():
    comp = {"rows": [{"brand": f"rival-{c}", "is_own": False, "share_delta_pp": 1.0} for c in "zyxwv"]}
    own = {"availability": 0.5, "ad_sov_delta_pp": -9.0}
    launches = {"rows": [{"brand": "rival-q", "is_own": False, "trend": "ramping"}]}
    out = tripwires(comp, own, launches)
    assert len(out) == 5 and all(a.severity == 1 for a in out)
    assert [a.as_tuple() for a in out] == sorted(a.as_tuple() for a in out)
    assert len(tripwires(comp, own, launches, WatchdogConfig(max_alerts=8))) == 8
    assert alert_lines([Alert(2, "x")]) == ["[2] x"]


def test_tripwires_accept_dataclass_rows():
    from cpm_lineage.market.competition import BrandRow, CompetitionTable
    table = CompetitionTable(rows=(BrandRow("rival-b", share_delta_pp=1.2), BrandRow("own-brand", is_own=True)),
                             segment_sales=1000.0)
    out = tripwires(table)
    assert len(out) == 1 and "rival-b" in out[0].line


def test_arm_and_sweep_are_pure():
    wl: list = []
    wl2 = arm(wl, "adv-001", "snack-bars")
    assert wl == [] and wl2 == [("adv-001", "snack-bars")] and arm(wl2, "adv-001", "snack-bars") == wl2
    wl3 = arm(wl2, "adv-002")
    seen = []

    def loader(adv, seg, days):
        seen.append(days)
        if adv == "adv-002":
            raise RuntimeError("feed unreachable")
        return None, {"availability": 0.5}, None

    out = sweep(wl3, loader)
    assert seen == [30, 30]
    assert out[("adv-001", "snack-bars")][0].severity == 1
    assert out[("adv-002", None)] == [Alert(3, "sweep failed: RuntimeError")]


# ----------------------------------------------------------------- action queue

MOVES = {
    "tighten": [{"entity_id": "c-tight", "daily_waste": 400.0, "roas": 0.6},
                {"entity_id": "c-zero", "daily_waste": 0.0}],
    "scale": [{"entity_id": "c-curve", "daily_budget": 1000.0, "extra_daily_sales": 900.0, "sized": "curve", "raise_to": 1800.0},
              {"entity_id": "c-linear", "daily_budget": 1000.0, "extra_daily_sales": 900.0, "sized": "linear"}],
    "release": [{"entity_id": "c-park", "freeable": 300.0}, {"entity_id": "c-none", "freeable": 0.0}],
}
DRAG = [{"key": "c-tight", "drag": 99999.0}, {"key": "c-drag", "drag": 8000.4, "roas": 1.1},
        {"key": "c-dead", "drag": 50000.0, "extras": {"status": "archived"}},
        {"key": "c-paused", "drag": 50000.0, "status": "stopped"}, {"key": "c-neg", "drag": -5.0}]


def test_queue_rows_weights_and_order():
    q = build_action_queue(bleed={"cuttable": 20000.4, "n_cut": 7}, moves=MOVES, draggers=DRAG)
    by = {r.entity_id: r for r in q.rows}
    assert by[None].group == GROUP_BLEED and by[None].amount == 20000 and by[None].confw == 1.0
    assert by["c-tight"].amount == 12000 and by["c-tight"].conf == "high"
    assert by["c-curve"].amount == 27000 and by["c-curve"].conf == "high" and "1,800" in by["c-curve"].detail
    assert by["c-linear"].conf == "low" and by["c-linear"].confw == 0.3 and "1,500" in by["c-linear"].detail
    assert "small test (+20%)" in by["c-linear"].detail and "7 days" in by["c-linear"].detail
    assert by["c-park"].group == GROUP_STRUCTURE and by["c-park"].amount == 9000 and by["c-park"].confw == 0.6
    assert by["c-drag"].amount == 8000 and by["c-drag"].conf == "med"
    # skipped: zero amounts, covered entity, hard-dead draggers, negative drag
    assert set(by) == {None, "c-tight", "c-curve", "c-linear", "c-park", "c-drag"}
    scores = [r.score for r in q.rows]
    assert scores == sorted(scores, reverse=True)
    assert [r.entity_id for r in q.rows] == ["c-curve", None, "c-tight", "c-linear", "c-park", "c-drag"]
    assert q.n_more == 0 and q.period_days == 30
    assert len({r.unit for r in q.rows}) >= 4                      # units differ: never sum across rows


def test_queue_over_investment_damper():
    q = build_action_queue(moves=MOVES, ms_sov=0.49)
    by = {r.entity_id: r for r in q.rows}
    assert by["c-curve"].conf == "low" and by["c-curve"].confw == 0.5 and "over-invested" in by["c-curve"].detail
    assert by["c-linear"].confw == pytest.approx(0.15)
    assert by["c-tight"].confw == 1.0 and "over-invested" not in by["c-tight"].detail
    assert q.rows[0].entity_id == "c-curve" and q.rows[1].entity_id == "c-tight"   # 13500 vs 12000
    q2 = build_action_queue(moves=MOVES, ms_sov=0.5)
    assert {r.entity_id: r for r in q2.rows}["c-curve"].confw == 1.0


def test_queue_floor_and_period_scaling():
    small = {"tighten": [{"entity_id": "a", "daily_waste": 66.0}, {"entity_id": "b", "daily_waste": 67.0}]}
    q = build_action_queue(moves=small)                      # floor 2000 per 30 days
    assert [r.entity_id for r in q.rows] == ["b"] and q.floor == 2000.0
    q = build_action_queue(moves=small, period_days=15)      # floor and amounts both halve
    assert q.floor == 1000.0 and [r.amount for r in q.rows] == [1005]
    # 1% of the largest row can exceed the absolute floor
    big = {"tighten": [{"entity_id": "a", "daily_waste": 20000.0}, {"entity_id": "b", "daily_waste": 150.0},
                       {"entity_id": "c", "daily_waste": 200.0}]}
    q = build_action_queue(moves=big)
    assert q.floor == 6000.0 and [r.entity_id for r in q.rows] == ["a", "c"]
    # the absolute floor follows the currency unit
    q = build_action_queue(moves=small, economics=Economics(currency_unit=0.01))
    assert len(q.rows) == 2 and q.floor == pytest.approx(20.1)
    assert build_action_queue().reason == "no findings to queue"
    assert build_action_queue(moves={"tighten": [{"entity_id": "a", "daily_waste": 1.0}]}).reason


def test_queue_cap_and_tail():
    many = {"tighten": [{"entity_id": f"c{i}", "daily_waste": 1000.0 + i} for i in range(20)]}
    q = build_action_queue(moves=many)
    assert len(q.rows) == 15 and q.n_more == 5 and q.rows[0].entity_id == "c19"
    assert q.more_amount == sum(round((1000.0 + i) * 30) for i in range(5))
    assert build_action_queue(moves=many, cfg=QueueConfig(max_rows=3)).n_more == 17


def test_queue_from_budget_scan_on_synthetic(synthetic_dataset, windows_30):
    from cpm_lineage.budget.moves import BudgetMoves, ScaleMove, TightenMove
    from cpm_lineage.decompose import DragRow
    moves = BudgetMoves(
        scale=[ScaleMove("cmp-winner", 5000.0, 5.5, 1.0, 2500.0, sized=None, marginal_unverified=True)],
        tighten=[TightenMove("cmp-conquest", 6000.0, 0.35, 3100.0)])
    drag = [DragRow("cmp-overlap", 50000.0, 65000.0, 1.3, 85000.0, 60.0),
            DragRow("cmp-paused", 1.0, 1.0, 1.0, 90000.0, 100.0, extras={"status": "PAUSED"})]
    q = build_action_queue(moves=moves, draggers=drag, status_map=synthetic_dataset.profile.status_map)
    assert [r.entity_id for r in q.rows] == ["cmp-conquest", "cmp-overlap", "cmp-winner"]
    assert q.rows[0].group == GROUP_BLEED and q.rows[-1].group == GROUP_SCALE and q.rows[-1].conf == "low"
    assert q.rows[-1].estimator == "linear_average"


# ------------------------------------------------------------------ regressions

def test_prescreen_lag_never_pairs_weeks_across_a_dropped_week():
    from datetime import timedelta

    driver = [1, 5, 2, 8, 3, 9, 4, 7, 6, 2, 8, 1]
    pts = []
    for t in range(12):
        share = 10 + (driver[t - 1] if t > 0 else 3)              # share[t] is exactly 10 + driver[t - 1]
        seg = 0.0 if t == 5 else 1000.0                           # one week with no segment sell-out
        pts.append(WeekPoint(week_start=date(2025, 1, 6) + timedelta(days=7 * t), own_offtake=seg * share / 100,
                             segment_offtake=seg, ad_sov=float(driver[t])))
    res = prescreen(pts)
    read = next(d for d in res.drivers if d.driver == "ad share of voice")
    assert res.weeks == 11 and read.r_lag1 == pytest.approx(1.0)


def test_tripwire_availability_delta_follows_the_scale_of_the_level():
    dip = tripwires(own={"availability": 90, "availability_delta": -0.5, "ad_sov_delta_pp": -1})
    assert not [a for a in dip if "availability" in a.line]            # half a point is not a 50-point fall
    fall = tripwires(own={"availability": 90, "availability_delta": -4.0})
    assert [a.line for a in fall if "availability" in a.line] == ["own availability fell 4.0 pp"]
    frac = tripwires(own={"availability": 0.90, "availability_delta": -0.04})
    assert [a.line for a in frac if "availability" in a.line] == ["own availability fell 4.0 pp"]
