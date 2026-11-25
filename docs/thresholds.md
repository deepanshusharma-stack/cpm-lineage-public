# Threshold reference

<!-- GENERATED FILE. Do not edit by hand: run `python tools/gen_thresholds.py`. -->

Every tunable value in `cpm_lineage`: 1040 fields on 40 config dataclasses. This file is generated from the source by `tools/gen_thresholds.py`; the default and the meaning of each field are copied from the dataclass definition and its inline comment. A test (`tests/tools/test_gen_thresholds.py`) fails when this file and the code disagree.

## How to read and change a value

* **The defaults are judgment values.** They are starting points, not calibrated constants. No field here has been fitted to, or validated on, data from a real account in this repository.
* **Every class is frozen.** Build a changed copy and pass it to the function that takes it:

  ```python
  from dataclasses import replace
  from cpm_lineage import EngineConfig, Economics, PipelineConfig, plan

  cfg = EngineConfig(economics=Economics(breakeven=1.2, currency_unit=0.01))
  pipe = replace(PipelineConfig(), holdout_pct=0, pending_cap=40)
  plan(data, config=cfg, pipeline_cfg=pipe)
  ```

* **Per-account overrides from a mapping** go through `cpm_lineage.apply_overrides(cfg, {...})`, which returns `(new_config, applied, unknown)` and never mutates the input.
* **Money fields** are written in reference units (an account whose material campaigns spend thousands of currency units a day) and are multiplied by `Economics.currency_unit` at use. Change the unit once instead of editing each money field.
* **ROAS bars** written as "x breakeven" are multiplied by the effective breakeven `Economics.be = max(breakeven / max(ltv_mult, 0.1), 0.1)`.
* The **Default** column shows the default as written in the source. `X()` means "a default instance of `X`", documented in its own section.

## Where each class is passed

| Class | Passed as |
|---|---|
| `EngineConfig` (holds `Economics`, `AvailabilityPolicy`, `BudgetBoundPolicy`, `LivenessConfig`, `PositionConfig`, `TierConfig`, `WindowDefaults`, `TradingDay`) | `diagnose(data, config=...)`, `plan(data, config=...)` |
| `OrchestrationConfig` | `diagnose(..., options=...)`, `plan(..., options=...)` |
| `CpmConfig` | `diagnose(..., cpm_cfg=...)` and the functions in `cpm_lineage.cpm` |
| `ActionsConfig` | `plan(..., actions_cfg=...)` |
| `PipelineConfig` | `plan(..., pipeline_cfg=...)` |
| `VerdictConfig` | `plan(..., verdict_cfg=...)` |
| `OutcomeConfig` | `outcomes.pair_outcomes(..., cfg=...)`, `outcomes.evaluate(..., cfg=...)` |
| `LedgerConfig` | `store.ActionLedger(cfg=...)` |
| every other class | the `cfg=` (or similarly named) argument of the functions in its own module; `diagnose()` and `plan()` run those subsystems on their defaults |
| `PlatformProfile`, `AdTypeCapability`, `PositionSemantics` | declared by the adapter (`Dataset.profile`), or `diagnose(data, profile=...)` |

## Contents

* [`Economics`](#economics) - `cpm_lineage.config` (5 fields)
* [`AvailabilityPolicy`](#availabilitypolicy) - `cpm_lineage.config` (8 fields)
* [`BudgetBoundPolicy`](#budgetboundpolicy) - `cpm_lineage.config` (8 fields)
* [`LivenessConfig`](#livenessconfig) - `cpm_lineage.config` (2 fields)
* [`PositionConfig`](#positionconfig) - `cpm_lineage.config` (5 fields)
* [`TierConfig`](#tierconfig) - `cpm_lineage.config` (3 fields)
* [`WindowDefaults`](#windowdefaults) - `cpm_lineage.config` (12 fields)
* [`TradingDay`](#tradingday) - `cpm_lineage.config` (4 fields)
* [`EngineConfig`](#engineconfig) - `cpm_lineage.config` (13 fields)
* [`OrchestrationConfig`](#orchestrationconfig) - `cpm_lineage.engine` (7 fields)
* [`ActionsConfig`](#actionsconfig) - `cpm_lineage.actions.config` (123 fields)
* [`PipelineConfig`](#pipelineconfig) - `cpm_lineage.actions.pipeline_config` (67 fields)
* [`AttributionConfig`](#attributionconfig) - `cpm_lineage.attribution.config` (72 fields)
* [`BudgetConfig`](#budgetconfig) - `cpm_lineage.budget.config` (87 fields)
* [`ColdStartConfig`](#coldstartconfig) - `cpm_lineage.coldstart` (36 fields)
* [`ConflictConfig`](#conflictconfig) - `cpm_lineage.conflicts` (6 fields)
* [`CpmConfig`](#cpmconfig) - `cpm_lineage.cpm.config` (54 fields)
* [`DecomposeConfig`](#decomposeconfig) - `cpm_lineage.decompose` (13 fields)
* [`DrillConfig`](#drillconfig) - `cpm_lineage.drill` (8 fields)
* [`GeoConfig`](#geoconfig) - `cpm_lineage.geo.config` (30 fields)
* [`IdentityConfig`](#identityconfig) - `cpm_lineage.identity` (7 fields)
* [`KeywordConfig`](#keywordconfig) - `cpm_lineage.keywords.config` (49 fields)
* [`MarketConfig`](#marketconfig) - `cpm_lineage.market.config` (48 fields)
* [`OutcomeConfig`](#outcomeconfig) - `cpm_lineage.outcomes` (27 fields)
* [`PrescribeConfig`](#prescribeconfig) - `cpm_lineage.prescribe` (26 fields)
* [`DrillCallConfig`](#drillcallconfig) - `cpm_lineage.prescribe` (10 fields)
* [`QueueConfig`](#queueconfig) - `cpm_lineage.queue` (13 fields)
* [`RcaConfig`](#rcaconfig) - `cpm_lineage.rca.config` (72 fields)
* [`RecommendConfig`](#recommendconfig) - `cpm_lineage.recommend` (72 fields)
* [`ScopingConfig`](#scopingconfig) - `cpm_lineage.scoping` (7 fields)
* [`ScorecardConfig`](#scorecardconfig) - `cpm_lineage.scorecard` (10 fields)
* [`ScreenConfig`](#screenconfig) - `cpm_lineage.screen` (9 fields)
* [`ChangeLogConfig`](#changelogconfig) - `cpm_lineage.store.changelog` (8 fields)
* [`HistoryConfig`](#historyconfig) - `cpm_lineage.store.history` (5 fields)
* [`LedgerConfig`](#ledgerconfig) - `cpm_lineage.store.ledger` (11 fields)
* [`VerdictConfig`](#verdictconfig) - `cpm_lineage.verdict` (51 fields)
* [`WatchdogConfig`](#watchdogconfig) - `cpm_lineage.watchdog` (7 fields)
* Platform declaration: [`AdTypeCapability`](#adtypecapability), [`PlatformProfile`](#platformprofile), [`PositionSemantics`](#positionsemantics)

## Economics

`cpm_lineage.config.Economics` · defined in `src/cpm_lineage/config.py`

Account economics that re-base every profitability threshold.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `breakeven` | `1.0` | `float` | ROAS at which a first purchase breaks even (about 1 / contribution margin) |
| `ltv_mult` | `1.0` | `float` | lifetime value per first-purchase currency unit |
| `currency_unit` | `None` | `float \| None` | multiplier applied to money thresholds written in reference units; None = take the profile's (else 1) |
| `scale_bar_mult` | `2.0` | `float` | scale bar = 2 x be: clearly profitable, safe to add spend |
| `min_knob` | `0.1` | `float` | floor on both knobs so a typo cannot zero the breakeven |

## AvailabilityPolicy

`cpm_lineage.config.AvailabilityPolicy` · defined in `src/cpm_lineage/config.py`

One home for every availability threshold, on a 0..1 scale.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `block` | `0.6` | `float` | below this, fix availability before any ad change: out-of-stock products drop out of search |
| `caution` | `0.75` | `float` | below this, do not add spend (classic profile supply gate) |
| `floor` | `0.85` | `float` | healthy floor: geo-expand gate, root-cause floor, watchdog tripwire |
| `target` | `0.9` | `float` | level a healthy listing should hold |
| `drill_warn` | `0.7` | `float` | drill rows flag a product x geo below this |
| `share_band_floors` | `((10.0, False, 0.85), (5.0, True, 0.88))` | `tuple[tuple[float, bool, float], ...]` | market share > 10 %: strong brands tolerate a lower floor market share >= 5 % (share % bound, inclusive?, floor), checked in order |
| `share_band_default` | `0.9` | `float` | small brands need the highest floor |
| `enabled` | `True` | `bool` | turn all availability gates off (the verdict profile gates nothing on availability) |

## BudgetBoundPolicy

`cpm_lineage.config.BudgetBoundPolicy` · defined in `src/cpm_lineage/config.py`

Thresholds for the four budget-bound definitions (see ``BudgetTelemetry.is_bound``).

| Field | Default | Type | Meaning |
|---|---|---|---|
| `method` | `'auto'` | `str` | "auto" \| "sufficiency" \| "exhaustion" \| "capped_days" \| "held_days" |
| `sufficiency_max` | `0.9` | `float` | served under 90 % of expected live time over the last 3 days |
| `early_exhaust_minutes` | `720.0` | `float` | budget gone before 12 h into the day |
| `min_spend_fraction` | `0.5` | `float` | exhaust flag only trusted when spend >= half the cap |
| `min_exhaust_day_share` | `0.4` | `float` | exhaustion must persist on >= 40 % of days |
| `capped_spend_fraction` | `0.9` | `float` | a day counts as capped at spend >= 90 % of the cap |
| `capped_day_share` | `0.6` | `float` | capped on >= 60 % of active days |
| `held_days_min` | `3` | `int` | platform-held on >= 3 days: top up, not a free scale |

## LivenessConfig

`cpm_lineage.config.LivenessConfig` · defined in `src/cpm_lineage/config.py`

Day counts for the liveness rules.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `soft_dead_days` | `3` | `int` | soft-dead states are dead only with no spend in the last 3 days |
| `stale_days` | `7` | `int` | hard-dead entities stay in analyses while they spent in the last 7 days |

## PositionConfig

`cpm_lineage.config.PositionConfig` · defined in `src/cpm_lineage/config.py`

Validity range and semantics of the ``position`` field.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `valid_min` | `1.0` | `float` | slots are 1-based |
| `valid_max` | `100.0` | `float` | feeds carry sentinel values far above any real slot |
| `cap` | `21` | `int` | positions >= cap are bucketed as "cap+" |
| `lower_is_better` | `True` | `bool` | 1 = top slot |
| `sanity_max` | `500.0` | `float` | looser bound used when cleaning modelling features |

## TierConfig

`cpm_lineage.config.TierConfig` · defined in `src/cpm_lineage/config.py`

Active-day bounds of the data tiers.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `warm_min_days` | `7` | `int` | below this: COLD, nothing but priors |
| `mature_min_days` | `21` | `int` | move model and saturation curve need three weeks |
| `full_min_days` | `35` | `int` | full-length model |

## WindowDefaults

`cpm_lineage.config.WindowDefaults` · defined in `src/cpm_lineage/config.py`

Default window lengths per subsystem (all data-anchored unless noted).

| Field | Default | Type | Meaning |
|---|---|---|---|
| `identity_days` | `30` | `int` | campaign diagnosis: 30 vs prior 30 |
| `rca_days` | `14` | `int` | root-cause analysis: 14 vs prior 14 |
| `shiftshare_days` | `30` | `int` | account triage: 30 vs prior 30 |
| `drill_days` | `30` | `int` | drilldown buckets |
| `actions_days` | `30` | `int` | action lookback (14 / 30 / 60 offered) |
| `label_days` | `15` | `int` | placement-label evidence |
| `changelog_days` | `7` | `int` | input-change scan |
| `min_rows` | `5` | `int` | fewer rows than this in a window triggers the fallback split |
| `week_end_isoweekday` | `7` | `int` | budget reporting window ends on a completed Sunday (calendar-anchored) |
| `short_month_span_days` | `20` | `int` | budget window: trailing 3 weeks when the month has < 1 full week |
| `wow_shift_days` | `7` | `int` | custom range, week-over-week shift |
| `dod_shift_days` | `1` | `int` | custom range, day-over-day shift |

## TradingDay

`cpm_lineage.config.TradingDay` · defined in `src/cpm_lineage/config.py`

Local trading-day hours used by pacing and revenue-at-stake maths.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `start_hour` | `6.0` | `float` | serving starts |
| `end_hour` | `18.0` | `float` | last hour that still converts to same-day orders |
| `funding_target_hour` | `22.0` | `float` | a raise funds serving up to this hour |
| `tz_offset_minutes` | `0` | `int` | offset of local time from the feed's timestamps |

## EngineConfig

`cpm_lineage.config.EngineConfig` · defined in `src/cpm_lineage/config.py`

Shared base configuration passed to (or embedded by) every subsystem.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `economics` | `Economics()` | `Economics` | The embedded [`Economics`](#economics) (see its own section). |
| `availability` | `AvailabilityPolicy()` | `AvailabilityPolicy` | The embedded [`AvailabilityPolicy`](#availabilitypolicy) (see its own section). |
| `budget_bound` | `BudgetBoundPolicy()` | `BudgetBoundPolicy` | The embedded [`BudgetBoundPolicy`](#budgetboundpolicy) (see its own section). |
| `liveness` | `LivenessConfig()` | `LivenessConfig` | The embedded [`LivenessConfig`](#livenessconfig) (see its own section). |
| `position` | `PositionConfig()` | `PositionConfig` | The embedded [`PositionConfig`](#positionconfig) (see its own section). |
| `tiers` | `TierConfig()` | `TierConfig` | The embedded [`TierConfig`](#tierconfig) (see its own section). |
| `windows` | `WindowDefaults()` | `WindowDefaults` | The embedded [`WindowDefaults`](#windowdefaults) (see its own section). |
| `trading_day` | `None` | `TradingDay \| None` | None = built from the platform profile's trading-day hours |
| `indirect_sales_weight` | `1.0` | `float` | share of PerfRow.indirect_sales counted in entity ROAS by diagnose() and plan(): 1 = total, 0 = direct only |
| `attribution` | `'ridge_jury'` | `str` | attribution method; "shapley" is the optional exact alternative |
| `rule_profile` | `'classic'` | `str` | action rule profile: "classic" \| "marginal" \| "verdict" |
| `role_cohort_min_n` | `12` | `int` | a role cohort needs 12 units before its percentiles are used |
| `lever_min_coverage` | `0.2` | `float` | a lever with coverage at or below this is reported as not available |

## OrchestrationConfig

`cpm_lineage.engine.OrchestrationConfig` · defined in `src/cpm_lineage/engine.py`

Thresholds of the orchestration layer only (subsystems keep their own).

| Field | Default | Type | Meaning |
|---|---|---|---|
| `window_days` | `30` | `int` | default comparison: last 30 days vs the 30 before |
| `max_entities` | `25` | `int` | per-entity diagnosis is run on the top entities by window spend |
| `availability_drop_min` | `0.05` | `float` | a geo's own availability fall (0..1) that counts as an event |
| `product_drop_min` | `0.1` | `float` | a product-in-geo fall that names the product as the cause |
| `max_findings_per_kind` | `10` | `int` | keep the report reviewable |
| `rca` | `True` | `bool` | run the market root-cause read when the sell-out feed exists |
| `min_finding_days` | `7` | `int` | keyword-cut and collision findings need this many delivery days in the current window |

## ActionsConfig

`cpm_lineage.actions.config.ActionsConfig` · defined in `src/cpm_lineage/actions/config.py`

Every threshold of the rule families (every field has a default).

| Field | Default | Type | Meaning |
|---|---|---|---|
| **scope** | | | |
| `max_segments` | `6` | `int` | segments analysed per run, top by spend: keeps a plan reviewable |
| `scoped_bench_min_n` | `12` | `int` | verdict profile, user-scoped run: thinner role cohorts abstain |
| **classic budget cut** | | | |
| `cut_roas_mult` | `0.9` | `float` | cut when ROAS < 0.9 x breakeven: sustained loss, not noise at the line |
| `cut_min_spend` | `25000.0` | `float` | money: only campaigns at scale earn a cut card |
| `cut_pct` | `20.0` | `float` | fixed step: small enough to read out, large enough to matter |
| `cut_cooldown_days` | `7` | `int` | a cut needs a week to read out; stops cuts compounding on their echo |
| **classic budget scale** | | | |
| `scale_min_mult` | `3.0` | `float` | scale band floor, x breakeven: clearly proven |
| `scale_max_mult` | `12.0` | `float` | band ceiling: above it ROAS is usually carried by own-brand terms |
| `scale_high_own_share_max` | `0.5` | `float` | the ceiling lifts only when own-brand keyword share is known and below this |
| `scale_min_spend` | `10000.0` | `float` | money: evidence floor for scaling and for verdict-profile judging |
| `scale_certainty_mult` | `2.0` | `float` | lower ROAS bound must hold 2 x breakeven: one lucky week buys nothing |
| `scale_haircut` | `0.9` | `float` | diminishing-returns haircut on the computed raise |
| `scale_pct_min` | `5.0` | `float` | raise clamp floor, percent |
| `scale_pct_max` | `50.0` | `float` | raise clamp ceiling: survive a mis-read exhaust hour |
| `scale_stake_roas_cap_mult` | `6.0` | `float` | stake counts ROAS up to 6 x breakeven only |
| **verdict profile: unified threshold and band ladder** | | | |
| `raise_margin` | `0.1` | `float` | raise only when ROAS beats the bar by >= 10% (dead-zone edge) |
| `dead_zone_low` | `0.9` | `float` | ratio >= 0.90 of bar: no budget move |
| `deep_band_max` | `0.5` | `float` | ratio < 0.50 of bar: budget cut and bid cuts together |
| `exit_needed_min` | `0.4` | `float` | > 40% below bar and below breakeven = mischosen, start exiting |
| `exit_step_pct` | `30.0` | `float` | first step of an exit |
| `cut_step_min` | `0.1` | `float` | proportional cut clamp floor (share of the shortfall) |
| `cut_step_max` | `0.3` | `float` | proportional cut clamp ceiling |
| `sufficiency_floor` | `0.05` | `float` | floor on sufficiency inside the raise formula (avoids divide-by-zero) |
| `console_cut_tolerance` | `0.5` | `float` | money: latest cap lower than the cap 3 days ago by more than this = already cut |
| `bid_probe_days` | `14` | `int` | an applied bid cut holds the next one for two weeks |
| `reread_days` | `5` | `int` | days before a verdict-profile move is re-read |
| `drag_roas_mult` | `0.9` | `float` | drag keyword: ROAS below 0.90 x bar |
| `drag_roas_mult_top_volume` | `0.8` | `float` | wider tolerance for top-search-volume terms |
| `drag_min_spend` | `5000.0` | `float` | money: drag keyword evidence floor |
| `drag_max_listed` | `6` | `int` | keywords listed on a campaign bid-cut card |
| `new_user_top_share` | `0.25` | `float` | top 25% of terms by new users per (segment, bucket): never bid-cut |
| `volume_top_share` | `0.1` | `float` | top 10% by search volume: wider dead-zone |
| `daypart_stake_share` | `0.1` | `float` | day-part experiment stake = 10% of spend |
| **keyword pause** | | | |
| `brand_floor_mult` | `3.0` | `float` | own/sibling-brand terms carry 3 x the bar: brand ROAS is cheap |
| `pause_conquest_min_spend` | `10000.0` | `float` | money: waste concentrates in conquest terms, so a lower floor |
| `pause_other_roas_mult` | `0.35` | `float` | classic, non-conquest: ROAS <= 0.35 x floor |
| `pause_other_min_spend` | `25000.0` | `float` | money |
| `rescue_bucket_min_spend` | `50000.0` | `float` | money: bucket must be big enough to define "normal for its kind" |
| `rescue_bucket_avg_mult` | `0.5` | `float` | bucket average must itself be >= 0.5 x floor (no self-blessing) |
| `rescue_keyword_mult` | `0.6` | `float` | keyword within 0.6 x its bucket average is normal, not waste |
| `pause_verdict_mult` | `0.5` | `float` | verdict: ROAS <= 0.50 x bar ... |
| `pause_verdict_conquest_mult` | `0.6` | `float` | ... 0.60 x for conquest terms, both capped at breakeven |
| `pause_verdict_min_spend` | `10000.0` | `float` | money |
| `pause_max_listed` | `5` | `int` | keywords listed on a pause card |
| `conquest_keep_roas_mult` | `1.5` | `float` | wording only: conquest terms at >= 1.5 x breakeven are kept |
| **growth** | | | |
| `growth_min_pct` | `5.0` | `float` | segment sales growth that counts as a tide |
| `growth_share_delta_min_pp` | `-0.1` | `float` | own share must not be falling |
| `growth_roas_min_mult` | `2.0` | `float` | candidate band floor, x breakeven (ceiling = scale_max_mult) |
| `growth_raise_pct` | `10.0` | `float` | fixed raise |
| `growth_stake_share` | `0.1` | `float` | stake = own sales x growth x 10% |
| `growth_keyword_segments` | `2` | `int` | add-keyword cards only for the biggest segments |
| `growth_keyword_max_terms` | `5` | `int` | terms kept from the recommender's first 8 |
| `growth_keyword_ask` | `8` | `int` | terms read from the recommender output |
| **defense** | | | |
| `gainer_min_pp` | `0.3` | `float` | rival share gain that counts |
| `entrant_prior_share_max` | `0.5` | `float` | percent: was negligible ... |
| `entrant_current_share_min` | `2.0` | `float` | ... and is now material |
| `defend_share_drop_pp` | `0.3` | `float` | own share loss that triggers a defend raise |
| `moat_min_keyword_spend` | `5000.0` | `float` | money: moat campaign evidence floor |
| `moat_min_own_share` | `0.4` | `float` | moat = at least 40% of keyword spend on own/sibling terms |
| `defend_raise_pct` | `15.0` | `float` | fixed raise |
| `defend_cooldown_days` | `14` | `int` | one defend raise per campaign per fortnight (anti-spoof) |
| **bid** | | | |
| `head_min_spend` | `50000.0` | `float` | money: head-term pressure check only on a real head term |
| `head_cpm_rise` | `1.1` | `float` | geo CPM up > 10% counts as pressure |
| `head_min_geos` | `3` | `int` | pressure must be broad |
| `head_yield_pct` | `15.0` | `float` | bid-down when the head term cannot afford the fight |
| `head_stake_share` | `0.1` | `float` | stake = 10% of head-term spend |
| `bid_min_spend_classic` | `25000.0` | `float` | money |
| `bid_min_spend_verdict` | `2500.0` | `float` | money: a raise is reversible, so a low floor |
| `bid_cut_min_spend_verdict` | `5000.0` | `float` | money: cuts need more evidence than raises |
| `bid_up_bucket_mult` | `1.2` | `float` | classic up: beats its own kind by 20% ... |
| `bid_up_be_mult` | `1.5` | `float` | ... and clears 1.5 x breakeven |
| `bid_up_min_position` | `5.0` | `float` | classic up: position worse than 5 (unseen) |
| `bid_step_pct` | `15.0` | `float` | classic step, both directions |
| `bid_down_be_mult` | `0.8` | `float` | classic down: below 0.8 x breakeven ... |
| `bid_down_bucket_mult` | `0.5` | `float` | ... and below half its bucket average |
| `bid_top_slot` | `2.0` | `float` | classic down only at position <= 2 (paying the top-slot premium) |
| `bid_settle_position` | `3.0` | `float` | wording only: where a bid-down should settle |
| `bid_raise_min_position` | `6.0` | `float` | verdict raise: position worse than 6 |
| `bid_headroom_min` | `0.08` | `float` | verdict raise: headroom under 8% is not worth a move |
| `bid_raise_cap` | `0.25` | `float` | verdict raise step ceiling |
| `bid_raise_cap_deep` | `0.4` | `float` | ceiling when buried (position >= bid_deep_position) |
| `bid_deep_position` | `11.0` | `float` | buried: off the first screen |
| `rented_owner_share` | `0.6` | `float` | a rival owning >= 60% of a term cancels the deep escalation |
| `bid_cut_max_position` | `9.0` | `float` | verdict cut only while still visible; never below the fold |
| `bid_dead_zone` | `0.1` | `float` | verdict cut: shortfall under 10% is noise |
| `bid_dead_zone_top_volume` | `0.2` | `float` | 20% for top-search-volume terms |
| `bid_fixed_step_pct` | `10.0` | `float` | raise step ceiling where the auction is not pay-your-bid |
| **experiment** | | | |
| `experiment_exhaust_minutes` | `600.0` | `float` | budget gone before 10:00 local |
| `experiment_credible_spend_fraction` | `0.5` | `float` | exhaustion is believable only if spend >= half the cap |
| `experiment_roas_min_mult` | `0.8` | `float` | undecided band, x breakeven |
| `experiment_roas_max_mult` | `2.5` | `float` | undecided band ceiling, x breakeven: above it the entity is a clear winner, not a test |
| `experiment_min_days` | `14` | `int` | young campaigns do not need the test |
| `experiment_days` | `7` | `int` | test length |
| `experiment_success_pct` | `15.0` | `float` | worst-slot ROAS improvement that counts as success |
| `experiment_stake_share` | `0.1` | `float` | stake = 10% of the entity's window spend |
| `experiment_keywords` | `5` | `int` | keywords attached to an experiment card |
| **sov-gap** | | | |
| `sov_under_gap_pp` | `8.0` | `float` | market share above ad share of voice by 8 points |
| `sov_under_min_worth` | `50000.0` | `float` | money: 8 points of a tiny segment is noise money |
| `sov_roas_min_mult` | `1.5` | `float` | candidate band floor (ceiling = scale_max_mult) |
| `sov_generic_own_share_max` | `0.4` | `float` | prefer generic-heavy campaigns |
| `sov_raise_pct` | `15.0` | `float` | fixed raise on the share-of-voice gap card, percent |
| `sov_stake_share` | `0.1` | `float` | stake = segment sales x gap x 10% |
| `sov_over_gap_pp` | `10.0` | `float` | share of voice above market share by 10 points |
| `sov_over_blended_mult` | `2.0` | `float` | ... and blended ROAS below 2 x breakeven |
| `sov_over_stake_share` | `0.15` | `float` | stake of the over-invested card = 15% of the segment's ad spend |
| **geo** | | | |
| `geo_row_min_spend` | `2000.0` | `float` | money: campaign x geo rows below this are ignored |
| `geo_top_entities` | `8` | `int` | campaigns scanned, top by spend |
| `geo_min_spend` | `50000.0` | `float` | money: campaign must be big enough to hide a geography |
| `geo_min_rows` | `3` | `int` | a campaign needs at least this many geo rows before one can be called weak |
| `geo_weak_spend_share` | `0.15` | `float` | weak geo must carry >= 15% of the campaign's spend |
| `geo_weak_roas_mult` | `0.6` | `float` | ... at < 0.6 x the campaign average |
| `geo_decisive_roas_mult` | `0.4` | `float` | below 0.4 x = decisive (split); in between = experiment |
| `geo_experiment_days` | `14` | `int` | length of the geo carve test, days |
| `geo_controls` | `2` | `int` | matched control geos |
| `geo_experiment_stake_share` | `0.2` | `float` | stake = 20% of the weak geo's spend |
| `geo_control_default_availability` | `0.85` | `float` | availability assumed when a geo does not report it |
| `geo_expand_share_gap_pp` | `3.0` | `float` | own share at least 3 points under the blend |
| `geo_expand_min_volume_share` | `0.03` | `float` | geo must hold >= 3% of segment volume |
| `geo_expand_min_availability` | `0.85` | `float` | classic profile only; verdict does not gate media on availability |
| **collision** | | | |
| `collision_min_spend` | `10000.0` | `float` | money: per campaign per geo |
| `collision_stake_share` | `0.15` | `float` | stake = 15% of the non-best spend |
| **operator floors** | | | |
| `operator_floor_min` | `0.1` | `float` | stated floors outside [0.1, 100] are discarded |
| `operator_floor_max` | `100.0` | `float` | upper bound of an accepted operator floor (see operator_floor_min) |

## PipelineConfig

`cpm_lineage.actions.pipeline_config.PipelineConfig` · defined in `src/cpm_lineage/actions/pipeline_config.py`

Every threshold of the post-rule pipeline (every field has a default).

| Field | Default | Type | Meaning |
|---|---|---|---|
| **holdout** | | | |
| `holdout_pct` | `15.0` | `float` | share of eligible entities receiving no cards: standing counterfactual |
| `holdout_period` | `'month'` | `str` | "month" \| "week": the assignment is stable within a period, then rotates |
| **vetoes** | | | |
| `under_test_days` | `14` | `int` | an applied experiment blocks other moves this long: concurrent moves confound it |
| `budget_bound_method` | `None` | `str \| None` | None = by rule profile ("exhaustion" classic, "sufficiency" verdict) |
| `supply_gate_enabled` | `None` | `bool \| None` | None = by rule profile (on for classic/marginal, off for verdict) |
| **conflict table** | | | |
| `priority` | `dict(PRIORITY)` | `Mapping[str, float]` | deterministic table, never a model |
| `stake_ratio_valve` | `3.0` | `float` | loser stake > 3 x winner stake: abstain, a table should not pre-decide it |
| `valve_min_winner_stake` | `1.0` | `float` | floor on the winner stake in the ratio (avoids divide-by-zero) |
| `valve_counts_allies` | `False` | `bool` | False = only the opposing side can trip the valve (intended); True = allies count too |
| `merge_keeper` | `'higher_stake'` | `str` | "higher_stake" (first-seen tiebreak) \| "first_seen" |
| `contested_min_ratio` | `0.3` | `float` | collision cards below this share of contested days are a schedule, not a collision |
| **confidence** | | | |
| `conf_base` | `40.0` | `float` | additive score starts here |
| `conf_min` | `25.0` | `float` | lower bound: never claim nothing is known |
| `conf_max` | `90.0` | `float` | upper bound: never claim certainty |
| `conf_long_window_days` | `30` | `int` | a lookback at least this long earns the full window credit |
| `conf_long_window` | `15.0` | `float` | credit for a long evidence window |
| `conf_short_window` | `8.0` | `float` | credit for a shorter window |
| `conf_spend_per_point` | `20000.0` | `float` | money: one point per this much evidence spend |
| `conf_spend_max` | `10.0` | `float` | spend-depth credit ceiling |
| `conf_clarity_far` | `0.5` | `float` | \|ROAS - breakeven\| at least this: effect is clear |
| `conf_clarity_far_pts` | `15.0` | `float` | credit for a clear effect |
| `conf_clarity_mid` | `0.25` | `float` | \|ROAS - breakeven\| at least this: effect is visible |
| `conf_clarity_mid_pts` | `8.0` | `float` | credit for a visible effect |
| `conf_clarity_near_pts` | `3.0` | `float` | credit when ROAS sits at the line |
| `conf_keyword_down` | `12.0` | `float` | keyword pause: pure waste, little inference |
| `conf_bid` | `8.0` | `float` | bid card: rank-economics signal |
| `conf_experiment` | `5.0` | `float` | experiment: a test, not a bet |
| `conf_geo` | `10.0` | `float` | geo card: arithmetic, not inference |
| `conf_collision` | `12.0` | `float` | collision: structural |
| `conf_watch` | `4.0` | `float` | watch card: informational |
| `conf_avail_broken` | `-8.0` | `float` | availability below the caution band |
| `conf_avail_shaky` | `-5.0` | `float` | availability between caution and floor |
| `conf_avail_unmeasured` | `-8.0` | `float` | scale-up in a segment with no availability reading |
| `conf_young_days` | `14` | `int` | entities live for fewer days are young |
| `conf_young` | `-10.0` | `float` | young-entity penalty: history too short |
| `winrate_min_n` | `5` | `int` | realised family win rate is used only with this many paired outcomes |
| `conf_winrate_scale` | `20.0` | `float` | points per unit of (rate - 0.5) |
| `conf_winrate_max` | `10.0` | `float` | win-rate swing bound |
| `conf_precedent_scale` | `16.0` | `float` | points per unit of (won/n - 0.5) for this family on this entity |
| `conf_precedent_max` | `8.0` | `float` | precedent swing bound |
| **expected value** | | | |
| `ev_prior` | `0.5` | `float` | P(success) without a realised win rate: flat and auditable |
| `ev_use_effective_breakeven` | `False` | `bool` | False = bleed avoided uses literal 1 - ROAS (the default); True = breakeven - ROAS |
| **guardrail** | | | |
| `spend_base_floor` | `1.0` | `float` | spend-ceiling base when no budget telemetry exists |
| **equity gate** | | | |
| `equity_own_share_min` | `0.4` | `float` | entity carries the brand moat at this own-brand keyword spend share |
| `equity_share_loss_pp` | `-0.2` | `float` | own market share change below this (pp) = already losing share |
| `equity_freefall_pct` | `-10.0` | `float` | segment total below this: gate stands down, do not ride a dying segment |
| **annotations** | | | |
| `posture_saturation_share` | `60.0` | `float` | own market share (pct) at which growth has a steep marginal cost |
| `posture_decline_pct` | `-10.0` | `float` | segment growth below this: efficiency posture, not expansion |
| `term_owner_share` | `0.6` | `float` | a brand taking this share of a term's organic impressions owns it |
| `term_crowded_advertisers` | `8` | `int` | this many advertisers on a term = crowded auction |
| **caps** | | | |
| `family_caps` | `dict(FAMILY_CAPS)` | `Mapping[tuple[str, str], int]` | reviewability |
| `family_cap_default` | `99` | `int` | cap for (family, type) pairs not listed |
| `pending_cap` | `25` | `int` | a plan an operator can review in one sitting |
| `polish_batch` | `12` | `int` | pending cards offered to the optional prose polisher |
| `polish_why_chars` | `300` | `int` | polished wording truncation |
| `polish_caution_chars` | `200` | `int` | polished caution truncation |
| **marginal sizing** | | | |
| `marginal_min_days` | `8` | `int` | spending days needed for a finite-difference reading |
| `marginal_min_spend_gap` | `100.0` | `float` | money per day: the two halves must differ by more than this |
| `marginal_veto_mult` | `0.8` | `float` | raise vetoed when marginal ROAS < 0.8 x breakeven |
| `marginal_halve_mult` | `1.5` | `float` | raise halved when marginal ROAS < 1.5 x breakeven |
| `marginal_min_pct` | `5.0` | `float` | floor of a halved raise, percent |
| `marginal_deepen_mult` | `0.5` | `float` | cut deepened when marginal ROAS < 0.5 x breakeven |
| `marginal_deepen_step` | `5.0` | `float` | extra percent on a deepened cut |
| `marginal_deepen_floor` | `25.0` | `float` | a deepened cut is at least this deep, percent |
| **budget-neutral rebalance** | | | |
| `rebalance_max_bleed` | `0.3` | `float` | never take more than 30% of a donor's cap per step |
| `rebalance_min_pct` | `5.0` | `float` | smaller donor steps are console noise |
| `rebalance_confidence` | `66` | `int` | flat donor confidence: the swap, not the donor, is the thesis |

## AttributionConfig

`cpm_lineage.attribution.config.AttributionConfig` · defined in `src/cpm_lineage/attribution/config.py`

Named thresholds of the lever-attribution layer (every field has a default).

| Field | Default | Type | Meaning |
|---|---|---|---|
| **feature preparation** | | | |
| `min_dln` | `0.0001` | `float` | below this \|d ln ROAS\| the share denominator falls back (model total, then this) |
| `log_floor` | `1e-09` | `float` | floor inside ln() of a channel value; avoids ln(0) |
| `coverage_min` | `0.2` | `float` | lever is "live" only above this share of real, non-zero days (judged before imputation) |
| `impute_max` | `0.4` | `float` | lever is suppressed above this share of imputed days (model would mostly see synthetic values) |
| `thin_min_levers` | `4` | `int` | fewer live levers than this -> no entity fit, account elasticities only |
| `position_sanity_min` | `1.0` | `float` | positions below this are data errors -> missing |
| `position_sanity_max` | `500.0` | `float` | positions above this are mis-joined fields -> missing |
| `share_min` | `0.0` | `float` | share-type levers must lie in [share_min, share_max] |
| `share_max` | `1.0` | `float` | share-type levers must lie in [share_min, share_max] |
| `discount_min` | `-0.5` | `float` | a "discount" below -50% is a price above list by half: data error |
| `discount_max` | `0.95` | `float` | a discount above 95% is a data error |
| `price_gap_min` | `0.1` | `float` | own/competitor price ratio below 0.1 is a unit mismatch |
| `price_gap_max` | `10.0` | `float` | own/competitor price ratio above 10 is a unit mismatch |
| `adversarial_role` | `'competitor'` | `str` | targeting role whose spend share is the targeting-mix lever |
| `zero_is_real` | `('own_discount', 'comp_discount')` | `tuple[str, ...]` | levers where a true 0 is a valid observation, not "missing" |
| **collinearity guard** | | | |
| `vif_max` | `8.0` | `float` | price-cluster VIF at or above this folds the discounts into price_gap |
| `vif_min_rows` | `8` | `int` | fewer complete rows than this -> VIF reported as 1.0 (cannot judge) |
| `vif_r2_floor` | `1e-09` | `float` | floor on 1 - R^2 so a perfect fit yields a huge, finite VIF |
| **pooled sign-constrained ridge** | | | |
| `sd_floor` | `1e-09` | `float` | floor on a lever's standard deviation when z-scoring |
| `lambdas` | `(1.0, 4.0, 16.0, 64.0)` | `tuple[float, ...]` | candidate pooling strengths chosen by blocked time-series CV |
| `default_lambda` | `4.0` | `float` | used when CV cannot discriminate; also the account-prior strength |
| `short_lambda` | `16.0` | `float` | fixed heavy pooling in short mode (tiny panel) |
| `prior_lambda` | `4.0` | `float` | ridge strength of the account-level prior fit (toward zero) |
| `cv_splits` | `4` | `int` | blocked time-series folds in full mode |
| `cv_splits_short` | `3` | `int` | blocked time-series folds in short mode |
| `cv_min_train` | `10` | `int` | folds with fewer training rows are dropped |
| `cv_min_test` | `3` | `int` | folds with fewer test rows are not scored |
| `solver_max_sweeps` | `5000` | `int` | coordinate-descent sweep cap of the bounded solver |
| `solver_tol` | `1e-12` | `float` | largest coefficient change that still counts as "not converged" |
| **first-difference cross-check** | | | |
| `delta_lambda` | `4.0` | `float` | ridge strength of the first-difference model |
| `delta_min_rows` | `12` | `int` | difference model and adstock variants need at least this many rows |
| `adstock_lambdas` | `(0.2, 0.4)` | `tuple[float, ...]` | geometric carry-over rates tried for visibility levers |
| `adstock_margin` | `0.05` | `float` | a carry-over variant must beat the current best skill by more than this |
| `skill_min_predictions` | `5` | `int` | fewer one-step predictions -> skill undefined |
| `skill_min_sse` | `1e-08` | `float` | persistence error below this means a constant channel -> skill undefined |
| **jury, verdict, explained share** | | | |
| `jury_materiality` | `0.05` | `float` | a model "votes" on a lever only at >= 5% of the move |
| `explained_floor_pct` | `20.0` | `float` | \|net explained\| below this -> verdict "unexplained" |
| `skill_floor` | `-0.25` | `float` | move skill below this (clearly worse than persistence) -> "unexplained" |
| `confirmed_min_skill` | `0.0` | `float` | "confirmed" needs the cross-check to be at least as good as persistence |
| `small_move_dln` | `0.08` | `float` | \|log ROAS move\| below this may be a "small move" |
| `small_move_gross_mult` | `2.0` | `float` | ... when gross lever activity exceeds this multiple of the move |
| `chip_min_lever_pct` | `3.0` | `float` | no per-channel split for levers under 3% of the move |
| `chip_min_channel_pct` | `5.0` | `float` | a channel is shown at >= 5% of the move |
| `chip_min_rel` | `0.15` | `float` | ... and >= 15% of the lever's largest channel |
| **demand-evidence gate (tier-2 levers)** | | | |
| `evidence_min_rows` | `14` | `int` | days with both series positive needed to judge a demand lever |
| `evidence_min_r` | `0.35` | `float` | \|correlation\| below this is "no-link" |
| `evidence_half_r` | `0.15` | `float` | both half-sample correlations must reach this, same sign, to be "stable" |
| `evidence_lag_r` | `0.55` | `float` | a no-link with \|lag-1 correlation\| at or above this is "leading" |
| **elasticity-sized moves** | | | |
| `size_bid_step` | `0.1` | `float` | bid test step: 10% multiplicative |
| `size_price_gap_step` | `0.05` | `float` | relative price test step: 5% multiplicative |
| `size_position_step` | `1.0` | `float` | position test step: one slot |
| `size_share_step` | `0.05` | `float` | share-type levers test step: 5 points, clamped to [0, 1] |
| `size_min_beta` | `0.001` | `float` | net elasticity smaller than this is not sized |
| `size_min_draw` | `0.005` | `float` | raw step smaller than this (after clamping) is not sized |
| `size_min_droas` | `0.02` | `float` | ROAS effect below this is noise, suppressed |
| `size_max_droas_share` | `0.6` | `float` | ROAS effect above 60% of current ROAS is implausible, suppressed |
| `size_roas_floor` | `0.1` | `float` | floor on current ROAS inside the plausibility bound |
| **confidence label** | | | |
| `conf_high_min_days` | `60` | `int` | High needs two full comparable windows |
| `conf_high_max_imputed` | `0.25` | `float` | High needs at most a quarter of live-lever days imputed |
| `conf_low_max_days` | `35` | `int` | fewer days than this -> Low |
| `conf_low_min_imputed` | `0.45` | `float` | more imputation than this -> Low |
| **raw facts** | | | |
| `fact_min_days` | `3` | `int` | each window needs this many real days of a lever to state a fact |
| `fact_share_points` | `0.07` | `float` | share-type levers: fact at a 7-point difference of window means |
| `fact_bid_rel` | `0.2` | `float` | bid: fact at a 20% relative change |
| `fact_other_rel` | `0.25` | `float` | other levers: fact at a 25% relative change |
| `fact_align_rel` | `0.1` | `float` | aligned channel is mentioned when it moved at least 10% |
| `fact_max` | `7` | `int` | cap on the number of facts |
| `sov_drop_points` | `0.05` | `float` | paid share-of-voice fall that triggers the denominator split |
| `sov_pool_up` | `0.1` | `float` | ad pool per day up more than this -> rivals scaled in |
| `sov_own_down` | `0.1` | `float` | own ad impressions per day down more than this -> own pull-back |
| `displacement_impr_up` | `0.2` | `float` | impressions per day up this much with falling share -> deeper slots |
| **exact Shapley (optional attributor)** | | | |
| `shapley_max_features` | `18` | `int` | exact enumeration costs 2^n model rows; 18 -> 262,144 |

## BudgetConfig

`cpm_lineage.budget.config.BudgetConfig` · defined in `src/cpm_lineage/budget/config.py`

Named thresholds for sufficiency, the saturation curve, raise gating, budget moves, the ad-type mix and the three money estimators.

| Field | Default | Type | Meaning |
|---|---|---|---|
| **sufficiency / required budget** | | | |
| `oob_spend_fraction` | `0.5` | `float` | an exhaustion flag counts only when spend reached half the cap (a flag raised far below the cap is a pacing artefact, not exhaustion) |
| `sufficiency_min` | `0.01` | `float` | clamp floor: guards division by zero and caps the multiplier at 100x |
| `sufficiency_max` | `1.0` | `float` | clamp ceiling: a day cannot be more than fully served |
| `placeholder_cap` | `100.0` | `float` | MONEY: a cap below this is a placeholder, no top-up maths on it |
| `oob_min_extra` | `1.0` | `float` | MONEY: extra below this is rounding dust, kept out of the summary |
| `projection_days` | `30` | `int` | display-only monthly projection of the daily extra |
| `exhaust_minute_origin` | `0.0` | `float` | minutes after local midnight at which serving starts; subtracted when the exhaust minute stands in for live minutes (0 = the clock starts at midnight) |
| `tag_separator` | `'/'` | `str` | tag identity is group + separator + name: one name can exist under two groups |
| `display_decimals` | `0` | `int` | rounding of money figures in display outputs (never used inside decisions) |
| **budget telemetry** | | | |
| `telemetry_days` | `30` | `int` | lookback of the per-entity telemetry |
| `telemetry_min_spend` | `5000.0` | `float` | MONEY: entities at or below this window spend carry too little evidence |
| `telemetry_se_min_days` | `5` | `int` | standard error of daily ROAS needs at least 5 days |
| `telemetry_z` | `1.28` | `float` | about 90% one-sided normal bound on ROAS |
| `recent_days` | `3` | `int` | "last 3 days" for served-time sufficiency and the before-recent cap |
| **saturation curve** | | | |
| `sat_lookback_days` | `90` | `int` | every curve fit reads about 90 daily points |
| `sat_min_days` | `21` | `int` | fewer spending days cannot identify two parameters |
| `sat_min_cv` | `0.15` | `float` | spend that never varies cannot reveal a response |
| `sat_min_corr` | `0.2` | `float` | no positive spend-sales relation (must be strictly greater) = fiction |
| `regime_time_corr` | `0.6` | `float` | \|corr(spend, time)\| above this ... |
| `regime_half_cv` | `0.15` | `float` | ... with both half-window CVs below this = two flat clusters confounded with time |
| `sat_b_min` | `1e-06` | `float` | lower bound of the scale parameter |
| `sat_theta_min` | `0.01` | `float` | lower bound of the half-saturation parameter |
| `sat_theta_span` | `1000000.0` | `float` | theta is searched up to this multiple of the largest observed spend (near-linear data runs to the bound) |
| `sat_grid_points` | `96` | `int` | coarse log-spaced theta grid of the profile search |
| `sat_refine_iters` | `80` | `int` | golden-section refinement steps around the best grid cell |
| `sat_min_r2` | `0.05` | `float` | below this the fit is rejected outright |
| `sat_decision_r2` | `0.3` | `float` | only fits at or above this may block a raise or size a ceiling |
| `sat_ceiling_support` | `2.0` | `float` | a ceiling beyond 2x the largest observed spend is an optimiser artefact |
| `whatif_mults` | `(0.7, 1.0, 1.3, 1.5, 2.0)` | `tuple[float, ...]` | cap multipliers of the what-if grid |
| `extrapolation_mult` | `1.05` | `float` | a what-if point beyond 1.05x observed spend is extrapolation |
| **per-entity probe and raise gating** | | | |
| `probe_days` | `30` | `int` | lookback of the budget probe |
| `cap_hit_fraction` | `0.9` | `float` | a day is "capped" at spend >= 90% of the daily budget |
| `capped_day_share` | `0.6` | `float` | the cap binds when capped on >= 60% of active days |
| `min_active_days` | `7` | `int` | fewer spending days is not enough to call anything |
| `util_anomaly` | `5.0` | `float` | mean spend above 5x the cap: the cap does not constrain |
| `soft_cap_exhaust_share` | `0.3` | `float` | capped by spend but flagged exhausted on < 30% of days: pacing artefact |
| `canonical_days` | `7` | `int` | window of the serving-evidence override |
| `held_days_canonical` | `5` | `int` | held (or day-parted) on >= 5 of the last 7 days is canonical |
| `small_slot_cap` | `2000.0` | `float` | MONEY: a day-parted entity below this cap risks exhausting inside a slot |
| `marginal_block` | `0.9` | `float` | unrounded marginal ROAS below this blocks a raise (below 1 with a noise margin) |
| `marginal_ceiling` | `1.0` | `float` | a raise needs a marginal ROAS of at least this at the cap: below it the next unit of spend returns less than it costs |
| `range_min_headroom` | `0.01` | `float` | the marginal-1 spend must exceed the cap by this fraction before a raise range is offered |
| `capped_roas_min` | `1.0` | `float` | x breakeven: capped-day ROAS below this = exhausting but inefficient |
| `range_low_mult` | `1.3` | `float` | recommended range, low end |
| `range_rec_mult` | `1.5` | `float` | recommended range, recommended point |
| `range_high_mult` | `2.0` | `float` | recommended range, high end (before the ceiling clamp) |
| `range_requires_headroom` | `True` | `bool` | no range when a decision-grade ceiling sits at or below the cap or the gating curve's marginal is under marginal_ceiling (False = fall back to the full range) |
| `util_ladder_max` | `0.85` | `float` | the under-utilisation ladder applies below this utilisation |
| `util_severe` | `0.2` | `float` | below this utilisation: investigate before anything else |
| `ladder_top_position_gap` | `0.5` | `float` | mean position within half a slot of the top slot = already at the top |
| `ladder_rank1_share` | `0.6` | `float` | top-slot impression share at or above this confirms a volume ceiling |
| `stepwise_cpm_ratio` | `0.85` | `float` | realised CPM under 85% of the own bid = winning below bid |
| `stepwise_spend_share` | `0.5` | `float` | ... on at least half of spend |
| `stepwise_min_targets` | `3` | `int` | ... across at least 3 targets |
| `bleed_share_hold` | `0.4` | `float` | 40% of spend below breakeven: fix the bleed before raising |
| `organic_share_hold` | `0.7` | `float` | 70% organic share: paid demand is mostly non-incremental |
| `hold_monitor_days` | `7` | `int` | profitable but not scale-worthy: hold and re-read in a week |
| **account-level budget moves** | | | |
| `moves_days` | `30` | `int` | lookback of the budget-constraint scan |
| `release_util_max` | `0.7` | `float` | parked budget: utilisation below 70% ... |
| `release_roas_max` | `1.0` | `float` | x breakeven: ... and ROAS below breakeven |
| `release_fraction` | `0.4` | `float` | release 40% of the cap, bounded by realised spend |
| `tighten_roas_max` | `1.0` | `float` | x breakeven: a binding cap funding a loss |
| `scale_step` | `0.5` | `float` | first-pass raise of +50%, an upper bound only |
| `moves_top_n` | `8` | `int` | rows kept per bucket |
| `moves_require_decision_r2` | `True` | `bool` | a fit below sat_decision_r2 neither sizes nor ejects a raise (False = any accepted fit may do both) |
| `release_blocked_statuses` | `('operator_paused', 'ended', 'removed', 'budget_held', 'platform_paused')` | `tuple[str, ...]` | canonical statuses whose budget is not "parked" |
| **ad-type mix** | | | |
| `mix_days` | `30` | `int` | length of each of the two adjacent windows |
| `plateau_marginal` | `1.0` | `float` | ad-type marginal ROAS below this = near plateau, not a rebalance sink |
| `material_type_spend` | `50000.0` | `float` | MONEY: an ad type below this spend is not worth a recommendation |
| `reduce_type_spend` | `100000.0` | `float` | MONEY: a non-drillable loss-making type above this gets "reduce / cap" |
| `pareto_share` | `0.8` | `float` | entities inside this cumulative share of drag are "the few" |
| **estimator (a): stake and expected value** | | | |
| `stake_roas_cap_mult` | `6.0` | `float` | x breakeven: caps ROAS in the stake so outliers do not dominate |
| `exhaust_haircut` | `0.9` | `float` | haircut on the hours-to-fund raise |
| `raise_pct_min` | `5.0` | `float` | smallest raise step, percent |
| `raise_pct_max` | `50.0` | `float` | largest raise step, percent |
| `ev_prior` | `0.5` | `float` | success probability before any realised outcomes |
| `ev_min_outcomes` | `5` | `int` | a family's realised win rate replaces the prior at >= 5 paired outcomes |
| `ev_breakeven_relative` | `False` | `bool` | False = bleed-avoided uses literal 1 - ROAS (the default); True = breakeven - ROAS |
| **estimator (c): finite-difference marginal ROAS** | | | |
| `fd_min_days` | `8` | `int` | need at least 8 spending days to split low / high halves |
| `fd_min_spend_gap` | `100.0` | `float` | MONEY: mean-spend gap between halves must exceed this |
| `fd_veto_mult` | `0.8` | `float` | x breakeven: a raise is vetoed below this marginal |
| `fd_halve_mult` | `1.5` | `float` | x breakeven: a raise is halved below this marginal |
| `fd_deepen_mult` | `0.5` | `float` | x breakeven: a cut is deepened below this marginal |
| `fd_min_step_pct` | `5.0` | `float` | a halved raise never goes below 5% |
| `fd_deepen_step_pct` | `5.0` | `float` | a deepened cut adds 5 points ... |
| `fd_cut_floor_pct` | `-25.0` | `float` | ... and is at least a 25% cut |
| `fd_exempt_families` | `('defense',)` | `tuple[str, ...]` | families the marginal pass never touches |

## ColdStartConfig

`cpm_lineage.coldstart.ColdStartConfig` · defined in `src/cpm_lineage/coldstart.py`

Thresholds of the cold-start layer. ROAS bars are breakeven multiples.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `shrink_k` | `14.0` | `float` | pseudo-count: evidence gets half weight at 14 evidence-units |
| `camp_evidence` | `4.0` | `float` | evidence-units per independent campaign (replication is strong evidence) |
| `blend_tau_ref` | `2000.0` | `float` | pseudo-spend of the local blend: own data gets half weight at this own spend |
| `blend_min_own_spend_ref` | `500.0` | `float` | below this own spend, own ROAS is a handful of orders: do not blend |
| `conf_high_weight` | `0.7` | `float` | self-weight at or above -> high confidence (persistence depth, not age) |
| `conf_med_weight` | `0.4` | `float` | self-weight at or above -> medium confidence |
| `brand_defense_roas_be` | `15.0` | `float` | own-brand term at or above: absurd ROAS = organic capture, never scale on it |
| `winner_roas_be` | `2.0` | `float` | absolute winner bar (also met by the cohort p75) |
| `bleeder_roas_be` | `1.0` | `float` | absolute bleeder bar (also met by falling under the cohort p25) |
| `proven_min_camps` | `2` | `int` | proven needs replication in two campaigns ... |
| `proven_min_days` | `10` | `int` | ... or ten active days (a few good days are a lead, not proof) |
| `halo_spare_share` | `0.2` | `float` | indirect share above this: cap, do not kill |
| `blend_disclosure` | `0.15` | `float` | \|blended - shrunk\| at or above this adds an explanatory line |
| `sibling_prefix_len` | `5` | `int` | typo-prefix head match needs both heads this long |
| `sibling_remainder_len` | `4` | `int` | substring remainder match needs the shorter remainder this long |
| `cohort_min_n` | `12` | `int` | a role cohort smaller than this is not trusted (5 is a looser alternative) |
| `product_prior_roas_be` | `1.5` | `float` | flat product prior when the history store is empty (no product cohorts) |
| `product_proven_roas_be` | `2.0` | `float` | product at or above -> proven |
| `product_bleeder_roas_be` | `1.0` | `float` | product below -> bleeder |
| `geo_min_count` | `2` | `int` | a geo signal needs at least two geos |
| `geo_list_n` | `3` | `int` | geos listed per side |
| `geo_strong_roas_be` | `1.0` | `float` | strong at or above, weak below |
| `coverage_high_pct` | `80.0` | `float` | share of units with history for High state confidence |
| `coverage_med_pct` | `40.0` | `float` | ... for Medium |
| `max_keyword_bleed_actions` | `2` | `int` | stop-loss first: two keyword bleeders ... |
| `max_product_bleed_actions` | `1` | `int` | ... and one product bleeder |
| `max_defense_actions` | `1` | `int` | one brand-defense action |
| `max_winner_actions` | `2` | `int` | two keyword winners |
| `max_product_winner_actions` | `1` | `int` | one product winner |
| `max_state_actions` | `3` | `int` | an operator acts on a short list |
| `regime_lookback_days` | `30` | `int` | how far back an operator change is searched |
| `regime_bid_step` | `0.25` | `float` | day-over-day step in the spend-weighted bid that counts as a change |
| `regime_post_days_max` | `2` | `int` | at most this many post-change days -> numbers describe the prior regime |
| `regime_rerun_days` | `3` | `int` | advice: re-run after this many post-change days |
| `regime_fields` | `('bid', 'daily_budget', 'budget', 'status')` | `tuple[str, ...]` | change-event fields that reset a regime |
| `regime_bid_fields` | `('bid',)` | `tuple[str, ...]` | fields for which automated changes are ignored |

## ConflictConfig

`cpm_lineage.conflicts.ConflictConfig` · defined in `src/cpm_lineage/conflicts.py`

Orderings used by the two resolvers.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `grain_order` | `DEFAULT_GRAIN_ORDER` | `tuple[str, ...]` | coarsest -> finest; the finest signal wins |
| `grain_aliases` | `(('category', 'segment'), ('sku', 'product'), ('city', 'geo'), ('campaign', 'entity'), ('targeting_unit', 'keyword'), ('account', 'portfolio'))` | `tuple[tuple[str, str], ...]` | common synonyms folded onto the order above |
| `overrides` | `DEFAULT_OVERRIDES` | `tuple[Override, ...]` | checked in order, before granularity |
| `cut_verbs` | `('stop', 'cap', 'pause', 'cut', 'negative', 'reduce', 'tighten')` | `tuple[str, ...]` | substrings marking a cut |
| `source_authority` | `('operational', 'analytics', 'warehouse')` | `tuple[str, ...]` | most authoritative first; real-time beats lagged |
| `unknown_source_rank` | `9` | `int` | unknown sources rank below every named one |

## CpmConfig

`cpm_lineage.cpm.config.CpmConfig` · defined in `src/cpm_lineage/cpm/config.py`

Named thresholds for CPM views, the CPM-move classifier, restatement detection, the ownership guard, the bid-ceiling audit, auction intelligence and the keyword overpay check.

| Field | Default | Type | Meaning |
|---|---|---|---|
| **views** | | | |
| `top_n` | `4` | `int` | size of the tracked top-keyword set (by spend; the rest are rolled into one row) |
| `window_top_min` | `1` | `int` | lower clamp of the window-compare top-n (input guard) |
| `window_top_max` | `50` | `int` | upper clamp of the window-compare top-n (input guard) |
| `track_days` | `7` | `int` | the tracker shows at least a week of columns, then grows |
| `baseline_days` | `7` | `int` | baseline = the 7 days immediately before the tracker start |
| `rivals_top` | `6` | `int` | rival table keeps the six largest brands per keyword and period |
| `min_category_spend` | `25000.0` | `float` | MONEY: month-to-date spend below which a category folds into the remainder row (keeps the matrix readable) |
| `other_category_label` | `'Other keywords'` | `str` | label of the remainder row (unmapped and folded keywords) |
| `keyword_targeting_type` | `'keyword'` | `str` | normalised targeting_type value that marks keyword-targeted rows |
| `live_state_days` | `3` | `int` | position-bidder state rows older than this are not "live" |
| **ownership guard** | | | |
| `ownership_lookback_days` | `270` | `int` | majority-of-days owner is decided on the trailing 270 days |
| **CPM-move classifier** | | | |
| `explain_min_rise` | `0.05` | `float` | only CPM rises strictly above 5% are explained |
| `explain_position_step` | `0.3` | `float` | position moved by at least 0.3 slots = "bought position" / "worse spot" (judgment) |
| `explain_sov_gain` | `0.05` | `float` | own ad share up by 5 points = bought more share |
| `explain_rival_gain` | `0.03` | `float` | a rival's ad share up by 3 points = rival pressure |
| `explain_roas_fell` | `0.8` | `float` | current ROAS below 80% of reference = "(returns fell)" |
| **restatement** | | | |
| `restate_tol` | `0.01` | `float` | a day is restated when it moved by more than 1% ... |
| `restate_floor` | `1.0` | `float` | ... of max(\|first reading\|, 1 unit) |
| **position-bidder audit** | | | |
| `rank_visible_min` | `1.0` | `float` | the bidder searches the first 60 slots ... |
| `rank_visible_max` | `60.0` | `float` | ... anything outside is "position not found" (bidding blind) |
| `guardrail_bands` | `(50.0, 100.0, 200.0, 300.0)` | `tuple[float, ...]` | upper bounds (% over base) of the permissiveness bands; above the last is the open band |
| `hot_cap` | `0.3` | `float` | JUDGMENT: share of decisions pinned at the ceiling that makes a rule set "hot" |
| `low_hit` | `0.5` | `float` | JUDGMENT: target-hit rate below which a ceiling-pinned rule set is "hot" |
| `blind_seen` | `0.6` | `float` | JUDGMENT: position-seen rate below which the bidder is "blind" (outage threshold) |
| `percentage_ignores_max_bid` | `True` | `bool` | in percentage mode the rule's max bid is NOT a cap; False = ceiling is the lower of the two |
| **auction intelligence** | | | |
| `clearing_days` | `30` | `int` | decision lookback for clearing bids, competitor stack and timing |
| `clearing_target_min` | `1.0` | `float` | only near-top targets describe a slot price ... |
| `clearing_target_max` | `5.0` | `float` | ... targets 1..5 |
| `clearing_slot_split` | `2.0` | `float` | target <= 2 is the top slot group, deeper targets the second group |
| `min_obs` | `5` | `int` | groups with fewer than 5 observations are noise (clearing, competitors, organic trend) |
| `timing_min_obs` | `10` | `int` | a daypart needs 10 decisions before its average bid is quoted |
| `timing_min_parts` | `2` | `int` | cheap/dear needs at least two dayparts to compare |
| `dayparts` | `DEFAULT_DAYPARTS` | `tuple[tuple[str, int, int], ...]` | local-hour buckets for time-of-day pressure |
| `threat_ceiling_pct` | `20.0` | `float` | a rival at its ceiling on more than 20% of decisions is ceiling-constrained |
| `threat_top_slot` | `2.0` | `float` | rival mean target <= 2 = chasing the top slot |
| `threat_deep_slot` | `3.0` | `float` | rival mean target >= 3 = competing for deeper slots |
| `organic_days` | `42` | `int` | six weekly buckets for the organic trend |
| `organic_min_weeks` | `2` | `int` | a trend needs two weeks |
| `suggested_days` | `30` | `int` | suggested-bid window, anchored to that feed's own latest date |
| `suggested_max_keywords` | `100` | `int` | at most 100 keywords per suggested-bid lookup |
| `suggested_geo_min_rows` | `3` | `int` | a geo needs three captures before its suggestion is quoted |
| `suggested_geo_top` | `12` | `int` | geos shown in the per-geo suggestion drill |
| `intel_min_spend` | `1000.0` | `float` | MONEY: keywords at or below this window spend get no auction hint |
| `intel_top_keywords` | `8` | `int` | auction hints for an entity's top 8 keywords by spend |
| `bid_round_digits` | `0` | `int` | decimals kept on reported bid statistics (whole units by default; raise for small-unit currencies) |
| **keyword overpay check** | | | |
| `flat_min_decisions` | `10` | `int` | JUDGMENT: FLAT needs at least 10 decisions in the day |
| `flat_pct` | `0.02` | `float` | JUDGMENT: (max bid - min bid) / median bid below 2% = bidder is not moving |
| `overshoot_share` | `0.5` | `float` | JUDGMENT: better-than-target on at least half the decisions = overshoot |
| `self_floor_pct` | `0.8` | `float` | JUDGMENT: our own next campaign's bid at >= 80% of this row's CPM = we set our own price |
| `overpay_ratio` | `1.25` | `float` | JUDGMENT: CPM strictly above 1.25x the band median = overpay |
| `overpay_bands` | `(1.0, 3.0, 6.0, 10.0, 100.0)` | `tuple[float, ...]` | upper bounds of the position bands 1 \| 2-3 \| 4-6 \| 7-10 \| 11+ |
| `overpay_ref_min_rows` | `3` | `int` | a band median needs three rows (noise control) |
| `overpay_top_geos` | `10` | `int` | geos displayed, by spend on the checked keywords |
| `unknown_geo_label` | `'All/unknown'` | `str` | geo label when neither the row nor the campaign name yields one |

## DecomposeConfig

`cpm_lineage.decompose.DecomposeConfig` · defined in `src/cpm_lineage/decompose.py`

Thresholds of the decomposition layer (every field has a default).

| Field | Default | Type | Meaning |
|---|---|---|---|
| `pareto_top_n` | `10` | `int` | rows returned by the drag ranking ("worst 10, or all if fewer") |
| `pareto_cum_pct` | `80.0` | `float` | the "vital few" cut: classic 80/20 |
| `composition_min_spend` | `1000.0` | `float` | reference money: an entity "exists" in a period only above this spend |
| `composition_top_n` | `6` | `int` | entrants / exits / movers listed |
| `asp_min_move_pct` | `10.0` | `float` | the ASP split is only worth reading when window ASP moved this much |
| `asp_min_abs_delta` | `1.0` | `float` | reference money: and when \|dASP\| exceeds one currency unit |
| `asp_mover_min_pct` | `5.0` | `float` | products contributing at least this % of dASP are listed as movers |
| `growth_entrant_prior_share_max` | `0.05` | `float` | share points: effectively absent in the prior period |
| `growth_entrant_share_min` | `0.3` | `float` | share points: but visible now, so it is an entrant, not a gainer |
| `growth_mix_shift_min_pp` | `0.5` | `float` | tier mix shift is reported only when the largest shift reaches this |
| `growth_min_tiers` | `2` | `int` | a mix shift needs at least two tiers |
| `growth_entrant_names` | `3` | `int` | entrant names listed |
| `growth_ramping_label` | `'ramping'` | `str` | launch trend label counted as still ramping |

## DrillConfig

`cpm_lineage.drill.DrillConfig` · defined in `src/cpm_lineage/drill.py`

Named thresholds with their defaults.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `days` | `30` | `int` | current and baseline bucket length (matches the diagnosis windows) |
| `keyword_top_n` | `15` | `int` | keyword rows: losers plus gainers |
| `keyword_gainers` | `5` | `int` | winners are half the story |
| `product_top_n` | `12` | `int` | product rows |
| `product_gainers` | `4` | `int` | of the product rows, this many are gainers; the rest are losers |
| `auction_top_n` | `8` | `int` | keywords sent to the auction provider |
| `auction_min_spend_ref` | `1000.0` | `float` | reference money: keywords below this spend are not worth an auction read |
| `position_max` | `500.0` | `float` | positions outside 1..500 are discarded |

## GeoConfig

`cpm_lineage.geo.config.GeoConfig` · defined in `src/cpm_lineage/geo/config.py`

Named thresholds with their defaults.

| Field | Default | Type | Meaning |
|---|---|---|---|
| **quadrants** | | | |
| `window_days` | `30` | `int` | current vs prior window when no WindowPair is passed |
| `min_segment_sales_ref` | `1.0` | `float` | reference money: geos with less segment sales are dropped |
| `exit_segment_growth_pct` | `-5.0` | `float` | under-invested geo in a segment shrinking faster than this: exit |
| `availability_weight_floor` | `1.0` | `float` | velocity weight floor for active products (source: at least 1) |
| `standing_min_checks` | `30.0` | `float` | a brand needs this many shelf checks to be ranked on availability |
| `standing_min_brands` | `3` | `int` | a quantile on fewer brands is noise |
| `tier_min_geos_for_quartile` | `4` | `int` | with fewer geos the ROAS quartile collapses to the median |
| **posture** | | | |
| `posture_defend_q1_pct` | `60.0` | `float` | Defending: > 60 % of own sales in leader geos ... |
| `posture_defend_gaining_pct` | `50.0` | `float` | ... and > 50 % of geos gaining share |
| `posture_gaining_pct` | `75.0` | `float` | Gaining: >= 75 % of geos gaining share |
| `posture_build_q4_pct` | `40.0` | `float` | Building: > 40 % of own sales in under-invested geos ... |
| `posture_build_growth_pct` | `15.0` | `float` | ... in a segment growing > 15 % ... |
| `posture_build_gaining_pct` | `50.0` | `float` | ... with > 50 % of geos gaining |
| `posture_stall_pct` | `50.0` | `float` | Stalling: leader + ad-dependent > 50 % of sales ... |
| `posture_stall_gaining_pct` | `50.0` | `float` | ... and fewer than 50 % of geos gaining (its own knob) |
| `posture_pressure_pct` | `50.0` | `float` | Under Pressure: organic + under-invested > 50 % of sales ... |
| `posture_pressure_gaining_pct` | `25.0` | `float` | ... or fewer than 25 % of geos gaining |
| `rank_leader_pct` | `25.0` | `float` | rank label from national share (approximate, not a true rank) |
| `rank_contender_pct` | `10.0` | `float` | national share at or above this (and not a leader) reads as Contender |
| `rank_mid_pct` | `3.0` | `float` | national share at or above this reads as Mid-Tier; below it the smallest label |
| **geo drilldown** | | | |
| `drill_top_n` | `10` | `int` | geos listed by impact |
| `tier_scale_min_availability` | `0.8` | `float` | SCALE needs healthy supply (the FIX-FIRST line is AvailabilityPolicy.block: supply precedes demand) |
| **money at stake** | | | |
| `supply_bleed_availability` | `0.7` | `float` | share loss with availability below this is supply-driven |
| `run_rate_multiplier` | `2.0` | `float` | capturable = current gain x 2 (a labelled model, not a forecast) |
| `segment_growth_capture_pct` | `10.0` | `float` | segment growing faster than this: growth worth capturing |
| **product x geo map** | | | |
| `map_top_geos` | `6` | `int` | geos by own sales |
| `map_top_products` | `8` | `int` | own products within them |
| `map_priority_rank` | `3` | `int` | a product is a priority in a geo when in its top 3 by sales |
| `map_invest_roas_be_mult` | `1.0` | `float` | weak-organic priority cell: invest at or above breakeven, else fix first |
| `map_position_max` | `500.0` | `float` | organic positions outside 1..500 are discarded |

## IdentityConfig

`cpm_lineage.identity.IdentityConfig` · defined in `src/cpm_lineage/identity.py`

Thresholds of the identity layer (every field has a default).

| Field | Default | Type | Meaning |
|---|---|---|---|
| `log_floor` | `1e-09` | `float` | channel values are floored here before ln, so ln(0) never occurs |
| `min_dln` | `0.0001` | `float` | below this \|d ln ROAS\| the share denominator falls back (model total, then this) |
| `share_digits` | `None` | `int \| None` | optional rounding of share-of-move percentages (None = unrounded) |
| `verdict_bid_flat_pct` | `7.0` | `float` | \|bid change\| under this % counts as "bid flat" in the layer-1 verdict |
| `verdict_min_spend` | `500.0` | `float` | reference money: comparison-window spend needed before a verdict is given |
| `spend_cut_note_pct` | `-15.0` | `float` | spend change at or below this % adds the "separate decisions" note |
| `quartile_min_days` | `4` | `int` | the good-days/bad-days bucket needs at least one day per quartile |

## KeywordConfig

`cpm_lineage.keywords.config.KeywordConfig` · defined in `src/cpm_lineage/keywords/config.py`

Named thresholds with their defaults.

| Field | Default | Type | Meaning |
|---|---|---|---|
| **role classifier** | | | |
| `stop_words` | `DEFAULT_STOP_WORDS` | `frozenset[str]` | tokens that never identify a brand or a category |
| `brand_token_min_len` | `3` | `int` | shorter tokens are too ambiguous to identify a brand |
| `despaced_min_len` | `5` | `int` | substring match needs this length: stops short names matching inside longer words |
| `abbrev_brand_min_len` | `5` | `int` | abbreviation rule: the brand token must be at least this long |
| `abbrev_keyword_min_len` | `4` | `int` | ... and the keyword token that prefixes it at least this long |
| `catvocab_min_titles` | `2` | `int` | a token is category vocabulary when it appears in this many own titles |
| `universe_lookback_days` | `400` | `int` | keyword universe: long panels still find old keywords |
| `volume_window_days` | `30` | `int` | search-volume proxy window for the keyword dimension |
| `lexicon_version` | `'1'` | `str` | bump when classifier logic changes so cached roles are regenerated |
| **role cohorts** | | | |
| `cohort_window_days` | `60` | `int` | cohort percentiles are read over the last 60 days |
| `cohort_spend_floor_ref` | `1000.0` | `float` | reference money: a keyword needs this spend to count in its cohort |
| `cohort_min_n` | `12` | `int` | a percentile is trusted only with this many keywords (5 was noisy) |
| **CUT** | | | |
| `cut_roas_be_mult` | `1.0` | `float` | cut gate = this x breakeven: below break-even loses money outright |
| `cut_floor_ref` | `8000.0` | `float` | reference money: absolute materiality floor |
| `cut_floor_share` | `0.03` | `float` | ... or 3 % of scope spend, whichever is larger (scope-relative) |
| `spare_min_new_users` | `3.0` | `float` | acquisition spare needs at least this many new users |
| `spare_cpnu_mult` | `1.5` | `float` | ... at a cost per new user within 1.5 x the role median |
| `cut_top_n` | `12` | `int` | rows returned |
| **ADD** | | | |
| `add_min_exp_roas_be_mult` | `1.0` | `float` | never add a role whose cohort median loses money (x breakeven) |
| `explorer_min_relevance` | `0.5` | `float` | below this the platform itself calls the match weak |
| `unknown_relevance` | `0.45` | `float` | neutral relevance when no feed: presence already implies some |
| `demand_volume_weight` | `0.6` | `float` | demand = 0.6 x market volume ... |
| `demand_presence_weight` | `0.4` | `float` | ... + 0.4 x own impressions |
| `demand_log_scale` | `6.0` | `float` | log10 scale: one million per window reads about 1.0 |
| `payoff_cap` | `5.0` | `float` | one freak cohort cannot dominate the score |
| `payoff_unknown` | `1.0` | `float` | expected ROAS used when the cohort is unknown |
| `organic_bonus` | `1.25` | `float` | organic top slots prove conversion relevance |
| `organic_bonus_position` | `10.0` | `float` | ... when the organic position is at or better than this |
| `organic_position_max` | `500.0` | `float` | positions outside 1..500 are garbage and discarded |
| `add_pool_presence` | `300` | `int` | candidate cap, proven-presence pool |
| `add_pool_relevance` | `250` | `int` | candidate cap, platform-relevance pool |
| `fragment_min_len` | `3` | `int` | a one-word term shorter than this is always a typed fragment |
| `add_top_n` | `12` | `int` | rows returned |
| **cross-campaign leaks and re-slot** | | | |
| `leak_min_spend_ref` | `50000.0` | `float` | reference money: materiality for leaks, re-slot and untracked |
| `leak_roas_be_mult` | `1.0` | `float` | a leak is material spend below breakeven |
| `leaks_top_n` | `8` | `int` | rows returned |
| `reslot_min_position` | `6.0` | `float` | a "deep" slot |
| `reslot_cpm_quantile` | `0.75` | `float` | ... bought at a top-quartile CPM |
| `reslot_min_keywords` | `4` | `int` | quantile needs this many material keywords |
| `reslot_top_n` | `5` | `int` | rows returned |
| `untracked_top_n` | `4` | `int` | rows returned |
| `position_sanity_max` | `500.0` | `float` | weighted positions use 1..500 only |
| **owned niches** | | | |
| `niche_sov_floor_pct` | `40.0` | `float` | own ad share of voice needed to call a generic term "owned" |
| `niche_min_ad_impressions` | `5000.0` | `float` | material denominator: 100 % of a tiny term is "sole bidder" |
| `niche_probe_spend_ref` | `500.0` | `float` | reference money: below this the term is only a probe |
| `niche_scale_roas_be_mult` | `1.5` | `float` | scale class at 1.5 x breakeven |
| `niche_fix_roas_be_mult` | `1.0` | `float` | fix-conversion class below breakeven |
| `niche_top_position` | `1.5` | `float` | at the top slot bids cannot buy a better one |
| `niche_exhaust_window_days` | `7` | `int` | budget-exhausted days counted over the last 7 days |

## MarketConfig

`cpm_lineage.market.config.MarketConfig` · defined in `src/cpm_lineage/market/config.py`

Named thresholds with their defaults. Shares are percent.

| Field | Default | Type | Meaning |
|---|---|---|---|
| **market stats** | | | |
| `top_segments` | `4` | `int` | segments listed, by own sales |
| `ms_sov_block` | `0.5` | `float` | share / voice below this: over-investing, block scale |
| `ms_sov_over` | `0.7` | `float` | below this: over-investing in ads, attach the barrier chain |
| `ms_sov_mild` | `1.0` | `float` | below this: mild under-conversion |
| `ms_sov_healthy_max` | `1.5` | `float` | up to this: healthy; above: may be under-investing |
| **saturation screen** | | | |
| `saturation_net_decline` | `0.95` | `float` | both legs down AND newest < 0.95 x oldest (one-leg 2 % moves are noise) |
| `saturation_cost_rise` | `1.05` | `float` | ad cost share of sales up more than 5 % on the prior period |
| `saturation_cold_organic` | `0.2` | `float` | organic share below this: flywheel cold, cutting is self-harm |
| `saturation_confirmed_organic` | `0.5` | `float` | organic share above this: saturation confirmed |
| `saturation_test_cut` | `'10-15%'` | `str` | size of the reduction test suggested in the middle case |
| `saturation_test_weeks` | `2` | `int` | re-measure horizon of that test |
| **flywheel** | | | |
| `flywheel_cold` | `0.2` | `float` | organic share below: Cold |
| `flywheel_warming` | `0.5` | `float` | below: Warming |
| `flywheel_active` | `0.7` | `float` | below: Active; at or above: Self-Sustaining |
| `flywheel_material_share` | `0.9` | `float` | mixed-state read ignores the long tail beyond 90 % of impressions |
| `flywheel_top_n` | `10` | `int` | product rows returned |
| **share of voice** | | | |
| `rivals_top` | `6` | `int` | brands kept per keyword and period |
| **competition table** | | | |
| `competition_top_n` | `8` | `int` | rivals listed besides the own row |
| `advertisers_top_n` | `5` | `int` | top advertisers by ad share of voice |
| **price tiers** | | | |
| `tier_min_products` | `6` | `int` | terciles on fewer priced products are not tiers |
| `tier_trend_pp` | `0.3` | `float` | share move beyond +-0.3 pp is rising / falling |
| `tier_owned_pct` | `85.0` | `float` | band cut-offs: five honest bands beat a fake rank |
| `tier_led_pct` | `55.0` | `float` | own share of the tier at or above this: led |
| `tier_contested_pct` | `25.0` | `float` | at or above this: contested |
| `tier_minority_pct` | `1.0` | `float` | at or above this: minority; below it the brand is absent from the tier |
| `whitespace_max_own_share_pct` | `0.5` | `float` | own share of a tier pool below this is a listing gap |
| `whitespace_top_geos` | `8` | `int` | geos scanned |
| `whitespace_top_n` | `6` | `int` | gaps returned |
| `trend_min_month_volume` | `0.2` | `float` | drop months under 20 % of the mean monthly volume |
| `trend_min_months` | `3` | `int` | a trend needs three usable months |
| **pricing power** | | | |
| `elasticity_min_points` | `6` | `int` | OLS on fewer points is noise |
| `elasticity_weeks` | `12` | `int` | weekly points read |
| **break detection** | | | |
| `break_min_points` | `14` | `int` | daily points needed |
| `break_min_others` | `5` | `int` | other day-over-day deltas needed for the yardstick |
| `break_sigma` | `3.0` | `float` | the break must exceed 3 sd of the other deltas |
| `break_min_pp` | `0.5` | `float` | ... and half a share point: never invent an event |
| `mover_availability_pp` | `2.0` | `float` | same-day mover: own availability change, points |
| `mover_ad_sov_pp` | `2.0` | `float` | same-day mover: own ad share of voice change, points |
| `mover_rival_ad_pct` | `25.0` | `float` | same-day mover: rival ad impressions change, percent |
| **launches** | | | |
| `launch_window_days` | `90` | `int` | first seen within this many days |
| `launch_history_gate_days` | `30` | `int` | and more than this after the data history starts (honesty gate) |
| `launch_min_sales_ref` | `25000.0` | `float` | reference money: current-window sales to matter |
| `launch_ramping` | `1.3` | `float` | current rate >= 1.3 x lifetime rate: ramping |
| `launch_fading` | `0.7` | `float` | current rate < 0.7 x lifetime rate: fading |
| **seasonality / small reads** | | | |
| `seasonality_min_months` | `6` | `int` | one observed cycle needs six months |
| `rival_slot_min_ad_impressions` | `1000.0` | `float` | a top rival needs this many ad impressions |
| `geo_role_top_geos` | `8` | `int` | geos listed in the geo x keyword-type read |
| `entrant_prior_share_pct` | `0.05` | `float` | (kept for callers building growth-decomposition inputs) |

## OutcomeConfig

`cpm_lineage.outcomes.OutcomeConfig` · defined in `src/cpm_lineage/outcomes.py`

Thresholds for outcome labels, pairing and the difference-in-differences read.

| Field | Default | Type | Meaning |
|---|---|---|---|
| **tide** | | | |
| `damp_min` | `0.5` | `float` | floor of the tide multiplier |
| `damp_max` | `2.0` | `float` | ceiling of the tide multiplier |
| **outcome labels** | | | |
| `too_early_days` | `3` | `int` | no label before day 3 |
| `flat_band` | `0.15` | `float` | watch / freeze: within 15% of expectation confirms "flat" |
| `cut_hold` | `0.95` | `float` | cut is on track when efficiency holds >= 95% of expectation |
| `revenue_retention` | `0.6` | `float` | ... and at least 60% of revenue is kept (no celebrating a collapse) |
| `raise_hold` | `0.85` | `float` | raise is on track at >= 85% of expectation (scale dilutes a little) |
| `cf_cut_ratio` | `0.95` | `float` | counterfactual cut was right when efficiency fell below 95% of expectation |
| **early stop-loss (display only)** | | | |
| `early_from_day` | `3` | `int` | early call window opens ... |
| `early_to_day` | `7` | `int` | ... and closes at the first horizon |
| `early_stop_ratio` | `0.5` | `float` | under half of expectation: losers stop early, winners run the window |
| **pairing** | | | |
| `pairing_cap` | `20` | `int` | realised and counterfactual rows paired per run, each |
| `edge_guard` | `'feed'` | `str` | "feed": the feed's latest date must reach the horizon end (an entity that stopped serving is scored); "entity": the entity's own data must |
| **difference-in-differences** | | | |
| `did_min_age_days` | `7` | `int` | younger entries are "too-early" |
| `did_max_window_days` | `14` | `int` | W = min(14, age) |
| `did_band` | `0.1` | `float` | +/- ROAS points separating landed / flat / worsened |
| `did_min_post_spend` | `1000.0` | `float` | money: below this the entity stopped or is negligible -> no-data |
| **taken-detection** | | | |
| `budget_fields` | `('daily_budget',)` | `tuple[str, ...]` | change-log fields that are a budget change |
| `bid_fields` | `('bid',)` | `tuple[str, ...]` | change-log fields that are a bid change |
| `status_fields` | `('status',)` | `tuple[str, ...]` | change-log fields that are a status change |
| `keyword_cut_fields` | `('keyword_removed', 'negative_keyword_added')` | `tuple[str, ...]` | change-log fields that record a keyword cut |
| `keyword_add_fields` | `('keyword_added',)` | `tuple[str, ...]` | change-log fields that record a keyword addition |
| `taken_max_lines` | `4` | `int` | evidence lines kept per entry |
| `taken_budget_epsilon` | `1.0` | `float` | money: budget differences at or below this are not a change |
| `taken_share_min` | `0.01` | `float` | target keywords must hold > 1% of spend before the action to infer anything |
| `taken_cut_ratio` | `0.5` | `float` | spend share fell below half: the cut was made |
| `taken_scale_ratio` | `1.2` | `float` | spend share rose above 1.2x: the scale was made |

## PrescribeConfig

`cpm_lineage.prescribe.PrescribeConfig` · defined in `src/cpm_lineage/prescribe.py`

Thresholds of the prescription gate stack. ROAS bars are breakeven multiples.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `halo_direct_max_be` | `2.0` | `float` | halo gate: direct ROAS at or below the scale bar ... |
| `halo_total_min_be` | `2.0` | `float` | ... total (direct + indirect) ROAS at or above it ... |
| `halo_total_to_direct` | `1.5` | `float` | ... and total at least 1.5 x direct |
| `halo_direct_floor` | `0.01` | `float` | floor on direct ROAS in the ratio test (avoids a zero divisor) |
| `mixed_role_share` | `0.65` | `float` | dominant role under this spend share: blended ROAS cannot judge the campaign |
| `demand_min_searches` | `50.0` | `float` | max searches per day below this: budget cannot create searches |
| `pause_roas` | `0.0` | `float` | at or below: pause (zero or negative return) |
| `min_trim_pct` | `1` | `int` | a trim smaller than this many percent is reported as a hold, not as an action |
| `marginal_ceiling_be` | `1.0` | `float` | marginal ROAS below this at the cap: at the curve ceiling |
| `marginal_full_step_be` | `1.5` | `float` | marginal ROAS at or above: full step; uncapped winner may scale |
| `held_days_min` | `3` | `int` | platform-held on this many days: a top-up, not a free scale |
| `availability_drop` | `0.05` | `float` | a 5-point availability drop qualifies every scale |
| `budget_round_ref` | `100.0` | `float` | trim targets are rounded to this money step |
| `cut_tokens` | `('cut', 'negative', 'pause')` | `tuple[str, ...]` | top-action verbs that are surgical cuts |
| `launch_tokens` | `('launch',)` | `tuple[str, ...]` | top-action verbs that launch a replacement |
| `passthrough_tokens` | `('investigate tracking', 'investigate stop', 'hold the budget raise', 'audit before spending', 'pre-fund', 'hold lever changes')` | `tuple[str, ...]` | engine verdicts passed through verbatim |
| `passthrough_any_action` | `False` | `bool` | False: only the leading action is the engine verdict |
| `availability_tokens` | `('availability', 'supply')` | `tuple[str, ...]` | engine verbs that blame availability |
| `burn_tokens` | `('burn',)` | `tuple[str, ...]` | engine headlines flagging an auto-targeted burn |
| `own_brand_role` | `'own_brand'` | `str` | role value that marks an own-brand term |
| `conquest_role` | `'competitor'` | `str` | role value that marks a competitor-conquest term |
| `brand_name_tokens` | `('brand', 'branded')` | `tuple[str, ...]` | name-token fallback when no keyword data exists |
| `conquest_name_tokens` | `('comp', 'conq', 'conquest', 'competitor', 'competition', 'competitive')` | `tuple[str, ...]` | name tokens that mark a conquest campaign when no keyword data exists |
| `name_delimiters` | `'[\\s_\\-\|/:.,]+'` | `str` | how an entity name is split into tokens |
| `removed_statuses` | `(CanonicalStatus.REMOVED.value,)` | `tuple[str, ...]` | -> launch a replacement |
| `stopped_statuses` | `(CanonicalStatus.ENDED.value, CanonicalStatus.OPERATOR_PAUSED.value)` | `tuple[str, ...]` | -> investigate the stop |

## DrillCallConfig

`cpm_lineage.prescribe.DrillCallConfig` · defined in `src/cpm_lineage/prescribe.py`

Thresholds of the one-word drill-row calls. ROAS bars are breakeven multiples.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `stopped_spend_ref` | `1.0` | `float` | current spend under this and baseline above it: already stopped |
| `cut_spend_ref` | `20000.0` | `float` | material spend for a cut call |
| `cut_roas_be` | `1.0` | `float` | below breakeven with material spend -> cut |
| `scale_roas_be` | `2.0` | `float` | at or above with a positive contribution -> scale |
| `contribution_band` | `0.02` | `float` | \|shift-share contribution\| beyond this is a real move |
| `step_ratio` | `1.1` | `float` | bid or position 10% worse than baseline |
| `availability_fix` | `0.7` | `float` | product availability under this: fix / gate |
| `availability_restock` | `0.6` | `float` | geo availability under this: restock / gate |
| `availability_delisted` | `0.02` | `float` | stopped product at or under this: possibly delisted |
| `availability_gap` | `-0.05` | `float` | own minus rival availability under this: trailing rivals |

## QueueConfig

`cpm_lineage.queue.QueueConfig` · defined in `src/cpm_lineage/queue.py`

Ranking and filtering thresholds of the action queue.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `weight_high` | `1.0` | `float` | realised waste counts in full |
| `weight_med` | `0.6` | `float` | structural estimates |
| `weight_low` | `0.3` | `float` | hypothetical upside |
| `period_days` | `30` | `int` | daily amounts are scaled to this period |
| `scale_fallback_mult` | `1.5` | `float` | raise target when the curve gave none: 1.5 x the cap |
| `test_raise_pct` | `20.0` | `float` | unverified scale: raise as a small test ... |
| `test_recheck_days` | `7` | `int` | ... and re-check after this many days |
| `over_invest_ms_sov` | `0.5` | `float` | market share : share of voice under this = over-invested in ads |
| `over_invest_damper` | `0.5` | `float` | ... every scale row's weight is halved |
| `floor_ref` | `2000.0` | `float` | anti-dump floor per 30 days (reference money) ... |
| `floor_ref_days` | `30` | `int` | ... scaled by period_days / this |
| `floor_share_of_top` | `0.01` | `float` | ... or 1% of the largest row, whichever is larger |
| `max_rows` | `15` | `int` | list length; the tail is collapsed into a count and a sum |

## RcaConfig

`cpm_lineage.rca.config.RcaConfig` · defined in `src/cpm_lineage/rca/config.py`

Every RCA threshold as a named field with a default.

| Field | Default | Type | Meaning |
|---|---|---|---|
| **gate table (per-account overridable)** | | | |
| `segment_move_pct` | `5.0` | `float` | segment value moved this much: the market itself moved |
| `share_move_pp` | `0.1` | `float` | share change that counts as share changing hands |
| `promo_depth_pp` | `3.0` | `float` | discount-depth pullback beyond this fires the promo gate |
| `sov_drop_pp` | `1.0` | `float` | own ad share-of-voice fall worth explaining |
| `pullback_gap_pp` | `10.0` | `float` | own impression delta trailing the pool by more = genuine pullback |
| `availability_floor` | `0.85` | `float` | under this, ad money hits empty shelves |
| `availability_drop` | `0.03` | `float` | window-over-window fall that fires the supply gate |
| `mover_pp` | `0.5` | `float` | rival share move that makes the movers table |
| `sticker_mode_share` | `0.1` | `float` | 2+ list prices each above this share = mixed rollout |
| `launch_share_pct` | `1.0` | `float` | a launch at >= this % of segment value is a real entrant |
| `roas_guard` | `1.1` | `float` | x breakeven; direct ROAS below this triggers the efficiency lever |
| `spend_cut_pct` | `15.0` | `float` | first-party ad spend cut that is a budget decision |
| `step_pct` | `12.0` | `float` | run-rate level change that is a step, not a drift |
| `pocket_growth_pct` | `20.0` | `float` | keyword pool growth with ~no own voice = unworked pocket |
| `pocket_own_sov` | `2.0` | `float` | own ad SOV under this % = effectively absent |
| `vacuum_drop` | `0.6` | `float` | rival ad volume at <= this fraction of prior has vacated voice |
| `geo_collapse_pct` | `-20.0` | `float` | segment fell beyond this in a geo = no-spend zone |
| `passenger_availability_gap` | `0.1` | `float` | product this far under the floor (or zero sales) is a passenger |
| **windows and routing** | | | |
| `default_days` | `14` | `int` | default window length, anchored to the market feed's latest date |
| `allowed_days` | `(7, 14, 30, 60, 90)` | `tuple[int, ...]` | window lengths a routed question may ask for |
| `route_cache_cap` | `200` | `int` | routed questions memoised per cache |
| **step scan** | | | |
| `step_min_points` | `14` | `int` | daily points needed before a change-point is looked for |
| `step_min_segment` | `5` | `int` | both sides of a split need this many days |
| **fact extraction** | | | |
| `battleground_size` | `12` | `int` | generic keywords (by own ad impressions) that define the battleground |
| `keyword_list_cap` | `20` | `int` | generic keywords listed by current pool |
| `advertiser_list_cap` | `8` | `int` | advertisers listed on the battleground |
| `sticker_cluster_radius` | `15.0` | `float` | money; list prices this close are one price point |
| `sticker_max_points` | `3` | `int` | price points recorded per product |
| `launch_lookback_days` | `60` | `int` | first sale within this many days of feed max = launch |
| `other_lines_cap` | `6` | `int` | other own segments listed |
| `how_deep_discount_pct` | `30.0` | `float` | rival playbook: deep price |
| `how_high_availability` | `0.9` | `float` | rival playbook: "+ high availability" |
| `how_distribution_availability` | `0.92` | `float` | rival playbook: distribution-led |
| `how_low_discount_pct` | `10.0` | `float` | rival playbook: "at almost no discount" |
| `how_media_sov_pct` | `15.0` | `float` | rival playbook: media-led |
| `how_range_products` | `7` | `int` | rival playbook: range-led |
| **attribution literals** | | | |
| `pocket_min_pool_share` | `0.03` | `float` | a pocket keyword must hold this share of the listed pool |
| `vacuum_min_prior_sov_pct` | `5.0` | `float` | advertiser needed this prior SOV to leave a vacuum |
| `vacuum_exclude_own` | `True` | `bool` | the own brand cannot vacate voice to itself |
| `mix_shift_loss_fraction` | `0.5` | `float` | another own line growing by this share of the loss = mix shift |
| `geo_beat_pp` | `2.0` | `float` | own beats / trails the segment in a geo by more than this |
| `geo_trail_far_pp` | `5.0` | `float` | "trailed even that" wording in a collapsed geo |
| `geo_top_n` | `10` | `int` | geos (by own prior value) scanned for spend / avoid |
| `share_geo_top_n` | `8` | `int` | geos used for the "beat the segment in N of K" proof |
| `resilient_geo_count` | `3` | `int` | living geos kept as the shift destination |
| `conversion_steady_frac` | `0.1` | `float` | conversion within this of prior = steady |
| `organic_held_frac` | `0.9` | `float` | organic impressions at >= this of prior = held |
| `growth_multiple_pct` | `300.0` | `float` | growth above this is shown as a multiple |
| `sticker_checkpoint_share` | `0.95` | `float` | single modal list price target after a migration |
| **verification** | | | |
| `join_coverage_min_pct` | `99.0` | `float` | segment value that must map to a brand |
| `tolerance_value` | `1.0` | `float` | money; absolute tolerance on value sums |
| `tolerance_units` | `0.5` | `float` | absolute tolerance on the unit decomposition |
| `tolerance_rel` | `1e-09` | `float` | relative tolerance, whichever is larger applies |
| **playbook literals** | | | |
| `under_voiced_gap_pp` | `-1.0` | `float` | voice minus market share below this = under-invested |
| `over_voiced_gap_pp` | `3.0` | `float` | voice minus market share above this = over-voiced |
| `mix_ratio` | `1.3` | `float` | best / worst ad-type ROAS that makes the mix "not optimal" |
| `mix_worst_share` | `0.2` | `float` | ... while the worst type holds this share of spend |
| `mix_min_share` | `0.05` | `float` | ad types under this share of spend are not compared |
| `roas_fell_frac` | `0.95` | `float` | ROAS under this fraction of prior = it fell |
| `rival_ceding_pp` | `-0.3` | `float` | a rival losing more share than this is the exposed one |
| `loss_stable_frac` | `0.25` | `float` | availability loss within this of prior = unchanged |
| `not_driver_own_pct` | `-10.0` | `float` | ... while own value fell beyond this: availability did not drive it |
| `share_word_band_pp` | `0.05` | `float` | rose / fell / held wording band |
| `run_rate_tolerance_pct` | `10.0` | `float` | "spend back within this of the prior run-rate" |
| `vacuum_gain_pp` | `2.0` | `float` | checkpoint for contesting vacated voice |
| `voice_parity_weeks` | `4` | `int` | checkpoint horizon for buying voice up to share |
| **optional language layer and history** | | | |
| `free_year_min` | `2020` | `int` | calendar years the numeral guard always allows |
| `free_year_max` | `2034` | `int` | upper end of that calendar-year range |
| `narrative_max_length_ratio` | `2.0` | `float` | a rewritten block may not exceed this x the template |
| `context_max_words` | `120` | `int` | external-context caption cap |
| `history_depth` | `12` | `int` | snapshots kept per (advertiser, segment) |
| `diff_decimals` | `2` | `int` | an axis "moved" when it differs at this precision |

## RecommendConfig

`cpm_lineage.recommend.RecommendConfig` · defined in `src/cpm_lineage/recommend.py`

Thresholds of the recommendation gate stack (every field has a default).

| Field | Default | Type | Meaning |
|---|---|---|---|
| `max_actions` | `3` | `int` | an operator acts on a short list; more is noise |
| **materiality floors (relative to the entity's own targeting-unit spend)** | | | |
| `bleed_floor_ref` | `10000.0` | `float` | absolute floor for a bleeder candidate |
| `bleed_floor_share` | `0.05` | `float` | ... or 5% of unit spend, whichever is larger |
| `pause_floor_ref` | `8000.0` | `float` | absolute floor for the pause list |
| `pause_floor_share` | `0.03` | `float` | ... or 3% of unit spend |
| `cap_floor_ref` | `5000.0` | `float` | absolute floor for a bid-cap target |
| `cap_floor_share` | `0.02` | `float` | ... or 2% of unit spend |
| `cohort_spend_floor_ref` | `1000.0` | `float` | a unit needs this much spend to count in its role cohort |
| `cohort_min_n` | `12` | `int` | percentile trusted only with this many units (5 was noisy) |
| **ROAS bars (multiples of effective breakeven)** | | | |
| `bleed_roas_be` | `1.0` | `float` | below breakeven a unit destroys value |
| `pause_roas_be` | `1.5` | `float` | pause-list bar: thin margin, material spend |
| `cap_roas_be` | `1.8` | `float` | bid-cap target bar |
| `boost_roas_be` | `2.0` | `float` | a unit must clear this before visibility is bought on it |
| `budget_raise_roas_be` | `2.0` | `float` | capped entity: raise at or above, hold between breakeven and this |
| `winner_roas_be` | `1.5` | `float` | scale candidate floor |
| `halo_roas_be` | `3.0` | `float` | scale on halo: total ROAS at least this ... |
| `halo_total_to_direct` | `1.5` | `float` | ... and at least 1.5 x direct ROAS |
| `thin_halo_ratio` | `1.3` | `float` | auto-targeted burn: total under 1.3 x direct = thin halo |
| `prefund_min_roas_be` | `1.0` | `float` | never pre-fund a sub-breakeven entity |
| **targeting-unit quality gates** | | | |
| `min_search_volume` | `50.0` | `float` | searches per day below which a unit is too small to scale or boost |
| `min_relevance` | `0.3` | `float` | relevance score below which a unit is not boosted |
| `max_winner_organic_rank` | `6.0` | `float` | organic rank at or better than this already wins the shelf: do not pay for it |
| `head_term_top_n` | `3` | `int` | generic head terms: top 3 by search volume ... |
| `head_term_volume_share` | `0.1` | `float` | ... or 10% of generic search volume |
| `max_targets` | `3` | `int` | named targets per action |
| `cap_fallback_n` | `2` | `int` | bid-cap fallback: the two lowest-ROAS units |
| `max_stops` | `3` | `int` | stops per run |
| `max_staged_caps` | `3` | `int` | staged bid caps per run |
| `max_pause_list` | `6` | `int` | length of the pause list |
| `max_spared` | `4` | `int` | spared units reported |
| **availability** | | | |
| `availability_move` | `0.05` | `float` | a 5-point drop or recovery window over window |
| `availability_sparse_days` | `2` | `int` | 1-2 observed days: fail closed to caution (no resume) |
| **organic share of own impressions** | | | |
| `organic_cold` | `0.2` | `float` | below: own-brand terms still need paid support |
| `organic_self_sustaining` | `0.7` | `float` | at or above: paid on own demand is mostly non-incremental |
| **budget branch** | | | |
| `bleed_hold_pct` | `40.0` | `float` | structural bleed share that blocks a budget raise |
| `marginal_roas_block` | `0.9` | `float` | marginal ROAS at the cap below this is the hard block (wording) |
| `marginal_roas_ceiling` | `1.0` | `float` | a raise needs at least this marginal ROAS: below it incremental revenue < incremental spend |
| `curve_min_r2` | `0.3` | `float` | the saturation curve is trusted only at or above this fit |
| `severe_position_split` | `3.0` | `float` | severe under-utilisation: position worse than this -> bids, else units |
| **auto-targeted burn** | | | |
| `burn_spend_up` | `0.05` | `float` | spend up more than 5% ... |
| `burn_roas_down` | `0.2` | `float` | ... while ROAS fell at least 20% |
| **bleeder loop** | | | |
| `halo_share` | `0.2` | `float` | indirect share above this spares or caps instead of stopping |
| `acquisition_watch_mult` | `2.0` | `float` | cost per new user up to 2 x cohort p25: spare, watch |
| **pre-fund** | | | |
| `demand_trend_min` | `0.25` | `float` | demand up 25% or more |
| `new_product_days` | `90` | `int` | products younger than this are exempt from the cannibalisation gate |
| **scores of the candidate pool** | | | |
| `score_tighten_account` | `80.0` | `float` | stop a starved loser first |
| `score_ladder_severe` | `78.0` | `float` | an entity spending under 20% of its cap |
| `score_raise_account` | `72.0` | `float` | fund a starved winner |
| `score_scale_winners` | `70.0` | `float` | scale units that already win |
| `score_prefund_capped` | `64.0` | `float` | demand spike on a capped entity |
| `score_ladder` | `56.0` | `float` | other utilisation-ladder candidates |
| `score_lever_base` | `50.0` | `float` | hurting tier-1 lever: 50 + 0.4 x min(\|pct\|, 40) -> 50..66 |
| `score_lever_slope` | `0.4` | `float` | ... slope |
| `score_lever_pct_cap` | `40.0` | `float` | ... cap on \|pct\| |
| `score_prefund` | `48.0` | `float` | demand spike, not capped |
| `score_resume` | `42.0` | `float` | resume scaling after availability recovered |
| `score_protect` | `18.0` | `float` | helping tier-1 lever: protect / maintain |
| `account_moves_n` | `2` | `int` | account-level budget moves considered per direction |
| **post-filters** | | | |
| `exec_fail_rate` | `0.5` | `float` | more than half of recent automated operations failed |
| `exec_min_ops` | `10` | `int` | ... out of at least this many |
| `stale_days` | `3` | `int` | data older than this is stale |
| `wind_down_share` | `0.5` | `float` | spend at or below half the prior window = being wound down |
| `acquisition_rate` | `0.2` | `float` | new-user rate at or above: healthy acquisition |
| `recapture_rate` | `0.05` | `float` | new-user rate below: existing-user recapture |
| `soft_roas_band` | `0.1` | `float` | "softened" = within 10% below the prior ROAS |
| **annotators** | | | |
| `self_competition_share` | `0.3` | `float` | share of unit spend also targeted by sibling entities |
| `history_cut_roas_be` | `1.0` | `float` | history agrees with a cut below this |
| `history_grow_roas_be` | `2.0` | `float` | history agrees with growth at or above this |
| **structural bleed and priority** | | | |
| `bleed_worst_pool` | `15` | `int` | "worst" is drawn from the top 15 recent units by spend |
| `bleed_worst_n` | `3` | `int` | ... first three |
| `priority_bleed_pct` | `40.0` | `float` | p0 at this bleed share |
| `priority_drop` | `-0.25` | `float` | p1 at a 25% ROAS drop |

## ScopingConfig

`cpm_lineage.scoping.ScopingConfig` · defined in `src/cpm_lineage/scoping.py`

Named thresholds with their defaults.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `product_cap` | `15000` | `int` | product ids per segment scope; beyond it the scope is truncated and flagged |
| `purity_campaign_cap` | `5000` | `int` | campaigns purity-checked; the rest are excluded and flagged, never guessed |
| `stale_days` | `7` | `int` | hard stale-stopped window (spend in the last 7 days) |
| `soft_stale_days` | `3` | `int` | soft window (spend in the last 3 days) |
| `label_share_floor` | `0.02` | `float` | a label under 2 % of the top label's spend is mislabelled / legacy |
| `label_spend_days` | `15` | `int` | delivery evidence window for placement labels |
| `label_new_days` | `60` | `int` | a campaign started within this many days is "new" |

## ScorecardConfig

`cpm_lineage.scorecard.ScorecardConfig` · defined in `src/cpm_lineage/scorecard.py`

Grading thresholds (every field has a default).

| Field | Default | Type | Meaning |
|---|---|---|---|
| `min_age_days` | `3.0` | `float` | a baseline younger than this is not overwritten: re-runs must not wipe the week's yardstick |
| `too_soon_days` | `5.0` | `float` | time-dependent checkpoints cannot be MISSED against a baseline younger than this |
| `bleed_met_ratio` | `0.5` | `float` | waste halved = met |
| `spend_raised_ratio` | `1.05` | `float` | spend up at least 5% = the raise landed |
| `roas_hold_ratio` | `0.9` | `float` | ROAS held at >= 90% of baseline after the raise |
| `rival_gain_pp` | `0.5` | `float` | a watched rival gaining >= 0.5 pp of share = missed |
| `tier_met_pp` | `1.0` | `float` | own share of a price tier up >= 1.0 pp = met |
| `tier_pending_pp` | `0.25` | `float` | ... up >= 0.25 pp = still on track |
| `discount_hold_pp` | `1.5` | `float` | discount held within 1.5 pp of baseline |
| `time_dependent_kinds` | `('waste_bleed', 'tier_share')` | `tuple[str, ...]` | kinds covered by the too-soon guard |

## ScreenConfig

`cpm_lineage.screen.ScreenConfig` · defined in `src/cpm_lineage/screen.py`

Thresholds of the pre-screen.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `weeks` | `8` | `int` | default window: eight weekly points |
| `min_weeks` | `6` | `int` | fewer paired weeks than this: no correlation is reported |
| `strong_r` | `0.7` | `float` | \|r\| at or above -> STRONG |
| `moderate_r` | `0.4` | `float` | \|r\| at or above -> MODERATE, else WEAK |
| `rival_note_r` | `-0.5` | `float` | competitor activity at or below this r: competitive-response note |
| `min_segment_offtake` | `1.0` | `float` | weeks with segment sell-out under this are skipped (no share defined) |
| `promo_min_sd_pp` | `1.0` | `float` | a discount push must exceed the mean gap by at least 1 pp (or 1 sd) |
| `promo_baseline_weeks` | `3` | `int` | share baseline before, and retention after, the push |
| `round_dp` | `2` | `int` | correlation rounding |

## ChangeLogConfig

`cpm_lineage.store.changelog.ChangeLogConfig` · defined in `src/cpm_lineage/store/changelog.py`

Fields diffed and the before / after read around each change.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `fields` | `('status', 'daily_budget', 'bid')` | `tuple[str, ...]` | configuration inputs tracked |
| `status_field` | `'status'` | `str` | the field whose platform-set values are ignored |
| `platform_set_statuses` | `(CanonicalStatus.BUDGET_HELD.value, CanonicalStatus.PLATFORM_PAUSED.value)` | `tuple[str, ...]` | states the platform sets by itself: not an input change |
| `before_days` | `7` | `int` | read days [-7, 0) before the change |
| `after_start` | `1` | `int` | skip the change day itself ... |
| `after_days` | `7` | `int` | ... then read days [1, 8) |
| `material_change` | `0.1` | `float` | a non-status change must move the value by >= 10% to make the shortlist |
| `top_n` | `3` | `int` | shortlist size, one per entity |

## HistoryConfig

`cpm_lineage.store.history.HistoryConfig` · defined in `src/cpm_lineage/store/history.py`

Depth and freshness of the entity history.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `window_days` | `90` | `int` | trailing history depth; long enough to outlive campaign churn |
| `fresh_hours` | `24.0` | `float` | a cached document older than this is rebuilt |
| `availability_days` | `30` | `int` | product availability is read over the latest N days only |
| `default_role` | `'generic'` | `str` | role of a keyword missing from the targeting-unit dimension |
| `round_dp` | `2` | `int` | decimals kept on ratios |

## LedgerConfig

`cpm_lineage.store.ledger.LedgerConfig` · defined in `src/cpm_lineage/store/ledger.py`

Ledger horizons, windows and vocabularies.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `horizons` | `(7, 14, 30)` | `tuple[int, ...]` | days after apply at which an action is measured |
| `markable_statuses` | `('applied', 'skipped', 'pending')` | `tuple[str, ...]` | the only statuses an operator may set |
| `unappliable_statuses` | `('superseded', 'vetoed', 'merged')` | `tuple[str, ...]` | a loser must never be recorded as executed |
| `counterfactual_statuses` | `('superseded', 'tension', 'vetoed')` | `tuple[str, ...]` | not-taken cards worth scoring |
| `status_order` | `('pending', 'tension', 'applied', 'skipped', 'superseded', 'vetoed')` | `tuple[str, ...]` | load order |
| `win_rate_horizon` | `7` | `int` | only the first horizon trains confidence |
| `win_rate_days` | `90` | `int` | emitted within this many days |
| `cf_calibration_days` | `120` | `int` | counterfactual audit window |
| `cf_count_nosignal` | `True` | `bool` | True: unreadable counterfactuals stay in the denominator |
| `accept_rate_days` | `90` | `int` | plans created within this many days |
| `applied_lookback_plans` | `10` | `int` | plans scanned for applied actions |

## VerdictConfig

`cpm_lineage.verdict.VerdictConfig` · defined in `src/cpm_lineage/verdict.py`

Every threshold of the verdict spine. Money values are reference amounts scaled by ``Economics.money`` at use, so no currency is baked in.

| Field | Default | Type | Meaning |
|---|---|---|---|
| **integrity** | | | |
| `quarantine_min_cohort` | `8` | `int` | same-ad-type entities alive exactly one day that mark an ingestion leak |
| `quarantine_min_window_days` | `14` | `int` | one live day is normal in a short window, so only check long ones |
| `quarantine_live_days` | `1` | `int` | the "alive exactly N days" shape of a phantom cohort |
| `placeholder_budget` | `100.0` | `float` | money: a daily cap below this is a placeholder, not a decision |
| **lifecycle** | | | |
| `launch_grace_days` | `5` | `int` | CPM delivery starts on day one, so signal lands fast; no cuts before this |
| **evidence** | | | |
| `evidence_min_impressions` | `1000.0` | `float` | minimum window impressions to judge an entity at all |
| `evidence_exempt_families` | `('experiment',)` | `tuple[str, ...]` | a probe exists to buy significance |
| **identity / price event** | | | |
| `price_swing_pp` | `8.0` | `float` | segment discount swing (percentage points) that holds media verdicts |
| `price_hold_types` | `('budget', 'bid', 'keyword')` | `tuple[str, ...]` | levers held during a price event |
| `price_exempt_families` | `('defense',)` | `tuple[str, ...]` | defending the shelf is not a media-efficiency call |
| **action lock** | | | |
| `lock_days` | `7` | `int` | one open action per entity until the first measurement horizon |
| `bid_down_memory_days` | `14` | `int` | the bid ladder is not repeated inside this many days |
| **risk bars: confidence an ACT must clear, by reversibility class** | | | |
| `bar_restore` | `30` | `int` | restore / freeze: near-free to undo |
| `bar_watch` | `25` | `int` | a watch costs attention only |
| `bar_experiment` | `30` | `int` | bounded downside by construction (see ``risk_bar``) |
| `bar_bid` | `45` | `int` | reversible within a day |
| `bar_budget_up` | `55` | `int` | spends real money before it can be read |
| `bar_cut` | `65` | `int` | cuts lose position and history; hardest to undo |
| `bar_default` | `50` | `int` | anything unclassified |
| `cold_start_max_days` | `21` | `int` | a book younger than this has not derived its own parameters |
| `cold_start_bump` | `10` | `int` | added to every bar while the book is cold |
| `restore_title_markers` | `('Restore', 'Freeze')` | `tuple[str, ...]` | fallback when no explicit reversibility is set |
| **graduate: ladder, staircase, sweeps** | | | |
| `ladder_bid_step_pct` | `-25.0` | `float` | first response to a keyword cut: the cheapest reversible lever |
| `ladder_reread_days` | `5` | `int` | then re-read before the original call ships |
| `staircase_step_pct` | `25.0` | `float` | largest one-shot budget raise; blanket raises decay at the margin |
| `staircase_reread_days` | `7` | `int` | re-read marginal efficiency before the next step |
| `bleeder_book_share` | `0.02` | `float` | cumulative loss >= 2% of book spend: small daily bleeds never trip a run rate |
| `bleeder_confidence` | `55` | `int` | just above the bid bar; a sweep is a prior, not a measurement |
| `bleeder_conf_part` | `15.0` | `float` | labelled part behind that confidence |
| `freeze_ramp_mult` | `1.5` | `float` | last-7-day daily spend above 1.5x the window daily average is a ramp |
| `freeze_min_live_days` | `14` | `int` | need a stable window average to call a ramp |
| `freeze_recent_days` | `7` | `int` | length of the "recent" spend read |
| `freeze_confidence` | `60` | `int` | freezing is near-free; drift is not |
| `freeze_conf_part` | `20.0` | `float` | labelled part behind the freeze confidence |
| **extra detectors** | | | |
| `dark_roas_mult` | `3.0` | `float` | a "winner" earns at least 3x breakeven |
| `dark_min_live_days` | `7` | `int` | enough history to call it proven |
| `dark_min_spend` | `10000.0` | `float` | money: enough spend to call it proven |
| `dark_roas_cap` | `6.0` | `float` | caps the ROAS used in the stake so one outlier cannot dominate |
| `dark_confidence` | `60` | `int` | absence of serving is not "nothing" |
| `dark_conf_part` | `20.0` | `float` | labelled part behind the dark-winner confidence |
| `untracked_min_spend` | `50000.0` | `float` | money: untracked spend large enough to need a justification |
| `untracked_confidence` | `55` | `int` | confidence of a justify-or-kill card: a prompt to explain the spend, not a measurement |
| `untracked_conf_part` | `15.0` | `float` | labelled part behind that confidence |
| **probes** | | | |
| `probe_weekly_cap` | `500.0` | `float` | money per week; a parameter, not a law: re-derive per account |
| `probe_weeks` | `2` | `int` | stake = weekly cap x weeks |
| `probe_max_per_plan` | `2` | `int` | richest candidates first |
| `probe_min_live_days` | `7` | `int` | entity had time to serve and still did not reach the floor |
| `probe_confidence` | `40` | `int` | capped downside, unproven upside |
| `probe_conf_part` | `15.0` | `float` | labelled part behind the probe confidence |
| **contested days** | | | |
| `partition_max_ratio` | `0.3` | `float` | below this share of contested days it is a schedule, not a collision |
| `partition_min_entities` | `2` | `int` | a collision needs at least two entities |

## WatchdogConfig

`cpm_lineage.watchdog.WatchdogConfig` · defined in `src/cpm_lineage/watchdog.py`

Tripwire thresholds.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `rival_share_gain_pp` | `0.5` | `float` | a rival gaining this many share points is a severity-1 alert |
| `availability_floor` | `0.85` | `float` | own availability under this: ad money is hitting empty shelves |
| `availability_drop` | `-0.03` | `float` | else a drop of three points or more: severity 2 |
| `ad_sov_drop_pp` | `-5.0` | `float` | own ad share of voice down five points or more: severity 2 |
| `launch_trend` | `'ramping'` | `str` | rival launches with this trend are flagged (severity 3) |
| `max_alerts` | `5` | `int` | alert lines per pair |
| `window_days` | `30` | `int` | data window the sweep loader is asked for |

## Constants used as defaults

Some defaults above are written as a named module constant. Their values:

**`DEFAULT_DAYPARTS`**

```python
(('06-10', 6, 10), ('10-14', 10, 14), ('14-18', 14, 18), ('18-22', 18, 22), ('22-06', 22, 6))
```

**`DEFAULT_GRAIN_ORDER`**

```python
('portfolio', 'segment', 'product', 'geo', 'entity', 'keyword')
```

**`DEFAULT_OVERRIDES`**

```python
(Override('availability', False, 'availability block wins: fix supply before any demand action'), Override('acquisition', True, 'new-customer acquisition gate wins over a cut'))
```

**`DEFAULT_PLAYBOOK`**

```python
{CapabilityClass.KEYWORD_AUCTION.value: frozenset({Lever.BUDGET.value, Lever.KEYWORD.value, Lever.BID.value, Lever.EXPERIMENT.value, Lever.GEO.value, Lever.WATCH.value}), CapabilityClass.RETARGETING.value: frozenset({Lever.BUDGET.value, Lever.WATCH.value}), CapabilityClass.PLACEMENT.value: frozenset({Lever.BUDGET.value, Lever.WATCH.value}), CapabilityClass.REACH.value: frozenset({Lever.WATCH.value})}
```

**`DEFAULT_STOP_WORDS`**

```python
frozenset({'a', 'an', 'the', 'and', 'or', 'of', 'for', 'with', 'in', 'on', 'to', 'by', 'at', 'from', 'g', 'gm', 'gms', 'gram', 'grams', 'kg', 'kgs', 'mg', 'ml', 'l', 'ltr', 'litre', 'liter', 'oz', 'lb', 'pc', 'pcs', 'piece', 'pieces', 'pack', 'packs', 'combo', 'set', 'box', 'pair', 'buy', 'online', 'best', 'new', 'price', 'offer', 'offers', 'sale', 'free', 'size', 'x'})
```

**`FAMILY_CAPS`**

```python
{('efficiency-cut', 'budget'): 8, ('efficiency-cut', 'keyword'): 8, ('bid', 'bid'): 6, ('growth', 'budget'): 5, ('growth', 'keyword'): 3, ('experiment', 'experiment'): 3, ('sov-gap', 'budget'): 4, ('sov-gap', 'watch'): 2, ('geo', 'geo'): 6, ('collision', 'keyword'): 6, ('defense', 'watch'): 3, ('efficiency-scale', 'budget'): 6, ('defense', 'budget'): 5}
```

**`PRIORITY`**

```python
{'defense': 4.0, 'supply': 3.5, 'efficiency-cut': 3.0, 'collision': 2.5, 'growth': 2.0, 'sov-gap': 2.0, 'geo': 1.8, 'efficiency-scale': 1.5, 'bid': 1.2, 'experiment': 1.0}
```

# Platform declaration

These three classes describe the platform, not a threshold. They are listed here because several of their fields (`currency_unit`, the trading-day hours, the position range) feed the thresholds above.

## AdTypeCapability

`cpm_lineage.adapters.base.AdTypeCapability` · defined in `src/cpm_lineage/adapters/base.py`

Capability row for one canonical ad type.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `name` | `(required)` | `str` | canonical ad-type name |
| `capability_class` | `CapabilityClass.KEYWORD_AUCTION.value` | `str` | playbook key: which levers the type exposes |
| `targeting` | `'keyword'` | `str` | "keyword" \| "auto" \| "none" |
| `keyword_drillable` | `True` | `bool` | targeting-unit grain data exists |
| `sales_attributed` | `True` | `bool` | False: reach-class, never judged on ROAS |
| `judged_on` | `'roas'` | `str` | "roas" \| "deflated_roas" \| "reach" |
| `roas_deflator` | `1.0` | `float` | multiply reported ROAS (retargeting over-attributes) |
| `has_clicks` | `False` | `bool` | descriptive: the type reports clicks (no rule needs clicks) |
| `has_reach` | `False` | `bool` | descriptive: the type reports reach |
| `levers` | `None` | `frozenset[str] \| None` | None -> the class's playbook entry |

## PlatformProfile

`cpm_lineage.adapters.base.PlatformProfile` · defined in `src/cpm_lineage/adapters/base.py`

Everything platform-specific, declared once.

| Field | Default | Type | Meaning |
|---|---|---|---|
| `name` | `'generic'` | `str` | platform name; namespaces ledger rows |
| `has_clicks` | `False` | `bool` | descriptive: the platform reports clicks (no rule needs clicks) |
| `auction_type` | `AuctionType.UNKNOWN.value` | `str` | "first_price" \| "second_price" \| "unknown"; the self-floor check runs only on second price |
| `bid_unit` | `'cpm'` | `str` | unit of ``PerfRow.bid`` |
| `units_kind` | `'units'` | `str` | "units" \| "orders" |
| `position` | `PositionSemantics()` | `PositionSemantics` | The embedded [`PositionSemantics`](#positionsemantics) (see its own section). |
| `ad_types` | `dict()` | `Mapping[str, AdTypeCapability]` | canonical ad-type name -> capability row |
| `ad_type_aliases` | `dict()` | `Mapping[str, str]` | raw label (any case) -> canonical name |
| `default_capability_class` | `CapabilityClass.KEYWORD_AUCTION.value` | `str` | unknown types are fully levered |
| `playbook` | `dict(DEFAULT_PLAYBOOK)` | `Mapping[str, frozenset[str]]` | capability class -> allowed levers |
| `status_map` | `dict()` | `Mapping[str, str]` | raw status -> CanonicalStatus value |
| `geo_grain` | `'none'` | `str` | "city" \| "region" \| "country" \| "none" |
| `geo_rollup` | `dict()` | `Mapping[str, str]` | child geo -> parent geo (terminal) |
| `aggregate_geo_labels` | `frozenset({'others', 'other'})` | `frozenset[str]` | long-tail roll-up labels (lower case): never a treatment, control or target |
| `aggregate_geo_prefixes` | `('other ',)` | `tuple[str, ...]` | long-tail roll-ups are never a target |
| `placement_targets` | `frozenset()` | `frozenset[str]` | pseudo targeting values that are not search terms |
| `placement_target_prefixes` | `()` | `tuple[str, ...]` | prefixes of pseudo targeting values that are not search terms |
| `biddable_targeting_types` | `frozenset({'keyword'})` | `frozenset[str]` | PerfRow.targeting_type values that are biddable units (add "audience", "category" ...) |
| `currency` | `''` | `str` | display only; no currency in core maths |
| `currency_unit` | `1.0` | `float` | see ``config.Economics.currency_unit`` |
| `timezone` | `'UTC'` | `str` | local-date bucketing of bidder decision timestamps in the CPM audit and overpay check |
| `trading_day_start_hour` | `6.0` | `float` | serving starts (local hour) |
| `trading_day_end_hour` | `18.0` | `float` | last hour that still converts to same-day orders |
| `funding_target_hour` | `22.0` | `float` | a raise funds serving up to this hour |
| `feed_lag_days` | `0` | `int` | descriptive: days the feed trails the calendar |
| `feeds` | `frozenset({'perf'})` | `frozenset[str]` | which feeds the adapter can supply |
| `automated_sources` | `frozenset()` | `frozenset[str]` | change-log sources that are automations |

## PositionSemantics

`cpm_lineage.adapters.base.PositionSemantics` · defined in `src/cpm_lineage/adapters/base.py`

What the platform's ``position`` field means (or that it has none).

| Field | Default | Type | Meaning |
|---|---|---|---|
| `available` | `True` | `bool` | False: the platform reports no position; every position rule abstains |
| `kind` | `'average'` | `str` | "most_viewed" \| "average" \| "impression_share" ... |
| `lower_is_better` | `True` | `bool` | True: 1 is the top slot |
| `valid_min` | `1.0` | `float` | positions outside [valid_min, valid_max] are sentinels and are ignored |
| `valid_max` | `100.0` | `float` | upper end of the valid position range |
| `top_value` | `1.0` | `float` | value of the best slot |
| `cap` | `21` | `int` | bucket cap for per-position tables |
| `page_one_depth` | `12` | `int` | slots on the first results page |
