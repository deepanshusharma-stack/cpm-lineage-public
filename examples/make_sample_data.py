"""Regenerate ``sample_data.csv`` and ``sample_budgets.csv`` from the synthetic generator.

The files imitate a platform export: the column names and the ad-type
labels are deliberately *not* the library's own, so that
``bring_your_own_data.py`` has something real to map.  Every number is
synthetic (seeded), so the files are reproducible byte for byte.

Run:  python examples/make_sample_data.py
"""

from __future__ import annotations

import csv
from datetime import timedelta
from pathlib import Path

from cpm_lineage import SyntheticAdapter

HERE = Path(__file__).resolve().parent
DAYS = 60                                   # the last 60 days of the 90-day synthetic history
RAW_AD_TYPE = {"keyword_listing": "KW Listing", "recommendation": "Reco", "featured_listing": "Banner"}


def main() -> None:
    data = SyntheticAdapter().dataset
    start = data.latest_date - timedelta(days=DAYS - 1)

    perf_cols = ["day", "campaign_id", "campaign_name", "campaign_type", "state", "keyword", "city", "sku",
                 "category", "cost", "impr", "revenue", "orders", "halo_revenue", "new_customers",
                 "max_bid", "avg_slot"]
    with open(HERE / "sample_data.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(perf_cols)
        for r in data.perf:
            if r.date < start:
                continue
            w.writerow([r.date.isoformat(), r.entity_id, r.entity_name, RAW_AD_TYPE.get(r.ad_type, r.ad_type),
                        r.status, r.targeting_unit or "", r.geo or "", r.product_id or "", r.segment or "",
                        f"{r.spend:.2f}", f"{r.impressions:.0f}", f"{r.sales:.2f}", f"{r.units:.0f}",
                        "" if r.indirect_sales is None else f"{r.indirect_sales:.2f}",
                        "" if r.new_users is None else f"{r.new_users:.0f}",
                        "" if r.bid is None else f"{r.bid:.2f}",
                        "" if r.position is None else f"{r.position:.2f}"])

    budget_cols = ["day", "campaign_id", "daily_cap", "cost", "revenue", "impr", "ran_out_minute",
                   "served_minutes", "scheduled_minutes", "held_for_budget", "state"]
    with open(HERE / "sample_budgets.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(budget_cols)
        for b in data.budget_days:
            if b.date < start:
                continue
            w.writerow([b.date.isoformat(), b.entity_id, "" if b.daily_budget is None else f"{b.daily_budget:.0f}",
                        f"{b.spend:.2f}", f"{b.sales:.2f}", f"{b.impressions:.0f}",
                        "" if b.exhausted_at_minute is None else f"{b.exhausted_at_minute:.0f}",
                        "" if b.live_minutes is None else f"{b.live_minutes:.0f}",
                        "" if b.expected_live_minutes is None else f"{b.expected_live_minutes:.0f}",
                        "" if b.platform_held is None else str(b.platform_held).lower(), b.status or ""])
    print(f"wrote {HERE / 'sample_data.csv'} and {HERE / 'sample_budgets.csv'}")


if __name__ == "__main__":
    main()
