"""Neutral record types shared by every subsystem.

Everything here is a plain dataclass with no behaviour beyond trivial derived
properties.  Adapters turn platform exports into these records; every analysis
function takes them (or plain dicts built from them) and returns plain
dataclasses or dicts.

Conventions
-----------
* Money is a plain number in the advertiser's currency.  No currency is baked
  in; thresholds are scaled through ``config.Economics.currency_unit``.
* ``availability`` is always a ratio in ``[0, 1]``.
* ``position`` is a rank (slot).  Whether a lower number is better is declared
  by the adapter (``PlatformProfile.position``); the default is "1 is best".
* A field typed ``X | None`` is optional: ``None`` means "the platform does
  not report this" and every consumer must degrade (skip the gate, report
  ``None``) rather than treat it as zero.
* No click metric is required anywhere.  ``clicks`` exists only so that
  platforms which report it can split the conversion term further.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "CanonicalStatus", "LivenessClass", "CapabilityClass", "TargetingRole",
    "AuctionType", "DataTier", "Lever", "Direction", "CardStatus",
    "PerfRow", "Entity", "Campaign", "TargetingUnit", "Product", "MarketRow",
    "OfftakeRow", "ShelfRow", "BudgetDayRow", "BudgetTelemetry",
    "ChangeEvent", "BidRecord", "BidDecision", "SuggestedBid", "AppliedAction", "RunCheck", "RoleCohort",
    "APPLIED_KINDS",
    "Window", "WindowPair", "MetricCell", "Change", "KeywordRef", "ActionCard",
    "card_id", "make_card", "FAMILIES", "CARD_STATUSES", "ACTIONABLE_STATUSES",
]


# --------------------------------------------------------------------------
# Enumerations (all str-valued so records serialise to plain JSON)
# --------------------------------------------------------------------------

class CanonicalStatus(str, Enum):
    """Platform-neutral entity status.  The adapter maps raw strings to these."""

    ACTIVE = "active"                    # serving normally
    BUDGET_HELD = "budget_held"          # platform paused it today: daily budget exhausted, resumes next day
    PLATFORM_PAUSED = "platform_paused"  # platform-reported pause/stop whose flag may lag reality
    PRE_LAUNCH = "pre_launch"            # draft / scheduled / under review
    OPERATOR_PAUSED = "operator_paused"  # a person stopped or paused it
    ENDED = "ended"                      # completed its flight
    REMOVED = "removed"                  # archived / deleted
    UNKNOWN = "unknown"                  # unmapped; treated as live


class LivenessClass(str, Enum):
    """Three-way class the liveness rules operate on."""

    LIVE = "live"
    SOFT_DEAD = "soft_dead"   # dead only when recent spend is zero
    HARD_DEAD = "hard_dead"   # dead regardless of spend


class CapabilityClass(str, Enum):
    """What levers an ad product exposes (the capability playbook key)."""

    KEYWORD_AUCTION = "keyword_auction"  # keyword-targeted listing ad: budget, keyword, bid, geo, experiment
    RETARGETING = "retargeting"          # auto-targeted recommendation-style placement: budget only
    PLACEMENT = "placement"              # shelf / collection placement: budget only
    REACH = "reach"                      # display / banner: no attributed sales, watch only


class TargetingRole(str, Enum):
    """Role of a targeting unit relative to the advertiser."""

    OWN_BRAND = "own_brand"
    SIBLING_BRAND = "sibling_brand"
    COMPETITOR = "competitor"
    GENERIC = "generic"
    PLACEMENT = "placement"   # non-search pseudo unit (feed, shelf, audience bucket)


class AuctionType(str, Enum):
    FIRST_PRICE = "first_price"
    SECOND_PRICE = "second_price"
    UNKNOWN = "unknown"


class DataTier(str, Enum):
    """Data-availability tier from the count of active days (see ``windows.route_tier``)."""

    COLD = "cold"
    WARM = "warm"
    MATURE = "mature"
    FULL = "full"


class Lever(str, Enum):
    """Card ``type``: the lever an action card pulls."""

    BUDGET = "budget"
    BID = "bid"
    KEYWORD = "keyword"
    EXPERIMENT = "experiment"
    GEO = "geo"
    WATCH = "watch"


class Direction(str, Enum):
    UP = "up"
    DOWN = "down"
    NONE = "none"


class CardStatus(str, Enum):
    PENDING = "pending"        # actionable
    TENSION = "tension"        # actionable, exactly one per tension group
    SUPERSEDED = "superseded"  # lost a conflict; visible, not applyable
    VETOED = "vetoed"          # a gate killed it; visible with veto_reason
    MERGED = "merged"          # duplicate; dropped from the plan
    APPLIED = "applied"        # human state
    SKIPPED = "skipped"        # human state


FAMILIES: tuple[str, ...] = (
    "efficiency-cut", "efficiency-scale", "growth", "defense", "bid",
    "experiment", "sov-gap", "geo", "collision", "supply",
)
CARD_STATUSES: tuple[str, ...] = tuple(s.value for s in CardStatus)
APPLIED_KINDS: tuple[str, ...] = ("cut", "defend", "biddown", "bid", "budget", "experiment")
ACTIONABLE_STATUSES: frozenset[str] = frozenset({CardStatus.PENDING.value, CardStatus.TENSION.value})


# --------------------------------------------------------------------------
# Performance fact and dimensions
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PerfRow:
    """One delivery fact: entity x day, optionally x targeting unit x geo x product.

    Required: ``date``, ``entity_id``, ``spend``, ``impressions``.  ``sales``
    and ``units`` default to 0 (an ad type without attribution reports 0 and
    the adapter flags the type as ``sales_attributed=False``).  Everything
    else is optional and consumers degrade without it.

    Additive measures (``spend, impressions, sales, units, clicks,
    indirect_sales, new_users``) may be summed across rows.  ``bid`` and
    ``position`` are row-level rates and must only be combined as weighted
    means (spend-weighted bid, impression-weighted position) -- see
    ``metrics.cell_from_rows``.
    """

    date: date
    entity_id: str
    spend: float
    impressions: float
    sales: float = 0.0                   # directly attributed revenue
    units: float = 0.0                   # attributed units (or orders; the adapter chooses)
    advertiser_id: str | None = None
    targeting_unit: str | None = None    # keyword / audience / category value, normalised by the adapter
    targeting_type: str | None = None    # "keyword" | "category" | "audience" | "auto" | "placement" ...
    match_type: str | None = None
    geo: str | None = None
    product_id: str | None = None
    clicks: float | None = None          # only on platforms / ad types that report clicks
    bid: float | None = None             # bid in force that day (same currency as spend)
    position: float | None = None        # ad slot / rank for the row
    ad_type: str | None = None           # raw or canonical ad-type label
    status: str | None = None            # raw status string as of the row
    indirect_sales: float | None = None  # halo sales; total sales = sales + indirect_sales
    new_users: float | None = None       # new-to-brand customers
    entity_name: str | None = None
    segment: str | None = None           # category / competitive set


@dataclass(frozen=True)
class Entity:
    """A budget-holding ad entity (campaign, ad group or line item).

    Only ``entity_id`` is required.  The rolling-spend and ROAS fields are
    conveniences an adapter may pre-compute; ``None`` means "not supplied",
    and rules treat a ``None`` ROAS as "cannot judge", never as zero.
    """

    entity_id: str
    name: str | None = None
    advertiser_id: str | None = None
    ad_type: str | None = None             # raw or canonical label; unknown -> keyword-auction class
    status: str | None = None              # raw platform status; unknown -> live
    daily_budget: float | None = None      # current daily cap
    segment: str | None = None
    is_pure: bool | None = None            # all advertised products belong to ``segment``
    auto_targeted: bool | None = None      # no targeting-unit control (recommendation-style)
    start_date: date | None = None
    tags: tuple[tuple[str, str], ...] = () # (group, name)
    owner: str | None = None
    product_ids: tuple[str, ...] = ()
    spend_3d: float | None = None          # spend in the last 3 days of the feed
    spend_7d: float | None = None
    spend_window: float | None = None      # spend in the analysis window
    roas_primary: float | None = None      # total (direct + indirect) ROAS, if the platform has it
    roas_direct: float | None = None       # direct ROAS
    own_brand_share: float | None = None   # share of spend on own-brand targeting units, 0..1


Campaign = Entity


@dataclass(frozen=True)
class TargetingUnit:
    """A targeting unit (keyword, audience, category, placement) and its role.

    ``entity_id`` is ``None`` for an account-level unit description and set
    for a unit inside one entity.  Missing ``role`` defaults to generic.
    """

    value: str
    kind: str = "keyword"                  # "keyword" | "audience" | "category" | "placement" ...
    role: str = TargetingRole.GENERIC.value
    entity_id: str | None = None
    is_placement: bool = False             # non-search pseudo unit; excluded from keyword logic
    match_type: str | None = None
    search_volume: float | None = None     # searches per day
    live_bid: float | None = None
    base_bid: float | None = None
    relevance: float | None = None         # platform relevance score, 0..1
    segment: str | None = None


@dataclass(frozen=True)
class Product:
    """Catalogue row: product -> brand, ownership and segment."""

    product_id: str
    brand: str | None = None
    is_own: bool = True
    segment: str | None = None
    title: str | None = None
    list_price: float | None = None
    first_sale_date: date | None = None
    is_sibling: bool = False               # belongs to another brand of the same advertiser


@dataclass(frozen=True)
class MarketRow:
    """Share-of-voice / market-view fact: targeting unit x brand x day.

    Optionally finer (x product x geo).  ``is_own`` flags the advertiser's
    brand.  Without this feed every share-of-voice lever is ``None``.
    """

    date: date
    targeting_unit: str
    brand: str
    is_own: bool
    ad_impressions: float = 0.0
    organic_impressions: float = 0.0
    total_impressions: float | None = None   # ad + organic when omitted
    is_sibling: bool = False
    product_id: str | None = None
    geo: str | None = None
    segment: str | None = None
    organic_position: float | None = None
    unit_role: str | None = None             # native keyword type if the feed has one

    @property
    def impressions(self) -> float:
        """Total impressions (``total_impressions`` or ad + organic)."""
        if self.total_impressions is not None:
            return self.total_impressions
        return self.ad_impressions + self.organic_impressions


@dataclass(frozen=True)
class OfftakeRow:
    """Market sales fact: segment x brand (x product x geo) x day, all brands.

    ``sales`` is total sell-out (not only ad-attributed) and may be an
    estimate.  Without whole-segment rows there is no market share.
    """

    date: date
    brand: str
    is_own: bool
    sales: float
    units: float = 0.0
    segment: str | None = None
    product_id: str | None = None
    geo: str | None = None
    is_sibling: bool = False
    list_value: float | None = None          # sales valued at list price (for discount depth)


@dataclass(frozen=True)
class ShelfRow:
    """Shelf observation: product x geo x day, own and competitor.

    ``availability`` = ``available_count / check_count`` (``None`` when there
    are no checks).  ``weight`` is an optional velocity weight (default 1).
    """

    date: date
    product_id: str
    geo: str | None = None
    available_count: float | None = None
    check_count: float | None = None
    price: float | None = None
    list_price: float | None = None
    is_own: bool = True
    brand: str | None = None
    segment: str | None = None
    weight: float = 1.0
    units: float | None = None
    sales: float | None = None
    is_active: bool = True

    @property
    def availability(self) -> float | None:
        """In-stock ratio in [0, 1], or ``None`` without observations."""
        if self.available_count is None or not self.check_count:
            return None
        return self.available_count / self.check_count

    @property
    def discount(self) -> float | None:
        """Promo depth ``1 - price / list_price``, or ``None``."""
        if self.price is None or not self.list_price:
            return None
        return 1.0 - self.price / self.list_price


@dataclass(frozen=True)
class BudgetDayRow:
    """Budget pacing fact: entity x day.

    ``exhausted_at_minute`` is minutes after local midnight at which the
    daily budget ran out (``None`` = did not exhaust).  ``live_minutes`` /
    ``expected_live_minutes`` describe serving time against the scheduled
    serving time (day-part aware).  All three are optional telemetry.
    """

    date: date
    entity_id: str
    daily_budget: float | None
    spend: float
    sales: float = 0.0
    impressions: float = 0.0
    exhausted_at_minute: float | None = None
    live_minutes: float | None = None
    expected_live_minutes: float | None = None
    platform_held: bool | None = None     # platform held the entity for budget on this day
    status: str | None = None


@dataclass(frozen=True)
class BudgetTelemetry:
    """Per-entity budget summary over a lookback window.

    Built from ``BudgetDayRow`` by the budget / action primitives.  ``is_bound`` puts the four common "budget-bound" definitions behind
    one policy object (``config.BudgetBoundPolicy``).
    """

    entity_id: str
    n_days: int = 0                          # days with a row
    avg_budget: float | None = None          # mean daily cap
    avg_spend: float | None = None           # mean daily spend
    spend: float = 0.0                       # window spend
    impressions: float = 0.0
    roas: float | None = None                # sum direct sales / sum spend
    roas_recent: float | None = None         # same, recent half of the window
    roas_sd: float | None = None             # sample sd of daily sales/spend
    roas_se: float | None = None             # sd / sqrt(n_days) when n_days >= 5
    roas_lo: float | None = None             # roas - 1.28 se
    roas_hi: float | None = None             # roas + 1.28 se
    exhaust_minute: float | None = None      # mean exhaust minute over exhausted days
    exhaust_days: int | None = None          # days with a non-null exhaust minute
    capped_days: int | None = None           # days with spend >= capped fraction of the cap
    held_days: int | None = None             # days the platform held the entity for budget
    active_days: int | None = None           # days with spend > 0
    budget_latest: float | None = None       # cap on the latest day
    budget_before_recent: float | None = None  # latest cap before the last 3 days
    sufficiency_3d: float | None = None      # mean served / expected live time, last 3 days
    ad_type: str | None = None

    @property
    def utilisation(self) -> float:
        """``100 * avg_spend / avg_budget`` (0 without a cap)."""
        if not self.avg_budget or self.avg_spend is None:
            return 0.0
        return 100.0 * self.avg_spend / self.avg_budget

    def is_bound(self, policy: Any = None, method: str | None = None) -> bool:
        """Decide whether the entity is constrained by its daily budget.

        ``policy`` is a ``config.BudgetBoundPolicy`` (default policy when
        ``None``); ``method`` overrides ``policy.method``:

        * ``"sufficiency"``: ``sufficiency_3d < policy.sufficiency_max``.
        * ``"exhaustion"``: mean exhaust minute ``< early_exhaust_minutes``
          and ``avg_spend >= min_spend_fraction * avg_budget`` and, when both
          counts exist, ``exhaust_days >= min_exhaust_day_share * n_days``.
        * ``"capped_days"``: ``capped_days >= capped_day_share * active_days``.
        * ``"held_days"``: ``held_days >= held_days_min``.
        * ``"auto"``: sufficiency when ``sufficiency_3d`` is known, otherwise
          exhaustion.

        No cap (or a zero cap) is never bound.  Returns a bool.
        """
        if policy is None:
            from .config import BudgetBoundPolicy
            policy = BudgetBoundPolicy()
        m = method or policy.method
        if not self.avg_budget or self.avg_budget <= 0:
            return False
        if m == "auto":
            m = "sufficiency" if self.sufficiency_3d is not None else "exhaustion"
        if m == "sufficiency":
            return self.sufficiency_3d is not None and self.sufficiency_3d < policy.sufficiency_max
        if m == "exhaustion":
            if self.exhaust_minute is None or self.exhaust_minute >= policy.early_exhaust_minutes:
                return False
            if (self.avg_spend or 0.0) < policy.min_spend_fraction * self.avg_budget:
                return False
            if self.exhaust_days is not None and self.n_days:
                return self.exhaust_days >= policy.min_exhaust_day_share * self.n_days
            return True
        if m == "capped_days":
            days = self.active_days if self.active_days is not None else self.n_days
            if self.capped_days is None or not days:
                return False
            return self.capped_days >= policy.capped_day_share * days
        if m == "held_days":
            return self.held_days is not None and self.held_days >= policy.held_days_min
        raise ValueError(f"unknown budget-bound method: {m!r}")


# --------------------------------------------------------------------------
# Optional operational feeds
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ChangeEvent:
    """One configuration change on an entity (optional change-log feed)."""

    timestamp: datetime
    entity_id: str
    field: str                       # "status" | "daily_budget" | "bid" ...
    old: Any = None
    new: Any = None
    source: str | None = None        # who / what made the change
    automated: bool = False          # made by an automation (excluded from regime detection)
    verified: bool | None = None
    targeting_unit: str | None = None


@dataclass(frozen=True)
class BidRecord:
    """Bid store row: one targeting unit inside one entity."""

    entity_id: str
    targeting_unit: str
    live_bid: float | None = None
    base_bid: float | None = None
    bid_id: str | None = None
    match_type: str | None = None
    status: str | None = None
    is_negative: bool = False
    is_archived: bool = False
    geo: str | None = None


@dataclass(frozen=True)
class BidDecision:
    """One decision of a position-targeting bidder (optional decision log)."""

    timestamp: datetime
    targeting_unit: str
    new_bid: float
    rule_id: str | None = None
    bid_id: str | None = None
    entity_id: str | None = None
    success: bool | None = None
    observed_position: float | None = None   # None / out of range = not found
    target_position_min: float | None = None
    target_position_max: float | None = None
    at_ceiling: bool | None = None           # bid was pinned at the guardrail ceiling
    organic_position: float | None = None
    geo: str | None = None
    brand: str | None = None                 # set only in pooled cross-advertiser logs


@dataclass(frozen=True)
class SuggestedBid:
    """Platform-suggested bid range for a targeting unit (optional)."""

    targeting_unit: str
    low: float | None = None
    high: float | None = None
    searches: float | None = None
    geo: str | None = None
    as_of: date | None = None


@dataclass(frozen=True)
class AppliedAction:
    """A previously applied action, for cooldowns and action locks.

    ``kind`` vocabulary (``APPLIED_KINDS``): ``"cut"`` (budget or keyword
    cut), ``"defend"`` (defense raise), ``"biddown"`` (bid decrease),
    ``"bid"`` (bid change; ``direction`` says which way), ``"budget"``,
    ``"experiment"``.  A family name is also accepted in ``kind`` for
    stores that record nothing finer; consumers match on ``kind`` first and
    ``family`` second.
    """

    kind: str                         # one of APPLIED_KINDS, or a family name
    entity_id: str | None
    applied_on: date
    card_id: str | None = None
    family: str | None = None
    direction: str | None = None


@dataclass(frozen=True)
class RoleCohort:
    """Percentiles of one targeting role's units (same account, cohort window).

    The one cohort record shared by the per-entity recommendation gates,
    the keyword recommenders and the cold-start pre-flight.  A cohort with
    ``n`` below the consumer's minimum (12 by default) must not be trusted.
    """

    n: int = 0
    roas_p10: float | None = None
    roas_p25: float | None = None
    roas_p50: float | None = None
    roas_p75: float | None = None
    cpnu_p25: float | None = None      # cost per new user
    cpnu_p50: float | None = None


@dataclass(frozen=True)
class RunCheck:
    """A run-level goal or check, e.g. "ROAS >= 200%" = ``RunCheck("roas", ">=", 200, "pct")``.

    The one check record shared by the rule families (which read the ROAS
    value as a floor) and the plan pipeline (which enforces it as a ROAS
    floor or a spend ceiling).
    """

    metric: str | None = None    # "roas" | "ad_spend" | "ad_sov" ...
    op: str | None = None        # ">", ">=", "<", "<="; None = the natural operator of the metric
    value: float | None = None
    mode: str = "abs"            # "abs" (a multiple / an amount) | "pct"


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Window:
    """Inclusive date range ``[start, end]``."""

    start: date
    end: date
    label: str = ""

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"window end {self.end} is before start {self.start}")

    @property
    def days(self) -> int:
        """Inclusive day count."""
        return (self.end - self.start).days + 1

    def contains(self, d: date) -> bool:
        """True when ``d`` lies in the window (both ends inclusive)."""
        return self.start <= d <= self.end

    def as_tuple(self) -> tuple[date, date]:
        """``(start, end)``."""
        return (self.start, self.end)


@dataclass(frozen=True)
class WindowPair:
    """A comparison: ``prior`` (baseline) versus ``current``.

    ``cmp`` names how the prior window was derived (``"trailing"``,
    ``"dod"``, ``"wow"``, ``"mom"``, ``"fallback"``); ``note`` carries any
    human-readable caveat (clamps, fallbacks) that a report must show.
    """

    prior: Window
    current: Window
    cmp: str = "trailing"
    note: str = ""

    @property
    def days(self) -> int:
        """Inclusive length of the current window."""
        return self.current.days

    def as_dict(self) -> dict[str, Any]:
        """Flat dict of the four window bounds (and the note)."""
        return {
            "cur_from": self.current.start, "cur_to": self.current.end,
            "pri_from": self.prior.start, "pri_to": self.prior.end,
            "days": self.current.days, "note": self.note, "cmp": self.cmp,
        }


# --------------------------------------------------------------------------
# Metric cell
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricCell:
    """Additive sums for one slice, with ratios always recomputed from sums.

    Raw sums: ``spend, impressions, sales, units`` plus the position
    accumulators ``pos_weight`` (sum of position x impressions) and
    ``pos_impressions`` (sum of impressions), both **only over rows whose
    position is inside the valid range**, and the bid accumulators
    ``bid_weight`` (sum of bid x spend) / ``bid_spend``.  ``entities`` is the
    count of distinct entities with spend > 0 (``None`` after combining
    cells) and ``days`` the count of distinct days.

    Ratios are ``None`` when their denominator is zero -- never 0, never an
    average of row-level ratios.
    """

    spend: float = 0.0
    impressions: float = 0.0
    sales: float = 0.0
    units: float = 0.0
    pos_weight: float = 0.0
    pos_impressions: float = 0.0
    bid_weight: float = 0.0
    bid_spend: float = 0.0
    clicks: float | None = None
    indirect_sales: float | None = None
    new_users: float | None = None
    entities: int | None = None
    days: int = 0

    @property
    def cpm(self) -> float | None:
        """``1000 * spend / impressions``."""
        return 1000.0 * self.spend / self.impressions if self.impressions > 0 else None

    @property
    def roas(self) -> float | None:
        """``sales / spend`` (direct)."""
        return self.sales / self.spend if self.spend > 0 else None

    @property
    def total_roas(self) -> float | None:
        """``(sales + indirect_sales) / spend``; equals ``roas`` without halo data."""
        if self.spend <= 0:
            return None
        return (self.sales + (self.indirect_sales or 0.0)) / self.spend

    @property
    def icr(self) -> float | None:
        """Impression conversion rate ``units / impressions``."""
        return self.units / self.impressions if self.impressions > 0 else None

    @property
    def icr1k(self) -> float | None:
        """``1000 * icr`` (display / modelling unit)."""
        v = self.icr
        return None if v is None else 1000.0 * v

    @property
    def asp(self) -> float | None:
        """Average selling price ``sales / units``."""
        return self.sales / self.units if self.units > 0 else None

    @property
    def rpm(self) -> float | None:
        """Revenue per mille ``1000 * sales / impressions``."""
        return 1000.0 * self.sales / self.impressions if self.impressions > 0 else None

    @property
    def position(self) -> float | None:
        """Impression-weighted mean position over valid-position rows."""
        return self.pos_weight / self.pos_impressions if self.pos_impressions > 0 else None

    @property
    def bid(self) -> float | None:
        """Spend-weighted mean bid over rows that carry a bid."""
        return self.bid_weight / self.bid_spend if self.bid_spend > 0 else None

    @property
    def ctr(self) -> float | None:
        """``clicks / impressions`` when clicks exist."""
        if self.clicks is None or self.impressions <= 0:
            return None
        return self.clicks / self.impressions

    @property
    def cvr(self) -> float | None:
        """``units / clicks`` when clicks exist."""
        if not self.clicks:
            return None
        return self.units / self.clicks

    def as_dict(self) -> dict[str, Any]:
        """Raw sums plus derived ratios as a plain dict."""
        return {
            "spend": self.spend, "impressions": self.impressions, "sales": self.sales,
            "units": self.units, "pos_weight": self.pos_weight,
            "pos_impressions": self.pos_impressions, "bid_weight": self.bid_weight,
            "bid_spend": self.bid_spend, "clicks": self.clicks,
            "indirect_sales": self.indirect_sales, "new_users": self.new_users,
            "entities": self.entities, "days": self.days,
            "cpm": self.cpm, "roas": self.roas, "total_roas": self.total_roas,
            "icr": self.icr, "asp": self.asp, "rpm": self.rpm,
            "position": self.position, "bid": self.bid,
        }


# --------------------------------------------------------------------------
# Action card
# --------------------------------------------------------------------------

@dataclass
class Change:
    """A sized move: percentage step and, for budgets, from / to daily values."""

    pct: float | None = None
    frm: float | None = None
    to: float | None = None
    pct_requested: float | None = None   # step asked for before a sizing gate trimmed it


@dataclass
class KeywordRef:
    """A targeting unit named on a card, with the numbers that selected it."""

    kw: str
    spend: float | None = None
    roas: float | None = None
    position: float | None = None
    bucket: str | None = None        # role bucket
    bench: float | None = None       # bucket benchmark ROAS
    volume: float | None = None      # search volume


@dataclass
class ActionCard:
    """A candidate action flowing from the rule families through the pipeline.

    The first block is written by the family that emits the card; the second
    block is filled in by the pipeline (conflict resolution, confidence, EV,
    waves); the third by the verdict spine and the action store.  The card is
    deliberately mutable: pipeline stages annotate it in place.

    ``stake`` is a currency amount rounded to 0 decimals; its unit differs by
    family (ad money at risk versus market opportunity) and ``ev_kind``
    records which.  ``estimator`` labels which money estimator produced the
    numbers (stake / fitted curve / finite-difference marginal), because the
    three are never reconciled.
    """

    # -- written by the rule family -----------------------------------
    id: str
    family: str
    type: str                                  # Lever value
    title: str
    segment: str | None = None
    entity_id: str | None = None               # None for account / segment-level cards
    direction: str = Direction.NONE.value
    change: Change | None = None
    keywords: list[KeywordRef] | None = None
    trigger: str = ""                          # why it fired, with the numbers
    targets: str = ""                          # what to do / success criterion / re-read window
    evidence: dict[str, Any] = field(default_factory=dict)
    stake: float = 0.0
    status: str = CardStatus.PENDING.value
    facts: str = ""                            # terse numeric string; the only numbers a wording layer may cite
    rule_profile: str | None = None            # "classic" | "marginal" | "verdict"
    # -- written by the pipeline --------------------------------------
    veto_reason: str | None = None
    superseded_by: str | None = None
    tension_group: str | None = None
    tension_with: str | None = None
    confidence: int | None = None              # 25..90
    conf_parts: list[tuple[str, float]] = field(default_factory=list)
    precedent: dict[str, Any] | None = None    # {n, won, line}
    est_day: float | None = None               # estimated currency / day
    p_success: float | None = None
    ev_kind: str | None = None                 # "money" | "opportunity"
    ev_day: float | None = None
    ev_density: float | None = None
    estimator: str | None = None               # which money estimator sized the card
    sov_headroom: float | None = None          # share-of-voice proxy after the move
    posture: str | None = None                 # market posture stamp
    kw_priority: int | None = None             # demand-priority rank among keyword cards
    lever_field: str | None = None             # concrete field the card edits (e.g. "daily_budget")
    wave: int | None = None
    coordination: str | None = None            # sibling-card note for the same entity
    entity_name: str | None = None
    entity_status: str | None = None
    caution: str | None = None
    goal_aligned: bool | None = None
    wording: str | None = None                 # optional polished prose (never a source of numbers)
    # -- written by the verdict spine / store -------------------------
    verdict: dict[str, Any] | None = None      # {state, why, lane}
    ladder: str | None = None
    applied_at: str | None = None
    skip_reason: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def is_actionable(self) -> bool:
        """True while the card is ``pending`` or ``tension``."""
        return self.status in ACTIONABLE_STATUSES

    def keyword_values(self) -> list[str]:
        """Targeting-unit strings named on the card."""
        return [k.kw for k in (self.keywords or [])]


def card_id(
    family: str,
    type: str,
    entity_id: str | None,
    segment: str | None,
    title: str,
    keywords: Iterable[str] | None = None,
) -> str:
    """Stable 12-hex card identity.

    ``sha1("family|type|entity|segment|title" [+ "|" + first 8 sorted
    keyword values joined by ","])[:12]``.  Keyword values are part of the
    identity so two keyword-grain cards on one entity never collide; anything
    else that varies per card (for example a geo) must be in the title.
    Stable across runs for the same inputs.
    """
    key = f"{family}|{type}|{entity_id}|{segment}|{title}"
    kws = sorted(str(k) for k in (keywords or []))[:8]
    if kws:
        key += "|" + ",".join(kws)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def make_card(
    family: str,
    type: str,
    title: str,
    *,
    segment: str | None = None,
    entity_id: str | None = None,
    direction: str = Direction.NONE.value,
    change: Change | Mapping[str, Any] | None = None,
    keywords: Sequence[KeywordRef | Mapping[str, Any] | str] | None = None,
    trigger: str = "",
    targets: str = "",
    evidence: Mapping[str, Any] | None = None,
    stake: float = 0.0,
    facts: str = "",
    rule_profile: str | None = None,
) -> ActionCard:
    """Build a pending ``ActionCard`` with its stable id.

    Accepts ``change`` as a ``Change`` or a ``{pct, frm, to}`` mapping and
    ``keywords`` as ``KeywordRef``s, mappings with a ``kw`` key, or bare
    strings.  ``stake`` is rounded to 0 decimals.  ``type`` and
    ``direction`` may be enum members or their string values.
    """
    type_s = type.value if isinstance(type, Enum) else str(type)
    dir_s = direction.value if isinstance(direction, Enum) else str(direction)
    refs: list[KeywordRef] | None = None
    if keywords:
        refs = []
        for k in keywords:
            if isinstance(k, KeywordRef):
                refs.append(k)
            elif isinstance(k, Mapping):
                refs.append(KeywordRef(**{f: k[f] for f in KeywordRef.__dataclass_fields__ if f in k}))
            else:
                refs.append(KeywordRef(kw=str(k)))
    chg: Change | None
    if change is None or isinstance(change, Change):
        chg = change
    else:
        chg = Change(**{f: change[f] for f in Change.__dataclass_fields__ if f in change})
    return ActionCard(
        id=card_id(family, type_s, entity_id, segment, title, [r.kw for r in refs] if refs else None),
        family=family, type=type_s, title=title, segment=segment, entity_id=entity_id,
        direction=dir_s, change=chg, keywords=refs, trigger=trigger, targets=targets,
        evidence=dict(evidence or {}), stake=float(round(stake or 0.0)),
        status=CardStatus.PENDING.value, facts=facts, rule_profile=rule_profile,
    )
