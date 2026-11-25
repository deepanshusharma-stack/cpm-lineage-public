"""Adapter contract, reference adapters and the synthetic ground truth."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest

from cpm_lineage.adapters import BaseAdapter, Dataset, PlatformAdapter, PlatformProfile
from cpm_lineage.adapters.base import AdTypeCapability, build_records, coerce_value
from cpm_lineage.adapters.csv import CsvAdapter, read_perf_csv
from cpm_lineage.adapters.dataframe import DataFrameAdapter, records_from_frame
from cpm_lineage.adapters.synthetic import CAMPAIGNS, SyntheticAdapter, generate
from cpm_lineage.liveness import is_dead, stale_stopped
from cpm_lineage.metrics import cell_from_rows, group_cells, share
from cpm_lineage.schema import BudgetTelemetry, CanonicalStatus, DataTier, Entity, PerfRow, ShelfRow
from cpm_lineage.windows import filter_rows, split_rows, tier_for_rows


# ---------------------------------------------------------------- profile

def test_default_profile_is_generic():
    p = PlatformProfile()
    assert not p.has_clicks and not p.pay_your_bid and not p.has_geo
    cap = p.capability("Some New Format")
    assert cap.capability_class == "keyword_auction"            # unknown type: fully levered
    assert p.canonical_ad_type("some new format") == "Some New Format"
    assert p.canonical_ad_type(None) == "Unknown" and p.canonical_ad_type("  ") == "Unknown"
    assert p.is_aggregate_geo("Others") and p.is_aggregate_geo("other west") and not p.is_aggregate_geo("otherville")
    assert not p.is_search_keyword(None) and p.is_search_keyword("protein bar")


def test_profile_capability_matrix(synthetic_profile):
    p = synthetic_profile
    assert p.pay_your_bid and p.has_geo
    assert p.canonical_ad_type(" RECO ") == "recommendation"
    assert p.allowed_levers("keyword_listing") >= {"budget", "bid", "keyword", "geo"}
    assert p.allowed_levers("reco") == {"budget", "watch"}
    assert p.allowed_levers("banner") == {"watch"}
    assert p.capability("recommendation").roas_deflator == 0.6 and p.capability("reco").auto_targeted
    assert p.keyword_grain_types() == {"keyword_listing"}
    assert p.no_attribution_types() == {"featured_listing"}
    assert p.canonical_status("BUDGET_HELD") is CanonicalStatus.BUDGET_HELD
    assert not p.is_search_keyword("feed")
    custom = AdTypeCapability(name="x", capability_class="reach", levers=frozenset({"budget"}))
    assert custom.allowed_levers() == {"budget"}


def test_rollup_geo():
    p = PlatformProfile(geo_grain="city", geo_rollup={"suburb-a": "metro-1"})
    assert p.rollup_geo("suburb-a") == "metro-1" and p.rollup_geo("metro-2") == "metro-2" and p.rollup_geo(None) is None


def test_adapters_satisfy_protocol(synthetic_adapter):
    assert isinstance(synthetic_adapter, PlatformAdapter)
    empty = BaseAdapter()
    assert isinstance(empty, PlatformAdapter)
    assert list(empty.perf_rows()) == [] and list(empty.market_rows()) == [] and empty.profile.name == "generic"
    assert Dataset().latest_date is None


# ---------------------------------------------------------------- mapping

def test_coerce_value():
    assert coerce_value("1,234.5", "float") == 1234.5
    assert coerce_value("", "float | None") is None and coerce_value(float("nan"), "float | None") is None
    assert coerce_value("2026-03-01 00:00:00", "date") == date(2026, 3, 1)
    assert coerce_value("yes", "bool | None") is True and coerce_value("0", "bool") is False
    assert coerce_value(7.0, "int") == 7


def test_build_records_mapping_defaults_and_errors():
    recs = [{"day": "2026-01-02", "cid": "c1", "cost": "10.5", "impr": "1000", "kw": " Protein Bar "}]
    cmap = {"date": "day", "entity_id": "cid", "spend": "cost", "impressions": "impr", "targeting_unit": "kw"}
    rows = build_records(PerfRow, recs, cmap, defaults={"ad_type": "keyword_listing"})
    assert rows == [PerfRow(date=date(2026, 1, 2), entity_id="c1", spend=10.5, impressions=1000.0,
                            targeting_unit="Protein Bar", ad_type="keyword_listing")]
    with pytest.raises(ValueError, match="required field 'spend'"):
        build_records(PerfRow, [{"day": "2026-01-02", "cid": "c1"}], cmap)
    assert build_records(PerfRow, [{"day": "bad", "cid": "c1"}], cmap, skip_invalid=True) == []
    with pytest.raises(ValueError, match="unknown PerfRow fields"):
        build_records(PerfRow, recs, {"not_a_field": "x"})


def test_csv_adapter_round_trip(tmp_path):
    perf = tmp_path / "perf.csv"
    perf.write_text(
        "day,campaign,cost,impr,revenue,orders,keyword,city,format,state\n"
        "2026-01-01,c1,100,1000,300,3, Protein Bar ,suburb-a,KW Listing,ACTIVE\n"
        "2026-01-02,c1,120,1100,,,Protein Bar,metro-2,kw listing,PAUSED\n"
        "2026-01-01,c2,50,900,0,0,,metro-2,Mystery,ACTIVE\n")
    shelf = tmp_path / "shelf.csv"
    shelf.write_text("day,sku,avail,checks\n2026-01-01,p1,12,24\n")
    profile = PlatformProfile(name="demo", geo_grain="city", geo_rollup={"suburb-a": "metro-1"},
                              ad_type_aliases={"kw listing": "keyword_listing"})
    cmap = {"date": "day", "entity_id": "campaign", "spend": "cost", "impressions": "impr",
            "sales": "revenue", "units": "orders", "targeting_unit": "keyword", "geo": "city",
            "ad_type": "format", "status": "state"}
    ad = CsvAdapter(perf, cmap, profile, shelf_path=shelf,
                    shelf_map={"date": "day", "product_id": "sku", "available_count": "avail", "check_count": "checks"})
    rows = ad.perf_rows()
    assert len(rows) == 3 and rows[0].targeting_unit == "protein bar" and rows[0].geo == "metro-1"
    assert rows[0].ad_type == rows[1].ad_type == "keyword_listing" and rows[2].ad_type == "Mystery"
    assert rows[1].sales == 0.0 and rows[2].targeting_unit is None
    ents = {e.entity_id: e for e in ad.entities()}
    assert ents["c1"].status == "PAUSED" and ents["c1"].start_date == date(2026, 1, 1)   # latest row's status
    assert ad.shelf_rows()[0].availability == 0.5
    assert ad.profile.has_feed("shelf") and not ad.profile.has_feed("market") and list(ad.market_rows()) == []
    assert read_perf_csv(perf, cmap)[0].geo == "suburb-a"                               # no profile: no roll-up


class _FakeFrame:
    """Minimal stand-in for a dataframe: only ``to_dict('records')``."""

    def __init__(self, records):
        self._records = records

    def to_dict(self, orient):
        assert orient == "records"
        return list(self._records)


def test_dataframe_adapter_without_pandas():
    frame = _FakeFrame([
        {"date": date(2026, 1, 1), "entity_id": "c1", "spend": 10.0, "impressions": 100.0,
         "sales": float("nan"), "position": float("nan"), "targeting_unit": "A"},
        {"date": "2026-01-02", "entity_id": "c1", "spend": 20.0, "impressions": 200.0, "sales": 50.0},
    ])
    ad = DataFrameAdapter(frame, entities=[{"entity_id": "c1", "daily_budget": "500", "status": "ACTIVE"}])
    rows = ad.perf_rows()
    assert rows[0].sales == 0.0 and rows[0].position is None and rows[0].targeting_unit == "a"
    assert ad.entities() == [Entity(entity_id="c1", daily_budget=500.0, status="ACTIVE")]
    assert records_from_frame(None, PerfRow) == []
    assert cell_from_rows(rows).roas == pytest.approx(50.0 / 30.0)


def test_dataframe_adapter_with_pandas(perf_rows):
    pd = pytest.importorskip("pandas")
    from cpm_lineage.adapters.dataframe import to_dataframe

    sample = perf_rows[:200]
    df = to_dataframe(sample)
    df["date"] = pd.to_datetime(df["date"])
    back = DataFrameAdapter(df, profile=PlatformProfile(name="rt")).perf_rows()
    assert back == sample


# ---------------------------------------------------------------- synthetic

def test_synthetic_is_deterministic_and_seeded(synthetic_dataset):
    again = generate()
    assert again.perf == synthetic_dataset.perf and again.market == synthetic_dataset.market
    assert again.shelf == synthetic_dataset.shelf and again.budget_days == synthetic_dataset.budget_days
    assert generate(seed=8).perf != synthetic_dataset.perf
    with pytest.raises(ValueError):
        generate(days=30)


def test_synthetic_shape(synthetic_dataset):
    ds = synthetic_dataset
    assert (ds.end_date - ds.start_date).days == 89 and ds.latest_date == ds.end_date
    assert {e.entity_id for e in ds.entities} == {c["id"] for c in CAMPAIGNS} and len(ds.entities) == 9
    assert len({r.geo for r in ds.perf}) == 5 and len({r.targeting_unit for r in ds.perf if r.targeting_unit}) >= 12
    assert all(r.clicks is None for r in ds.perf)                       # no click data anywhere
    assert all(r.spend > 0 and r.impressions > 0 for r in ds.perf)
    assert set(ds.events) == {"cpm_spike", "availability_drop", "budget_capped_winner",
                              "bleeding_keyword", "own_overlap"}
    assert ds.profile.feeds >= {"market", "shelf", "budget_days", "offtake"}
    assert len(ds.market) > 0 and len(ds.offtake) > 0 and len(ds.shelf) > 0 and len(ds.change_events) == 2
    # every targeting unit in the fact has a role row
    assert {(u.entity_id, u.value) for u in ds.targeting_units} == {
        (r.entity_id, r.targeting_unit) for r in ds.perf if r.targeting_unit}


def test_event_cpm_spike(synthetic_dataset, synthetic_events, windows_30):
    ev = synthetic_events["cpm_spike"]
    assert ev.start == windows_30.current.start
    rows = [r for r in synthetic_dataset.perf if r.entity_id == ev.entity_ids[0] and r.targeting_unit == ev.targeting_unit]
    pri, cur = (cell_from_rows(x) for x in split_rows(rows, windows_30))
    assert cur.cpm / pri.cpm == pytest.approx(ev.magnitude, rel=0.03)
    assert cur.bid / pri.bid == pytest.approx(ev.magnitude, rel=0.03)
    assert abs(cur.position - pri.position) < 0.15                       # position did not move
    assert cur.icr / pri.icr == pytest.approx(1.0, abs=0.08)             # conversion did not move
    assert cur.roas / pri.roas == pytest.approx(1 / ev.magnitude, abs=0.08)
    # rival pressure in the market view
    mk = [m for m in synthetic_dataset.market if m.targeting_unit == ev.targeting_unit]

    def rival_ad(window):
        return sum(m.ad_impressions for m in filter_rows(mk, window) if m.brand == ev.details["rival"])

    assert rival_ad(windows_30.current) / rival_ad(windows_30.prior) == pytest.approx(2.5, rel=0.08)
    # other keywords in the same campaign are untouched
    other = [r for r in synthetic_dataset.perf if r.entity_id == ev.entity_ids[0] and r.targeting_unit == "granola"]
    p2, c2 = (cell_from_rows(x) for x in split_rows(other, windows_30))
    assert c2.cpm / p2.cpm == pytest.approx(1.0, abs=0.03)


def test_event_availability_drop(synthetic_dataset, synthetic_events):
    ev = synthetic_events["availability_drop"]
    shelf = [s for s in synthetic_dataset.shelf if s.product_id == ev.product_id and s.geo == ev.geo]

    def avail(rows: list[ShelfRow]) -> float:
        return sum(s.available_count for s in rows) / sum(s.check_count for s in rows)

    before = avail([s for s in shelf if s.date < ev.start])
    after = avail([s for s in shelf if s.date >= ev.start])
    assert before == pytest.approx(0.95, abs=0.03) and after == pytest.approx(ev.magnitude, abs=0.05)
    elsewhere = [s for s in synthetic_dataset.shelf
                 if s.product_id == ev.product_id and s.geo != ev.geo and s.date >= ev.start]
    assert avail(elsewhere) > 0.9
    # conversion on the affected product x geo falls with availability; other geos hold
    rows = [r for r in synthetic_dataset.perf if r.product_id == ev.product_id]
    hit = [r for r in rows if r.geo == ev.geo]
    icr_before = cell_from_rows([r for r in hit if r.date < ev.start]).icr
    icr_after = cell_from_rows([r for r in hit if r.date >= ev.start]).icr
    assert icr_after / icr_before == pytest.approx(ev.magnitude / 0.95, abs=0.07)
    safe = [r for r in rows if r.geo != ev.geo]
    ratio = cell_from_rows([r for r in safe if r.date >= ev.start]).icr / cell_from_rows(
        [r for r in safe if r.date < ev.start]).icr
    assert ratio == pytest.approx(1.0, abs=0.07)
    assert {r.entity_id for r in hit} == set(ev.entity_ids)


def test_event_budget_capped_winner(synthetic_dataset, synthetic_events, synthetic_profile):
    ev = synthetic_events["budget_capped_winner"]
    cid = ev.entity_ids[0]
    days = [b for b in synthetic_dataset.budget_days if b.entity_id == cid]
    assert len(days) == 90 and all(b.exhausted_at_minute is not None and b.platform_held for b in days)
    assert all(b.spend == pytest.approx(b.daily_budget, rel=0.005) for b in days)
    suff = [b.live_minutes / b.expected_live_minutes for b in days]
    assert sum(suff) / len(suff) == pytest.approx(ev.magnitude, abs=0.03)
    last3 = sorted(days, key=lambda b: b.date)[-3:]
    tele = BudgetTelemetry(
        entity_id=cid, n_days=90, avg_budget=days[0].daily_budget,
        avg_spend=sum(b.spend for b in days) / 90,
        exhaust_minute=sum(b.exhausted_at_minute for b in days) / 90, exhaust_days=90,
        capped_days=90, active_days=90, held_days=90,
        sufficiency_3d=sum(b.live_minutes / b.expected_live_minutes for b in last3) / 3)
    for method in ("sufficiency", "exhaustion", "capped_days", "held_days", "auto"):
        assert tele.is_bound(method=method), method
    ent = synthetic_dataset.entity(cid)
    assert ent.roas_direct > 4.0
    # status reads budget-held, but it is spending: live
    assert synthetic_profile.canonical_status(ent.status) is CanonicalStatus.BUDGET_HELD
    assert not is_dead(ent.status, ent.spend_3d, status_map=synthetic_profile.status_map)
    # nobody else is budget-bound
    others = [b for b in synthetic_dataset.budget_days if b.entity_id != cid]
    assert all(b.exhausted_at_minute is None for b in others)


def test_event_bleeding_keyword(synthetic_dataset, synthetic_events, windows_30):
    ev = synthetic_events["bleeding_keyword"]
    cells = group_cells([r for r in synthetic_dataset.perf if r.entity_id == ev.entity_ids[0]], "targeting_unit")
    bleeder = cells[ev.targeting_unit]
    assert bleeder.roas == pytest.approx(ev.magnitude, abs=0.06) and bleeder.total_roas < 0.5
    assert share(bleeder.spend, sum(c.spend for c in cells.values())) > 0.3      # material
    assert all(c.roas > 1.0 for kw, c in cells.items() if kw != ev.targeting_unit)
    for part in split_rows([r for r in synthetic_dataset.perf if r.targeting_unit == ev.targeting_unit], windows_30):
        assert cell_from_rows(part).roas < 0.5                                   # persistent, not a blip
    assert bleeder.new_users / bleeder.units < 0.15


def test_event_own_overlap(synthetic_dataset, synthetic_events):
    ev = synthetic_events["own_overlap"]
    rows = [r for r in synthetic_dataset.perf if r.targeting_unit == ev.targeting_unit]
    assert {r.entity_id for r in rows} == set(ev.entity_ids)
    assert {r.product_id for r in rows} == {ev.product_id}
    by_cell: dict[tuple, set[str]] = {}
    for r in rows:
        by_cell.setdefault((r.date, r.geo), set()).add(r.entity_id)
    assert len(by_cell) == 90 * 5 and all(len(v) == 2 for v in by_cell.values())
    roas = {k: c.roas for k, c in group_cells(rows, "entity_id").items()}
    assert roas[ev.details["better"]] > 2 * roas[ev.details["worse"]]
    # no other keyword is shared between campaigns
    owners: dict[str, set[str]] = {}
    for r in synthetic_dataset.perf:
        if r.targeting_unit:
            owners.setdefault(r.targeting_unit, set()).add(r.entity_id)
    assert [k for k, v in owners.items() if len(v) > 1] == [ev.targeting_unit]


def test_background_entities(synthetic_dataset, synthetic_profile):
    ds, sm = synthetic_dataset, synthetic_profile.status_map
    paused, young, reach = ds.entity("cmp-paused"), ds.entity("cmp-young"), ds.entity("cmp-reach")
    assert is_dead(paused.status, paused.spend_3d, status_map=sm)
    assert stale_stopped(paused.status, paused.spend_7d, status_map=sm)
    assert max(r.date for r in ds.perf_for("cmp-paused")) == ds.start_date + timedelta(days=39)
    assert tier_for_rows(ds.perf_for("cmp-young")) is DataTier.WARM and young.start_date == ds.end_date - timedelta(days=9)
    assert tier_for_rows(ds.perf_for("cmp-brand")) is DataTier.FULL
    assert cell_from_rows(ds.perf_for("cmp-reach")).sales == 0.0 and reach.roas_direct == 0.0
    assert tier_for_rows(ds.perf_for("cmp-reach")) is DataTier.COLD        # no units: no active panel days
    assert ds.entity("cmp-brand").own_brand_share == 1.0 and ds.entity("cmp-reco").auto_targeted


def test_market_view_consistent_with_fact(synthetic_dataset):
    ds = synthetic_dataset
    day = ds.end_date
    own = sum(m.ad_impressions for m in ds.market if m.date == day and m.targeting_unit == "energy bar" and m.is_own)
    fact = sum(r.impressions for r in ds.perf if r.date == day and r.targeting_unit == "energy bar")
    assert own == fact
    own_sales = sum(o.sales for o in ds.offtake if o.is_own)
    all_sales = sum(o.sales for o in ds.offtake)
    assert 0.2 < own_sales / all_sales < 0.5


def test_synthetic_adapter_is_fresh_per_seed():
    a = SyntheticAdapter(seed=3, days=60)
    assert len({r.date for r in a.perf_rows()}) == 60 and a.events["cpm_spike"].magnitude == 1.6
    assert dataclasses.is_dataclass(a.dataset)
