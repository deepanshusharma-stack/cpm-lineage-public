"""A second, differently shaped CPM platform behind its own adapter.

The quick start uses a first-price marketplace with no clicks, city geos and
an average-position field.  This example invents the opposite: "streamshop",
a CPM-billed platform that

* reports **clicks** (so impressions -> clicks -> orders exists),
* runs a **second-price** auction (the bid is a ceiling, not the price paid),
* has **no position field** at all,
* sells by **country**, with a "ROW" long-tail bucket,
* counts **orders**, not units, and reports money in **micros**,
* names its ad formats and statuses its own way, and
* reports served versus scheduled minutes instead of an exhaustion time.

Nothing in the library is edited to support it.  The adapter converts the
platform's nested records into the neutral schema and a ``PlatformProfile``
declares the semantics; every rule that needs a capability the platform lacks
(position, first-price bid maths) abstains by itself.

The data is generated here (seeded) with two planted events: the CPM of the
query "trail shoes" rises 50% in the last 30 days, and line item ``li-hero``
is profitable but runs out of budget every day.

Run:  python examples/custom_platform_adapter.py
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import numpy as np

from cpm_lineage import (AdTypeCapability, BaseAdapter, BudgetDayRow, Dataset, Economics, EngineConfig,
                         Entity, PerfRow, PlatformProfile, PositionSemantics, TargetingUnit, diagnose,
                         plan, render_report)

MICROS = 1_000_000

# ---------------------------------------------------------------------------
# 1. What the platform's API would hand back (generated, nested, in micros)
# ---------------------------------------------------------------------------

LINE_ITEMS = [
    # id, label, format, state, daily cap (currency), queries [(text, intent, imps/day, cpm, ctr, cvr, order value)]
    ("li-brand", "Brand search", "SPONSORED_SEARCH", "RUNNING", 400.0,
     [("northpeak shoes", "BRAND", 6000, 9.0, 0.050, 0.10, 80.0)]),
    ("li-generic", "Category search", "SPONSORED_SEARCH", "RUNNING", 900.0,
     [("trail shoes", "CATEGORY", 20000, 12.0, 0.020, 0.05, 90.0),
      ("running socks", "CATEGORY", 9000, 6.0, 0.015, 0.06, 18.0)]),
    ("li-hero", "Hero product push", "SPONSORED_SEARCH", "OUT_OF_BUDGET", 150.0,
     [("waterproof boots", "CATEGORY", 30000, 10.0, 0.030, 0.07, 140.0)]),
    ("li-rival", "Rival terms", "SPONSORED_SEARCH", "RUNNING", 500.0,
     [("ridgeline boots", "RIVAL", 14000, 14.0, 0.008, 0.02, 120.0)]),
    ("li-video", "Brand film", "VIDEO_REACH", "RUNNING", 600.0,
     [(None, None, 60000, 7.0, 0.002, 0.0, 0.0)]),
]
REGIONS = [("DE", 0.45), ("FR", 0.30), ("NL", 0.17), ("ROW", 0.08)]
DAYS, END = 70, date(2026, 6, 30)


def fetch_report(seed: int = 11) -> list[dict[str, Any]]:
    """Stand-in for the platform's reporting API: one nested record per line item, query, region and day."""
    rng = np.random.default_rng(seed)
    out: list[dict[str, Any]] = []
    for d in range(DAYS):
        day = END - timedelta(days=DAYS - 1 - d)
        recent = d >= DAYS - 30
        weekday = 1.0 + 0.12 * np.sin(2 * np.pi * d / 7.0)
        for li, label, fmt, state, cap, queries in LINE_ITEMS:
            # The capped line item only ever serves about a third of the day.
            served_share = 0.34 if li == "li-hero" else 1.0
            for query, intent, imps, cpm, ctr, cvr, value in queries:
                if query == "trail shoes" and recent:
                    cpm *= 1.5                                   # planted: auction got dearer
                for region, weight in REGIONS:
                    n = imps * weight * weekday * served_share * rng.normal(1.0, 0.06)
                    clicks = n * ctr * rng.normal(1.0, 0.08)
                    orders = float(rng.poisson(max(clicks * cvr, 0.0)))
                    out.append({
                        "day": day.isoformat(),
                        "line_item": {"id": li, "label": label, "format": fmt, "state": state,
                                      "daily_cap_micros": int(cap * MICROS)},
                        "targeting": {"query": query, "intent": intent, "max_cpm_micros": int(cpm * 1.3 * MICROS)},
                        "region": region,
                        "metrics": {"cost_micros": int(n / 1000.0 * cpm * rng.normal(1.0, 0.03) * MICROS),
                                    "imps": int(n), "clicks": int(clicks), "conversions": orders,
                                    "conv_value_micros": int(orders * value * MICROS)},
                        "pacing": {"minutes_served": int(960 * served_share), "minutes_scheduled": 960},
                    })
    return out


# ---------------------------------------------------------------------------
# 2. The platform's semantics, declared once
# ---------------------------------------------------------------------------

STREAMSHOP = PlatformProfile(
    name="streamshop",
    has_clicks=True,
    auction_type="second_price",
    units_kind="orders",
    position=PositionSemantics(available=False),
    ad_types={
        "sponsored_search": AdTypeCapability(name="sponsored_search", capability_class="keyword_auction",
                                             has_clicks=True),
        "video_reach": AdTypeCapability(name="video_reach", capability_class="reach", targeting="none",
                                        keyword_drillable=False, sales_attributed=False, judged_on="reach",
                                        has_reach=True),
    },
    ad_type_aliases={"SPONSORED_SEARCH": "sponsored_search", "VIDEO_REACH": "video_reach"},
    status_map={"RUNNING": "active", "OUT_OF_BUDGET": "budget_held", "STOPPED": "operator_paused",
                "DELETED": "removed"},
    geo_grain="country",
    aggregate_geo_labels=frozenset({"row"}),
    currency="",
    trading_day_start_hour=0.0, trading_day_end_hour=24.0, funding_target_hour=24.0,
    feeds=frozenset({"perf", "entities", "targeting_units", "budget_days"}),
)
INTENT_ROLE = {"BRAND": "own_brand", "CATEGORY": "generic", "RIVAL": "competitor"}


# ---------------------------------------------------------------------------
# 3. The adapter: nested platform records -> neutral schema records
# ---------------------------------------------------------------------------

class StreamshopAdapter(BaseAdapter):
    """Turns streamshop report records into a ``Dataset``."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        perf: list[PerfRow] = []
        entities: dict[str, Entity] = {}
        units: dict[tuple[str, str], TargetingUnit] = {}
        pacing: dict[tuple[str, date], dict[str, float]] = {}
        for rec in records:
            li, m, t = rec["line_item"], rec["metrics"], rec["targeting"]
            day = date.fromisoformat(rec["day"])
            query = t["query"].strip().lower() if t["query"] else None
            perf.append(PerfRow(
                date=day, entity_id=li["id"], entity_name=li["label"],
                ad_type=STREAMSHOP.canonical_ad_type(li["format"]), status=li["state"],
                targeting_unit=query, targeting_type="keyword" if query else None, geo=rec["region"],
                spend=m["cost_micros"] / MICROS, impressions=float(m["imps"]), clicks=float(m["clicks"]),
                units=float(m["conversions"]), sales=m["conv_value_micros"] / MICROS,
                bid=t["max_cpm_micros"] / MICROS, position=None, segment="outdoor"))
            entities.setdefault(li["id"], Entity(
                entity_id=li["id"], name=li["label"], ad_type=STREAMSHOP.canonical_ad_type(li["format"]),
                status=li["state"], daily_budget=li["daily_cap_micros"] / MICROS, segment="outdoor"))
            if query:
                units.setdefault((li["id"], query), TargetingUnit(
                    value=query, role=INTENT_ROLE.get(t["intent"], "generic"), entity_id=li["id"],
                    live_bid=t["max_cpm_micros"] / MICROS, segment="outdoor"))
            p = pacing.setdefault((li["id"], day), {"spend": 0.0, "sales": 0.0, "imps": 0.0,
                                                    "served": rec["pacing"]["minutes_served"],
                                                    "scheduled": rec["pacing"]["minutes_scheduled"],
                                                    "cap": li["daily_cap_micros"] / MICROS,
                                                    "held": li["state"] == "OUT_OF_BUDGET"})
            p["spend"] += m["cost_micros"] / MICROS
            p["sales"] += m["conv_value_micros"] / MICROS
            p["imps"] += m["imps"]
        budget_days = [
            BudgetDayRow(date=day, entity_id=li, daily_budget=p["cap"], spend=p["spend"], sales=p["sales"],
                         impressions=p["imps"], live_minutes=p["served"], expected_live_minutes=p["scheduled"],
                         # this platform reports served time, not the minute the budget ran out
                         exhausted_at_minute=p["served"] if p["served"] < p["scheduled"] else None,
                         platform_held=bool(p["held"]))
            for (li, day), p in sorted(pacing.items())
        ]
        super().__init__(Dataset(profile=STREAMSHOP, perf=perf, entities=list(entities.values()),
                                 targeting_units=list(units.values()), budget_days=budget_days))


def main() -> None:
    adapter = StreamshopAdapter(fetch_report())
    data = adapter.dataset
    print(f"{STREAMSHOP.name}: {len(data.perf)} rows, auction {STREAMSHOP.auction_type}, "
          f"clicks {'yes' if STREAMSHOP.has_clicks else 'no'}, position "
          f"{'yes' if STREAMSHOP.position.available else 'no'}, geo grain {STREAMSHOP.geo_grain}")
    print()

    # Money thresholds in the library are written for a reference currency in which a
    # material campaign spends thousands a day.  Amounts here are about 100x smaller,
    # so every money threshold is scaled once, in one place.
    config = EngineConfig(economics=Economics(currency_unit=0.01))

    diagnosis = diagnose(data, config=config)
    action_plan = plan(data, config=config, rule_profile="verdict")
    print(render_report(diagnosis, action_plan))

    # Clicks exist on this platform, so the funnel can be read in two steps as well.
    cur = [r for r in data.perf if diagnosis.windows.current.contains(r.date) and r.entity_id == "li-generic"]
    imps, clicks, orders = (sum(getattr(r, f) or 0.0 for r in cur) for f in ("impressions", "clicks", "units"))
    print()
    print(f"li-generic funnel: CTR {100 * clicks / imps:.2f}%  x  CVR {100 * orders / clicks:.2f}%  "
          f"=  ICR {1000 * orders / imps:.3f} orders per 1,000 impressions")


if __name__ == "__main__":
    main()
