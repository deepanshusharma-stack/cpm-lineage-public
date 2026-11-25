"""Thresholds of the keyword subsystem (role classifier, CUT / ADD, leaks, niches).

Every money default is a *reference* amount: it is passed through
``Economics.money()`` so one ``currency_unit`` rescales all of them.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["KeywordConfig", "DEFAULT_STOP_WORDS", "DEFAULT_NATIVE_ROLE_MAP"]

# Articles, units, pack words and shopping filler.  Replace per language / market.
DEFAULT_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "of", "for", "with", "in", "on", "to", "by", "at", "from",
    "g", "gm", "gms", "gram", "grams", "kg", "kgs", "mg", "ml", "l", "ltr", "litre", "liter", "oz", "lb",
    "pc", "pcs", "piece", "pieces", "pack", "packs", "combo", "set", "box", "pair",
    "buy", "online", "best", "new", "price", "offer", "offers", "sale", "free", "size", "x",
})

# Native keyword-type labels a shelf feed may carry -> neutral role.
DEFAULT_NATIVE_ROLE_MAP: tuple[tuple[str, str], ...] = (
    ("brand", "own_brand"),
    ("own_brand", "own_brand"),
    ("own-brand", "own_brand"),
    ("competition", "competitor"),
    ("competitor", "competitor"),
    ("sibling_brand", "sibling_brand"),
    ("generic", "generic"),
)


@dataclass(frozen=True)
class KeywordConfig:
    """Named thresholds with their defaults."""

    # -- role classifier --------------------------------------------------
    stop_words: frozenset[str] = DEFAULT_STOP_WORDS  # tokens that never identify a brand or a category
    brand_token_min_len: int = 3            # shorter tokens are too ambiguous to identify a brand
    despaced_min_len: int = 5               # substring match needs this length: stops short names matching inside longer words
    abbrev_brand_min_len: int = 5           # abbreviation rule: the brand token must be at least this long
    abbrev_keyword_min_len: int = 4         # ... and the keyword token that prefixes it at least this long
    catvocab_min_titles: int = 2            # a token is category vocabulary when it appears in this many own titles
    universe_lookback_days: int = 400       # keyword universe: long panels still find old keywords
    volume_window_days: int = 30            # search-volume proxy window for the keyword dimension
    lexicon_version: str = "1"              # bump when classifier logic changes so cached roles are regenerated

    # -- role cohorts -----------------------------------------------------
    cohort_window_days: int = 60            # cohort percentiles are read over the last 60 days
    cohort_spend_floor_ref: float = 1000.0  # reference money: a keyword needs this spend to count in its cohort
    cohort_min_n: int = 12                  # a percentile is trusted only with this many keywords (5 was noisy)

    # -- CUT --------------------------------------------------------------
    cut_roas_be_mult: float = 1.0           # cut gate = this x breakeven: below break-even loses money outright
    cut_floor_ref: float = 8000.0           # reference money: absolute materiality floor
    cut_floor_share: float = 0.03           # ... or 3 % of scope spend, whichever is larger (scope-relative)
    spare_min_new_users: float = 3.0        # acquisition spare needs at least this many new users
    spare_cpnu_mult: float = 1.5            # ... at a cost per new user within 1.5 x the role median
    cut_top_n: int = 12                     # rows returned

    # -- ADD --------------------------------------------------------------
    add_min_exp_roas_be_mult: float = 1.0   # never add a role whose cohort median loses money (x breakeven)
    explorer_min_relevance: float = 0.5     # below this the platform itself calls the match weak
    unknown_relevance: float = 0.45         # neutral relevance when no feed: presence already implies some
    demand_volume_weight: float = 0.6       # demand = 0.6 x market volume ...
    demand_presence_weight: float = 0.4     # ... + 0.4 x own impressions
    demand_log_scale: float = 6.0           # log10 scale: one million per window reads about 1.0
    payoff_cap: float = 5.0                 # one freak cohort cannot dominate the score
    payoff_unknown: float = 1.0             # expected ROAS used when the cohort is unknown
    organic_bonus: float = 1.25             # organic top slots prove conversion relevance
    organic_bonus_position: float = 10.0    # ... when the organic position is at or better than this
    organic_position_max: float = 500.0     # positions outside 1..500 are garbage and discarded
    add_pool_presence: int = 300            # candidate cap, proven-presence pool
    add_pool_relevance: int = 250           # candidate cap, platform-relevance pool
    fragment_min_len: int = 3               # a one-word term shorter than this is always a typed fragment
    add_top_n: int = 12                     # rows returned

    # -- cross-campaign leaks and re-slot ----------------------------------
    leak_min_spend_ref: float = 50000.0     # reference money: materiality for leaks, re-slot and untracked
    leak_roas_be_mult: float = 1.0          # a leak is material spend below breakeven
    leaks_top_n: int = 8                    # rows returned
    reslot_min_position: float = 6.0        # a "deep" slot
    reslot_cpm_quantile: float = 0.75       # ... bought at a top-quartile CPM
    reslot_min_keywords: int = 4            # quantile needs this many material keywords
    reslot_top_n: int = 5                   # rows returned
    untracked_top_n: int = 4                # rows returned
    position_sanity_max: float = 500.0      # weighted positions use 1..500 only

    # -- owned niches -------------------------------------------------------
    niche_sov_floor_pct: float = 40.0       # own ad share of voice needed to call a generic term "owned"
    niche_min_ad_impressions: float = 5000.0  # material denominator: 100 % of a tiny term is "sole bidder"
    niche_probe_spend_ref: float = 500.0    # reference money: below this the term is only a probe
    niche_scale_roas_be_mult: float = 1.5   # scale class at 1.5 x breakeven
    niche_fix_roas_be_mult: float = 1.0     # fix-conversion class below breakeven
    niche_top_position: float = 1.5         # at the top slot bids cannot buy a better one
    niche_exhaust_window_days: int = 7      # budget-exhausted days counted over the last 7 days
