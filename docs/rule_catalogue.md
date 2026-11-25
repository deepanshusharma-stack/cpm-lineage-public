# Rule catalogue

Every recommendation the library's campaign-level and account-level engines can
emit, restated platform-neutrally as *trigger -> prescribed action*. This page is
documentation of intent. Where it disagrees with a module's own docstring or
tests, the code wins; known disagreements are listed at the end.

Conventions used on this page:

- **Breakeven.** Every ROAS bar is a multiple of the account's effective
  breakeven `be = breakeven / ltv_multiplier` (`Economics.be`; an unconfigured
  account has `be = 1.0`). "ROAS >= 2" below means `2 x be`, the scale bar.
- **Money.** Absolute floors are reference amounts scaled by the account's
  `currency_unit` (`Economics.money`). They always keep the form
  `max(absolute floor, % of spend)`.
- **Availability** is on a `0..1` scale in code; percentages here are for
  reading.
- **Ad types** are described by capability, never by name: *keyword-targeted*
  (judged on ROAS, drillable to keyword grain), *auto-targeted retargeting*
  (ROAS deflated before judging), *reach placement* (no attributed sales; judged
  on reach). The adapter maps a platform's labels onto these.
- **Automation hint.** A rule may carry an optional free-text hint naming an
  automation the operator could use. It is never required.

## 1. Rules every other rule inherits

1. Aggregate correctly for the grain: sum additive measures, take the max of
   campaign-level attributes, never average raw rows of a feed with several
   rows per campaign-day.
2. ROAS means different things by ad type: keyword-targeted types are judged on
   ROAS; auto-targeted retargeting is deflated first (the adapter states
   `AdTypeCapability.roas_deflator` per ad type; the default is 1.0, no deflation); reach
   placements are judged on reach, never on sales ROAS.
3. Budget sufficiency = live minutes / expected live minutes. Low live time
   *without* the platform's exhausted flag is operator scheduling, not
   out-of-budget.
4. Status semantics: platform-held means "needs money", not "needs
   reactivation"; operator-stopped means "find the reason first"; a dead
   campaign with spend in the last 7 days is recent, otherwise it is stale and
   excluded everywhere.
5. Auctions are per geo. An aggregate "others" bucket is a reporting artefact,
   never a place to act. Self-competition needs the same keyword, the same
   named geo and a shared product: a portfolio occupying a shelf is not
   self-competition.
6. Segment purity: act on a campaign inside a segment only if all of its
   advertised products are in that segment.
7. Conflict grammar: every held action names its blocker. Granularity order:
   product / supply beats campaign; keyword-level beats budget-level.
8. Data-sufficiency terminals ("maintain", "insufficient data") are valid
   outputs. A verb is never forced.
9. Outcome tracking: equal before / after windows, exact horizons (7 / 14 / 30
   days), never a verdict off a stalled feed; a market-adjusted expectation
   (`before x sqrt(segment move)`, clamped to `[0.5, 2.0]`); early stop-loss
   from day 3 for losers only (below half the adjusted baseline);
   counterfactual scoring of superseded and vetoed cards; a standing holdout.

## 2. Campaign action engine

### 2.1 Selection mechanics

- **Mandatory leads** go straight into the top three, in this order:
  availability, budget, keyword bleed.
- Everything else enters a **scored pool**; the pool is sorted by score and
  fills the remaining slots; the list is cut to three.
- De-duplication: a keyword named by an earlier action is removed from later
  keyword actions; a keyword action left with no targets is dropped.
- A lever story is told only when the diagnosis is neither "unexplained" nor
  "thin data".
- Score ladder: account tighten 80 > severe under-utilisation 78 > account
  raise 72 > winner scale 70 > pre-fund when capped 64 > standard ladder 56 >
  hurting lever `50 + min(|pct|, 40) x 0.4` (50 to 66) > pre-fund uncapped 48 >
  availability recovered 42 > helping lever 18.

### 2.2 Rules

| # | Trigger | Prescribed action |
|---|---|---|
| 1 | Availability in the caution band, or dropped, or low-confidence | Decorate any scale-class headline: "scale only in-stock products" |
| 2 | Availability fell window over window but is above the block line | LEAD "Fix availability first": the order is supply, then pricing, then ads |
| 3 | Availability below the block line (default 60%) | LEAD "Fix availability first"; ALL scale actions held |
| 4 | Capped and the campaign is removed | Conflict note only: budget actions are invalid; launch a replacement if the niche matters |
| 5 | Capped and ROAS unattributed | LEAD "Investigate tracking": never raise blind |
| 6 | Capped, ROAS >= 2, operator day-parting detected | Conflict note: low serving hours are intentional; no raise without coordinating the schedule |
| 7 | Capped, ROAS >= 2, availability block | Conflict note: availability (product level) wins |
| 8 | Capped, ROAS >= 2, keyword bleed >= 40% of spend | LEAD "Hold the budget raise": scaling funds the bleed; fix keywords first |
| 9 | Capped, ROAS >= 2, marginal ROAS at the cap < 1 (below 0.9 is the hard block; 0.9 to 1 still holds, because the next unit returns less than it costs) | LEAD "Hold the budget raise": the next unit buys the flat part of the curve |
| 10 | Capped, ROAS >= 2, clean | Status-aware verb: platform-held -> "Top up daily budget"; operator-stopped or ended -> "Investigate stop and reactivate"; else "Raise daily budget". Sized by the recommended range, revenue at stake and marginal ROAS |
| 11 | Capped, 1.0 <= ROAS < 2.0 | "Hold and monitor" for about 7 days; raise only if ROAS climbs to 2.0 |
| 12 | Capped, ROAS < 1, live | LEAD "Tighten daily budget": the cap is funding the bleed |
| 13 | Under-utilised, bid-constrained, winning below own bid | "Stepwise bid test" (56) |
| 14 | Under-utilised, bid-constrained | "Raise bids, not budget" (56) |
| 15 | Under-utilised, position about 1 with a high top-slot share | "Add keywords": a volume ceiling (56) |
| 16 | Utilisation < 20% | "Audit before spending" (78) |
| 17 | Sub-breakeven keywords above the materiality floor that fail every spare gate | LEAD "Pause / negative" (top 3 by spend) |
| 18 | Segment search demand on own keywords up >= 25% | "Pre-fund ahead of the demand spike" (64 capped, 48 not) |
| 19 | Availability recovered into band | "Resume held scaling" (42) |
| 20 | A top-tier lever is hurting | Lever-specific fix; targets chosen as in section 5 (50 to 66) |
| 21 | A hurting boost lever while availability is blocked | Conflict note: the boost is held until supply recovers |
| 22 | Ladder candidate built | Scoring step (78 severe, else 56) |
| 23 | Keywords passing the winner gate | "Scale" (70) |
| 24 | A top-tier lever is helping | "Protect / maintain" (18) |
| 25 | Account-level starved winner | "Raise budget on {campaign}" (72) |
| 26 | Account-level raise while availability is blocked | Conflict note |
| 27 | Account-level wasteful capped campaign | "Tighten budget on {campaign}" (80). Never availability-gated: tightening is always safe |
| 28 | Always | Fill the remaining slots by score |
| 29 | Fewer than 3 actions and the diagnosis is thin or unexplained | "Hold lever changes": act on budget and keyword facts only |
| 30 | Campaign has 21 to 34 active days and a growth verb was selected | Prefix: reversible half-step, re-check in 7 days |
| 31 | The platform rejected more than half of recent write operations | Prefix: fix execution before re-issuing |

The dead band matters: a campaign between breakeven and the scale bar always
gets a verb ("hold and monitor"), never silence.

## 3. Budget probe flags

These are the facts the budget verbs consume.

| Flag | Definition |
|---|---|
| `capped` | >= 7 active days and spend >= 90% of the daily budget on >= 60% of them |
| `soft_cap` | capped, but the platform's exhausted flag is set on < 30% of days: a pacing artefact, verify first |
| canonical override | >= 5 of the last 7 days with a platform-held hour forces `capped`; zero held hours downgrades to `soft_cap` |
| `dayparted` | >= 5 days of operator pauses around real serving hours: no top-up |
| `slot_exhaust_risk` | day-parted with a small daily cap (reference 2,000 per day) |
| `mroas_block` | capped, capped-day ROAS >= 1, not day-parted, marginal ROAS < 0.9 from an identified curve (r2 >= 0.3, >= 21 daily points) |
| `rev_at_stake`, `rec_range` | capped, capped-day ROAS >= 1, not day-parted, not marginal-blocked, trough ROAS >= 1, exhaust time inside the trading day |
| `past_ceiling` (no `rec_range`) | the gating curve's marginal ROAS at the cap is under 1, or its marginal-1 spend is within 1% of the cap; the range is always clamped to that spend, for the own and the pooled curve alike |
| `exh_inefficient` | capped with capped-day ROAS < 1 |
| `ladder` | not capped, utilisation < 0.85, >= 7 days, live: one of severe (< 0.20), volume ceiling (average position <= 1.5 and top-slot impression share >= 0.6), bid-constrained |
| `stepwise` | >= 50% of spend clearing at a CPM below 0.85 x own bid across >= 3 keywords |

Supporting definitions:

- Trust the exhausted flag only when the day's spend is at least half the
  budget (some placement types report it unreliably).
- A placeholder cap (reference: under 100 per day) gets no budget maths.
- Recommended raise range: `(min(1.3b, hi), hi, min(1.5b, hi))` with
  `hi = min(2.0b, curve ceiling)` and `b` the current daily budget.
- Revenue at stake = capped-day revenue per served hour x unserved hours x
  capped days, where the exhaust hour falls inside the configured trading day
  and unserved hours run to its end. Medium confidence.
- The spend-to-sales curve is correlational: it has no demand controls.

## 4. Account-level rules

| Trigger | Prescribed action |
|---|---|
| Every raw ad-type label | Canonicalise aliases into a small set of first-class ad types; unmapped labels pass through title-cased |
| Ad type is keyword-targeted | `drillable = True` (the only type judged at keyword grain) |
| Ad type is a reach placement with no attributed sales | `no_attrib = True`; excluded from drag maths; "judge on new users / halo before cutting" |
| `|sum mix| >= |sum rate|` in the ad-type shift-share | Route MIX, else RATE |
| MIX, and low-ROAS types gained share (share up, ROAS below the account's, material spend) | "Rebalance budget back to {best type}; cap the share-gainers" (up to 3 named) |
| MIX, no gainers | "Protect {best type} budget share" |
| Choosing the MIX destination | Exclude types whose marginal ROAS < 1 (near plateau) unless that empties the list |
| RATE, worst type drillable | "About N campaigns explain about 80% of the drag: drill them" |
| RATE, worst type not drillable | "No keyword data: act at the spend level" |
| Non-drillable type, ROAS < 1, material spend | "Reduce / cap; split cold reach from retargeting" |
| Cap binding, ROAS >= 2 and >= the account's ROAS | SCALE: +50% budget; upside `0.5 x budget x ROAS`, replaced by `predict(1.5b) - predict(b)` when a curve fits |
| The curve says zero upside at +50% | Eject from SCALE (past the peak) |
| Curve fit failed for a flat spend history | Low confidence: "flat spend, upside unverified" |
| Cap binding, ROAS < 1 | TIGHTEN: waste `average spend x (1 - ROAS)` per day |
| Cap NOT binding, utilisation < 0.70, ROAS < 1, live and not platform-held or paused | RELEASE: freeable `min(0.4 x budget, average spend)` |
| Release and scale both non-empty | Note "reallocation, not new money" (freed versus needed) |
| Campaigns of attributed types | Pareto drag `spend x (account ROAS - campaign ROAS)`; top 10; `n80` = count inside 80% of the positive drag |
| Pareto rows | Attach the latest status by latest row (never the max of a string) so dead campaigns are excluded |
| Sub-breakeven keywords account-wide | Partition spend into spared (halo above 0.2 with a p10 grace; conquest with cost per new user at or under the cohort p25) and cuttable; placement pseudo-keywords excluded |
| Cuttable bleed > 0 | ONE queue row "Cut / negative the cuttable bleeders" (never one row per keyword) |
| Live Pareto draggers not already queued | Queue row "Drill {campaign}" |
| Market share : share of voice < 0.5 | Halve confidence on every scale row ("over-investing in ads; clean the ROAS gate before scaling") |
| Always | Queue floor `max(reference 2,000 x period / 30, 1% of the largest row)`, rank by `value x confidence weight`, cap 15 rows, roll the tail into "+N more" |

Queue groups in display order: STOP THE BLEED, SCALE WINNERS, FIX STRUCTURE.

The mix-versus-rate identity is the exact midpoint shift-share: for each ad
type with spend share `w` and ROAS `r`,
`mix = (w_c - w_p) x (r_c + r_p) / 2` and
`rate = (w_c + w_p) / 2 x (r_c - r_p)`; the two sums add up to the change in
the account's blended ROAS.

Materiality references for MIX recommendations: spend above 50,000 for
share-gainers, above 100,000 for a non-drillable type under breakeven.

## 5. Keyword rules

### 5.1 Role classification

Roles: own-brand, sibling-brand, competitor (conquest), generic, placement.

- Own match = token subset, or the de-spaced concatenation (at least 5
  characters), or an abbreviation prefix (at least 4 characters of an own token
  of 5 or more).
- Placement pseudo-keywords are dropped before classification.
- Sibling is checked before competitor.
- A "bare competitor head" is a conquest term with no own-segment vocabulary.

### 5.2 Bleeder gates

A bleeder candidate has ROAS < 1.0, spend above the bleed floor
(`max(reference 10,000, 5% of keyword spend)`) and is not a placement. Then, in
this order:

| Gate | Outcome |
|---|---|
| Conquest term with ZERO own organic impressions on it (unknown is not zero) | HARD stop: relevance fail. Overrides the acquisition gate and is not waived for new products |
| Conquest, 0 direct ROAS, 0 new users, indirect share <= 0.2 | HARD stop: "definitive stop, not a bid cap" |
| Conquest, cost per new user <= cohort p25 | SPARE: an acquisition keyword, judged on cost per new user; maintain, re-check in 2 weeks |
| Conquest, cost per new user <= 2 x cohort p25 | SPARE with watch |
| Own-brand term while the organic flywheel is Cold | SPARE: cap bids about 20%, do NOT pause |
| ROAS >= the term's own role-cohort p25 | SPARE: inside the cohort norm (a flat floor would misjudge this type) |
| Indirect share > 0.2 and ROAS >= cohort p10 (or no p10) | SPARE: halo. Cap about 20% instead of stopping; re-evaluate in 2 weeks |
| Indirect share > 0.2 and ROAS < cohort p10 | SPARE with a staged exit: cap 30% now, stop in week 2 if unchanged |
| Survives all of the above | "Pause / negative" (top 3 by spend) |

Head-term protection: a generic head term (top 3 by search volume, or at least
10% of generic demand) is never stopped on direct ROAS alone.

Other floors: pause materiality `max(reference 8,000, 3% of keyword spend)`;
cap-bids floor `max(reference 5,000, 2% of keyword spend)` with ROAS < 1.8.

### 5.3 Winner (scale) gate

All must hold:

- not a placement, not already named, not spared, not a bleeder;
- not an own-brand term while the flywheel is Self-Sustaining;
- demand >= 50 searches per day (missing does not block);
- relevance >= 0.3 (missing does not block);
- conversion confidence is not low;
- not a relevance-failed conquest term;
- base ROAS >= 1.5 absolute;
- base ROAS >= its own role-cohort p75 (or >= 3 when there is no cohort).

For own-brand terms on products at least 90 days old, the base is incremental
ROAS `= ROAS x (1 - organic / (organic + ad) impressions)`, and it must also
clear the own-brand p25. New products waive only that gate.

### 5.4 Conversion-confidence composite

Signals, when present: relevance >= 0.7; organic presence; conversion curve
>= 1.5; keyword weight at or above the median.

- LOW if any hard fail (relevance < 0.3, or curve < 0.8);
- HIGH if two or more signals are strong;
- LOW if three or more signals exist and all are weak;
- else MEDIUM.

LOW means a bid-cap posture.

### 5.5 Target selection per lever

- Competitor-share lever: "Negative / pause" conquest terms.
- Boost levers (position, paid voice, organic voice): top 3 by spend among
  keywords with ROAS >= 2 that pass the demand, relevance, confidence and
  relevance-fail gates.
- Default: "Cap bids on" the top 3 keywords with ROAS < 1.8 above the cap
  floor, falling back to the 2 lowest-ROAS keywords.

### 5.6 Facts and postures (annotations, not verbs)

- Top-slot impression-share displacement: share fell below 0.67 x prior, with
  prior >= 5% and both halves >= 2,000 impressions. It is free of survival
  bias: an average rank cannot see hours in which you were not serving.
- Share-of-voice denominator decomposition (paid SOV drop >= 5pp with >= 3
  non-null days per window): own volume versus pool, the same discriminator as
  the RCA's pullback-versus-dilution step.
- Displacement signature: impressions per day up 20% while paid SOV fell 5pp
  (more impressions, but in deeper, cheaper slots).
- Self-competition: >= 30% of keyword spend on keywords another own campaign
  also bids. Bid-raise actions get a "consolidate first" prefix.
- Mix posture notes: own-brand above 50% of spend; generic under 30%; conquest
  ROAS under half the generic ROAS.
- History bridge: an action that contradicts the keyword's long-run history is
  damped to "watch / confirm over 7 days" and marked provisional. A keyword is
  never flipped across windows.

### 5.7 Organic flywheel bands

Organic share of own impressions on own-brand terms:

| Band | Share | Posture |
|---|---|---|
| Cold | < 0.20 | cap, do not cut |
| Warming | 0.20 to 0.50 | maintain investment, watch the organic trend |
| Active | 0.50 to 0.70 | test trimming own-brand bids: organic likely catches them |
| Self-Sustaining | >= 0.70 | de-scale own-brand bids |

When availability is blocked the band is "undetermined": the reading is invalid
until supply is fixed.

## 6. Cold-start and short-campaign rules

- Tier by active days: COLD < 7, WARM 7 to 20, MATURE 21 to 34, FULL >= 35.
  Below 21 days there is no period-over-period move to explain, so the engine
  switches from MOVE attribution to a STATE read built from the account's
  persistent keyword history.
- Shrinkage: the estimate is `w x self + (1 - w) x cohort p50`, with `w`
  rising with evidence. The implemented weight counts campaign breadth as well
  as days (`n_eff = days + 4 x campaigns`, see `coldstart`); a simpler
  alternative is `w = n / (n + 14)`. Confidence rides `w`
  (depth of history), not campaign age: high at 0.7, medium at 0.4.
- Per-keyword pre-flight call:

| Condition | Call | Action text |
|---|---|---|
| No history | no-history (low) | start capped and monitor |
| Judge >= cohort p75 or >= 2.0 | proven-winner | fund it from day 1 |
| Judge < cohort p25 or < 1.0, halo > 0.2 | known-bleeder | cap, do not stop |
| Same, no halo | known-bleeder | start capped / negative |
| Between p25 and p75 | neutral | start at the type-median bid and watch |
| History but no ROAS | thin-history (low) | start conservative |
| Own bid below the top-slot clearing p50 | (addendum) | underbidding: expect thin impressions |
| No own bid, clearing price known | (addendum) | set the opening bid at clearing p50 / p75 |

- Ordering: bleeders before winners. The state top three is up to 2 bleeders
  ("Cap / negative (reversible)", half-step cap, re-check in 7 days) and then
  up to 2 winners ("Fund from day 1").
- The campaign's own short-window ROAS is reported as a noisy FACT and never
  used for attribution.
- Coverage (% of keywords with usable history) sets confidence: high at 80%,
  medium at 40%. Any known bleeder escalates priority.

## 7. Framing

Verb -> banner class is a first-match cascade, and the order is load-bearing:

1. CUT / TIGHTEN (tighten, cut, pause, negative, cap bid)
2. SCALE (top up, raise, scale, boost, strengthen)
3. REACTIVATE (reactivate, investigate)
4. FIX STOCK FIRST
5. RE-PRICE
6. HOLD / WATCH (hold, audit, maintain, watch)
7. REVIEW

## 8. Auction threat classes and banners

| Trigger | Output |
|---|---|
| A competitor's logged bid actions hit their ceiling more than 20% of the time (>= 5 observations) | "ceiling-constrained: a modest lift past their cap wins" |
| A competitor targets a top slot (<= 2) with a maximum bid above yours | "active threat" |
| A competitor sits in deeper slots (>= 3) bidding below you | "background" |
| A competitor is present with no pattern | "present" |
| Gross channel movement >= 3 x net and >= 0.3 log-points | Banner "Regime change, not a drift: read composition first" |
| Sub-breakeven spend >= 30% of all spend | Banner "the structural problem dwarfs the period move: fix the bleed first" |

These rules need an auction-intelligence feed and stay silent without one.

## 9. Root-cause analysis gates

The RCA (`cpm_lineage.rca`) answers "why did sales or share move" for own
products in one segment. Its gates run in a fixed order; the order is the order
of the cause list. Thresholds are fields of `RcaConfig`.

| Step | Fires when | Cause | Lever (axis) |
|---|---|---|---|
| Quadrant | segment moved >= 5% and / or share moved >= 0.10pp | six classes from "market move, brand outperformed" to "stable" | none |
| Promo | discount depth fell >= 3pp | `PROMO_PULLBACK` | restore depth (pricing) |
| List-price mix | a product shows 2+ list-price points, each >= 10% of observations | `STICKER_MIX` | finish the migration (pricing) |
| Ad voice | SOV fell >= 1pp, own volume fell and trails the pool by >= 10pp | `AD_PULLBACK` | re-enter voice to an SOV floor (sov) |
| Ad voice | SOV fell >= 1pp otherwise | `OUT_ESCALATED` | buy voice to a share target (sov) |
| Availability | under 85%, or fell >= 3pp, or any geo under the floor | `AVAILABILITY_DRAG` | supply triage (availability) |
| Rivals | a non-own, non-sibling brand gained >= 0.5pp | `COMPETITOR_ABSORPTION` | defend the lane (competition) |
| Rivals | a launch in the last 60 days holds >= 1% of the segment | `NEW_ENTRANT` | none |
| Spend | first-party ad spend fell >= 15% | `AD_SPEND_CUT` | voice floor if voice held, else restore budget (sov) |
| Efficiency | direct ROAS under 1.1 x breakeven | none | efficiency guard (conversion) |
| Products | growing product more than 10pp under the floor | none | unblock the scaling product (availability) |
| Products | zero-sales or low-availability products while the hero is healthy | `PASSENGERS` | fix or prune (availability) |
| Pockets | keyword pool grew >= 20% with own SOV under 2% | none | work the pockets (sov) |
| Vacuum | an advertiser with >= 5% prior SOV fell to <= 60% of its volume | none | contest the vacated voice (sov) |
| Share | share fell > 0.10pp with no absorbing rival | `SHARE_CEDED` | none |
| Internal mix | own value fell and another own line grew >= half the loss | `MIX_SHIFT` | none (verify with basket data) |
| Geo | market-move quadrant with collapsed or outperforming geos | none | rebalance geo spend (geo) |

Two rules worth remembering:

- **Absolute versus pool.** A falling share of voice with *rising* own volume
  can only be dilution. A genuine pullback needs own volume to fall *and* trail
  the pool by more than the gap.
- **Collapse is tested first.** Relative outperformance inside a collapsing geo
  is never a reason to spend there.

Every causal label is a threshold attribution on window-over-window deltas. It
is not causal inference: there is no significance test, no control for
co-occurring causes, and the fixed order decides which cause leads.

## 10. Design choices where more than one definition is common

| Topic | Variants | What the library does |
|---|---|---|
| Role-cohort minimum sample | 12 in one place, 5 in another | one config value, default 12 (`EngineConfig.role_cohort_min_n`) |
| Conquest spare gate | cost per new user <= p25 / <= 2 x p25, versus <= 1.5 x cohort p50 with >= 3 new users | see the keyword module's config; this page lists the first form |
| Shrinkage weight | `n / (n + 14)` versus evidence that also counts campaigns | the evidence form (see `coldstart`) |
| Scale bar | absolute 2.0 versus breakeven-relative | `scale_bar = 2 x be`; defaults reproduce 1.0 and 2.0 |
| Availability thresholds | 60 block, 75 caution, 85 floor, share-band floors of 85 / 88 / 90 | one `AvailabilityPolicy` on a `0..1` scale |
| "Budget-bound" | four definitions (early exhaustion, sufficiency, capped-day share, held days) | `BudgetTelemetry.is_bound(policy)` with a named method |
| Cut sizing | fixed step with cooldown (action queue) versus trimming to the `(1 - ROAS)` slice (one-line prescription) | both kept, one per surface |
