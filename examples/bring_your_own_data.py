"""Bring your own data: a CSV export plus a column mapping.

``sample_data.csv`` stands in for a platform export.  Its column names and
ad-type labels are the "platform's", not the library's, so two things are
declared here and nowhere else:

* a **column map** (library field -> your column header), and
* a **PlatformProfile** (what your ad types can do, what your statuses
  mean, which geo labels are roll-up buckets, the auction type ...).

Only the delivery file is required.  The budget pacing file is optional;
without it the engine says "not assessed" for budget-bound tests instead of
guessing.  Other optional feeds (share of voice, shelf availability, market
sell-out) plug in the same way through ``CsvAdapter``.

Run:  python examples/bring_your_own_data.py [path/to/delivery.csv]
"""

from __future__ import annotations

import sys
from pathlib import Path

from cpm_lineage import (AdTypeCapability, CsvAdapter, Economics, EngineConfig, PlatformProfile,
                         PositionSemantics, RunCheck, diagnose, plan, render_report)

HERE = Path(__file__).resolve().parent

# library field -> column header in the file
PERF_COLUMNS = {
    "date": "day", "entity_id": "campaign_id", "entity_name": "campaign_name",
    "ad_type": "campaign_type", "status": "state", "targeting_unit": "keyword",
    "geo": "city", "product_id": "sku", "segment": "category",
    "spend": "cost", "impressions": "impr", "sales": "revenue", "units": "orders",
    "indirect_sales": "halo_revenue", "new_users": "new_customers",
    "bid": "max_bid", "position": "avg_slot",
}
BUDGET_COLUMNS = {
    "date": "day", "entity_id": "campaign_id", "daily_budget": "daily_cap", "spend": "cost",
    "sales": "revenue", "impressions": "impr", "exhausted_at_minute": "ran_out_minute",
    "live_minutes": "served_minutes", "expected_live_minutes": "scheduled_minutes",
    "platform_held": "held_for_budget", "status": "state",
}

PROFILE = PlatformProfile(
    name="my-platform",
    has_clicks=False,                       # impressions-to-orders; no click feed
    auction_type="first_price",
    position=PositionSemantics(available=True, kind="average", lower_is_better=True),
    ad_types={
        "keyword_listing": AdTypeCapability(name="keyword_listing", capability_class="keyword_auction"),
        "recommendation": AdTypeCapability(
            name="recommendation", capability_class="retargeting", targeting="auto",
            keyword_drillable=False, judged_on="deflated_roas", roas_deflator=0.6),
        "featured_listing": AdTypeCapability(
            name="featured_listing", capability_class="reach", targeting="none",
            keyword_drillable=False, sales_attributed=False, judged_on="reach", has_reach=True),
    },
    # raw labels in the export -> canonical ad types above
    ad_type_aliases={"KW Listing": "keyword_listing", "Reco": "recommendation", "Banner": "featured_listing"},
    # raw statuses -> canonical statuses
    status_map={"ACTIVE": "active", "BUDGET_HELD": "budget_held", "PAUSED": "operator_paused"},
    geo_grain="city",
    aggregate_geo_labels=frozenset({"others"}),     # long-tail bucket: never a target
    placement_targets=frozenset({"feed"}),          # pseudo "keywords" that are not searches
    trading_day_start_hour=6.0, trading_day_end_hour=18.0, funding_target_hour=22.0,
)


def main(argv: list[str]) -> None:
    perf_path = Path(argv[1]) if len(argv) > 1 else HERE / "sample_data.csv"
    budget_path = HERE / "sample_budgets.csv" if len(argv) <= 1 else None

    adapter = CsvAdapter(perf_path, PERF_COLUMNS, PROFILE,
                         budget_path=budget_path, budget_map=BUDGET_COLUMNS if budget_path else None)
    data = adapter.dataset
    print(f"loaded {len(data.perf)} delivery rows, {len(data.entities)} campaigns, "
          f"{len(data.budget_days)} budget rows; feeds: {', '.join(sorted(data.profile.feeds))}")
    print()

    # Account economics re-base every profitability bar.  Here: a first purchase
    # breaks even at ROAS 1.2 and money thresholds are used as written.
    config = EngineConfig(economics=Economics(breakeven=1.2))

    diagnosis = diagnose(data, config=config)
    action_plan = plan(
        data, config=config,
        rule_profile="verdict",                        # bucket-relative ROAS bars
        budget_neutral=True,                           # fund raises with cuts from the weakest earners
        check=RunCheck("roas", ">=", 150, "pct"),      # the plan must keep blended ROAS at or above 1.5
    )
    print(render_report(diagnosis, action_plan))


if __name__ == "__main__":
    main(sys.argv)
