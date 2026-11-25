"""First-use behaviour of ``diagnose()`` / ``plan()``: one source of truth, guards and clear errors."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest

import cpm_lineage
from cpm_lineage import (AdTypeCapability, Dataset, Economics, EngineConfig, Entity, PerfRow, PipelineConfig,
                         PlatformProfile, SyntheticAdapter, TradingDay, diagnose, plan, render_report)
from cpm_lineage.metrics import fmt_money
from cpm_lineage.schema import BudgetDayRow

END = date(2026, 3, 31)
NH = PipelineConfig(holdout_pct=0.0)


def _days(n=60):
    return [END - timedelta(days=i) for i in range(n)][::-1]


def _book(scale=1.0, targeting_type="keyword", profile=None, n=60):
    """Two line items; the unit ``seg:a`` gets 60% dearer in the last 30 days."""
    perf = []
    for i, d in enumerate(_days(n)):
        recent = i >= n - 30
        perf.append(PerfRow(date=d, entity_id="li-a", spend=3000.0 * scale, impressions=30000.0 / (1.6 if recent else 1.0),
                            sales=9000.0 * scale, units=30.0, targeting_unit="seg:a", targeting_type=targeting_type))
        perf.append(PerfRow(date=d, entity_id="li-b", spend=2000.0 * scale, impressions=20000.0, sales=600.0 * scale,
                            units=2.0, targeting_unit="seg:b", targeting_type=targeting_type))
    ents = [Entity(entity_id="li-a", name="li-a", status="active", daily_budget=3200.0 * scale),
            Entity(entity_id="li-b", name="li-b", status="active", daily_budget=2500.0 * scale)]
    return Dataset(profile=profile or PlatformProfile(), perf=perf, entities=ents)


# ---------------------------------------------------------------- validation

def test_profile_rejects_misspelt_enum_strings():
    for kwargs in (dict(auction_type="first-price"), dict(geo_grain="dma"), dict(bid_unit="per-mille"),
                   dict(status_map={"RUNNING": "actve"}), dict(currency_unit=0.0),
                   dict(trading_day_start_hour=20.0, trading_day_end_hour=6.0)):
        with pytest.raises(ValueError):
            PlatformProfile(**kwargs)
    for kwargs in (dict(capability_class="banana"), dict(judged_on="vibes"), dict(targeting="audience"),
                   dict(levers=frozenset({"budgett"}))):
        with pytest.raises(ValueError):
            AdTypeCapability(name="x", **kwargs)
    with pytest.raises(ValueError):
        Economics(currency_unit=0)
    ok = PlatformProfile(auction_type="first_price", geo_grain="country", status_map={"RUNNING": "active"})
    assert ok.pay_your_bid and Economics().unit == 1.0 and Economics(currency_unit=0.5).money(100) == 50.0


def test_string_dates_fail_with_a_clear_message():
    row = PerfRow(date="2026-03-31", entity_id="e", spend=1.0, impressions=10.0)   # type: ignore[arg-type]
    with pytest.raises(TypeError, match="datetime.date"):
        diagnose([row])


def test_top_level_exports_cover_what_signatures_reference():
    for name in ("CanonicalStatus", "CapabilityClass", "TargetingRole", "AuctionType", "Lever", "Change",
                 "ActionsConfig", "PipelineConfig", "VerdictConfig", "CpmConfig", "build_records"):
        assert name in cpm_lineage.__all__ and hasattr(cpm_lineage, name), name


# ---------------------------------------------------------------- one source of truth

def test_profile_currency_unit_survives_a_partial_config():
    data = _book(scale=0.01, profile=PlatformProfile(currency_unit=0.01))
    base = plan(data, pipeline_cfg=NH)
    with_cfg = plan(data, config=EngineConfig(economics=Economics(breakeven=1.0)), pipeline_cfg=NH)
    assert [(c.title, c.entity_id, c.status) for c in with_cfg.actions] == [(c.title, c.entity_id, c.status)
                                                                            for c in base.actions]
    d0, d1 = diagnose(data), diagnose(data, config=EngineConfig(economics=Economics(breakeven=1.0)))
    assert [f.summary for f in d0.findings] == [f.summary for f in d1.findings]
    # an explicit config value still wins
    explicit = plan(data, config=EngineConfig(economics=Economics(currency_unit=1.0)), pipeline_cfg=NH)
    assert [(c.title, c.entity_id) for c in explicit.actions] != [(c.title, c.entity_id) for c in base.actions]


def test_money_rules_are_scale_invariant_when_the_unit_is_declared():
    big = plan(_book(), pipeline_cfg=NH)
    small = plan(_book(scale=0.01, profile=PlatformProfile(currency_unit=0.01)), pipeline_cfg=NH)
    assert [(c.title, c.entity_id, c.status, c.change.pct if c.change else None) for c in big.actions] == \
           [(c.title, c.entity_id, c.status, c.change.pct if c.change else None) for c in small.actions]


def test_undeclared_small_money_scale_is_flagged():
    d = diagnose(_book(scale=0.01))
    note = next(n for n in d.notes if n.startswith("money scale"))
    assert "currency_unit=0.01" in note
    assert not [n for n in diagnose(_book()).notes if n.startswith("money scale")]
    assert not [n for n in diagnose(_book(scale=0.01, profile=PlatformProfile(currency_unit=0.01))).notes
                if n.startswith("money scale")]


def _capped_book(profile):
    perf, budget = [], []
    for d in _days():
        perf.append(PerfRow(date=d, entity_id="hero", spend=1000.0, impressions=10000.0, sales=6000.0, units=20.0))
        budget.append(BudgetDayRow(date=d, entity_id="hero", daily_budget=1000.0, spend=1000.0, sales=6000.0,
                                   impressions=10000.0, exhausted_at_minute=540.0))
    ent = Entity(entity_id="hero", name="hero", status="active", daily_budget=1000.0)
    return Dataset(profile=profile, perf=perf, entities=[ent], budget_days=budget)


def test_trading_day_comes_from_the_profile_in_both_calls():
    day = PlatformProfile()                                                    # 06:00 - 18:00, fund to 22:00
    allday = PlatformProfile(trading_day_start_hour=0.0, trading_day_end_hour=24.0, funding_target_hour=24.0)
    stake_day = diagnose(_capped_book(day)).entities["hero"].probe.rev_at_stake
    stake_all = diagnose(_capped_book(allday)).entities["hero"].probe.rev_at_stake
    assert stake_all > stake_day > 0                                           # 15 unserved hours versus 9
    # an explicit config overrides the profile, for the diagnosis and for the plan's raise size alike
    cfg = EngineConfig(trading_day=TradingDay(start_hour=0.0, end_hour=24.0, funding_target_hour=24.0))
    assert diagnose(_capped_book(day), config=cfg).entities["hero"].probe.rev_at_stake == pytest.approx(stake_all)

    def raise_pct(profile, config=None):
        p = plan(_capped_book(profile), config=config, pipeline_cfg=NH, spine=False)
        return next(c.change.pct for c in p.actions if c.type == "budget" and c.direction == "up")

    assert raise_pct(allday) == raise_pct(day, cfg)
    noon = EngineConfig(trading_day=TradingDay(start_hour=0.0, end_hour=24.0, funding_target_hour=12.0))
    assert raise_pct(day, noon) < raise_pct(allday)


# ---------------------------------------------------------------- ROAS basis

def test_diagnose_and_plan_report_the_same_entity_roas():
    data = SyntheticAdapter().dataset
    d, p = diagnose(data), plan(data, pipeline_cfg=NH)
    card = next(c for c in p.actions if c.entity_id == "cmp-winner" and c.type == "budget")
    assert f"ROAS {d.entities['cmp-winner'].roas:.2f}x" in card.trigger
    assert "direct + indirect" in d.roas_basis and "ROAS basis: direct + indirect sales" in render_report(d)
    direct = EngineConfig(indirect_sales_weight=0.0)
    d0, p0 = diagnose(data, config=direct), plan(data, config=direct, pipeline_cfg=NH)
    assert d0.entities["cmp-winner"].roas < d.entities["cmp-winner"].roas
    card0 = next(c for c in p0.actions if c.entity_id == "cmp-winner" and c.type == "budget")
    assert f"ROAS {d0.entities['cmp-winner'].roas:.2f}x" in card0.trigger and d0.roas_basis == "direct sales only"
    with pytest.raises(ValueError):
        EngineConfig(indirect_sales_weight=1.5)


# ---------------------------------------------------------------- guards

def test_no_prior_window_is_reported_as_no_comparison():
    data = SyntheticAdapter().dataset
    last = max(r.date for r in data.perf)
    few = dataclasses.replace(data, perf=[r for r in data.perf if (last - r.date).days < 3])
    d = diagnose(few)
    assert d.has_comparison is False and any(n.startswith("no comparison window") for n in d.notes)
    assert not d.of_kind("bleeding_keyword") and not d.of_kind("own_collision")     # three days is not evidence
    text = render_report(d)
    assert "no comparison window" in text and "log-points" not in text
    full = diagnose(data)
    assert full.has_comparison and full.of_kind("bleeding_keyword") and "log-points" in render_report(full)


def test_non_keyword_targeting_is_flagged_then_honoured():
    skipped = diagnose(_book(targeting_type="audience"))
    assert not skipped.of_kind("cpm_rise")
    assert any("biddable_targeting_types" in n and "'audience'" in n for n in skipped.notes)
    declared = PlatformProfile(biddable_targeting_types=frozenset({"keyword", "Audience"}))
    d = diagnose(_book(targeting_type="audience", profile=declared))
    assert [f.targeting_unit for f in d.of_kind("cpm_rise")] == ["seg:a"]
    assert not any("biddable_targeting_types" in n for n in d.notes)
    assert [f.targeting_unit for f in diagnose(_book()).of_kind("cpm_rise")] == ["seg:a"]


def test_money_is_formatted_by_magnitude():
    assert [fmt_money(v) for v in (3.5, 5.61, 42.0, 149.4, 8255.0, 0, None)] == [
        "3.50", "5.61", "42.0", "149", "8,255", "0", "n/a"]
    d = diagnose(_book(scale=0.001, profile=PlatformProfile(currency_unit=0.001,
                                                            biddable_targeting_types=frozenset({"keyword"}))))
    rise = d.of_kind("cpm_rise")[0]
    assert "(0.10 -> 0.16)" in rise.summary
