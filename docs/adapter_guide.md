# Adapter guide: connecting a platform to cpm-lineage

How to bring data from any CPM-billed ad platform (quick-commerce marketplaces, retail media
networks, display exchanges) into `cpm_lineage`. Written by Deepanshu Sharma.

Nothing platform-specific is hard-coded in the library. An adapter does two jobs:

1. it turns the platform's export into the neutral records in `cpm_lineage.schema`, and
2. it declares the platform's semantics once, in a `PlatformProfile`.

The engine never raises because an optional feed or field is missing. It skips the checks that
depend on it and says so in `Diagnosis.notes` or `Plan.guard_note`.

Related: `docs/how_it_works.md` (what the engine does with the data) and `docs/thresholds.md`
(every config field).

## Contents

1. [Pick a route](#1-pick-a-route)
2. [Map the delivery rows](#2-map-the-delivery-rows)
3. [Declare the platform profile](#3-declare-the-platform-profile)
4. [Describe each ad type](#4-describe-each-ad-type)
5. [Map the statuses](#5-map-the-statuses)
6. [Add the optional feeds](#6-add-the-optional-feeds)
7. [Set the money scale, the economics and the trading day](#7-set-the-money-scale-the-economics-and-the-trading-day)
8. [Check the adapter](#8-check-the-adapter)
9. [How the engine degrades](#9-how-the-engine-degrades)
10. [Common mistakes](#10-common-mistakes)
11. [A complete small adapter](#11-a-complete-small-adapter)

## 1. Pick a route

The engine accepts a `Dataset`, an adapter object, or a plain list of `PerfRow`.

| You have | Use | Notes |
|---|---|---|
| CSV exports | `CsvAdapter(perf_path, column_map, profile, budget_path=..., ...)` | standard library only |
| a DataFrame or a list of dicts | `DataFrameAdapter(perf, column_map, profile, budget_days=..., ...)` | anything with `to_dict("records")`; pandas is not imported |
| an API with nested records | subclass `BaseAdapter` and fill `self.dataset` | full control; see section 11 |
| only delivery rows, in code | `diagnose(list_of_perf_rows, profile)` | entities are derived from the rows |

The formal contract is the `PlatformAdapter` protocol: an object with a `profile` attribute and a
`perf_rows()` method. The other eight methods (`entities`, `targeting_units`, `products`,
`market_rows`, `offtake_rows`, `shelf_rows`, `budget_days`, `change_events`) are optional and may
return an empty list.

A **column map** is a dict from the library's field name to your column name. Fields not in the
map are read from a column of the same name if one exists.

```python
from cpm_lineage import CsvAdapter, PlatformProfile

adapter = CsvAdapter(
    "delivery.csv",
    {"date": "day", "entity_id": "campaign_id", "spend": "cost", "impressions": "impr",
     "sales": "revenue", "units": "orders", "targeting_unit": "keyword", "geo": "city"},
    PlatformProfile(name="my-platform"),
    budget_path="pacing.csv",
    budget_map={"date": "day", "entity_id": "campaign_id", "daily_budget": "daily_cap", "spend": "cost"},
)
data = adapter.dataset
```

What the two file adapters do for you at ingestion: dates are parsed from ISO strings; numbers with
thousands separators are read; empty cells, `nan`, `null` and `None` become "not reported";
targeting units are trimmed and lower-cased; ad-type labels the profile knows are folded to their
canonical name; geos go through the profile's roll-up map. A row missing a required field raises a
`ValueError` naming the record and the column.

## 2. Map the delivery rows

One `PerfRow` per entity and day. Add targeting unit, geo and product to the grain when the
platform reports them.

| Field | Required | What to put in it |
|---|---|---|
| `date` | yes | a `datetime.date`. Not a string and not a `datetime`: the engine raises a `TypeError` on the first row otherwise. Use the platform's reporting day. |
| `entity_id` | yes | the id of whatever holds the budget: campaign, ad group or line item. Pick one level and use it everywhere. |
| `spend` | yes | money in currency units. Convert micros or cents here. |
| `impressions` | yes | impressions served |
| `sales` | for ROAS | directly attributed revenue, same currency as `spend`. Leave 0 for an ad type with no attribution, and mark that type `sales_attributed=False` in the profile. |
| `units` | for ROAS | attributed units or orders. Pick one and stay with it. |
| `targeting_unit` | no | the keyword, audience segment or category, lower-cased and trimmed |
| `targeting_type` | no | its kind: `"keyword"`, `"audience"`, `"category"`, `"auto"`, `"placement"` |
| `geo` | no | the auction or reporting region |
| `product_id` | no | the advertised product; links delivery to the shelf feed |
| `bid` | no | the bid in force that day, in the unit named by `PlatformProfile.bid_unit` |
| `position` | no | ad slot or rank. Leave `None` if the platform has none. |
| `ad_type` | no | the raw or canonical ad-type label |
| `status` | no | the raw status string as of the row |
| `indirect_sales` | no | halo or view-through sales, kept separate from `sales` |
| `new_users` | no | new-to-brand customers |
| `clicks` | no | clicks, if reported; no rule needs them |
| `segment`, `entity_name`, `advertiser_id`, `match_type` | no | labels |

Rules that keep the numbers right:

* **Sum only what is additive.** `spend`, `impressions`, `sales`, `units`, `clicks`,
  `indirect_sales` and `new_users` may be summed. `bid` and `position` are rates: if you have to
  collapse rows yourself, take a spend-weighted mean of `bid` and an impression-weighted mean of
  `position`.
* **Do not pre-aggregate to weeks.** Windows, tiers and the budget probe count days.
* **Hourly rows are fine.** Rows that repeat a date are summed.
* **Sentinel positions are handled.** Values outside `[valid_min, valid_max]` of the profile's
  `PositionSemantics` are ignored in position means but still count toward spend and CPM.
* **`None` is not zero.** Use `None` for "the platform does not report this". A zero is read as a
  real zero.

## 3. Declare the platform profile

`PlatformProfile` is a frozen dataclass. Enum-like strings are validated when it is built, so a
misspelt value fails there and not deep inside a run.

Fields the engine reads:

| Field | Default | Declare it when |
|---|---|---|
| `name` | `"generic"` | always; it namespaces ledger rows |
| `auction_type` | `"unknown"` | you know it: `"first_price"` or `"second_price"`. The self-floor check runs only on second price. |
| `bid_unit` | `"cpm"` | bids are quoted another way: `"per_impression"`, `"cpc"`, `"other"`. The overpay check compares bids with CPM only when this is `"cpm"`. |
| `position` | available, 1 is best, valid 1 to 100 | there is no position (`PositionSemantics(available=False)`), or a higher number is better |
| `ad_types`, `ad_type_aliases` | empty | always, if you have more than one kind of ad; see section 4 |
| `status_map` | empty | always; see section 5 |
| `geo_grain` | `"none"` | the platform reports geos: `"city"`, `"region"`, `"country"` |
| `geo_rollup` | empty | small geos should be read under a parent |
| `aggregate_geo_labels`, `aggregate_geo_prefixes` | `{"others", "other"}`, `("other ",)` | the export has long-tail buckets. Write the labels in lower case. They are never a target, a control or an expansion candidate. |
| `placement_targets`, `placement_target_prefixes` | empty | the targeting column carries pseudo values that are not searches (a feed, a shelf) |
| `biddable_targeting_types` | `{"keyword"}` | you bid on audiences or categories: add those `targeting_type` values |
| `currency_unit` | `1.0` | a material campaign does not spend thousands a day; see section 7 |
| `trading_day_start_hour`, `trading_day_end_hour`, `funding_target_hour` | 6, 18, 22 | serving runs on other hours |
| `timezone` | `"UTC"` | you use the bidder-log audits in `cpm_lineage.cpm`, which bucket decision timestamps by local day |
| `automated_sources` | empty | the change log carries changes made by automations |

Fields that are descriptive only (carried for reports, read by no rule): `has_clicks`,
`units_kind`, `feed_lag_days`, `feeds`, `currency`.

## 4. Describe each ad type

Give every ad type an `AdTypeCapability`. Its **capability class** decides which levers a card may
pull on an entity of that type:

| `capability_class` | Meaning | Levers allowed by the default playbook |
|---|---|---|
| `keyword_auction` | a keyword-targeted listing ad | budget, keyword, bid, experiment, geo, watch |
| `retargeting` | an auto-targeted recommendation-style placement | budget, watch |
| `placement` | a shelf or collection placement | budget, watch |
| `reach` | display or banner with no attributed sales | watch |

Other fields of `AdTypeCapability`:

| Field | Default | Set it when |
|---|---|---|
| `targeting` | `"keyword"` | the advertiser has no targeting control (`"auto"`) or there is none (`"none"`) |
| `keyword_drillable` | `True` | there are no targeting-unit rows for the type |
| `sales_attributed` | `True` | the platform attributes no sales to the type. Then it is never judged on ROAS. |
| `judged_on` | `"roas"` | `"deflated_roas"` or `"reach"` |
| `roas_deflator` | `1.0` | the type over-attributes; reported ROAS is multiplied by this before it is judged |
| `levers` | `None` | you want to override the class's lever set for one type |

An ad type that is not declared gets `PlatformProfile.default_capability_class`, which is
`keyword_auction`: fully levered and judged on ROAS. That is the wrong default for a display type,
so declare display types explicitly.

`ad_type_aliases` maps raw labels to canonical names, case-insensitively:
`{"SPONSORED_SEARCH": "search", "Banner": "display"}`.

## 5. Map the statuses

Map every raw status string to one of eight canonical values in `status_map`:

| Canonical status | Use for | Liveness |
|---|---|---|
| `active` | serving normally | live |
| `budget_held` | the platform paused it today because the daily budget ran out | dead only with no spend in the last 3 days |
| `platform_paused` | a platform-reported pause whose flag may lag reality | dead only with no spend in the last 3 days |
| `pre_launch` | draft, scheduled, under review | dead only with no spend in the last 3 days |
| `operator_paused` | a person stopped or paused it | dead |
| `ended` | completed its flight | dead |
| `removed` | archived or deleted | dead |
| `unknown` | anything unmapped | live |

Why it matters: a dead entity receives no card. A `budget_held` entity that is still spending is
exactly the budget-capped winner, so it must not be mapped to `operator_paused`.

Strings missing from your `status_map` fall back to a small generic vocabulary
(`liveness.DEFAULT_STATUS_MAP`: `paused` reads as operator-paused, `on_hold` as held for budget,
and so on). Those meanings may not match your platform. Anything still unrecognised reads as live.

## 6. Add the optional feeds

Each feed is a list of records on the `Dataset`. Add the ones the platform has.

### Entities: `Entity`

Only `entity_id` is required. Worth supplying: `status`, `ad_type`, `daily_budget` (the current
cap), `segment`, `product_ids`, `name`. When the feed is absent, entities are derived from the
delivery rows (name, ad type, segment and the status of the latest row; no budget).

### Budget pacing: `BudgetDayRow`

One row per entity and day: `date`, `entity_id`, `daily_budget`, `spend`, and ideally `sales` and
`impressions`. This feed unlocks budget-bound tests, the saturation curve and raise sizing.

Optional telemetry sharpens it, and platforms differ in which one they report:

| Field | Meaning | If absent |
|---|---|---|
| `exhausted_at_minute` | minutes after local midnight at which the budget ran out | the early-exhaustion definition cannot fire |
| `live_minutes`, `expected_live_minutes` | served time against scheduled time | the sufficiency definition cannot fire |
| `platform_held` | the platform held the entity for budget that day | the held-days definition cannot fire |

With none of the three, the budget probe in `diagnose()` still judges "capped" from spend against
the cap (`spend >= 90%` of the cap on at least 60% of active days) and still reports a budget-bound
finding. `plan()` proposes no raise card, though: the rule families' budget-bound test needs the
exhaustion minute or the served-time fields. Measured on the synthetic dataset with the three
fields blanked: the finding stays and the "Increase Budget" card disappears.

### Share of voice: `MarketRow`

One row per targeting unit, brand and day: `ad_impressions`, `organic_impressions`, and `is_own`
for the advertiser's brand. Include rival brands; without them there is no rival-pressure read.
Unlocks the share-of-voice levers, the "rival pressure" kind of CPM rise and keyword additions.

### Shelf: `ShelfRow`

One row per product, geo and day with `available_count` and `check_count` (availability is their
ratio), and optionally `price` and `list_price`. Include competitor products with `is_own=False`
if you can: own and competitor availability must come from the same feed at the same grain.
Unlocks the availability and price levers, the availability drill and the supply gate.

### Market sell-out: `OfftakeRow`

One row per brand, segment and day with `sales`, for **all** brands in the segment and `is_own` on
yours. Without whole-segment rows there is no market share. Unlocks the market root-cause read and
the tide used in outcome scoring.

### Targeting units and products: `TargetingUnit`, `Product`

`TargetingUnit(value, role)` tells the engine whether a keyword is `own_brand`, `sibling_brand`,
`competitor` or `generic`. A missing role defaults to `generic`, which matters: conquest terms have
their own pause thresholds. `cpm_lineage.keywords.build_lexicon` can classify keywords from a
product catalogue if the platform gives no keyword type. `Product(product_id, brand, is_own)`
separates own from rival products.

### Change log: `ChangeEvent`

`timestamp`, `entity_id`, `field` (`"status"`, `"daily_budget"`, `"bid"`), `old`, `new`. Used by
`outcomes.evaluate` to check whether a recommended change was actually made.

## 7. Set the money scale, the economics and the trading day

**Money scale.** Every absolute money threshold is written for an account whose material campaigns
spend **thousands of currency units a day**: a daily cap under 100 is treated as a placeholder, and
25,000 of window spend is the smallest campaign that earns a classic cut card. If your campaigns
spend about 50 a day, set `currency_unit=0.01` on the profile. One number rescales all of them.
`diagnose()` adds a note when the data sits far below the scale in force. `plan()` does not, so
check it once with `diagnose()`.

**Economics.** Profitability bars are multiples of the effective breakeven:

```python
from cpm_lineage import EngineConfig, Economics

config = EngineConfig(economics=Economics(
    breakeven=1.2,      # ROAS at which a first purchase breaks even
    ltv_mult=1.0,       # lifetime value per first-purchase currency unit
))
```

`Economics.currency_unit` left unset takes the profile's value. A value set on `Economics` wins.

**Indirect sales.** Entity ROAS counts `EngineConfig.indirect_sales_weight` of
`PerfRow.indirect_sales`: 1.0 by default, 0 for direct sales only.

**Trading day.** `trading_day_start_hour`, `trading_day_end_hour` and `funding_target_hour` drive
pacing and raise sizing. `BudgetBoundPolicy.early_exhaust_minutes` (720 by default) is minutes
after local midnight; move it if serving does not start in the morning.

## 8. Check the adapter

Run this once before trusting a plan.

```python
from cpm_lineage import diagnose

d = diagnose(data)

print(d.notes)                                 # what was not assessed, and any money-scale warning
print(d.layer1.exact)                          # True: all four sums are positive in both windows
print(d.windows.current, d.windows.prior)      # the two periods that were compared
print({e: x.tier for e, x in d.entities.items()})          # data tier per entity
print({e: x.status for e, x in d.entities.items()})        # raw statuses as the engine saw them
```

A short checklist:

- [ ] `len(data.perf)` and the date range match the export.
- [ ] Total `spend` and `sales` over a known period match the platform's own report.
- [ ] `d.notes` contains no money-scale warning, or you have set `currency_unit`.
- [ ] `d.notes` lists only feeds you know are absent.
- [ ] No entity you know is paused appears with a prescription to scale. If one does, check
      `status_map`.
- [ ] Display or video entities show `reach` in the report's entity table, not a ROAS. If they show
      a ROAS, declare the ad type with `sales_attributed=False`.
- [ ] Every raw status in the data is a key of `status_map`:
      `{r.status for r in data.perf} - set(data.profile.status_map)` is empty.
- [ ] Long-tail geo buckets are never named on a geo card. If one is, add it to
      `aggregate_geo_labels` in lower case.

## 9. How the engine degrades

Measured by removing feeds from the library's synthetic dataset (`SyntheticAdapter()`, seed 7) and
re-running `diagnose()` and `plan()` with the default configuration:

| Removed | Findings still reported | What changes |
|---|---|---|
| nothing | CPM rise, availability drop, budget-bound winner, bleeding keyword, own collision | baseline: 3 cards with the default holdout |
| budget pacing feed | CPM rise, availability drop, bleeding keyword, own collision | no budget-bound finding, no raise card; a note says budget tests are not assessed |
| share of voice, sell-out and shelf feeds | CPM rise (classified "auction price", not "rival pressure"), budget-bound winner, bleeding keyword, own collision | no availability finding, no market read; attribution is `thin-data` for every entity; the plan notes "supply gate NOT armed" |
| `bid` and `position` columns | all five | the bid and position levers are not modelled; the example entity's attribution goes from `confirmed` to `directional` |
| `geo` column | all five | the head-term pressure card is not proposed; collisions are judged on the whole account as one auction |
| every optional column and feed (bare `date`, `entity_id`, `spend`, `impressions`, `sales`, `units`) | none | the account identity, shift-share and Pareto drag still run; the first read is `cpm_rose_bid_unknown`; the plan is empty; three notes |

Two general rules follow from the code:

* Lever attribution needs at least four levers with coverage (`AttributionConfig.thin_min_levers`).
  Delivery rows supply two (`bid`, `position`) and targeting-unit roles a third
  (`competitor_share`), so attribution needs the share-of-voice feed or the shelf feed. Either one
  is enough on the synthetic dataset.
* Rules that need a field abstain without it. On a platform with no position, per-keyword bid rules
  and the re-slot screen do not run. On a platform that is not second-price, the self-floor check
  does not run.

## 10. Common mistakes

* **Dates as strings.** `PerfRow.date` must be a `datetime.date`. The CSV and DataFrame adapters
  parse them for you; a hand-built row does not.
* **Money in micros.** Spend, sales, bids and budgets must be in the same currency unit.
* **Display spend judged on ROAS.** An undeclared ad type is treated as a fully levered keyword
  auction. Declare reach types.
* **`budget_held` mapped to paused.** The capped winner then looks dead and gets no card.
* **Aggregate geo labels in the wrong case.** `aggregate_geo_labels` is matched against the
  lower-cased geo, so write `"row"`, not `"ROW"`.
* **Tuple fields from a CSV.** `Entity.product_ids` and `Entity.tags` are tuples and are not parsed
  from a CSV cell. Build `Entity` records in code if you need them.
* **A share-of-voice feed with only your own brand.** Shares need the other brands in the
  denominator.
* **Thinking the holdout is a bug.** `plan()` leaves 15% of eligible entities untouched each
  month. Pass `PipelineConfig(holdout_pct=0)` while you validate an adapter.
* **Forgetting the applied history.** Cooldowns, the action lock and the bid-ladder memory read
  `plan(..., applied=[...])`. Pass `ledger.applied_actions(account, platform)`.

## 11. A complete small adapter

This adapter converts nested API-style records for an invented platform with a second-price
auction, no position field, country geos and money in micros. It runs as written once `records`
holds your rows. `examples/custom_platform_adapter.py` is a longer version with generated data and
a budget pacing feed.

```python
from datetime import date

from cpm_lineage import (AdTypeCapability, BaseAdapter, Dataset, Entity, PerfRow, PlatformProfile,
                         PositionSemantics, TargetingUnit, diagnose, plan, render_report)

PROFILE = PlatformProfile(
    name="my-platform",
    auction_type="second_price",
    position=PositionSemantics(available=False),             # no position field
    ad_types={
        "search": AdTypeCapability(name="search", capability_class="keyword_auction"),
        "video": AdTypeCapability(name="video", capability_class="reach", targeting="none",
                                  keyword_drillable=False, sales_attributed=False, judged_on="reach"),
    },
    ad_type_aliases={"SPONSORED_SEARCH": "search", "VIDEO_REACH": "video"},
    status_map={"RUNNING": "active", "OUT_OF_BUDGET": "budget_held", "STOPPED": "operator_paused"},
    geo_grain="country",
    aggregate_geo_labels=frozenset({"row"}),                 # lower case
    currency_unit=0.01,                                      # material campaigns spend tens a day
    trading_day_start_hour=0.0, trading_day_end_hour=24.0, funding_target_hour=24.0,
)


class MyAdapter(BaseAdapter):
    """Turns the platform's report records into a Dataset."""

    def __init__(self, records: list[dict]) -> None:
        perf, entities, units = [], {}, {}
        for rec in records:
            query = rec["query"].strip().lower() if rec["query"] else None
            perf.append(PerfRow(
                date=date.fromisoformat(rec["day"]),          # a date, not a string
                entity_id=rec["line_item_id"],
                spend=rec["cost_micros"] / 1e6,               # convert to currency units
                impressions=float(rec["imps"]),
                sales=rec["conv_value_micros"] / 1e6,
                units=float(rec["conversions"]),
                targeting_unit=query, targeting_type="keyword" if query else None,
                geo=rec["region"], ad_type=rec["format"], status=rec["state"],
                bid=rec["max_cpm_micros"] / 1e6))
            entities.setdefault(rec["line_item_id"], Entity(
                entity_id=rec["line_item_id"], ad_type=rec["format"], status=rec["state"],
                daily_budget=rec["daily_cap_micros"] / 1e6))
            if query:
                units.setdefault(query, TargetingUnit(value=query, role=rec["intent"]))
        super().__init__(Dataset(profile=PROFILE, perf=perf, entities=list(entities.values()),
                                 targeting_units=list(units.values())))


data = MyAdapter(records).dataset
print(render_report(diagnose(data), plan(data)))
```

`rec["intent"]` is assumed to hold one of `own_brand`, `sibling_brand`, `competitor`, `generic`.
Map your platform's own labels to those four.

Next: add a `BudgetDayRow` list to the `Dataset` to unlock the budget checks, then the shelf and
share-of-voice feeds to unlock lever attribution.
