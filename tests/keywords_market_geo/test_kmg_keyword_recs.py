"""CUT / ADD recommenders, leaks, re-slot, niches, cannibalisation."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from cpm_lineage.config import Economics
from cpm_lineage.keywords import (COMPETITOR, GENERIC, OWN, SIBLING, KeywordConfig, KeywordStat, RelevanceRow,
                                  RoleCohort, ShelfPresence, acquisition_spare, add_recs, build_lexicon,
                                  cannibalisation_rate, cut_recs, exhausted_days_by_keyword, incremental_roas,
                                  is_fragment, keyword_leaks, keyword_recs, keyword_stats, niche_class,
                                  owned_niches, relevance_pool, role_cohorts, score_add, shelf_presence)
from cpm_lineage.schema import BudgetDayRow, MarketRow, SuggestedBid

CFG = KeywordConfig(cohort_min_n=1)
D0 = date(2026, 1, 1)


def _cohorts(**kw):
    base = dict(n=20, roas_p10=0.5, roas_p25=1.2, roas_p50=2.0, roas_p75=3.0, cpnu_p25=80.0, cpnu_p50=100.0)
    base.update(kw)
    return RoleCohort(**base)


# ----------------------------------------------------------------- CUT

def _cut_stats():
    return [
        KeywordStat("bleeder", spend=20000, sales=6000, new_users=0),
        KeywordStat("cheap acquirer", spend=20000, sales=8000, new_users=200),      # CPNU 100
        KeywordStat("dear acquirer", spend=20000, sales=8000, new_users=50),        # CPNU 400
        KeywordStat("profitable", spend=300000, sales=900000, new_users=10),
        KeywordStat("immaterial", spend=5000, sales=0, new_users=0),
    ]


def test_cut_pinned_cases():
    res = cut_recs(_cut_stats(), {}, {GENERIC: _cohorts()}, cfg=CFG)
    by = {r.keyword: r for r in res.rows}
    assert by["bleeder"].verdict == "cut" and by["bleeder"].bleed == 14000
    assert by["cheap acquirer"].verdict == "review_acquisition"
    assert by["dear acquirer"].verdict == "cut"
    assert "profitable" not in by and "immaterial" not in by
    assert [r.keyword for r in res.rows][0] == "bleeder"          # sorted by bleed
    assert res.floor == pytest.approx(max(8000, 0.03 * 365000))


def test_cut_floor_is_scope_relative_and_strict():
    stats = [KeywordStat("a", spend=8000, sales=0), KeywordStat("b", spend=8001, sales=0)]
    res = cut_recs(stats)
    assert [r.keyword for r in res.rows] == ["b"]                  # spend must exceed the floor
    big = stats + [KeywordStat("c", spend=1_000_000, sales=5_000_000)]
    assert cut_recs(big).rows == ()                                # 3 % of scope spend now dominates


def test_cut_roas_gate_at_breakeven_and_currency_scaling():
    assert cut_recs([KeywordStat("a", spend=10000, sales=10000)]).rows == ()          # ROAS exactly 1.0
    assert len(cut_recs([KeywordStat("a", spend=10000, sales=9999)]).rows) == 1
    econ = Economics(currency_unit=0.01)                                               # floor becomes 80
    assert len(cut_recs([KeywordStat("a", spend=100, sales=10), KeywordStat("b", spend=3000, sales=9000)],
                        economics=econ).rows) == 1
    econ2 = Economics(breakeven=2.0)
    assert len(cut_recs([KeywordStat("a", spend=10000, sales=15000)], economics=econ2).rows) == 1


def test_acquisition_spare_boundaries():
    assert acquisition_spare(450, 3, 100.0)             # CPNU 150 == 1.5 x 100, 3 new users
    assert not acquisition_spare(451, 3, 100.0)
    assert not acquisition_spare(200, 2, 100.0)         # fewer than 3 new users
    assert not acquisition_spare(200, None, 100.0)      # platform does not report new users
    assert not acquisition_spare(200, 5, None)          # no cohort reference


def test_cut_spare_disabled_by_thin_cohort_and_empty_scope():
    res = cut_recs(_cut_stats(), {}, {GENERIC: _cohorts(n=5)})        # default min n is 12
    assert {r.verdict for r in res.rows} == {"cut"}
    assert cut_recs([]).reason


def test_cut_on_synthetic_bleeding_keyword(synthetic_dataset, windows_30, synthetic_events):
    d = synthetic_dataset
    stats = keyword_stats(d.perf, windows_30.current, profile=d.profile)
    roles = {t.value: t.role for t in d.targeting_units}
    res = cut_recs(stats, roles, role_cohorts(stats, roles))
    assert [r.keyword for r in res.rows] == [synthetic_events["bleeding_keyword"].targeting_unit]
    assert res.rows[0].verdict == "cut" and res.rows[0].role == COMPETITOR


# ----------------------------------------------------------------- ADD

def test_score_add_properties():
    assert score_add(0.9, 50000, 10000, 2.0, None) > score_add(0.6, 50000, 10000, 2.0, None)   # monotone in relevance
    assert score_add(0.8, 50000, 0, 2.0, None) > 0                                            # explorer-only scores
    assert score_add(0.8, 50000, 20000, 2.0, None) > score_add(0.8, 50000, 0, 2.0, None)      # own presence ranks
    missing, known = score_add(None, 50000, 10000, 2.0, None), score_add(0.9, 50000, 10000, 2.0, None)
    assert 0 < missing < known                                                               # lower, not a veto
    assert score_add(0.8, 50000, 10000, 2.0, 10) == pytest.approx(
        1.25 * score_add(0.8, 50000, 10000, 2.0, 11), abs=0.11)                               # organic bonus
    assert score_add(0.8, 50000, 10000, 50.0, None) == score_add(0.8, 50000, 10000, 5.0, None)  # payoff cap
    assert score_add(1.0, 999_999, 999_999, 5.0, None) == pytest.approx(100.0, abs=0.1)


def test_is_fragment():
    uni = ["gra", "granola", "granola bar multipack", "soup", "so", "dry granola"]
    assert is_fragment("gra", uni)
    assert not is_fragment("granola", uni)                 # followed by a space in the longer term
    assert not is_fragment("granola bar multipack", uni)
    assert is_fragment("so", uni)                          # shorter than 3 characters
    assert not is_fragment("dry granola", uni)             # multi-word terms are never fragments
    assert not is_fragment("soup", uni)


def test_relevance_pool_floor_and_max():
    rows = [RelevanceRow("Face Serum", 0.4, 100, "p1"), RelevanceRow("face serum", 0.7, 50, "p2"),
            RelevanceRow("weak", 0.49, 9999, "p1"), {"keyword": "other", "relevance": 0.9, "volume": 5,
                                                     "product_id": "zz"}]
    pool = relevance_pool(rows, product_ids={"p1", "p2"})
    assert set(pool) == {"face serum"}
    assert pool["face serum"].relevance == 0.7 and pool["face serum"].volume == 100
    assert "other" in relevance_pool(rows)


def _presence():
    return [
        ShelfPresence("face serum", 50000, 0, 50000, 2, "generic", 6.0),
        ShelfPresence("fac", 9000, 0, 9000, 1, "generic", 4.0),               # fragment of "face serum"
        ShelfPresence("acme glow", 80000, 0, 80000, 2, "brand", 1.2),
        ShelfPresence("rivalo cream", 20000, 0, 20000, 1, "competition", 30.0),
        ShelfPresence("sisterly cream", 10000, 0, 10000, 1, "competition", 30.0),
        ShelfPresence("night cream", 30000, 0, 30000, 1, "generic", 15.0),    # already bid
        ShelfPresence("feed", 99999, 0, 99999, 1, None, None),
    ]


def test_add_recs_roles_gate_order(synthetic_profile):
    lex = build_lexicon([("Acme Glow", True), ("Rivalo", False)], sibling_brands=["Sisterly"])
    cohorts = {GENERIC: _cohorts(roas_p50=2.0), COMPETITOR: _cohorts(roas_p50=0.6), OWN: _cohorts(roas_p50=9.0)}
    res = add_recs(_presence(), {"Night Cream"}, cohorts, lexicon=lex, profile=synthetic_profile, cfg=CFG,
                   relevance={"hydrating serum": RelevanceRow("hydrating serum", 0.8, 40000)},
                   suggested_bids={"face serum": SuggestedBid("face serum", low=90, high=140, searches=1234)})
    by = {r.keyword: r for r in res.rows}
    assert "night cream" not in by and "feed" not in by and "fac" not in by
    assert res.n_fragments == 1
    assert "rivalo cream" not in by and res.n_gated == 1            # competitor cohort loses money
    assert by["sisterly cream"].role == SIBLING                     # conquest term re-labelled; no sibling cohort
    assert by["sisterly cream"].payoff_source == GENERIC            # ... so it is priced on the generic cohort
    assert by["acme glow"].intent == "defense" and by["acme glow"].role == OWN
    assert [r.intent for r in res.rows] == sorted((r.intent for r in res.rows), key=lambda i: i != "growth")
    assert res.rows[-1].keyword == "acme glow"                      # growth first even though it scores highest
    assert by["hydrating serum"].source == "relevance" and by["hydrating serum"].demand == 0
    assert by["face serum"].suggested_low == 90 and by["face serum"].searches == 1234
    growth = [r.score for r in res.rows if r.intent == "growth"]
    assert growth == sorted(growth, reverse=True)


def test_add_gate_unknown_passes_and_boundary():
    pres = [ShelfPresence("alpha term", 1000, 0, 1000, 1, None, None)]
    assert len(add_recs(pres, set()).rows) == 1                                           # unknown payoff passes
    assert len(add_recs(pres, set(), {GENERIC: _cohorts(roas_p50=1.0)}, cfg=CFG).rows) == 1   # exactly at the bar
    res = add_recs(pres, set(), {GENERIC: _cohorts(roas_p50=0.99)}, cfg=CFG)
    assert res.rows == () and res.reason
    assert add_recs(None, set()).reason and add_recs(None, set()).rows == ()


def test_shelf_presence_from_market_rows(synthetic_dataset, windows_30):
    d = synthetic_dataset
    pres = shelf_presence(d.market, windows_30.current, profile=d.profile)
    p = pres["protein bar"]
    assert p.own_impressions == pytest.approx(p.own_ad_impressions + p.own_organic_impressions)
    assert p.native_role == "generic"
    assert 1 <= (p.organic_position or 1) <= 500


def test_keyword_recs_scope_and_degradation(synthetic_dataset, windows_30):
    d = synthetic_dataset
    roles = {t.value: t.role for t in d.targeting_units}
    out = keyword_recs(d.perf, windows_30.current, entity_ids=[], roles=roles, profile=d.profile)
    assert out.cut.rows == () and out.add.rows == () and out.add.reason     # empty list matches nothing
    out = keyword_recs(d.perf, windows_30.current, roles=roles, market_rows=d.market, profile=d.profile)
    bid = set(keyword_stats(d.perf, windows_30.current, profile=d.profile))
    assert out.add.rows and all(r.keyword not in bid for r in out.add.rows)
    assert out.n_entities is None
    assert keyword_recs(d.perf, windows_30.current, entity_ids=["cmp-conquest", "cmp-brand"]).n_entities == 2


# ----------------------------------------------------------------- leaks / re-slot

def _row(make, kw, ent, spend, sales, impr, pos, day=0, **kw_):
    return make(day, entity_id=ent, spend=spend, sales=sales, impressions=impr, targeting_unit=kw, position=pos,
                **kw_)


def _leak_rows(make):
    return [
        _row(make, "split leak", "c1", 30000, 10000, 300000, 3.0),       # 60k across two campaigns, ROAS 0.5
        _row(make, "split leak", "c2", 30000, 20000, 300000, 3.0),
        _row(make, "deep dear", "c1", 80000, 160000, 200000, 8.0),       # CPM 400, position 8 -> reslot
        _row(make, "top cheap", "c1", 90000, 270000, 900000, 2.0),       # CPM 100
        _row(make, "mid", "c1", 70000, 140000, 400000, 7.0),             # CPM 175: deep but not top-quartile
        _row(make, "small leak", "c1", 49999, 0, 100000, 9.0),           # under the materiality floor
        _row(make, "reach only", "r1", 60000, 0, 900000, None),
        _row(make, "feed", "c1", 500000, 0, 900000, 1.0),
    ]


def test_leaks_cross_campaign_and_reslot(make_perf_row, synthetic_profile):
    res = keyword_leaks(_leak_rows(make_perf_row), judged_entity_ids={"c1", "c2"}, profile=synthetic_profile)
    assert [r.keyword for r in res.leaks] == ["split leak"]
    assert res.leaks[0].n_entities == 2 and res.leaks[0].roas == pytest.approx(0.5)
    assert res.cpm_p75 == pytest.approx(175.0)          # sorted CPMs [100, 100, 175, 400][int(0.75 * 3)]
    assert [r.keyword for r in res.reslot] == ["deep dear", "mid"]
    assert [r.keyword for r in res.untracked] == ["reach only"]
    assert res.untracked[0].untracked_spend == 60000


def test_reslot_gates(make_perf_row, synthetic_profile):
    rows = _leak_rows(make_perf_row)
    cfg = KeywordConfig(reslot_min_position=7.5)
    assert [r.keyword for r in keyword_leaks(rows, judged_entity_ids={"c1", "c2"}, cfg=cfg).reslot] == ["deep dear"]
    few = [r for r in rows if r.targeting_unit in ("deep dear", "mid", "top cheap")]
    res = keyword_leaks(few, profile=synthetic_profile)
    assert res.reslot == () and "fewer than 4" in (res.reslot_reason or "")
    from dataclasses import replace
    prof = replace(synthetic_profile, position=replace(synthetic_profile.position, lower_is_better=False))
    assert keyword_leaks(rows, profile=prof).reslot_reason
    assert keyword_leaks([]).reason


def test_leak_on_synthetic(synthetic_dataset, windows_30):
    res = keyword_leaks(synthetic_dataset.perf, windows_30.current, profile=synthetic_dataset.profile)
    assert [r.keyword for r in res.leaks] == ["rivalb bar"]


# ----------------------------------------------------------------- niches / cannibalisation

def _mrow(kw, own, ad, org=0.0, day=0, **kw_):
    return MarketRow(D0 + timedelta(days=day), kw, "own" if own else "rival", own, ad, org, **kw_)


def test_niche_classes_and_order():
    market = []
    for kw, own_ad in [("niche scale", 6000), ("niche top", 6000), ("niche topup", 6000), ("niche fix", 6000),
                       ("niche probe", 6000), ("niche hold", 6000), ("contested", 3999), ("tiny", 100)]:
        total = 10000 if kw != "tiny" else 200
        market += [_mrow(kw, True, own_ad), _mrow(kw, False, total - own_ad)]
    market += [_mrow("brand niche", True, 9000), _mrow("brand niche", False, 1000)]
    stats = {
        "niche scale": KeywordStat("niche scale", spend=9000, sales=18000, pos_weight=3.0, pos_impressions=1.0),
        "niche top": KeywordStat("niche top", spend=8000, sales=16000, pos_weight=1.5, pos_impressions=1.0),
        "niche topup": KeywordStat("niche topup", spend=7000, sales=14000, pos_weight=1.2, pos_impressions=1.0),
        "niche fix": KeywordStat("niche fix", spend=6000, sales=3000),
        "niche probe": KeywordStat("niche probe", spend=499, sales=5000),
        "niche hold": KeywordStat("niche hold", spend=5000, sales=6000),
    }
    res = owned_niches(market, stats, {"brand niche": OWN}, exhausted_days={"niche topup": 2})
    by = {r.keyword: r.klass for r in res.rows}
    assert by == {"niche scale": "scale", "niche top": "hold-top-slot", "niche topup": "topup-budget",
                  "niche fix": "fix-conversion", "niche probe": "probe", "niche hold": "hold"}
    assert [r.keyword for r in res.rows] == ["niche scale", "niche topup", "niche fix", "niche probe",
                                             "niche top", "niche hold"]
    assert res.rows[0].own_ad_sov_pct == pytest.approx(60.0)
    assert owned_niches([], stats, {}).reason


def test_niche_class_boundaries():
    assert niche_class(1.5) == "scale" and niche_class(1.49) == "hold"
    assert niche_class(1.0) == "hold" and niche_class(0.99) == "fix-conversion"
    assert niche_class(None) == "hold"


def test_exhausted_days_by_keyword(make_perf_row):
    anchor = D0 + timedelta(days=9)
    perf = [make_perf_row(9, entity_id="c1", targeting_unit="kw a"), make_perf_row(9, entity_id="c2",
                                                                                   targeting_unit="kw a"),
            make_perf_row(9, entity_id="c3", targeting_unit="kw b")]
    bud = [BudgetDayRow(D0 + timedelta(days=9), "c1", 100, 100, exhausted_at_minute=600),
           BudgetDayRow(D0 + timedelta(days=8), "c2", 100, 100, platform_held=True),
           BudgetDayRow(D0 + timedelta(days=9), "c2", 100, 100, exhausted_at_minute=700),
           BudgetDayRow(D0 + timedelta(days=1), "c1", 100, 100, exhausted_at_minute=600),   # outside 7 days
           BudgetDayRow(D0 + timedelta(days=9), "c3", 100, 50)]
    assert exhausted_days_by_keyword(perf, bud, anchor) == {"kw a": 2, "kw b": 0}


def test_cannibalisation_rate():
    rows = [_mrow("own term", True, 300, 700, unit_role="brand"), _mrow("own term", False, 500, 500),
            _mrow("generic term", True, 900, 100, unit_role="generic")]
    assert cannibalisation_rate(rows) == pytest.approx(0.7)
    assert cannibalisation_rate(rows, roles={"generic term": OWN}) == pytest.approx(0.1)
    assert cannibalisation_rate(rows, own_brand_keywords=["missing"]) is None
    assert incremental_roas(10.0, 0.7) == pytest.approx(3.0)
    assert incremental_roas(10.0, None) is None


def test_synthetic_cannibalisation_in_range(synthetic_dataset, windows_30):
    rate = cannibalisation_rate(synthetic_dataset.market, windows_30.current,
                                roles={t.value: t.role for t in synthetic_dataset.targeting_units})
    assert rate is not None and 0.0 < rate < 1.0
