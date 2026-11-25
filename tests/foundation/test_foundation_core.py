"""Schema, config, metrics, windows and liveness."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest

from cpm_lineage import __version__
from cpm_lineage.config import (
    AvailabilityPolicy, BudgetBoundPolicy, Economics, EngineConfig, TierConfig, apply_overrides,
)
from cpm_lineage.liveness import (
    canonical_status, dead_for_window, is_dead, liveness_class, recent_spend, stale_stopped,
    stopped_since_window,
)
from cpm_lineage.metrics import (
    cell_from_rows, clip_position, cpm, group_cells, identity_gap, normalise_keyword,
    pct_change, position_bucket, position_is_valid, roas, roas_from_identity, safe_div, sum_cells,
)
from cpm_lineage.schema import (
    ActionCard, BudgetTelemetry, CanonicalStatus, Change, DataTier, KeywordRef, LivenessClass,
    MetricCell, PerfRow, ShelfRow, Window, WindowPair, card_id, make_card,
)
from cpm_lineage.windows import (
    active_days, default_windows, drill_buckets, filter_rows, parse_iso_date, recent_half_start,
    reporting_window, resolve_window, route_tier, split_rows, trailing_windows,
)

D0 = date(2026, 1, 1)


def test_version():
    assert __version__ == "0.1.0"


# ---------------------------------------------------------------- schema

def test_perf_row_minimal_and_frozen():
    r = PerfRow(date=D0, entity_id="e", spend=1.0, impressions=10.0)
    assert r.sales == 0.0 and r.clicks is None and r.position is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.spend = 2.0  # type: ignore[misc]


def test_shelf_row_availability_and_discount():
    s = ShelfRow(date=D0, product_id="p", available_count=18, check_count=24, price=80, list_price=100)
    assert s.availability == pytest.approx(0.75)
    assert s.discount == pytest.approx(0.2)
    assert ShelfRow(date=D0, product_id="p").availability is None
    assert ShelfRow(date=D0, product_id="p", available_count=1, check_count=0).availability is None


def test_window_inclusive_days_and_validation():
    w = Window(D0, D0 + timedelta(days=13))
    assert w.days == 14 and w.contains(D0) and w.contains(w.end)
    assert not w.contains(D0 - timedelta(days=1))
    with pytest.raises(ValueError):
        Window(D0, D0 - timedelta(days=1))


def test_card_id_is_stable_and_keyword_sensitive():
    a = card_id("bid", "bid", "c1", "seg", "Lower bid", ["b", "a"])
    b = card_id("bid", "bid", "c1", "seg", "Lower bid", ["a", "b"])
    c = card_id("bid", "bid", "c1", "seg", "Lower bid", ["a", "c"])
    d = card_id("bid", "bid", "c1", "seg", "Lower bid")
    assert a == b and len(a) == 12 and int(a, 16) >= 0
    assert len({a, c, d}) == 3
    many = [f"k{i:02d}" for i in range(12)]
    assert card_id("f", "t", "c", "s", "x", many) == card_id("f", "t", "c", "s", "x", many[:8])


def test_make_card_defaults_and_coercion():
    card = make_card("efficiency-cut", "budget", "Trim budget", entity_id="c1", segment="seg",
                     direction="down", change={"pct": -20, "frm": 1000, "to": 800},
                     keywords=["x", {"kw": "y", "spend": 5.0}, KeywordRef("z")],
                     evidence={"roas": 0.7}, stake=1234.56)
    assert isinstance(card, ActionCard)
    assert card.status == "pending" and card.is_actionable
    assert card.stake == 1235.0
    assert isinstance(card.change, Change) and card.change.to == 800
    assert card.keyword_values() == ["x", "y", "z"] and card.keywords[1].spend == 5.0
    assert card.id == card_id("efficiency-cut", "budget", "c1", "seg", "Trim budget", ["x", "y", "z"])
    assert card.confidence is None and card.conf_parts == [] and card.verdict is None
    card.status = "vetoed"
    assert not card.is_actionable


# ---------------------------------------------------------------- config

def test_economics_effective_breakeven():
    assert Economics().be == 1.0 and Economics().scale_bar == 2.0
    assert Economics(breakeven=1.5, ltv_mult=3.0).be == pytest.approx(0.5)
    assert Economics(breakeven=1.5, ltv_mult=3.0).scale_bar == pytest.approx(1.0)
    assert Economics(breakeven=0.01).be == pytest.approx(0.1)           # floor on be
    assert Economics(breakeven=1.0, ltv_mult=0.0).be == pytest.approx(10.0)  # ltv floored at 0.1
    assert Economics(currency_unit=0.012).money(5000) == pytest.approx(60.0)


def test_economics_from_mapping_keeps_blanks_and_clamps():
    assert Economics.from_mapping(None) == Economics()
    e = Economics.from_mapping({"breakeven": "", "ltv_mult": "0.01"})
    assert e.breakeven == 1.0 and e.ltv_mult == 0.1
    assert Economics.from_mapping({"breakeven": 1.4}).as_dict() == {"breakeven": 1.4, "ltv_mult": 1.0, "be": 1.4}


def test_availability_policy_bands_at_thresholds():
    p = AvailabilityPolicy()
    assert p.band(None) == "unknown"
    assert p.band(0.5999) == "block" and p.band(0.60) == "caution"
    assert p.band(0.7499) == "caution" and p.band(0.75) == "below_floor"
    assert p.band(0.8499) == "below_floor" and p.band(0.85) == "ok"
    assert p.band(59.0) == "block" and p.band(92.0) == "ok"       # 0..100 inputs accepted
    assert dataclasses.replace(p, enabled=False).band(0.2) == "unknown"


def test_availability_floor_by_share_band():
    p = AvailabilityPolicy()
    assert p.floor_for_share(10.01) == 0.85
    assert p.floor_for_share(10.0) == 0.88     # > 10 is strict
    assert p.floor_for_share(5.0) == 0.88      # >= 5 is inclusive
    assert p.floor_for_share(4.99) == 0.90
    assert p.floor_for_share(None) == 0.90


def test_engine_config_validates_variant_names():
    cfg = EngineConfig()
    assert cfg.attribution == "ridge_jury" and cfg.rule_profile == "classic"
    assert cfg.role_cohort_min_n == 12
    with pytest.raises(ValueError):
        EngineConfig(attribution="v2")
    with pytest.raises(ValueError):
        EngineConfig(rule_profile="v3")


def test_apply_overrides_isolated_and_reports_unknown():
    base = AvailabilityPolicy()
    new, applied, unknown = apply_overrides(base, {"floor": "0.8", "nonsense": 3})
    assert new.floor == 0.8 and applied == {"floor": 0.8} and unknown == ["nonsense"]
    assert base.floor == 0.85
    t, applied, _ = apply_overrides(TierConfig(), {"warm_min_days": "10"})
    assert t.warm_min_days == 10 and isinstance(t.warm_min_days, int)


def _tele(**kw):
    base = dict(entity_id="c", n_days=30, avg_budget=1000.0, avg_spend=900.0)
    base.update(kw)
    return BudgetTelemetry(**base)


def test_budget_bound_sufficiency_threshold():
    pol = BudgetBoundPolicy(method="sufficiency")
    assert _tele(sufficiency_3d=0.899).is_bound(pol)
    assert not _tele(sufficiency_3d=0.90).is_bound(pol)
    assert not _tele(sufficiency_3d=None).is_bound(pol)
    assert not _tele(sufficiency_3d=0.5, avg_budget=None).is_bound(pol)   # no cap: never bound


def test_budget_bound_exhaustion_rule():
    pol = BudgetBoundPolicy(method="exhaustion")
    assert _tele(exhaust_minute=719, exhaust_days=12).is_bound(pol)
    assert not _tele(exhaust_minute=720, exhaust_days=12).is_bound(pol)
    assert not _tele(exhaust_minute=600, exhaust_days=11).is_bound(pol)             # 11 < 0.4 * 30
    assert not _tele(exhaust_minute=600, exhaust_days=20, avg_spend=499.0).is_bound(pol)
    assert _tele(exhaust_minute=600, exhaust_days=20, avg_spend=500.0).is_bound(pol)
    assert _tele(exhaust_minute=600, exhaust_days=None).is_bound(pol)               # count unknown: skipped


def test_budget_bound_other_methods_and_auto():
    assert _tele(capped_days=18, active_days=30).is_bound(method="capped_days")
    assert not _tele(capped_days=17, active_days=30).is_bound(method="capped_days")
    assert _tele(held_days=3).is_bound(method="held_days")
    assert not _tele(held_days=2).is_bound(method="held_days")
    assert _tele(sufficiency_3d=0.4).is_bound()                    # auto -> sufficiency
    assert not _tele(sufficiency_3d=1.0, exhaust_minute=300).is_bound()
    assert _tele(exhaust_minute=300).is_bound()                    # auto -> exhaustion fallback
    assert _tele().utilisation == pytest.approx(90.0)
    with pytest.raises(ValueError):
        _tele().is_bound(method="nope")


# ---------------------------------------------------------------- metrics

def test_ratio_helpers_return_none_not_zero():
    assert cpm(50.0, 0) is None and roas(10.0, 0) is None
    assert cpm(50.0, 1000) == pytest.approx(50.0)
    assert safe_div(1, 0) is None and safe_div(None, 2) is None and safe_div(1, 4) == 0.25
    assert pct_change(110, 100) == pytest.approx(0.1) and pct_change(1, 0) is None


def test_cell_from_rows_sums_and_weighted_position(make_perf_row):
    rows = [
        make_perf_row(0, "a", spend=100, impressions=1000, sales=300, units=3, position=2.0, bid=110.0),
        make_perf_row(0, "b", spend=300, impressions=2000, sales=600, units=4, position=5.0, bid=160.0),
        make_perf_row(1, "a", spend=0, impressions=500, sales=0, units=0, position=999999.0),
        make_perf_row(1, "c", spend=50, impressions=500, sales=0, units=0, position=None),
    ]
    c = cell_from_rows(rows)
    assert c.spend == 450 and c.impressions == 4000 and c.sales == 900 and c.units == 7
    assert c.pos_impressions == 3000 and c.position == pytest.approx((2 * 1000 + 5 * 2000) / 3000)
    assert c.bid == pytest.approx((110 * 100 + 160 * 300) / 400)
    assert c.cpm == pytest.approx(112.5) and c.roas == pytest.approx(2.0)
    assert c.entities == 3 and c.days == 2            # "a" spent on day 0; sentinel row still counts for impressions
    assert c.clicks is None and c.ctr is None


def test_identity_is_exact(make_perf_row, perf_rows):
    c = cell_from_rows([make_perf_row(i, spend=97.3 + i, impressions=1234 + 7 * i, sales=411.0 + i, units=3 + i)
                        for i in range(9)])
    assert abs(identity_gap(c)) < 1e-9
    assert roas_from_identity(c.icr, c.asp, c.cpm) == pytest.approx(c.roas, abs=1e-9)
    assert c.rpm == pytest.approx(1000 * c.icr * c.asp, abs=1e-9)
    for cell in group_cells(perf_rows, "entity_id").values():
        gap = identity_gap(cell)
        assert gap is None or abs(gap) < 1e-9
    assert identity_gap(MetricCell()) is None


def test_sum_cells_matches_cell_of_all_rows(perf_rows):
    parts = group_cells(perf_rows, ("entity_id", "geo"))
    total, direct = sum_cells(parts.values()), cell_from_rows(perf_rows)
    for f in ("spend", "impressions", "sales", "units", "pos_weight", "pos_impressions", "new_users"):
        assert getattr(total, f) == pytest.approx(getattr(direct, f), rel=1e-12)
    assert total.cpm == pytest.approx(direct.cpm, abs=1e-9)
    assert total.position == pytest.approx(direct.position, abs=1e-9)
    assert total.entities is None and total.days == direct.days == 90


def test_position_validity_and_buckets():
    assert position_is_valid(1) and position_is_valid(100) and not position_is_valid(100.01)
    assert not position_is_valid(0.99) and not position_is_valid(None) and not position_is_valid(float("nan"))
    assert clip_position(999999) is None and clip_position(3) == 3.0
    assert position_bucket(2.4) == "2" and position_bucket(20.4) == "20"
    assert position_bucket(21) == "21+" and position_bucket(64) == "21+" and position_bucket(500) is None


def test_normalise_keyword():
    assert normalise_keyword("  Protein  Bar ") == "protein  bar"
    assert normalise_keyword("  Protein  Bar ", collapse_whitespace=True) == "protein bar"
    assert normalise_keyword(None) == ""


def test_group_cells_key_forms(make_perf_row):
    rows = [make_perf_row(0, "a", geo="g1"), make_perf_row(0, "b", geo="g1"), make_perf_row(1, "a", geo="g2")]
    assert set(group_cells(rows, "geo")) == {"g1", "g2"}
    assert set(group_cells(rows, ("entity_id", "geo"))) == {("a", "g1"), ("b", "g1"), ("a", "g2")}
    assert group_cells(rows, lambda r: r.date.day)[1].spend == 200


# ---------------------------------------------------------------- windows

def test_trailing_windows_anchor_to_data_max():
    wp = trailing_windows(date(2026, 3, 31), 14)
    assert wp.current.as_tuple() == (date(2026, 3, 18), date(2026, 3, 31))
    assert wp.prior.as_tuple() == (date(2026, 3, 4), date(2026, 3, 17))
    assert wp.current.days == wp.prior.days == 14


def test_default_windows_standard_and_fallbacks():
    full = [D0 + timedelta(days=i) for i in range(90)]
    wp = default_windows(full)
    assert wp.cmp == "trailing" and wp.current.days == 30 and wp.prior.days == 30
    assert wp.prior.end + timedelta(days=1) == wp.current.start and wp.current.end == full[-1]
    # only 3 days fall in the standard prior window -> prior widens to everything earlier
    sparse = [D0 + timedelta(days=i) for i in (0, 1, 2, 3, 30, 31, 32)] + [D0 + timedelta(days=60 + i) for i in range(30)]
    wp2 = default_windows(sparse)
    assert wp2.cmp == "fallback" and wp2.prior.start == D0 and wp2.note
    # 8 days in total -> first / last k = max(5, 4) = 5
    wp3 = default_windows([D0 + timedelta(days=i) for i in range(8)])
    assert wp3.current.start == D0 + timedelta(days=3) and wp3.prior.end == D0 + timedelta(days=4)
    assert "overlap" in wp3.note
    assert default_windows([]) is None


def test_default_windows_accepts_rows(perf_rows):
    wp = default_windows(perf_rows, days=30)
    assert wp.current.end == max(r.date for r in perf_rows)


def test_resolve_window_wow_dod_and_long_ranges():
    wp = resolve_window("2026-07-13", "2026-07-19", "wow")
    assert wp.prior.as_tuple() == (date(2026, 7, 6), date(2026, 7, 12)) and wp.note == ""
    wp = resolve_window("2026-07-19", "2026-07-10", "wow")          # reversed, 10 days > 7
    assert wp.current.as_tuple() == (date(2026, 7, 10), date(2026, 7, 19))
    assert wp.prior.as_tuple() == (date(2026, 6, 30), date(2026, 7, 9)) and "previous 10-day" in wp.note
    wp = resolve_window("2026-07-15", "2026-07-15", "dod")
    assert wp.prior.as_tuple() == (date(2026, 7, 14), date(2026, 7, 14))
    assert wp.as_dict()["days"] == 1 and wp.as_dict()["cmp"] == "dod"
    assert resolve_window("2026-07-13", "2026-07-19").cmp == "wow"   # default


def test_resolve_window_mom_clamp_and_fallback():
    wp = resolve_window("2026-03-29", "2026-03-31", "mom")
    assert wp.prior.as_tuple() == (date(2026, 2, 28), date(2026, 2, 28))   # clamped to the shorter month
    wp = resolve_window("2026-03-10", "2026-03-16", "mom")
    assert wp.prior.as_tuple() == (date(2026, 2, 10), date(2026, 2, 16))
    wp = resolve_window("2026-01-05", "2026-01-11", "mom")
    assert wp.prior.as_tuple() == (date(2025, 12, 5), date(2025, 12, 11))
    wp = resolve_window("2026-02-01", "2026-03-15", "mom")                  # spans more than a month
    assert wp.prior.end < wp.current.start and wp.prior.days == wp.current.days and wp.note


def test_resolve_window_strict_input_and_clamp():
    assert resolve_window("2026-7-1", "2026-07-05") is None
    assert resolve_window(None, "2026-07-05") is None
    assert resolve_window("2026-07-01", "2026-07-05", "yoy") is None
    assert parse_iso_date("2026-02-30") is None and parse_iso_date("2026-02-28") == date(2026, 2, 28)
    wp = resolve_window("2026-07-01", "2026-07-20", "wow", clamp_to=date(2026, 7, 10))
    assert wp.current.end == date(2026, 7, 10) and "clamped" in wp.note


def test_reporting_window_pinned_examples():
    assert reporting_window(date(2026, 10, 22)).as_tuple() == (date(2026, 10, 1), date(2026, 10, 18))
    w = reporting_window(date(2026, 11, 2))
    assert w.end == date(2026, 11, 1) and (w.end - w.start).days == 20
    assert reporting_window(date(2026, 10, 18)).end == date(2026, 10, 11)     # a Sunday steps back a week


def test_drill_buckets():
    wp = drill_buckets([D0 + timedelta(days=i) for i in range(75)])
    dmax = D0 + timedelta(days=74)
    assert wp.current.as_tuple() == (dmax - timedelta(days=29), dmax)
    assert wp.prior.as_tuple() == (dmax - timedelta(days=59), dmax - timedelta(days=30))
    assert drill_buckets([]) is None


def test_route_tier_boundaries():
    assert [route_tier(n) for n in (0, 6, 7, 20, 21, 34, 35, 400)] == [
        DataTier.COLD, DataTier.COLD, DataTier.WARM, DataTier.WARM,
        DataTier.MATURE, DataTier.MATURE, DataTier.FULL, DataTier.FULL]


def test_active_days_uses_daily_totals(make_perf_row):
    rows = [make_perf_row(0, units=0), make_perf_row(0, units=2), make_perf_row(1, units=0),
            make_perf_row(2, spend=0), make_perf_row(3, impressions=0, units=1)]
    assert active_days(rows) == [D0]
    assert active_days(rows, require_units=False) == [D0, D0 + timedelta(days=1)]


def test_filter_split_and_recent_half(make_perf_row):
    rows = [make_perf_row(i) for i in range(28)]
    wp = trailing_windows(rows[-1].date, 14)
    pri, cur = split_rows(rows, wp)
    assert len(pri) == len(cur) == 14 and len(filter_rows(rows, wp.current)) == 14
    assert isinstance(wp, WindowPair)
    assert recent_half_start(date(2026, 3, 31), 30) == date(2026, 3, 17)


# ---------------------------------------------------------------- liveness

def test_is_dead_hard_soft_live():
    for hard in ("STOPPED", "ARCHIVED", "COMPLETED", "PAUSED"):
        assert is_dead(hard, 9999.0)
    for soft in ("ON_HOLD", "UNDER_REVIEW", "DRAFT", "SCHEDULED"):
        assert is_dead(soft, 0.0) and is_dead(soft, None) and not is_dead(soft, 0.01)
    assert not is_dead("ACTIVE", 0.0) and not is_dead("something new", 0.0) and not is_dead(None, 0.0)


def test_status_map_overrides_default_vocabulary():
    custom = {"PAUSED": "budget_held", "Sleeping": CanonicalStatus.REMOVED}
    assert canonical_status("PAUSED", custom) is CanonicalStatus.BUDGET_HELD
    assert not is_dead("PAUSED", 5.0, status_map=custom) and is_dead("paused", 0.0, status_map=custom)
    assert is_dead("sleeping", 100.0, status_map=custom)
    assert liveness_class(CanonicalStatus.PRE_LAUNCH) is LivenessClass.SOFT_DEAD
    assert is_dead(LivenessClass.HARD_DEAD, 10.0) and not is_dead(LivenessClass.LIVE, 0.0)


def test_stale_stopped():
    assert not stale_stopped("PAUSED", spend_recent=10.0)        # hard dead but spent in the last 7 days
    assert stale_stopped("PAUSED", spend_recent=0.0)
    assert stale_stopped("ON_HOLD", spend_recent=50.0, spend_3d=0.0)
    assert not stale_stopped("ON_HOLD", spend_recent=50.0, spend_3d=1.0)
    assert not stale_stopped("ACTIVE", spend_recent=0.0)


def test_dead_for_window_and_stopped_since():
    assert not dead_for_window("PAUSED", spend_in_window=1.0, spend_3d=0.0)
    assert dead_for_window("PAUSED", spend_in_window=0.0, spend_3d=0.0)
    assert not dead_for_window("ACTIVE", 0.0, 0.0)
    assert stopped_since_window("PAUSED", 1.0, 0.0)
    assert not stopped_since_window("ACTIVE", 1.0, 0.0)
    assert not stopped_since_window("PAUSED", 0.0, 0.0)


def test_recent_spend(make_perf_row):
    rows = [make_perf_row(i, "a", spend=10.0) for i in range(10)] + [make_perf_row(0, "b", spend=7.0)]
    anchor = D0 + timedelta(days=9)
    assert recent_spend(rows, anchor, 3, "a") == 30.0
    assert recent_spend(rows, anchor, 3) == {"a": 30.0, "b": 0.0}
