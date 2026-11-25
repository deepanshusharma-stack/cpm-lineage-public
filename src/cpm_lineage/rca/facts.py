"""Fact shapes for root-cause analysis, and pure extractors that build them.

The RCA never queries anything.  It consumes a :class:`Facts` bundle: seven
small fact records, each two windows of the same length (``_p`` = prior,
``_c`` = current).  An adapter may fill these shapes directly, or call the
``extract_*`` functions here on the neutral feed records
(``OfftakeRow``, ``MarketRow``, ``ShelfRow``, ``PerfRow``, ``Product``).

Scales: availability is ``0..1``; shares, share of voice, discount depth and
growth are percent.  A fact that is ``None`` means "the platform does not
report it": every gate that needs it goes silent (neither fired nor ruled
out) rather than reading it as zero.

Vocabulary: *product* = an advertised item; *segment* = the competitive set
it sells in; *geo* = the platform's auction / reporting region.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Mapping, Sequence

from cpm_lineage.config import Economics, apply_overrides
from cpm_lineage.schema import MarketRow, OfftakeRow, PerfRow, Product, ShelfRow, Window, WindowPair
from cpm_lineage.windows import resolve_window, trailing_windows

from .config import DEFAULT_RCA_CONFIG, RcaConfig

ALL_GEO = "all"
"""Label of the single synthetic geo used when a feed carries no geo."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def pct_move(current: float | None, prior: float | None) -> float | None:
    """``100 * (current - prior) / prior``, or ``None`` when prior is missing or 0.

    Written as a difference over the prior so that round inputs land exactly
    on round thresholds.
    """
    if current is None or not prior:
        return None
    return 100.0 * (current - prior) / prior


def _seg_ok(row_segment: str | None, segment: str | None) -> bool:
    """A row belongs to the scope's segment; rows without a segment belong to every scope."""
    return row_segment is None or segment is None or row_segment == segment


def _norm(text: str | None) -> str:
    return (text or "").strip().lower()


# ---------------------------------------------------------------------------
# scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Scope:
    """What one RCA run looks at: own products in one segment over two windows.

    ``config`` already has the per-account overrides applied;
    ``overrides`` maps each applied key to ``(default, value)`` and
    ``unknown_overrides`` lists ignored keys (both are printed by the
    verifier).  ``is_subset`` is true when the caller picked products rather
    than taking every own product in the segment.
    """

    own_brand: str
    segment: str
    own_products: tuple[str, ...]
    windows: WindowPair
    feed_max: date
    is_subset: bool = False
    config: RcaConfig = DEFAULT_RCA_CONFIG
    economics: Economics = field(default_factory=Economics)
    overrides: Mapping[str, tuple[Any, Any]] = field(default_factory=dict)
    unknown_overrides: tuple[str, ...] = ()
    sibling_brands: tuple[str, ...] = ()
    note: str = ""

    @property
    def days(self) -> int:
        return self.windows.days

    @property
    def current(self) -> Window:
        return self.windows.current

    @property
    def prior(self) -> Window:
        return self.windows.prior

    @property
    def breakeven(self) -> float:
        """Effective breakeven ROAS for this account."""
        return self.economics.be

    @property
    def roas_guard(self) -> float:
        """Efficiency guard in ROAS terms: ``config.roas_guard x breakeven``."""
        return self.config.roas_guard * self.economics.be

    def is_own_product(self, product_id: str | None) -> bool:
        """Row-level product test: unknown product ids pass unless the scope is a hand-picked subset."""
        if product_id is None:
            return not self.is_subset
        return product_id in self.own_products

    def is_sibling(self, brand: str | None) -> bool:
        return _norm(brand) in {_norm(b) for b in self.sibling_brands}


def own_segments(own_brand: str, offtake: Iterable[OfftakeRow], products: Iterable[Product] = ()) -> list[str]:
    """Segments in which the advertiser has own products or own sales, sorted by name."""
    out = {p.segment for p in products if p.is_own and p.segment}
    out |= {r.segment for r in offtake if r.is_own and r.segment}
    return sorted(out)


def resolve_scope(
    *,
    own_brand: str,
    segment: str | None = None,
    product_ids: Sequence[str] | None = None,
    products: Iterable[Product] = (),
    offtake: Iterable[OfftakeRow] = (),
    days: int | None = None,
    date_from: Any = None,
    date_to: Any = None,
    cmp: str = "wow",
    config: RcaConfig | None = None,
    overrides: Mapping[str, Any] | None = None,
    economics: Economics | None = None,
    sibling_brands: Sequence[str] = (),
) -> Scope:
    """Decide which products, segment, windows and thresholds one run uses.

    * Products given: the own set is that selection, the segment is taken
      from the first product when not passed, ``is_subset`` is true.
    * Otherwise a segment is required and the own set is every own product
      in it (from the product dimension, else from own sales rows).
    * Windows: with no custom range, ``current = [feed_max - (d-1),
      feed_max]`` and ``prior`` the ``d`` days before it, where ``feed_max``
      is the latest date in the market sales feed for the segment (the
      window is anchored to the data, never to today).  A custom range goes
      through ``windows.resolve_window`` and is clamped to ``feed_max``.
    * Thresholds: a copy of the config with known override keys applied;
      unknown keys are recorded and ignored.

    Raises ``ValueError`` when the segment or own product set is empty, the
    feed has no rows for the segment, or the custom range is malformed.
    Returns a :class:`Scope`.
    """
    base = config or DEFAULT_RCA_CONFIG
    products = list(products)
    offtake = list(offtake)
    by_id = {p.product_id: p for p in products}
    is_subset = bool(product_ids)
    if is_subset:
        own = tuple(dict.fromkeys(product_ids or ()))
        if not segment:
            first = by_id.get(own[0])
            segment = first.segment if first else next(
                (r.segment for r in offtake if r.product_id == own[0] and r.segment), None)
    else:
        if not segment:
            raise ValueError("a segment (or a product selection) is required")
        ids = [p.product_id for p in products if p.is_own and p.segment == segment]
        if not ids:
            ids = [r.product_id for r in offtake if r.is_own and r.segment == segment and r.product_id]
        own = tuple(dict.fromkeys(ids))
    if not segment:
        raise ValueError("no segment could be resolved for the selection")
    if not own:
        raise ValueError(f"no own products found in segment {segment!r}")
    seg_dates = [r.date for r in offtake if _seg_ok(r.segment, segment)]
    if not seg_dates:
        raise ValueError(f"the market sales feed has no rows for segment {segment!r}")
    feed_max = max(seg_dates)

    cfg, applied, unknown = apply_overrides(base, overrides)
    recorded = {k: (getattr(base, k), v) for k, v in applied.items()}
    note = ""
    if date_from is not None or date_to is not None:
        pair = resolve_window(date_from, date_to, cmp, clamp_to=feed_max)
        if pair is None:
            raise ValueError("custom range needs two YYYY-MM-DD dates and cmp in dod / wow / mom")
        note = pair.note
    else:
        pair = trailing_windows(feed_max, int(days or cfg.default_days))
    return Scope(
        own_brand=own_brand, segment=segment, own_products=own, windows=pair, feed_max=feed_max,
        is_subset=is_subset, config=cfg, economics=economics or Economics(), overrides=recorded,
        unknown_overrides=tuple(unknown), sibling_brands=tuple(sibling_brands), note=note,
    )


# ---------------------------------------------------------------------------
# fact records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProductValueRow:
    product_id: str
    cur_value: float
    pri_value: float


@dataclass(frozen=True)
class GeoRow:
    """One geo: own value both windows and the own / segment moves in percent."""

    geo: str
    cur_value: float
    pri_value: float
    own_pct: float | None
    segment_pct: float | None
    is_aggregate: bool = False      # an "others" bucket: kept in sums, never named as a place to act


@dataclass(frozen=True)
class OfftakeFacts:
    """Own and whole-segment sell-out, by product, geo and day."""

    cur_value: float
    pri_value: float
    cur_units: float
    pri_units: float
    segment_cur_value: float
    segment_pri_value: float
    product_rows: tuple[ProductValueRow, ...] = ()   # sorted by current value desc
    geo_rows: tuple[GeoRow, ...] = ()                # sorted by own PRIOR value desc
    daily: tuple[tuple[date, float], ...] = ()       # own value per day over both windows, ascending


@dataclass(frozen=True)
class ConversionFacts:
    """Impressions eligible for conversion and the units they produced.

    ``conv = units / impressions`` per window (0 without impressions), so
    ``impr x conv`` reproduces units exactly.
    """

    impr_p: float
    impr_c: float
    conv_p: float
    conv_c: float
    ad_p: float | None = None
    ad_c: float | None = None
    org_p: float | None = None
    org_c: float | None = None
    asp_p: float | None = None
    asp_c: float | None = None


@dataclass(frozen=True)
class AdTypeRow:
    """Spend and attributed sales of one ad type.  ``sales_tracked=False`` means ROAS is not defined for it."""

    ad_type: str
    spend_c: float
    spend_p: float
    sales_c: float
    sales_p: float
    sales_tracked: bool = True
    roas_deflator: float = 1.0

    @property
    def roas_c(self) -> float | None:
        """Current ROAS after the ad type's deflator; ``None`` when untracked or unfunded."""
        if not self.sales_tracked or self.spend_c <= 0:
            return None
        return self.roas_deflator * self.sales_c / self.spend_c


@dataclass(frozen=True)
class FunnelFacts:
    """First-party ads ledger for the own products.  ``stale`` means the feed does not reach the current window."""

    stale: bool = False
    feed_max: date | None = None
    spend_c: float = 0.0
    spend_p: float = 0.0
    impr_c: float = 0.0
    impr_p: float = 0.0
    mid_rate_c: float | None = None   # mid-funnel events per 100 impressions (clicks, add-to-cart, ...)
    mid_rate_p: float | None = None
    units_c: float = 0.0
    units_p: float = 0.0
    roas_c: float | None = None       # direct attributed sales / spend
    roas_p: float | None = None
    ad_types: tuple[AdTypeRow, ...] = ()            # sorted by current spend desc
    product_spend: Mapping[str, float] = field(default_factory=dict)   # current-window spend per product
    mid_label: str = "mid-funnel rate"


@dataclass(frozen=True)
class ProductAvailabilityRow:
    product_id: str
    availability_c: float | None
    units_c: float
    units_p: float
    loss_c: float = 0.0


@dataclass(frozen=True)
class AvailabilityFacts:
    """Units-weighted availability (0..1), own and whole segment.  ``product_rows[0]`` is the hero product."""

    own_c: float | None
    own_p: float | None
    segment_c: float | None = None
    segment_p: float | None = None
    loss_c: float | None = None       # estimated sales value lost to unavailability
    loss_p: float | None = None
    geo_flags: tuple[tuple[str, float], ...] = ()       # geos under the floor, by units desc
    product_rows: tuple[ProductAvailabilityRow, ...] = ()   # sorted by current units desc


@dataclass(frozen=True)
class AdvertiserRow:
    brand: str
    cur: float
    pri: float
    is_own: bool = False
    is_sibling: bool = False


@dataclass(frozen=True)
class KeywordPoolRow:
    keyword: str
    pool_c: float
    pool_p: float
    own_c: float
    own_p: float


@dataclass(frozen=True)
class VoiceFacts:
    """Ad and organic impressions on the battleground keywords: own versus everyone."""

    own_c: float
    own_p: float
    pool_c: float
    pool_p: float
    org_own_c: float = 0.0
    org_own_p: float = 0.0
    org_pool_c: float = 0.0
    org_pool_p: float = 0.0
    advertisers: tuple[AdvertiserRow, ...] = ()
    keywords: tuple[KeywordPoolRow, ...] = ()       # top generic keywords by current pool (not battleground-only)
    battleground: tuple[str, ...] = ()
    relevant: Mapping[str, bool] | None = None      # advisory relevance screen; filters suggestions only
    expansion: tuple[str, ...] = ()                 # advisory keyword ideas with live demand

    @property
    def sov_c(self) -> float | None:
        return 100.0 * self.own_c / self.pool_c if self.pool_c else None

    @property
    def sov_p(self) -> float | None:
        return 100.0 * self.own_p / self.pool_p if self.pool_p else None


@dataclass(frozen=True)
class PricingFacts:
    """Discount depth in percent (own and segment) and per-product list-price mix."""

    depth_c: float | None
    depth_p: float | None
    segment_depth_c: float | None = None
    segment_depth_p: float | None = None
    sticker_mix: Mapping[str, tuple[tuple[float, float], ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class RivalRow:
    """One brand's share of segment value in both windows plus a one-phrase playbook read."""

    brand: str
    share_c: float
    share_p: float
    d_pp: float
    value_delta: float
    is_own: bool = False
    is_sibling: bool = False
    availability: float | None = None
    discount_pct: float | None = None
    ad_sov_pct: float | None = None
    n_products: int | None = None
    how: str = ""
    lane: str | None = None


@dataclass(frozen=True)
class LaunchRow:
    brand: str
    title: str
    first_sale: date
    share_pct: float


@dataclass(frozen=True)
class LineRow:
    segment: str
    cur: float
    pri: float


@dataclass(frozen=True)
class RivalsFacts:
    """Brand share movers (sorted by ``|d_pp|`` desc), launches and the advertiser's other lines."""

    movers: tuple[RivalRow, ...] = ()
    join_coverage: float | None = None
    launches: tuple[LaunchRow, ...] = ()
    own_other_lines: tuple[LineRow, ...] = ()
    own_brand: str = ""
    own_lane: str | None = None


@dataclass(frozen=True)
class Facts:
    """Everything one RCA run reads.  Only ``scope`` and ``offtake`` are required."""

    scope: Scope
    offtake: OfftakeFacts
    conversion: ConversionFacts | None = None
    funnel: FunnelFacts | None = None
    availability: AvailabilityFacts | None = None
    voice: VoiceFacts | None = None
    pricing: PricingFacts | None = None
    rivals: RivalsFacts | None = None

    @property
    def config(self) -> RcaConfig:
        return self.scope.config


@dataclass(frozen=True)
class LossRow:
    """Optional estimate of sales value lost to unavailability (platform or third-party supplied)."""

    date: date
    product_id: str
    value: float
    geo: str | None = None


# ---------------------------------------------------------------------------
# extractors
# ---------------------------------------------------------------------------


def extract_offtake(
    offtake: Iterable[OfftakeRow],
    scope: Scope,
    *,
    is_aggregate_geo: Callable[[str | None], bool] | None = None,
) -> OfftakeFacts:
    """Sum own and whole-segment sell-out for both windows.

    Own = own-brand rows of the scope's products in the segment; segment =
    every brand's rows in the segment.  Geo rows carry ``own_pct = 100 *
    (cur / pri - 1)`` (``None`` when prior is 0) and the same on segment
    totals, sorted by own PRIOR value descending.  A feed without geo yields
    one row labelled ``"all"``.  Returns :class:`OfftakeFacts`.
    """
    cur, pri = scope.current, scope.prior
    own = {"cv": 0.0, "pv": 0.0, "cu": 0.0, "pu": 0.0}
    seg_c = seg_p = 0.0
    by_pid: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    geo_own: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    geo_seg: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    daily: dict[date, float] = defaultdict(float)
    for r in offtake:
        if not _seg_ok(r.segment, scope.segment):
            continue
        in_c, in_p = cur.contains(r.date), pri.contains(r.date)
        if not (in_c or in_p):
            continue
        i = 0 if in_c else 1
        geo = r.geo or ALL_GEO
        geo_seg[geo][i] += r.sales
        if in_c:
            seg_c += r.sales
        else:
            seg_p += r.sales
        if not (r.is_own and scope.is_own_product(r.product_id)):
            continue
        own["cv" if in_c else "pv"] += r.sales
        own["cu" if in_c else "pu"] += r.units
        by_pid[r.product_id or "unknown"][i] += r.sales
        geo_own[geo][i] += r.sales
        daily[r.date] += r.sales
    agg = is_aggregate_geo or (lambda g: False)
    geo_rows = [
        GeoRow(g, v[0], v[1], pct_move(v[0], v[1]), pct_move(geo_seg[g][0], geo_seg[g][1]),
               bool(g != ALL_GEO and agg(g)))
        for g, v in geo_own.items()
    ]
    geo_rows.sort(key=lambda g: (-g.pri_value, g.geo))
    prod_rows = sorted((ProductValueRow(p, v[0], v[1]) for p, v in by_pid.items()),
                       key=lambda p: (-p.cur_value, p.product_id))
    return OfftakeFacts(
        cur_value=own["cv"], pri_value=own["pv"], cur_units=own["cu"], pri_units=own["pu"],
        segment_cur_value=seg_c, segment_pri_value=seg_p, product_rows=tuple(prod_rows),
        geo_rows=tuple(geo_rows), daily=tuple(sorted(daily.items())),
    )


def extract_conversion(
    market: Iterable[MarketRow],
    offtake: Iterable[OfftakeRow],
    scope: Scope,
) -> ConversionFacts | None:
    """Impressions eligible for conversion, units, and the ad / organic split.

    Impressions = own-brand rows of the market-impression feed (ad +
    organic); units and list-price value come from the own sell-out rows.
    ``conv = units / impressions`` (0.0 without impressions); ``asp =
    list-price value / units`` (falling back to sales value when the feed
    has no list value).

    Both feeds are filtered by the scope's segment.  (Filtering
    impressions by product only would let the two-factor parts disagree
    with the sell-out delta for products outside the segment, so one scope
    is applied to both.)

    Returns ``None`` when the own brand has no impression rows in either
    window.
    """
    cur, pri = scope.current, scope.prior
    ad = [0.0, 0.0]
    org = [0.0, 0.0]
    tot = [0.0, 0.0]
    seen = False
    for m in market:
        if not (m.is_own and _seg_ok(m.segment, scope.segment) and scope.is_own_product(m.product_id)):
            continue
        i = 0 if cur.contains(m.date) else 1 if pri.contains(m.date) else None
        if i is None:
            continue
        seen = True
        ad[i] += m.ad_impressions
        org[i] += m.organic_impressions
        tot[i] += m.impressions
    if not seen:
        return None
    units = [0.0, 0.0]
    value = [0.0, 0.0]
    for r in offtake:
        if not (r.is_own and _seg_ok(r.segment, scope.segment) and scope.is_own_product(r.product_id)):
            continue
        i = 0 if cur.contains(r.date) else 1 if pri.contains(r.date) else None
        if i is None:
            continue
        units[i] += r.units
        value[i] += r.list_value if r.list_value is not None else r.sales
    return ConversionFacts(
        impr_p=tot[1], impr_c=tot[0],
        conv_p=units[1] / tot[1] if tot[1] else 0.0,
        conv_c=units[0] / tot[0] if tot[0] else 0.0,
        ad_p=ad[1], ad_c=ad[0], org_p=org[1], org_c=org[0],
        asp_p=value[1] / units[1] if units[1] else None,
        asp_c=value[0] / units[0] if units[0] else None,
    )


def extract_funnel(
    perf: Iterable[PerfRow],
    scope: Scope,
    *,
    profile: Any = None,
    mid_label: str = "click rate",
) -> FunnelFacts:
    """First-party ads ledger for the own products, both windows.

    Freshness guard first: when the feed's latest date is missing or earlier
    than the current window's start the result is ``FunnelFacts(stale=True)``
    and nothing else (the axis is marked unavailable, never silently
    reported).  Otherwise: spend, impressions, units, direct ROAS
    (``sales / spend``, ``None`` without spend), the mid-funnel rate
    (``100 x clicks / impressions`` when the platform reports clicks, else
    ``None``), per-ad-type spend and sales (sorted by current spend desc)
    and current spend per product.

    ``profile`` (a ``PlatformProfile``) canonicalises ad-type labels and
    says which types have attributed sales and a ROAS deflator; without it
    labels pass through and every type counts as tracked.
    """
    cur, pri = scope.current, scope.prior
    rows = [r for r in perf if _seg_ok(r.segment, scope.segment) and scope.is_own_product(r.product_id)]
    feed_max = max((r.date for r in rows), default=None)
    if feed_max is None or feed_max < cur.start:
        return FunnelFacts(stale=True, feed_max=feed_max, mid_label=mid_label)
    acc = {k: [0.0, 0.0] for k in ("spend", "impr", "units", "sales", "clicks")}
    has_clicks = [False, False]
    types: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    raw_type: dict[str, str | None] = {}
    pid_spend: dict[str, float] = defaultdict(float)
    for r in rows:
        i = 0 if cur.contains(r.date) else 1 if pri.contains(r.date) else None
        if i is None:
            continue
        acc["spend"][i] += r.spend
        acc["impr"][i] += r.impressions
        acc["units"][i] += r.units
        acc["sales"][i] += r.sales
        if r.clicks is not None:
            has_clicks[i] = True
            acc["clicks"][i] += r.clicks
        label = profile.canonical_ad_type(r.ad_type) if profile is not None else (r.ad_type or "unknown")
        raw_type.setdefault(label, r.ad_type)
        t = types[label]
        t[i] += r.spend
        t[2 + i] += r.sales
        if i == 0 and r.product_id is not None:
            pid_spend[r.product_id] += r.spend
    ad_rows = []
    for label, t in types.items():
        tracked, deflator = True, 1.0
        if profile is not None:
            cap = profile.capability(raw_type[label])
            tracked, deflator = bool(cap.sales_attributed), float(cap.roas_deflator)
        ad_rows.append(AdTypeRow(label, t[0], t[1], t[2], t[3], tracked, deflator))
    ad_rows.sort(key=lambda a: (-a.spend_c, a.ad_type))

    def rate(i: int) -> float | None:
        if not has_clicks[i] or not acc["impr"][i]:
            return None
        return 100.0 * acc["clicks"][i] / acc["impr"][i]

    return FunnelFacts(
        stale=False, feed_max=feed_max,
        spend_c=acc["spend"][0], spend_p=acc["spend"][1],
        impr_c=acc["impr"][0], impr_p=acc["impr"][1],
        mid_rate_c=rate(0), mid_rate_p=rate(1),
        units_c=acc["units"][0], units_p=acc["units"][1],
        roas_c=acc["sales"][0] / acc["spend"][0] if acc["spend"][0] else None,
        roas_p=acc["sales"][1] / acc["spend"][1] if acc["spend"][1] else None,
        ad_types=tuple(ad_rows), product_spend=dict(pid_spend), mid_label=mid_label,
    )


def _unit_lookup(offtake: Iterable[OfftakeRow] | None) -> dict[tuple[date, str | None, str | None], float]:
    out: dict[tuple[date, str | None, str | None], float] = defaultdict(float)
    for r in offtake or ():
        out[(r.date, r.product_id, r.geo)] += r.units
    return out


def _shelf_weight(row: ShelfRow, units: Mapping[tuple[date, str | None, str | None], float], joined: bool) -> float:
    """Units weight of a shelf observation: its own units, else joined sell-out units, else its ``weight``."""
    if row.units is not None:
        return float(row.units)
    if joined:
        return float(units.get((row.date, row.product_id, row.geo), 0.0))
    return float(row.weight)


def extract_availability(
    shelf: Iterable[ShelfRow],
    scope: Scope,
    *,
    offtake: Iterable[OfftakeRow] | None = None,
    loss_rows: Iterable[LossRow] | None = None,
    is_aggregate_geo: Callable[[str | None], bool] | None = None,
) -> AvailabilityFacts | None:
    """Units-weighted availability for own products and the whole segment.

    ``availability = sum((available / checks) x units) / sum(units)`` over
    observations with ``checks > 0``; ``None`` when there are no units.  The
    units weight is the shelf row's own ``units``, else sell-out units
    joined on (date, product, geo) when ``offtake`` is passed, else the
    row's ``weight``.

    ``geo_flags`` lists geos with own units whose current availability is
    under ``availability_floor`` (aggregate buckets excluded), by units
    desc.  ``product_rows`` is sorted by current units desc: row 0 is the
    hero product.  Loss estimates are optional and never invented.

    Returns ``None`` when the feed has no own observation in either window.
    """
    cfg = scope.config
    cur, pri = scope.current, scope.prior
    lookup = _unit_lookup(offtake)
    joined = offtake is not None
    agg = is_aggregate_geo or (lambda g: False)
    own = [[0.0, 0.0], [0.0, 0.0]]        # [window][num, den]
    seg = [[0.0, 0.0], [0.0, 0.0]]
    geo: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    pid: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])   # num, units_c, units_p
    seen = False
    for r in shelf:
        if not _seg_ok(r.segment, scope.segment):
            continue
        i = 0 if cur.contains(r.date) else 1 if pri.contains(r.date) else None
        a = r.availability
        if i is None or a is None:
            continue
        w = _shelf_weight(r, lookup, joined)
        seg[i][0] += a * w
        seg[i][1] += w
        if not (r.is_own and scope.is_own_product(r.product_id)):
            continue
        seen = True
        own[i][0] += a * w
        own[i][1] += w
        p = pid[r.product_id]
        if i == 0:
            p[0] += a * w
            p[1] += w
            if r.geo is not None and not agg(r.geo):
                geo[r.geo][0] += a * w
                geo[r.geo][1] += w
        else:
            p[2] += w
    if not seen:
        return None

    def ratio(pair: list[float]) -> float | None:
        return pair[0] / pair[1] if pair[1] > 0 else None

    loss = [None, None]
    pid_loss: dict[str, float] = defaultdict(float)
    if loss_rows is not None:
        loss = [0.0, 0.0]
        for lr in loss_rows:
            if not scope.is_own_product(lr.product_id):
                continue
            if cur.contains(lr.date):
                loss[0] += lr.value
                pid_loss[lr.product_id] += lr.value
            elif pri.contains(lr.date):
                loss[1] += lr.value
    flags = sorted(((g, v[0] / v[1], v[1]) for g, v in geo.items()
                    if v[1] > 0 and v[0] / v[1] < cfg.availability_floor), key=lambda t: (-t[2], t[0]))
    rows = sorted((ProductAvailabilityRow(p, v[0] / v[1] if v[1] > 0 else None, v[1], v[2], pid_loss.get(p, 0.0))
                   for p, v in pid.items()), key=lambda r: (-r.units_c, r.product_id))
    return AvailabilityFacts(
        own_c=ratio(own[0]), own_p=ratio(own[1]), segment_c=ratio(seg[0]), segment_p=ratio(seg[1]),
        loss_c=loss[0], loss_p=loss[1], geo_flags=tuple((g, a) for g, a, _ in flags), product_rows=tuple(rows),
    )


def extract_voice(
    market: Iterable[MarketRow],
    scope: Scope,
    *,
    roles: Mapping[str, str] | None = None,
    generic_role: str = "generic",
) -> VoiceFacts | None:
    """Share of voice on the battleground: the generic keywords the advertiser actually shows on.

    Battleground = the top ``battleground_size`` generic keywords ranked by
    OWN ad impressions summed over both windows (only keywords above 0).
    The whole tracked keyword pool can span other segments and dilute a real
    pullback to noise; keywords ranked by own presence are the deterministic
    middle ground.  A keyword is generic when its role (row ``unit_role``,
    else ``roles[keyword]``) equals ``generic_role``; rows with no role
    information at all are treated as generic.

    Returns own / pool ad and organic impressions on the battleground, the
    top advertisers there by current ad impressions, and the top generic
    keywords by current pool (NOT restricted to the battleground, so that
    unclaimed pockets are visible).  ``None`` when there are no generic rows.
    """
    cfg = scope.config
    cur, pri = scope.current, scope.prior
    roles = {_norm(k): v for k, v in (roles or {}).items()}
    kw: dict[str, list[float]] = defaultdict(lambda: [0.0] * 8)   # pool_c, pool_p, own_c, own_p, org x4
    brand_kw: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    flags: dict[str, tuple[bool, bool]] = {}
    for m in market:
        if not _seg_ok(m.segment, scope.segment):
            continue
        role = m.unit_role if m.unit_role is not None else roles.get(_norm(m.targeting_unit))
        if role is not None and role != generic_role:
            continue
        i = 0 if cur.contains(m.date) else 1 if pri.contains(m.date) else None
        if i is None:
            continue
        k = kw[m.targeting_unit]
        k[i] += m.ad_impressions
        k[4 + i] += m.organic_impressions
        if m.is_own:
            k[2 + i] += m.ad_impressions
            k[6 + i] += m.organic_impressions
        brand_kw[(m.brand, m.targeting_unit)][i] += m.ad_impressions
        flags[m.brand] = (m.is_own, m.is_sibling or scope.is_sibling(m.brand))
    if not kw:
        return None
    ranked = sorted(((k, v[2] + v[3]) for k, v in kw.items() if v[2] + v[3] > 0), key=lambda t: (-t[1], t[0]))
    ground = tuple(k for k, _ in ranked[: cfg.battleground_size])
    tot = [0.0] * 8
    for k in ground:
        for j in range(8):
            tot[j] += kw[k][j]
    adv: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    gset = set(ground)
    for (brand, k), v in brand_kw.items():
        if k in gset:
            adv[brand][0] += v[0]
            adv[brand][1] += v[1]
    advertisers = sorted((AdvertiserRow(b, v[0], v[1], *flags.get(b, (False, False))) for b, v in adv.items()),
                         key=lambda a: (-a.cur, a.brand))[: cfg.advertiser_list_cap]
    keywords = sorted((KeywordPoolRow(k, v[0], v[1], v[2], v[3]) for k, v in kw.items()),
                      key=lambda r: (-r.pool_c, r.keyword))[: cfg.keyword_list_cap]
    return VoiceFacts(
        own_c=tot[2], own_p=tot[3], pool_c=tot[0], pool_p=tot[1],
        org_own_c=tot[6], org_own_p=tot[7], org_pool_c=tot[4], org_pool_p=tot[5],
        advertisers=tuple(advertisers), keywords=tuple(keywords), battleground=ground,
    )


def sticker_points(
    prices: Iterable[float],
    *,
    mode_share: float,
    cluster_radius: float,
    max_points: int = 3,
    unit: float = 1.0,
) -> tuple[tuple[float, float], ...]:
    """Find distinct list-price points among one product's observations, without averaging.

    Each observed list price is rounded to ``unit``; ``share = count /
    total``; modes with ``share >= mode_share`` are kept, highest share
    first; a mode within ``cluster_radius`` of an already-kept mode is the
    same price point.  Returns up to ``max_points`` ``(price, share)`` pairs
    when at least two distinct points remain, else an empty tuple.
    """
    unit = unit or 1.0
    counts = Counter(round(p / unit) * unit for p in prices if p is not None and p > 0)
    total = sum(counts.values())
    if not total:
        return ()
    kept: list[tuple[float, float]] = []
    for price, n in sorted(counts.items(), key=lambda t: (-t[1], t[0])):
        s = n / total
        if s < mode_share:
            continue
        if any(abs(price - k) <= cluster_radius for k, _ in kept):
            continue
        kept.append((float(price), s))
    return tuple(kept[:max_points]) if len(kept) >= 2 else ()


def extract_pricing(
    shelf: Iterable[ShelfRow],
    scope: Scope,
    *,
    offtake: Iterable[OfftakeRow] | None = None,
) -> PricingFacts | None:
    """Discount depth and list-price ("sticker") mix.

    ``depth = 100 x (1 - sum(selling price x units) / sum(list price x
    units))`` over observations with both prices valid, for own products
    and the whole segment in both windows (units weight as in
    :func:`extract_availability`).  The sticker mix is computed per own
    product on the current window only via :func:`sticker_points`; the
    clustering radius is ``Economics.money(sticker_cluster_radius)``.

    Returns ``None`` when no own observation carries a valid price pair.
    """
    cfg, eco = scope.config, scope.economics
    cur, pri = scope.current, scope.prior
    lookup = _unit_lookup(offtake)
    joined = offtake is not None
    own = [[0.0, 0.0], [0.0, 0.0]]     # [window][selling value, list value]
    seg = [[0.0, 0.0], [0.0, 0.0]]
    stickers: dict[str, list[float]] = defaultdict(list)
    seen = False
    for r in shelf:
        if not _seg_ok(r.segment, scope.segment):
            continue
        i = 0 if cur.contains(r.date) else 1 if pri.contains(r.date) else None
        if i is None or r.price is None or not r.list_price or r.price <= 0:
            continue
        w = _shelf_weight(r, lookup, joined)
        seg[i][0] += r.price * w
        seg[i][1] += r.list_price * w
        if not (r.is_own and scope.is_own_product(r.product_id)):
            continue
        seen = True
        own[i][0] += r.price * w
        own[i][1] += r.list_price * w
        if i == 0:
            stickers[r.product_id].append(r.list_price)
    if not seen:
        return None

    def depth(pair: list[float]) -> float | None:
        return 100.0 * (1.0 - pair[0] / pair[1]) if pair[1] > 0 else None

    mix = {}
    for p, prices in stickers.items():
        pts = sticker_points(prices, mode_share=cfg.sticker_mode_share,
                             cluster_radius=eco.money(cfg.sticker_cluster_radius),
                             max_points=cfg.sticker_max_points, unit=eco.unit)
        if pts:
            mix[p] = pts
    return PricingFacts(depth_c=depth(own[0]), depth_p=depth(own[1]), segment_depth_c=depth(seg[0]),
                        segment_depth_p=depth(seg[1]), sticker_mix=mix)


def how_they_win(
    *,
    discount_pct: float | None,
    availability: float | None,
    ad_sov_pct: float | None = None,
    n_products: int | None = None,
    config: RcaConfig | None = None,
) -> str:
    """One-phrase read of how a brand competes; first matching rule wins.

    1. discount >= ``how_deep_discount_pct`` -> "Deep price (N% off)"
       (+ " + high availability" at >= ``how_high_availability``)
    2. availability >= ``how_distribution_availability`` and discount <=
       ``how_low_discount_pct`` -> "Distribution + heavy media" when ad SOV
       >= ``how_media_sov_pct``, else "Distribution, at almost no discount"
    3. ad SOV >= ``how_media_sov_pct`` -> "Media-led"
    4. product count >= ``how_range_products`` -> "Range: N products"
    5. else "Balanced"

    Missing inputs never match their branch.  (Ad SOV is an explicit input, so the
    media branches are reachable whenever the caller has it.)
    """
    c = config or DEFAULT_RCA_CONFIG
    d = discount_pct if discount_pct is not None else 0.0
    if discount_pct is not None and d >= c.how_deep_discount_pct:
        extra = " + high availability" if availability is not None and availability >= c.how_high_availability else ""
        return f"Deep price ({d:.0f}% off){extra}"
    media = ad_sov_pct is not None and ad_sov_pct >= c.how_media_sov_pct
    if availability is not None and availability >= c.how_distribution_availability and d <= c.how_low_discount_pct:
        return ("Distribution + heavy media: the full-stack leader" if media
                else "Distribution, at almost no discount")
    if media:
        return "Media-led: buys the visibility"
    if n_products is not None and n_products >= c.how_range_products:
        return f"Range: {n_products} products on shelf"
    return "Balanced: no single lever dominates"


def extract_rivals(
    offtake: Iterable[OfftakeRow],
    scope: Scope,
    *,
    products: Iterable[Product] = (),
    shelf: Iterable[ShelfRow] = (),
    market: Iterable[MarketRow] = (),
    lanes: Mapping[str, str] | None = None,
) -> RivalsFacts | None:
    """Brand share movers, launches and the advertiser's other lines.

    Per brand in the segment: share of segment value in each window (%),
    ``d_pp``, absolute value change, own / sibling flags, current-window
    availability and discount (from the shelf feed), ad SOV on generic
    keywords (from the impression feed), product count and the
    :func:`how_they_win` phrase.  Rows with an empty brand are left out of
    the list but stay in the denominator; ``join_coverage`` is the percent
    of current segment value that maps to a brand.

    Launches: non-own products whose first sale is within
    ``launch_lookback_days`` of the feed's latest date, with current value
    and a current share of segment >= ``launch_share_pct``.  Other own
    lines: the advertiser's other segments, top ``other_lines_cap`` by prior
    value.

    ``lanes`` is an optional advisory ``brand -> positioning lane`` map.
    Returns ``None`` when the segment has no sales in either window.
    """
    cfg = scope.config
    cur, pri = scope.current, scope.prior
    offtake = list(offtake)
    val: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    flags: dict[str, list[bool]] = defaultdict(lambda: [False, False])
    pid_cur: dict[str, float] = defaultdict(float)
    pid_brand: dict[str, str] = {}
    n_prod: dict[str, set[str]] = defaultdict(set)
    other: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    seg = [0.0, 0.0]
    mapped_c = 0.0
    for r in offtake:
        i = 0 if cur.contains(r.date) else 1 if pri.contains(r.date) else None
        if i is None:
            continue
        if r.segment is not None and r.segment != scope.segment:
            if r.is_own:
                other[r.segment][i] += r.sales
            continue
        seg[i] += r.sales
        brand = (r.brand or "").strip()
        if not brand:
            continue
        if i == 0:
            mapped_c += r.sales
        val[brand][i] += r.sales
        f = flags[brand]
        f[0] = f[0] or r.is_own
        f[1] = f[1] or r.is_sibling
        if r.product_id is not None:
            pid_brand[r.product_id] = brand
            if i == 0:
                pid_cur[r.product_id] += r.sales
                if r.sales > 0:
                    n_prod[brand].add(r.product_id)
    if seg[0] <= 0 and seg[1] <= 0:
        return None

    lookup = _unit_lookup(offtake)
    av: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    pr: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for s in shelf:
        if not (_seg_ok(s.segment, scope.segment) and cur.contains(s.date)):
            continue
        brand = (s.brand or pid_brand.get(s.product_id) or "").strip()
        if not brand:
            continue
        w = _shelf_weight(s, lookup, True)
        if s.availability is not None:
            av[brand][0] += s.availability * w
            av[brand][1] += w
        if s.price is not None and s.list_price and s.price > 0:
            pr[brand][0] += s.price * w
            pr[brand][1] += s.list_price * w
    ads: dict[str, float] = defaultdict(float)
    pool = 0.0
    for m in market:
        if not (_seg_ok(m.segment, scope.segment) and cur.contains(m.date)):
            continue
        if m.unit_role is not None and m.unit_role != "generic":
            continue
        ads[m.brand] += m.ad_impressions
        pool += m.ad_impressions

    lanes_n = {_norm(k): v for k, v in (lanes or {}).items()}
    rows = []
    for brand, (c, p) in val.items():
        share_c = 100.0 * c / seg[0] if seg[0] else 0.0
        share_p = 100.0 * p / seg[1] if seg[1] else 0.0
        a = av[brand][0] / av[brand][1] if av[brand][1] > 0 else None
        d = 100.0 * (1.0 - pr[brand][0] / pr[brand][1]) if pr[brand][1] > 0 else None
        sov = 100.0 * ads[brand] / pool if pool > 0 and brand in ads else None
        n = len(n_prod[brand]) if n_prod[brand] else None
        is_own = flags[brand][0]
        rows.append(RivalRow(
            brand=brand, share_c=share_c, share_p=share_p, d_pp=share_c - share_p, value_delta=c - p,
            is_own=is_own, is_sibling=(flags[brand][1] or scope.is_sibling(brand)) and not is_own,
            availability=a, discount_pct=d, ad_sov_pct=sov, n_products=n,
            how=how_they_win(discount_pct=d, availability=a, ad_sov_pct=sov, n_products=n, config=cfg),
            lane=lanes_n.get(_norm(brand)),
        ))
    rows.sort(key=lambda r: (-abs(r.d_pp), r.brand))

    launches = []
    horizon = scope.feed_max - timedelta(days=cfg.launch_lookback_days)
    for p in products:
        if p.is_own or p.is_sibling or p.first_sale_date is None or not _seg_ok(p.segment, scope.segment):
            continue
        v = pid_cur.get(p.product_id, 0.0)
        share = 100.0 * v / seg[0] if seg[0] else 0.0
        if p.first_sale_date >= horizon and v > 0 and share >= cfg.launch_share_pct:
            launches.append(LaunchRow(p.brand or pid_brand.get(p.product_id, ""), p.title or p.product_id,
                                      p.first_sale_date, share))
    launches.sort(key=lambda l: (-l.share_pct, l.title))
    lines = sorted((LineRow(s, v[0], v[1]) for s, v in other.items()),
                   key=lambda l: (-l.pri, l.segment))[: cfg.other_lines_cap]
    return RivalsFacts(
        movers=tuple(rows), join_coverage=100.0 * mapped_c / seg[0] if seg[0] else None,
        launches=tuple(launches), own_other_lines=tuple(lines), own_brand=scope.own_brand,
        own_lane=lanes_n.get(_norm(scope.own_brand)),
    )


def build_facts(
    scope: Scope,
    *,
    offtake: Iterable[OfftakeRow],
    market: Iterable[MarketRow] = (),
    shelf: Iterable[ShelfRow] = (),
    perf: Iterable[PerfRow] = (),
    products: Iterable[Product] = (),
    profile: Any = None,
    loss_rows: Iterable[LossRow] | None = None,
    roles: Mapping[str, str] | None = None,
    lanes: Mapping[str, str] | None = None,
) -> Facts:
    """Run the seven extractors in their fixed order: sell-out, conversion, funnel, availability, voice, pricing, rivals.

    Empty optional feeds leave their fact ``None`` (the matching gates go
    silent); an ads feed that is empty or does not reach the current window
    yields a ``stale`` funnel.  ``profile`` supplies ad-type semantics and
    the aggregate-geo test.  Returns a :class:`Facts` bundle.
    """
    offtake, market, shelf, perf, products = list(offtake), list(market), list(shelf), list(perf), list(products)
    agg = getattr(profile, "is_aggregate_geo", None)
    mid = "click rate" if profile is None or getattr(profile, "has_clicks", False) else "mid-funnel rate"
    return Facts(
        scope=scope,
        offtake=extract_offtake(offtake, scope, is_aggregate_geo=agg),
        conversion=extract_conversion(market, offtake, scope) if market else None,
        funnel=extract_funnel(perf, scope, profile=profile, mid_label=mid),
        availability=extract_availability(shelf, scope, offtake=offtake, loss_rows=loss_rows,
                                          is_aggregate_geo=agg) if shelf else None,
        voice=extract_voice(market, scope, roles=roles) if market else None,
        pricing=extract_pricing(shelf, scope, offtake=offtake) if shelf else None,
        rivals=extract_rivals(offtake, scope, products=products, shelf=shelf, market=market, lanes=lanes),
    )
