"""Shared primitives of the action rule families.

A rule family is a pure function ``ActionContext -> list[ActionCard]``.  This
module defines the context (plain window-aggregated records), the helpers
every family uses (effective breakeven, liveness, capability playbook,
decision ROAS, run check and floors, keyword buckets and benchmark, the
shared keyword threshold, campaign role, keyword demand flags, the
budget-bound test, cooldown register) and ``context_from_dataset``, which
aggregates raw adapter feeds into a context.

Two rule profiles are supported: ``"classic"`` (absolute bands relative to
breakeven) and ``"verdict"`` (one relative threshold per keyword bucket, a
band ladder, a sufficiency test).  ``"marginal"`` is ``"classic"`` plus a
marginal-ROAS gate applied later in the plan pipeline, so the families treat
it as classic.

Nothing here performs I/O.  Optional enrichments that need a provider in a
live system (keyword suggestions, per-geo head-term CPM) are passed in as
data; when absent the dependent rule abstains.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from ..adapters.base import Dataset, PlatformProfile
from ..budget.telemetry import utilisation
from ..config import RULE_PROFILES, AvailabilityPolicy, BudgetBoundPolicy, Economics
from ..liveness import is_dead
from ..metrics import fmt_money as _fmt_money
from ..schema import (ActionCard, AppliedAction, BudgetTelemetry, CapabilityClass, CardStatus,
                      KeywordRef, RunCheck, TargetingRole, WindowPair, make_card)
from .config import ActionsConfig

__all__ = [
    "BUCKET_BRANDED", "BUCKET_GENERIC", "BUCKET_COMPETITION", "BUCKET_OF_ROLE", "FLOOR_ALIASES",
    "CampaignStat", "KeywordStat", "KeywordMarketStat", "MarketPlayer", "SegmentMarket",
    "EntityGeoStat", "GeoMarketStat", "CollisionRow", "RunCheck", "KeywordDemand", "ActionContext",
    "money", "fmt_money", "fmt_roas", "bucket_of", "is_conquest", "is_branded",
    "campaign_is_dead", "capability_class", "allowed_card_types", "apply_playbook",
    "deflator", "decision_roas", "live_campaigns", "live_campaign_ids", "live_keywords", "entity_keywords", "bucket_average",
    "segments_in_scope", "check_roas", "roas_floor", "keyword_bench", "keyword_threshold",
    "parse_floors", "campaign_role", "keyword_demand", "budget_bound", "utilisation",
    "is_aggregate_geo", "cooldown_register", "raise_pct_from_exhaust",
    "raise_pct_from_sufficiency", "proportional_step", "new_card", "top_keywords",
    "context_from_dataset",
]

BUCKET_BRANDED = "branded"
BUCKET_GENERIC = "generic"
BUCKET_COMPETITION = "competition"

# Keyword role -> benchmark bucket.  A missing or unknown role is generic.
BUCKET_OF_ROLE: dict[str, str] = {
    TargetingRole.OWN_BRAND.value: BUCKET_BRANDED,
    TargetingRole.SIBLING_BRAND.value: BUCKET_BRANDED,
    TargetingRole.COMPETITOR.value: BUCKET_COMPETITION,
    TargetingRole.GENERIC.value: BUCKET_GENERIC,
}

# Words an operator may use when stating floors in free text -> floor key.
FLOOR_ALIASES: dict[str, str] = {
    "branded": BUCKET_BRANDED, "brand": BUCKET_BRANDED, "own brand": BUCKET_BRANDED,
    "own-brand": BUCKET_BRANDED,
    "generic": BUCKET_GENERIC, "generics": BUCKET_GENERIC, "category": BUCKET_GENERIC,
    "competition": BUCKET_COMPETITION, "competitor": BUCKET_COMPETITION, "comp": BUCKET_COMPETITION,
    "conquest": BUCKET_COMPETITION,
    "retargeting": CapabilityClass.RETARGETING.value, "placement": CapabilityClass.PLACEMENT.value,
    "reach": CapabilityClass.REACH.value, "keyword": CapabilityClass.KEYWORD_AUCTION.value,
}


# --------------------------------------------------------------------------
# Input records (window aggregates; all plain data)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class CampaignStat:
    """One campaign over the lookback window.

    ``roas_primary`` is the platform's headline (total) ROAS, ``roas_direct``
    the direct one; either may be ``None`` ("cannot judge", never 0).
    ``own_brand_share`` is the own-brand share of keyword spend (0..1) or
    ``None`` when unknown.  ``is_pure`` ``None`` is treated as pure.
    """

    entity_id: str
    segment: str | None = None
    status: str | None = None
    ad_type: str | None = None
    spend: float = 0.0
    spend_7d: float = 0.0
    spend_3d: float = 0.0
    roas_primary: float | None = None
    roas_direct: float | None = None
    is_pure: bool | None = True
    own_brand_share: float | None = None
    name: str | None = None


@dataclass(frozen=True)
class KeywordStat:
    """One keyword x campaign row over the window."""

    keyword: str
    entity_id: str
    spend: float = 0.0
    roas: float | None = None
    role: str | None = None
    segment: str | None = None
    new_users: float | None = None
    search_volume: float | None = None
    position: float | None = None


@dataclass(frozen=True)
class KeywordMarketStat:
    """One keyword x segment row of the account-wide keyword table.

    ``position_mode`` is the recent impression-weighted modal position when
    the caller has it; ``rival_owner_share`` is the largest single brand's
    share of ad impressions on the term (0..1) and ``rival_owner_is_own``
    says whether that brand is the advertiser's own.
    """

    keyword: str
    segment: str | None = None
    spend: float = 0.0
    direct_roas: float | None = None
    position: float | None = None
    cpm: float | None = None
    position_mode: float | None = None
    live_bid: float | None = None
    rival_owner_share: float | None = None
    rival_owner_is_own: bool | None = None
    role: str | None = None


@dataclass(frozen=True)
class MarketPlayer:
    """A brand's share of segment sales in both windows (percent)."""

    brand: str
    is_own: bool = False
    is_sibling: bool = False
    share_prior: float = 0.0
    share_current: float = 0.0

    @property
    def delta_pp(self) -> float:
        return self.share_current - self.share_prior


@dataclass(frozen=True)
class SegmentMarket:
    """Segment market totals for both windows plus the own ad share of voice."""

    segment: str
    total_sales_current: float | None = None
    total_sales_prior: float | None = None
    own_sales_current: float | None = None
    own_sales_prior: float | None = None
    players: tuple[MarketPlayer, ...] = ()
    sov_current: float | None = None      # own ad impressions / all ad impressions, percent
    availability: float | None = None     # 0..1

    @property
    def has_both_windows(self) -> bool:
        return bool(self.total_sales_current and self.total_sales_prior
                    and self.own_sales_current is not None and self.own_sales_prior is not None)

    @property
    def growth_pct(self) -> float | None:
        """``100 * (total_current / total_prior - 1)``."""
        if not self.has_both_windows:
            return None
        return 100.0 * (self.total_sales_current / self.total_sales_prior - 1.0)  # type: ignore[operator]

    @property
    def market_share_pct(self) -> float | None:
        """``100 * own_current / total_current``."""
        if not self.total_sales_current or self.own_sales_current is None:
            return None
        return 100.0 * self.own_sales_current / self.total_sales_current

    @property
    def share_delta_pp(self) -> float | None:
        """Own share change in percentage points, current minus prior."""
        if not self.has_both_windows:
            return None
        return (100.0 * self.own_sales_current / self.total_sales_current  # type: ignore[operator]
                - 100.0 * self.own_sales_prior / self.total_sales_prior)  # type: ignore[operator]


@dataclass(frozen=True)
class EntityGeoStat:
    """Campaign x geo spend and direct sales over the window."""

    entity_id: str
    geo: str
    spend: float = 0.0
    sales: float = 0.0


@dataclass(frozen=True)
class GeoMarketStat:
    """Account x geo market view: segment sales, own share (percent), availability (0..1)."""

    geo: str
    total_sales: float = 0.0
    own_share_pct: float = 0.0
    availability: float | None = None


@dataclass(frozen=True)
class CollisionRow:
    """Keyword x campaign x geo spend and sales, with the advertised product ids when known."""

    keyword: str
    entity_id: str
    geo: str | None = None
    spend: float = 0.0
    sales: float = 0.0
    product_ids: frozenset[str] | None = None
    segment: str | None = None


@dataclass(frozen=True)
class KeywordDemand:
    """Verdict-profile keyword demand flags (all empty under the classic profile)."""

    new_user_top: frozenset[tuple[str, str | None]] = frozenset()
    volume_top: frozenset[tuple[str, str | None]] = frozenset()
    priority: Mapping[tuple[str | None, str], int] = field(default_factory=dict)
    bucket: Mapping[tuple[str, str | None], str] = field(default_factory=dict)


@dataclass
class ActionContext:
    """Everything a rule family reads.  Build by hand or with ``context_from_dataset``.

    * ``recent_applied``: cooldown register, a set of ``(kind, entity_id)``
      with kinds ``"cut"``, ``"defend"``, ``"biddown"`` (see ``cooldown_register``).
    * ``head_cpm_by_geo``: ``{keyword: {geo: (cpm_current, cpm_prior)}}``;
      ``None`` disables the head-term pressure rule.
    * ``keyword_suggestions``: ``{entity_id or segment: [ {kw, volume} | str ]}``
      from an external recommender; ``None`` disables add-keyword cards.
    * ``operator_floors``: ``{bucket: ROAS floor}`` stated by the operator.
    * ``scoped``: the run is restricted to a user-selected campaign subset.
    """

    campaigns: Sequence[CampaignStat] = ()
    keywords: Sequence[KeywordStat] = ()
    keyword_market: Sequence[KeywordMarketStat] = ()
    telemetry: Mapping[str, BudgetTelemetry] = field(default_factory=dict)
    segments: Mapping[str, SegmentMarket] = field(default_factory=dict)
    entity_geo: Sequence[EntityGeoStat] = ()
    geo_market: Sequence[GeoMarketStat] = ()
    collisions: Sequence[CollisionRow] = ()
    recent_applied: frozenset[tuple[str, str]] = frozenset()
    head_cpm_by_geo: Mapping[str, Mapping[str, tuple[float | None, float | None]]] | None = None
    keyword_suggestions: Mapping[str, Sequence[Any]] | None = None
    economics: Economics = field(default_factory=Economics)
    profile: PlatformProfile = field(default_factory=PlatformProfile)
    cfg: ActionsConfig = field(default_factory=ActionsConfig)
    budget_policy: BudgetBoundPolicy = field(default_factory=BudgetBoundPolicy)
    availability_policy: AvailabilityPolicy = field(default_factory=AvailabilityPolicy)
    rule_profile: str = "classic"
    check: RunCheck | None = None
    operator_floors: Mapping[str, float] = field(default_factory=dict)
    scoped: bool = False
    selected_segments: frozenset[str] | None = None
    _memo: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.rule_profile not in RULE_PROFILES:
            raise ValueError(f"unknown rule profile: {self.rule_profile!r}")

    @property
    def verdict(self) -> bool:
        """True under the ``"verdict"`` rule profile."""
        return self.rule_profile == "verdict"

    @property
    def be(self) -> float:
        """Effective breakeven ROAS: ``max(breakeven / max(ltv_mult, 0.1), 0.1)``."""
        return self.economics.be

    def campaign(self, entity_id: str | None) -> CampaignStat | None:
        idx = self._memo.get("camp_idx")
        if idx is None:
            idx = self._memo["camp_idx"] = {c.entity_id: c for c in self.campaigns}
        return idx.get(entity_id)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def money(ctx: ActionContext, reference_amount: float) -> float:
    """Scale a reference-currency threshold into account currency (``Economics.money``)."""
    return ctx.economics.money(reference_amount)


def fmt_money(value: float | None) -> str:
    """Neutral money formatter (``metrics.fmt_money``): thousands separators, magnitude-aware decimals."""
    return _fmt_money(value)


def fmt_roas(value: float | None) -> str:
    """ROAS as a multiple, e.g. ``2.35x``."""
    return "n/a" if value is None else f"{value:.2f}x"


def bucket_of(role: str | None) -> str:
    """Benchmark bucket of a keyword role: own/sibling -> branded, competitor -> competition, else generic."""
    return BUCKET_OF_ROLE.get(str(getattr(role, "value", role) or ""), BUCKET_GENERIC)


def is_conquest(role: str | None) -> bool:
    return bucket_of(role) == BUCKET_COMPETITION


def is_branded(role: str | None) -> bool:
    return bucket_of(role) == BUCKET_BRANDED


def is_aggregate_geo(ctx: ActionContext, geo: str | None) -> bool:
    """True for long-tail roll-up labels: never a treatment, control, carve, expand or collision geo."""
    return ctx.profile.is_aggregate_geo(geo)


# --------------------------------------------------------------------------
# Liveness, capability, decision ROAS
# --------------------------------------------------------------------------

def campaign_is_dead(ctx: ActionContext, campaign: CampaignStat) -> bool:
    """Single liveness rule: hard-dead status, or soft-dead status with no spend in the last 3 days."""
    return is_dead(campaign.status, campaign.spend_3d, status_map=ctx.profile.status_map)


def capability_class(ctx: ActionContext, ad_type: str | None) -> str:
    """Capability class of an ad type per the platform profile (unknown types take the profile default)."""
    return ctx.profile.capability(ad_type).capability_class


def allowed_card_types(ctx: ActionContext, ad_type: str | None) -> frozenset[str]:
    """Card types (levers) the playbook allows on an entity of this ad type."""
    return ctx.profile.allowed_levers(ad_type)


def apply_playbook(cards: Iterable[ActionCard], ctx: ActionContext) -> list[ActionCard]:
    """Capability veto: a pending card whose type the campaign's ad type cannot pull becomes ``vetoed``.

    Runs before every other gate.  Cards without an entity, or on an entity
    the context does not know, are left alone.  Mutates and returns the cards.
    """
    out = list(cards)
    for card in out:
        if card.status != CardStatus.PENDING.value or not card.entity_id:
            continue
        camp = ctx.campaign(card.entity_id)
        if camp is None:
            continue
        if card.type not in allowed_card_types(ctx, camp.ad_type):
            card.status = CardStatus.VETOED.value
            card.veto_reason = (f"{capability_class(ctx, camp.ad_type)} ad type has no "
                                f"{card.type} lever")
    return out


def deflator(ctx: ActionContext, campaign: CampaignStat) -> float:
    """Attribution deflator of the campaign's ad type (1.0 unless the adapter states otherwise)."""
    return ctx.profile.capability(campaign.ad_type).roas_deflator


def decision_roas(ctx: ActionContext, campaign: CampaignStat) -> float | None:
    """ROAS used for decisions: ``(roas_primary if known else roas_direct) x deflator``.

    ``None`` when the campaign has no attribution (every rule then abstains)
    or its ad type has no attributed sales by construction.
    """
    cap = ctx.profile.capability(campaign.ad_type)
    if not cap.sales_attributed:
        return None
    raw = campaign.roas_primary if campaign.roas_primary is not None else campaign.roas_direct
    return None if raw is None else raw * cap.roas_deflator


def live_campaigns(ctx: ActionContext, segment: str | None) -> list[CampaignStat]:
    """Campaigns of the segment that are not dead and are pure (all advertised products in the segment)."""
    return [c for c in ctx.campaigns
            if c.segment == segment and c.is_pure is not False and not campaign_is_dead(ctx, c)]


def live_campaign_ids(ctx: ActionContext) -> frozenset[str]:
    """Ids of every campaign that is not dead (memoised)."""
    ids = ctx._memo.get("live_ids")
    if ids is None:
        ids = ctx._memo["live_ids"] = frozenset(
            c.entity_id for c in ctx.campaigns if not campaign_is_dead(ctx, c))
    return ids


def live_keywords(ctx: ActionContext) -> list[KeywordStat]:
    """Keyword rows whose campaign is known and not dead (dead campaigns' rows never reach a rule)."""
    rows = ctx._memo.get("live_kw")
    if rows is None:
        live = live_campaign_ids(ctx)
        rows = ctx._memo["live_kw"] = [k for k in ctx.keywords if k.entity_id in live]
    return rows


def _keywords_by_entity(ctx: ActionContext) -> dict[str, list[KeywordStat]]:
    idx = ctx._memo.get("kw_by_entity")
    if idx is None:
        idx = {}
        for k in live_keywords(ctx):
            idx.setdefault(k.entity_id, []).append(k)
        ctx._memo["kw_by_entity"] = idx
    return idx


def entity_keywords(ctx: ActionContext, entity_id: str) -> list[KeywordStat]:
    """Live keyword rows of one campaign."""
    return _keywords_by_entity(ctx).get(entity_id, [])


def top_keywords(ctx: ActionContext, entity_id: str, n: int) -> list[KeywordRef]:
    """The campaign's top-``n`` keywords by spend as card references."""
    rows = sorted(entity_keywords(ctx, entity_id), key=lambda k: (-k.spend, k.keyword))[:n]
    return [KeywordRef(kw=k.keyword, spend=k.spend, roas=k.roas, bucket=bucket_of(k.role)) for k in rows]


def segments_in_scope(ctx: ActionContext) -> list[str | None]:
    """Segments analysed this run: the selection (if any), then the top ``max_segments`` by campaign spend."""
    spend: dict[str | None, float] = {}
    for c in ctx.campaigns:
        if ctx.selected_segments is not None and c.segment not in ctx.selected_segments:
            continue
        spend[c.segment] = spend.get(c.segment, 0.0) + (c.spend or 0.0)
    order = sorted(spend, key=lambda s: (-spend[s], str(s)))
    return order[: ctx.cfg.max_segments]


# --------------------------------------------------------------------------
# Run check, floors, benchmark, threshold
# --------------------------------------------------------------------------

def check_roas(check: RunCheck | None) -> float | None:
    """The run's ROAS check as a multiple: ``value / 100`` in ``pct`` mode, else ``value``.

    Only ``roas`` / ``direct_roas`` checks with a value count; anything else is ``None``.
    """
    if check is None or check.value is None or check.metric not in ("roas", "direct_roas"):
        return None
    return check.value / 100.0 if check.mode == "pct" else float(check.value)


def roas_floor(ctx: ActionContext, role: str | None) -> float:
    """Classic floor: ``(check if set else breakeven) x 3`` for own/sibling-brand terms, ``x 1`` otherwise."""
    chk = check_roas(ctx.check)
    base = chk if chk is not None else ctx.be
    return base * (ctx.cfg.brand_floor_mult if is_branded(role) else 1.0)


def keyword_bench(ctx: ActionContext) -> dict[str, tuple[float, float]]:
    """Spend-weighted ROAS per keyword bucket: ``{bucket: (sum(spend x roas) / sum(spend), sum(spend))}``.

    Computed over live keyword rows with a known ROAS, per bucket and for
    ``"_all"``.  A bucket is kept only with positive spend and at least
    ``min_n`` rows, where ``min_n = scoped_bench_min_n`` under the verdict
    profile on a scoped run (thin cohorts abstain) and 1 otherwise.
    """
    bench = ctx._memo.get("bench")
    if bench is not None:
        return bench
    min_n = ctx.cfg.scoped_bench_min_n if (ctx.verdict and ctx.scoped) else 1
    acc: dict[str, list[float]] = {}
    for k in live_keywords(ctx):
        if k.roas is None:
            continue
        for b in (bucket_of(k.role), "_all"):
            a = acc.setdefault(b, [0.0, 0.0, 0.0])
            a[0] += k.spend * k.roas
            a[1] += k.spend
            a[2] += 1
    bench = {b: (a[0] / a[1], a[1]) for b, a in acc.items() if a[1] > 0 and a[2] >= min_n}
    ctx._memo["bench"] = bench
    return bench


def bucket_average(ctx: ActionContext, bucket: str) -> float | None:
    """Benchmark ROAS of a bucket, or ``None`` when the bucket abstains."""
    entry = keyword_bench(ctx).get(bucket)
    return entry[0] if entry else None


def keyword_threshold(ctx: ActionContext, role: str | None, bucket_avg: float | None) -> float:
    """The one bar a keyword (or keyword-led campaign) is judged against under the verdict profile.

    ``fl = check if set else be``; ``pivot = operator floor for the bucket if
    stated else (bucket_avg or fl)``; ``thr = min(fl, pivot)`` when a check is
    set else ``pivot``; result ``max(thr, be)``.  Bids, pauses and budgets all
    use this bar, there is no brand multiplier, and breakeven is a hard floor
    so a uniformly losing bucket cannot bless itself.
    """
    chk = check_roas(ctx.check)
    fl = chk if chk is not None else ctx.be
    stated = ctx.operator_floors.get(bucket_of(role)) if ctx.operator_floors else None
    pivot = stated if stated else (bucket_avg or fl)
    thr = min(fl, pivot) if chk is not None else pivot
    return max(thr, ctx.be)


_NUM = r"(\d+(?:\.\d+)?)\s*x?"


def parse_floors(text: str | None, cfg: ActionsConfig | None = None) -> dict[str, float]:
    """Parse operator floors from free text such as ``"branded 4x, generic 3, comp: 2.5"``.

    Pattern: an alias from ``FLOOR_ALIASES``, up to 28 characters that are
    neither digits nor newlines, a number, an optional ``x``.  Values outside
    ``[operator_floor_min, operator_floor_max]`` are dropped.  Returns
    ``{floor key: ROAS}``; the first mention of a key wins.
    """
    cfg = cfg or ActionsConfig()
    out: dict[str, float] = {}
    if not text:
        return out
    low = text.lower()
    found: list[tuple[int, str, float]] = []
    for alias in sorted(FLOOR_ALIASES, key=len, reverse=True):
        for m in re.finditer(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])[^\d\n]{0,28}?" + _NUM, low):
            found.append((m.start(), FLOOR_ALIASES[alias], float(m.group(1))))
    for _, key, val in sorted(found):
        if cfg.operator_floor_min <= val <= cfg.operator_floor_max:
            out.setdefault(key, val)
    return out


def campaign_role(ctx: ActionContext, entity_id: str) -> tuple[str | None, float]:
    """Spend-weighted majority keyword role of a campaign -> ``(role, share of keyword spend)``.

    ``(None, 0.0)`` when the campaign has no keyword rows.
    """
    rows = entity_keywords(ctx, entity_id)
    if not rows:
        return None, 0.0
    by_role: dict[str, float] = {}
    for k in rows:
        role = str(getattr(k.role, "value", k.role) or TargetingRole.GENERIC.value)
        by_role[role] = by_role.get(role, 0.0) + k.spend
    total = sum(by_role.values())
    role = sorted(by_role, key=lambda r: (-by_role[r], r))[0]
    return role, (by_role[role] / total if total > 0 else 0.0)


def keyword_demand(ctx: ActionContext) -> KeywordDemand:
    """Keyword demand flags per (segment, bucket), verdict profile only.

    Over the ``n`` distinct keywords of each group: ``new_user_top`` = the
    first ``ceil(n x 0.25)`` keywords with new users > 0, most first (never
    bid-cut); ``volume_top`` = the first ``ceil(n x 0.10)`` with search
    volume > 0 (wider dead-zone); ``priority[(segment, bucket)]`` ranks the
    buckets of a segment by total search volume (0 = highest).  The classic
    profile gets empty sets.
    """
    cached = ctx._memo.get("demand")
    if cached is not None:
        return cached
    if not ctx.verdict:
        ctx._memo["demand"] = KeywordDemand()
        return ctx._memo["demand"]
    seg_of = {c.entity_id: c.segment for c in ctx.campaigns}
    stats: dict[tuple[str, str | None], list[Any]] = {}
    for k in live_keywords(ctx):
        seg = k.segment if k.segment is not None else seg_of.get(k.entity_id)
        s = stats.setdefault((k.keyword, seg), [0.0, 0.0, bucket_of(k.role)])
        s[0] += k.new_users or 0.0
        s[1] = max(s[1], k.search_volume or 0.0)
    groups: dict[tuple[str | None, str], list[tuple[str, float, float]]] = {}
    for (kw, seg), (nu, vol, bucket) in stats.items():
        groups.setdefault((seg, bucket), []).append((kw, nu, vol))
    nu_top: set[tuple[str, str | None]] = set()
    sv_top: set[tuple[str, str | None]] = set()
    vol_total: dict[tuple[str | None, str], float] = {}
    for (seg, bucket), items in groups.items():
        n = len(items)
        by_nu = sorted((i for i in items if i[1] > 0), key=lambda i: (-i[1], i[0]))
        by_sv = sorted((i for i in items if i[2] > 0), key=lambda i: (-i[2], i[0]))
        nu_top.update((i[0], seg) for i in by_nu[: math.ceil(n * ctx.cfg.new_user_top_share)])
        sv_top.update((i[0], seg) for i in by_sv[: math.ceil(n * ctx.cfg.volume_top_share)])
        vol_total[(seg, bucket)] = sum(i[2] for i in items)
    priority: dict[tuple[str | None, str], int] = {}
    for seg in {s for s, _ in vol_total}:
        ranked = sorted((b for s, b in vol_total if s == seg), key=lambda b: (-vol_total[(seg, b)], b))
        for i, b in enumerate(ranked):
            priority[(seg, b)] = i
    demand = KeywordDemand(frozenset(nu_top), frozenset(sv_top), priority,
                           {key: v[2] for key, v in stats.items()})
    ctx._memo["demand"] = demand
    return demand


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------

def budget_bound(ctx: ActionContext, telemetry: BudgetTelemetry | None) -> bool:
    """Is the campaign constrained by its daily budget, under the run's rule profile?

    No telemetry or no cap -> not bound.  Classic: mean exhaust minute before
    the early-exhaust cut-off, mean spend at least half the cap, and
    exhaustion on at least 40% of days.  Verdict: served under 90% of the
    expected live time over the last 3 days; when the platform reports no
    served-time telemetry the verdict profile falls back to the classic test.
    """
    if telemetry is None:
        return False
    return telemetry.is_bound(ctx.budget_policy, "auto" if ctx.verdict else "exhaustion")


def _clamp_pct(ctx: ActionContext, raw: float) -> int:
    return int(min(ctx.cfg.scale_pct_max, max(ctx.cfg.scale_pct_min, round(raw))))


def raise_pct_from_exhaust(ctx: ActionContext, exhaust_minute: float) -> int:
    """Raise that funds the campaign to the funding-target hour of the local day.

    ``pct = clamp(round(100 x max(0, target_hour - exhaust_hour) / exhaust_hour x haircut), 5, 50)``.
    """
    hour = exhaust_minute / 60.0
    if hour <= 0:
        return int(ctx.cfg.scale_pct_max)
    target = ctx.profile.funding_target_hour
    return _clamp_pct(ctx, 100.0 * max(0.0, target - hour) / hour * ctx.cfg.scale_haircut)


def raise_pct_from_sufficiency(ctx: ActionContext, sufficiency: float | None) -> int:
    """Raise that would restore full serving: ``need = max(0, 1 / max(suff, 0.05) - 1)``;
    ``pct = clamp(round(100 x need x haircut), 5, 50)``.  Unknown sufficiency reads as 1 (minimum step).
    """
    suff = sufficiency or 1.0
    need = max(0.0, 1.0 / max(suff, ctx.cfg.sufficiency_floor) - 1.0)
    return _clamp_pct(ctx, 100.0 * need * ctx.cfg.scale_haircut)


def proportional_step(ctx: ActionContext, needed: float) -> int:
    """Cut step proportional to the shortfall: ``round(clamp(needed, 0.10, 0.30) x 100)`` percent."""
    return int(round(max(ctx.cfg.cut_step_min, min(needed, ctx.cfg.cut_step_max)) * 100))


def cooldown_register(
    applied: Iterable[AppliedAction],
    today: date,
    cfg: ActionsConfig | None = None,
) -> frozenset[tuple[str, str]]:
    """Build the cooldown register from the applied-action history.

    Keeps ``("cut", entity)`` applied less than ``cut_cooldown_days`` ago,
    ``("defend", entity)`` less than ``defend_cooldown_days`` ago and
    ``("biddown", entity)`` less than ``bid_probe_days`` ago.  Other kinds and
    actions without an entity are ignored.

    The cooldown kind is taken from ``kind`` when it already is one, else
    derived from what a card store records (card type in ``kind``, plus
    ``family`` and ``direction``), see :func:`cooldown_kind`.  That is what
    lets ``ActionLedger.applied_actions()`` arm the cooldowns directly.
    """
    cfg = cfg or ActionsConfig()
    windows = {"cut": cfg.cut_cooldown_days, "defend": cfg.defend_cooldown_days,
               "biddown": cfg.bid_probe_days}
    out: set[tuple[str, str]] = set()
    for a in applied:
        kind = cooldown_kind(a)
        days = windows.get(kind)
        if days is None or not a.entity_id:
            continue
        age = (today - a.applied_on).days
        if 0 <= age < days:
            out.add((kind, a.entity_id))
    return frozenset(out)


def cooldown_kind(action: AppliedAction) -> str | None:
    """Map an applied action to its cooldown kind (``"cut"``, ``"defend"``, ``"biddown"``) or ``None``.

    * ``kind`` already one of the three -> itself;
    * a bid card (``kind == "bid"``) with ``direction == "down"`` -> ``"biddown"``;
    * family ``efficiency-cut`` with a budget or keyword card going down
      (or no finer record than the family) -> ``"cut"``;
    * family ``defense`` with a budget card going up (or no finer record) -> ``"defend"``.
    """
    kind, family, direction = action.kind, action.family, action.direction
    if kind in ("cut", "defend", "biddown"):
        return kind
    if kind == "bid":
        return "biddown" if direction == "down" else None
    fam = family or kind
    if fam == "efficiency-cut" and kind in ("budget", "keyword", "efficiency-cut") and direction in ("down", None):
        return "cut"
    if fam == "defense" and kind in ("budget", "defense") and direction in ("up", None):
        return "defend"
    return None


def new_card(ctx: ActionContext, family: str, type: str, title: str, **kwargs: Any) -> ActionCard:
    """``schema.make_card`` plus the run's rule profile and the campaign's name and status."""
    card = make_card(family, type, title, rule_profile=ctx.rule_profile, **kwargs)
    camp = ctx.campaign(card.entity_id)
    if camp is not None:
        card.entity_name = camp.name
        card.entity_status = camp.status
    return card


# --------------------------------------------------------------------------
# Context from raw feeds
# --------------------------------------------------------------------------

def _roas_of(sales: float, spend: float) -> float | None:
    return sales / spend if spend > 0 else None


def context_from_dataset(
    dataset: Dataset,
    windows: WindowPair,
    *,
    economics: Economics | None = None,
    cfg: ActionsConfig | None = None,
    rule_profile: str = "classic",
    check: RunCheck | None = None,
    operator_floors: Mapping[str, float] | None = None,
    applied: Iterable[AppliedAction] = (),
    selected_entities: Iterable[str] | None = None,
    selected_segments: Iterable[str] | None = None,
    keyword_roles: Mapping[str, str] | None = None,
    keyword_suggestions: Mapping[str, Sequence[Any]] | None = None,
    telemetry: Mapping[str, BudgetTelemetry] | None = None,
    budget_policy: BudgetBoundPolicy | None = None,
    indirect_weight: float = 1.0,
) -> ActionContext:
    """Aggregate adapter feeds into an ``ActionContext`` for ``windows.current`` (vs ``windows.prior``).

    * campaigns: window spend, last-7 and last-3-day spend (anchored on the
      window end), primary ROAS = (sales + ``indirect_weight`` x indirect
      sales) / spend (weight 1 = total ROAS, 0 = direct only), direct
      ROAS = sales / spend, own-brand share of keyword spend.
    * keywords: keyword x campaign spend / ROAS / new users, role from
      ``keyword_roles`` then the targeting-unit feed, else generic.
    * keyword market: keyword x segment spend, direct ROAS, mean position,
      CPM, 7-day impression-weighted modal position, live bid, and the
      largest brand's ad-impression share from the market feed.
    * telemetry: ``budget.build_budget_telemetry`` over the same window
      unless supplied.
    * segments: sales totals and brand shares from the sell-out feed, own
      ad share of voice from the market feed, availability from the shelf feed.
    * geo: campaign x geo, account x geo market view, keyword x campaign x geo
      collision rows with product sets, per-geo head-term CPM in both windows.

    Optional feeds that are empty simply leave their part of the context
    empty, and the dependent families abstain.  Returns the context.
    """
    cfg = cfg or ActionsConfig()
    economics = (economics or Economics()).resolved(dataset.profile.currency_unit)
    profile = dataset.profile
    cur, pri = windows.current, windows.prior
    end = cur.end
    sel = frozenset(selected_entities) if selected_entities is not None else None
    ents = {e.entity_id: e for e in dataset.entities}
    roles: dict[Any, str] = {}
    volume: dict[str, float] = {}
    bids: dict[tuple[str, str | None], float] = {}
    for u in dataset.targeting_units:
        roles.setdefault((u.value, u.entity_id), u.role)
        roles.setdefault(u.value, u.role)
        if u.search_volume is not None:
            volume[u.value] = max(volume.get(u.value, 0.0), u.search_volume)
        if u.live_bid is not None:
            bids[(u.value, u.entity_id)] = u.live_bid
    if keyword_roles:
        roles.update(keyword_roles)

    camp: dict[str, list[float]] = {}        # spend, sales, indirect, has_indirect, s7, s3
    kw: dict[tuple[str, str], list[float]] = {}          # spend, sales, indirect, new_users, has_nu
    kwm: dict[tuple[str, str | None], list[float]] = {}  # spend, sales, impr, pos_w, pos_impr
    mode: dict[tuple[str, str | None], dict[int, float]] = {}
    egeo: dict[tuple[str, str], list[float]] = {}
    coll: dict[tuple[str, str, str | None], list[Any]] = {}
    cpm_geo: dict[tuple[str, str], list[float]] = {}     # cur spend, cur impr, pri spend, pri impr
    seg_seen: dict[str, str | None] = {}
    last7, last3 = end - timedelta(days=6), end - timedelta(days=2)
    pos = profile.position
    for r in dataset.perf:
        if sel is not None and r.entity_id not in sel:
            continue
        ent = ents.get(r.entity_id)
        seg = ent.segment if ent is not None and ent.segment is not None else r.segment
        seg_seen.setdefault(r.entity_id, seg)
        is_kw = profile.is_search_keyword(r.targeting_unit)
        in_cur, in_pri = cur.contains(r.date), pri.contains(r.date)
        if is_kw and r.geo and (in_cur or in_pri):
            g = cpm_geo.setdefault((r.targeting_unit, r.geo), [0.0, 0.0, 0.0, 0.0])  # type: ignore[arg-type]
            off = 0 if in_cur else 2
            g[off] += r.spend
            g[off + 1] += r.impressions
        if not in_cur:
            continue
        c = camp.setdefault(r.entity_id, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        c[0] += r.spend
        c[1] += r.sales
        if r.indirect_sales is not None:
            c[2] += indirect_weight * r.indirect_sales
            c[3] = 1.0
        if r.date >= last7:
            c[4] += r.spend
        if r.date >= last3:
            c[5] += r.spend
        if r.geo:
            g2 = egeo.setdefault((r.entity_id, r.geo), [0.0, 0.0])
            g2[0] += r.spend
            g2[1] += r.sales
        if not is_kw:
            continue
        unit: str = r.targeting_unit  # type: ignore[assignment]
        k = kw.setdefault((unit, r.entity_id), [0.0, 0.0, 0.0, 0.0, 0.0])
        k[0] += r.spend
        k[1] += r.sales
        k[2] += indirect_weight * (r.indirect_sales or 0.0)
        if r.new_users is not None:
            k[3] += r.new_users
            k[4] = 1.0
        m = kwm.setdefault((unit, seg), [0.0, 0.0, 0.0, 0.0, 0.0])
        m[0] += r.spend
        m[1] += r.sales
        m[2] += r.impressions
        if pos.available and r.position is not None and pos.valid_min <= r.position <= pos.valid_max:
            m[3] += r.position * r.impressions
            m[4] += r.impressions
            if r.date >= last7:
                hist = mode.setdefault((unit, seg), {})
                slot = int(round(r.position))
                hist[slot] = hist.get(slot, 0.0) + r.impressions
        cr = coll.setdefault((unit, r.entity_id, r.geo), [0.0, 0.0, set(), seg])
        cr[0] += r.spend
        cr[1] += r.sales
        if r.product_id:
            cr[2].add(r.product_id)

    kw_by_entity: dict[str, list[tuple[str, float]]] = {}
    keywords: list[KeywordStat] = []
    for (unit, eid), v in kw.items():
        role = roles.get((unit, eid)) or roles.get(unit) or TargetingRole.GENERIC.value
        role = str(getattr(role, "value", role))
        kw_by_entity.setdefault(eid, []).append((role, v[0]))
        keywords.append(KeywordStat(
            keyword=unit, entity_id=eid, spend=v[0], roas=_roas_of(v[1] + v[2], v[0]), role=role,
            segment=seg_seen.get(eid), new_users=v[3] if v[4] else None,
            search_volume=volume.get(unit)))

    campaigns: list[CampaignStat] = []
    for eid, v in camp.items():
        ent = ents.get(eid)
        krows = kw_by_entity.get(eid)
        own_share = None
        if krows and sum(s for _, s in krows) > 0:
            own_share = (sum(s for role, s in krows if is_branded(role))       # own + sibling brand
                         / sum(s for _, s in krows))
        elif ent is not None:
            own_share = ent.own_brand_share
        ad_type = ent.ad_type if ent is not None else None
        attributed = profile.capability(ad_type).sales_attributed
        campaigns.append(CampaignStat(
            entity_id=eid, segment=seg_seen.get(eid), status=ent.status if ent else None,
            ad_type=ad_type, spend=v[0], spend_7d=v[4], spend_3d=v[5],
            roas_primary=_roas_of(v[1] + v[2], v[0]) if (attributed and v[3]) else None,
            roas_direct=_roas_of(v[1], v[0]) if attributed else None,
            is_pure=ent.is_pure if ent else None, own_brand_share=own_share,
            name=ent.name if ent else None))

    # rival ownership per keyword from the market feed
    owner: dict[str, tuple[float, bool]] = {}
    sov_own: dict[str | None, float] = {}
    sov_all: dict[str | None, float] = {}
    by_kw_brand: dict[str, dict[tuple[str, bool], float]] = {}
    for mr in dataset.market:
        if not cur.contains(mr.date):
            continue
        sov_all[mr.segment] = sov_all.get(mr.segment, 0.0) + mr.ad_impressions
        if mr.is_own:
            sov_own[mr.segment] = sov_own.get(mr.segment, 0.0) + mr.ad_impressions
        d = by_kw_brand.setdefault(mr.targeting_unit, {})
        d[(mr.brand, mr.is_own)] = d.get((mr.brand, mr.is_own), 0.0) + mr.ad_impressions
    for unit, d in by_kw_brand.items():
        total = sum(d.values())
        if total > 0:
            (_, own), top = sorted(d.items(), key=lambda kv: (-kv[1], kv[0][0]))[0]
            owner[unit] = (top / total, own)

    top_entity: dict[tuple[str, str | None], tuple[float, str]] = {}
    for (unit, eid), v in kw.items():
        key = (unit, seg_seen.get(eid))
        if key not in top_entity or v[0] > top_entity[key][0]:
            top_entity[key] = (v[0], eid)
    keyword_market: list[KeywordMarketStat] = []
    for (unit, seg), v in kwm.items():
        hist = mode.get((unit, seg))
        own_info = owner.get(unit)
        eid = top_entity.get((unit, seg), (0.0, None))[1]
        keyword_market.append(KeywordMarketStat(
            keyword=unit, segment=seg, spend=v[0], direct_roas=_roas_of(v[1], v[0]),
            position=(v[3] / v[4]) if v[4] > 0 else None,
            cpm=(1000.0 * v[0] / v[2]) if v[2] > 0 else None,
            position_mode=float(sorted(hist, key=lambda s: (-hist[s], s))[0]) if hist else None,
            live_bid=bids.get((unit, eid)),
            rival_owner_share=own_info[0] if own_info else None,
            rival_owner_is_own=own_info[1] if own_info else None,
            role=str(getattr(roles.get(unit), "value", roles.get(unit)) or "") or None))

    if telemetry is None:
        from ..budget.telemetry import build_budget_telemetry
        days = [d for d in dataset.budget_days if sel is None or d.entity_id in sel]
        telemetry = build_budget_telemetry(days, anchor=end, days=cur.days, entities=ents,
                                           economics=economics) if days else {}

    # segment markets from the sell-out feed
    tot: dict[str | None, dict[str, list[float]]] = {}
    flags: dict[str, tuple[bool, bool]] = {}
    geo_tot: dict[str, list[float]] = {}
    for o in dataset.offtake:
        in_cur, in_pri = cur.contains(o.date), pri.contains(o.date)
        if not (in_cur or in_pri):
            continue
        b = tot.setdefault(o.segment, {}).setdefault(o.brand, [0.0, 0.0])
        b[0 if in_cur else 1] += o.sales
        flags[o.brand] = (o.is_own, o.is_sibling)
        if in_cur and o.geo:
            g3 = geo_tot.setdefault(o.geo, [0.0, 0.0])
            g3[0] += o.sales
            if o.is_own:
                g3[1] += o.sales
    avail: dict[str | None, list[float]] = {}
    geo_avail: dict[str, list[float]] = {}
    for s in dataset.shelf:
        if not cur.contains(s.date) or not s.is_own or s.availability is None:
            continue
        a = avail.setdefault(s.segment, [0.0, 0.0])
        a[0] += s.availability * s.weight
        a[1] += s.weight
        if s.geo:
            a2 = geo_avail.setdefault(s.geo, [0.0, 0.0])
            a2[0] += s.availability * s.weight
            a2[1] += s.weight
    segments: dict[str, SegmentMarket] = {}
    for seg, brands in tot.items():
        tc = sum(v[0] for v in brands.values())
        tp = sum(v[1] for v in brands.values())
        players = tuple(sorted(
            (MarketPlayer(brand=b, is_own=flags[b][0], is_sibling=flags[b][1],
                          share_prior=100.0 * v[1] / tp if tp > 0 else 0.0,
                          share_current=100.0 * v[0] / tc if tc > 0 else 0.0)
             for b, v in brands.items()),
            key=lambda p: (-p.delta_pp, p.brand)))
        av = avail.get(seg)
        segments[seg] = SegmentMarket(  # type: ignore[index]
            segment=seg,  # type: ignore[arg-type]
            total_sales_current=tc, total_sales_prior=tp,
            own_sales_current=sum(v[0] for b, v in brands.items() if flags[b][0]),
            own_sales_prior=sum(v[1] for b, v in brands.items() if flags[b][0]),
            players=players,
            sov_current=(100.0 * sov_own.get(seg, 0.0) / sov_all[seg]) if sov_all.get(seg) else None,
            availability=(av[0] / av[1]) if av and av[1] > 0 else None)

    geo_market = [
        GeoMarketStat(geo=g, total_sales=v[0], own_share_pct=100.0 * v[1] / v[0] if v[0] > 0 else 0.0,
                      availability=(geo_avail[g][0] / geo_avail[g][1])
                      if g in geo_avail and geo_avail[g][1] > 0 else None)
        for g, v in sorted(geo_tot.items())]

    head: dict[str, dict[str, tuple[float | None, float | None]]] = {}
    for (unit, g), v in cpm_geo.items():
        head.setdefault(unit, {})[g] = (
            1000.0 * v[0] / v[1] if v[1] > 0 else None, 1000.0 * v[2] / v[3] if v[3] > 0 else None)

    return ActionContext(
        campaigns=campaigns, keywords=keywords, keyword_market=keyword_market,
        telemetry=dict(telemetry), segments=segments,
        entity_geo=[EntityGeoStat(eid, g, v[0], v[1]) for (eid, g), v in egeo.items()],
        geo_market=geo_market,
        collisions=[CollisionRow(unit, eid, g, v[0], v[1], frozenset(v[2]) if v[2] else None, v[3])
                    for (unit, eid, g), v in coll.items()],
        recent_applied=cooldown_register(applied, end, cfg),
        head_cpm_by_geo=head or None, keyword_suggestions=keyword_suggestions,
        economics=economics, profile=profile, cfg=cfg,
        budget_policy=budget_policy or BudgetBoundPolicy(),
        rule_profile=rule_profile, check=check, operator_floors=dict(operator_floors or {}),
        scoped=sel is not None,
        selected_segments=frozenset(selected_segments) if selected_segments is not None else None)
