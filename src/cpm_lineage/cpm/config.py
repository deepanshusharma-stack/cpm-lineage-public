"""Thresholds of the CPM / position / auction subsystem.

Every value is a named field carrying a default and a one-line
rationale.  Absolute money amounts are written in *reference-currency units*
and must be scaled with ``Economics.money()`` (``money_of`` below) before
use, so no currency is baked in.

Judgment values: ``hot_cap``, ``low_hit``, ``blind_seen`` and the whole
overpay block (``flat_*``, ``overshoot_share``, ``self_floor_pct``,
``overpay_ratio``) are round numbers chosen by judgment.  They are not calibrated or
back-tested; treat them as starting points.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Economics

__all__ = ["CpmConfig", "money_of", "DEFAULT_DAYPARTS"]

# (label, start hour inclusive, end hour exclusive) in local time; a part whose
# end is not after its start wraps past midnight.
DEFAULT_DAYPARTS: tuple[tuple[str, int, int], ...] = (
    ("06-10", 6, 10),
    ("10-14", 10, 14),
    ("14-18", 14, 18),
    ("18-22", 18, 22),
    ("22-06", 22, 6),
)


@dataclass(frozen=True)
class CpmConfig:
    """Named thresholds for CPM views, the CPM-move classifier, restatement
    detection, the ownership guard, the bid-ceiling audit, auction
    intelligence and the keyword overpay check."""

    # --- views -------------------------------------------------------------
    top_n: int = 4                          # size of the tracked top-keyword set (by spend; the rest are rolled into one row)
    window_top_min: int = 1                 # lower clamp of the window-compare top-n (input guard)
    window_top_max: int = 50                # upper clamp of the window-compare top-n (input guard)
    track_days: int = 7                     # the tracker shows at least a week of columns, then grows
    baseline_days: int = 7                  # baseline = the 7 days immediately before the tracker start
    rivals_top: int = 6                     # rival table keeps the six largest brands per keyword and period
    min_category_spend: float = 25000.0     # MONEY: month-to-date spend below which a category folds into the remainder row (keeps the matrix readable)
    other_category_label: str = "Other keywords"  # label of the remainder row (unmapped and folded keywords)
    keyword_targeting_type: str = "keyword"  # normalised targeting_type value that marks keyword-targeted rows
    live_state_days: int = 3                # position-bidder state rows older than this are not "live"

    # --- ownership guard ---------------------------------------------------
    ownership_lookback_days: int = 270      # majority-of-days owner is decided on the trailing 270 days

    # --- CPM-move classifier -------------------------------------------------
    explain_min_rise: float = 0.05          # only CPM rises strictly above 5% are explained
    explain_position_step: float = 0.3      # position moved by at least 0.3 slots = "bought position" / "worse spot" (judgment)
    explain_sov_gain: float = 0.05          # own ad share up by 5 points = bought more share
    explain_rival_gain: float = 0.03        # a rival's ad share up by 3 points = rival pressure
    explain_roas_fell: float = 0.8          # current ROAS below 80% of reference = "(returns fell)"

    # --- restatement ---------------------------------------------------------
    restate_tol: float = 0.01               # a day is restated when it moved by more than 1% ...
    restate_floor: float = 1.0              # ... of max(|first reading|, 1 unit)

    # --- position-bidder audit ----------------------------------------------
    rank_visible_min: float = 1.0           # the bidder searches the first 60 slots ...
    rank_visible_max: float = 60.0          # ... anything outside is "position not found" (bidding blind)
    guardrail_bands: tuple[float, ...] = (50.0, 100.0, 200.0, 300.0)  # upper bounds (% over base) of the permissiveness bands; above the last is the open band
    hot_cap: float = 0.30                   # JUDGMENT: share of decisions pinned at the ceiling that makes a rule set "hot"
    low_hit: float = 0.50                   # JUDGMENT: target-hit rate below which a ceiling-pinned rule set is "hot"
    blind_seen: float = 0.60                # JUDGMENT: position-seen rate below which the bidder is "blind" (outage threshold)
    percentage_ignores_max_bid: bool = True  # in percentage mode the rule's max bid is NOT a cap; False = ceiling is the lower of the two

    # --- auction intelligence --------------------------------------------------
    clearing_days: int = 30                 # decision lookback for clearing bids, competitor stack and timing
    clearing_target_min: float = 1.0        # only near-top targets describe a slot price ...
    clearing_target_max: float = 5.0        # ... targets 1..5
    clearing_slot_split: float = 2.0        # target <= 2 is the top slot group, deeper targets the second group
    min_obs: int = 5                        # groups with fewer than 5 observations are noise (clearing, competitors, organic trend)
    timing_min_obs: int = 10                # a daypart needs 10 decisions before its average bid is quoted
    timing_min_parts: int = 2               # cheap/dear needs at least two dayparts to compare
    dayparts: tuple[tuple[str, int, int], ...] = DEFAULT_DAYPARTS  # local-hour buckets for time-of-day pressure
    threat_ceiling_pct: float = 20.0        # a rival at its ceiling on more than 20% of decisions is ceiling-constrained
    threat_top_slot: float = 2.0            # rival mean target <= 2 = chasing the top slot
    threat_deep_slot: float = 3.0           # rival mean target >= 3 = competing for deeper slots
    organic_days: int = 42                  # six weekly buckets for the organic trend
    organic_min_weeks: int = 2              # a trend needs two weeks
    suggested_days: int = 30                # suggested-bid window, anchored to that feed's own latest date
    suggested_max_keywords: int = 100       # at most 100 keywords per suggested-bid lookup
    suggested_geo_min_rows: int = 3         # a geo needs three captures before its suggestion is quoted
    suggested_geo_top: int = 12             # geos shown in the per-geo suggestion drill
    intel_min_spend: float = 1000.0         # MONEY: keywords at or below this window spend get no auction hint
    intel_top_keywords: int = 8             # auction hints for an entity's top 8 keywords by spend
    bid_round_digits: int = 0               # decimals kept on reported bid statistics (whole units by default; raise for small-unit currencies)

    # --- keyword overpay check -------------------------------------------------
    flat_min_decisions: int = 10            # JUDGMENT: FLAT needs at least 10 decisions in the day
    flat_pct: float = 0.02                  # JUDGMENT: (max bid - min bid) / median bid below 2% = bidder is not moving
    overshoot_share: float = 0.5            # JUDGMENT: better-than-target on at least half the decisions = overshoot
    self_floor_pct: float = 0.80            # JUDGMENT: our own next campaign's bid at >= 80% of this row's CPM = we set our own price
    overpay_ratio: float = 1.25             # JUDGMENT: CPM strictly above 1.25x the band median = overpay
    overpay_bands: tuple[float, ...] = (1.0, 3.0, 6.0, 10.0, 100.0)  # upper bounds of the position bands 1 | 2-3 | 4-6 | 7-10 | 11+
    overpay_ref_min_rows: int = 3           # a band median needs three rows (noise control)
    overpay_top_geos: int = 10              # geos displayed, by spend on the checked keywords
    unknown_geo_label: str = "All/unknown"  # geo label when neither the row nor the campaign name yields one


def money_of(cfg: CpmConfig, name: str, economics: Economics | None = None) -> float:
    """Return money threshold ``cfg.<name>`` scaled to the account currency.

    Formula: ``Economics.money(value)`` = ``value * currency_unit``; with no
    ``Economics`` the value is returned unchanged.
    """
    value = float(getattr(cfg, name))
    return economics.money(value) if economics is not None else value
