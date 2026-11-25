"""Scoping rules: segment audit, the pure-campaign hard rule and
placement-label validity.

Segment scope is always derived from the product catalogue (segment ->
products -> campaigns advertising them), never from a segment label on
the ads rows and never from a hand-picked campaign list.  A campaign is
eligible for segment-level action only when it is PURE: every product it
advertises belongs to exactly that segment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Collection, Iterable, Mapping, Sequence

from .adapters.base import PlatformProfile
from .liveness import stale_stopped
from .schema import Entity, PerfRow, Product, Window

__all__ = [
    "ScopingConfig", "AuditedCampaign", "PartitionTotals", "SegmentAudit", "PurityResult", "ExcludedCampaign",
    "segment_products", "segment_audit", "pure_campaigns",
    "LabelCampaign", "LabelEvidence", "LabelVerdict", "LabelOptions",
    "label_delivery", "label_evidence", "classify_labels", "label_options",
]


@dataclass(frozen=True)
class ScopingConfig:
    """Named thresholds with their defaults."""

    product_cap: int = 15000              # product ids per segment scope; beyond it the scope is truncated and flagged
    purity_campaign_cap: int = 5000       # campaigns purity-checked; the rest are excluded and flagged, never guessed
    stale_days: int = 7                   # hard stale-stopped window (spend in the last 7 days)
    soft_stale_days: int = 3              # soft window (spend in the last 3 days)
    label_share_floor: float = 0.02       # a label under 2 % of the top label's spend is mislabelled / legacy
    label_spend_days: int = 15            # delivery evidence window for placement labels
    label_new_days: int = 60              # a campaign started within this many days is "new"


_SC = ScopingConfig()
PARTITIONS: tuple[str, ...] = ("keyword", "auto", "reach", "other")


# --------------------------------------------------------------------------
# segment audit
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AuditedCampaign:
    """One campaign's segment-scoped money."""

    entity_id: str
    name: str | None
    ad_type: str | None
    partition: str                   # "keyword" | "auto" | "reach" | "other"
    status: str | None
    status_source: str               # "entity" | "rows" | "none"
    spend: float
    direct_sales: float
    indirect_sales: float | None
    impressions: float
    spend_7d: float
    spend_3d: float
    direct_roas: float | None
    total_roas: float | None


@dataclass(frozen=True)
class PartitionTotals:
    n: int = 0
    spend: float = 0.0
    direct_sales: float = 0.0
    indirect_sales: float = 0.0
    impressions: float = 0.0

    @property
    def direct_roas(self) -> float | None:
        return self.direct_sales / self.spend if self.spend > 0 else None

    @property
    def total_roas(self) -> float | None:
        return (self.direct_sales + self.indirect_sales) / self.spend if self.spend > 0 else None


@dataclass(frozen=True)
class SegmentAudit:
    """Every live campaign advertising a segment's products, by ad-type partition."""

    segment: str
    product_ids: frozenset[str] = frozenset()
    campaigns: tuple[AuditedCampaign, ...] = ()
    by_type: Mapping[str, PartitionTotals] = field(default_factory=dict)
    totals: PartitionTotals = field(default_factory=PartitionTotals)
    keyword_campaign_ids: tuple[str, ...] = ()       # ROAS-judged campaigns
    dropped_stale_stopped: tuple[str, ...] = ()
    products_truncated: bool = False
    reason: str | None = None


def segment_products(products: Iterable[Product], segment: str, *, own_only: bool = True,
                     subset: Collection[str] | None = None) -> set[str]:
    """Product ids of a segment from the catalogue (own products by
    default; ``subset`` optionally narrows to a user-chosen product list)."""
    ids = {p.product_id for p in products if p.segment == segment and (p.is_own or not own_only)}
    return ids if subset is None else ids & set(subset)


def _partition(ad_type: str | None, profile: PlatformProfile | None) -> str:
    if not ad_type:
        return "other"
    if profile is None or not profile.ad_types:
        return "keyword"   # no capability matrix supplied: default capability class
    canon = profile.canonical_ad_type(ad_type)
    if canon not in profile.ad_types:
        return "other"   # type not verified against the capability matrix
    cap = profile.capability(ad_type)
    if not cap.sales_attributed:
        return "reach"
    if cap.auto_targeted:
        return "auto"
    if cap.keyword_drillable:
        return "keyword"
    return "other"


def segment_audit(
    products: Iterable[Product],
    perf_rows: Iterable[PerfRow],
    segment: str,
    window: Window | None = None,
    *,
    entities: Iterable[Entity] = (),
    anchor: date | None = None,
    product_subset: Collection[str] | None = None,
    profile: PlatformProfile | None = None,
    cfg: ScopingConfig | None = None,
) -> SegmentAudit:
    """Audit which campaigns spend on a segment, and how.

    1. segment -> product ids from the catalogue (optionally a subset);
    2. per campaign, over rows advertising those products inside
       ``window``: spend, direct and indirect sales, impressions, plus
       spend in the last 7 and 3 days before ``anchor`` (default: the
       latest row date); ``direct_roas`` and ``total_roas``;
    3. ad type and status from the entity record (the system of record);
       fallback to the campaign's latest row, with ``status_source``
       disclosed;
    4. campaigns that are stale-stopped (see
       :func:`cpm_lineage.liveness.stale_stopped`) are dropped from
       everything and listed;
    5. partition by the adapter's capability matrix: ``keyword``
       (keyword-targeted, judged on ROAS), ``auto`` (auto-targeted),
       ``reach`` (no per-product sales attribution; its ROAS is ``None``,
       "cannot judge", never 0), ``other`` (type not verified).

    Needs product-grain performance rows.  Returns :class:`SegmentAudit`;
    ``reason`` is set when the catalogue has no product for the segment.
    """
    cfg = cfg or _SC
    ids = segment_products(products, segment, subset=product_subset)
    truncated = len(ids) > cfg.product_cap
    if truncated:
        ids = set(sorted(ids)[: cfg.product_cap])
    if not ids:
        return SegmentAudit(segment, reason="the catalogue maps no own product to this segment")
    rows = [r for r in perf_rows if r.product_id in ids]
    if not rows:
        return SegmentAudit(segment, frozenset(ids), products_truncated=truncated,
                            reason="no product-grain performance rows for the segment's products")
    end = anchor or max(r.date for r in rows)
    d7, d3 = end - timedelta(days=cfg.stale_days - 1), end - timedelta(days=cfg.soft_stale_days - 1)
    ent = {e.entity_id: e for e in entities}
    acc: dict[str, list[float]] = {}
    ind_seen: set[str] = set()
    last: dict[str, PerfRow] = {}
    for r in rows:
        a = acc.setdefault(r.entity_id, [0.0] * 6)
        if r.date <= end:
            if r.date >= d7:
                a[4] += r.spend or 0.0
            if r.date >= d3:
                a[5] += r.spend or 0.0
        if r.entity_id not in last or r.date > last[r.entity_id].date:
            last[r.entity_id] = r
        if window is not None and not (window.start <= r.date <= window.end):
            continue
        a[0] += r.spend or 0.0
        a[1] += r.sales or 0.0
        a[3] += r.impressions or 0.0
        if r.indirect_sales is not None:
            a[2] += r.indirect_sales
            ind_seen.add(r.entity_id)
    status_map = profile.status_map if profile is not None else None
    camps: list[AuditedCampaign] = []
    dropped: list[str] = []
    for cid, a in acc.items():
        e = ent.get(cid)
        if e is not None and e.status is not None:
            status, src = e.status, "entity"
        elif last[cid].status is not None:
            status, src = last[cid].status, "rows"
        else:
            status, src = None, "none"
        if stale_stopped(status, a[4], a[5], status_map=status_map):
            dropped.append(cid)
            continue
        ad_type = (e.ad_type if e is not None and e.ad_type else None) or last[cid].ad_type
        ind = a[2] if cid in ind_seen else None
        part = _partition(ad_type, profile)
        camps.append(AuditedCampaign(
            cid, (e.name if e else None) or last[cid].entity_name, ad_type, part,
            status, src, a[0], a[1], ind, a[3], a[4], a[5],
            a[1] / a[0] if a[0] > 0 and part != "reach" else None,
            (a[1] + (ind or 0.0)) / a[0] if a[0] > 0 and part != "reach" else None))
    camps.sort(key=lambda c: (-c.spend, c.entity_id))

    def tot(cs: Sequence[AuditedCampaign]) -> PartitionTotals:
        return PartitionTotals(len(cs), sum(c.spend for c in cs), sum(c.direct_sales for c in cs),
                               sum(c.indirect_sales or 0.0 for c in cs), sum(c.impressions for c in cs))

    by = {p: tot([c for c in camps if c.partition == p]) for p in PARTITIONS}
    return SegmentAudit(segment, frozenset(ids), tuple(camps), by, tot(camps),
                        tuple(c.entity_id for c in camps if c.partition == "keyword"),
                        tuple(sorted(dropped)), truncated)


@dataclass(frozen=True)
class ExcludedCampaign:
    entity_id: str
    spend: float
    reason: str          # "mixed" | "unscoped" | "not-checked"
    detail: str = ""


@dataclass(frozen=True)
class PurityResult:
    """Outcome of the pure-campaign rule."""

    pure_campaign_ids: tuple[str, ...] = ()
    excluded: tuple[ExcludedCampaign, ...] = ()
    purity_incomplete: bool = False


def pure_campaigns(
    audit: SegmentAudit,
    products: Iterable[Product],
    *,
    entities: Iterable[Entity] = (),
    perf_rows: Iterable[PerfRow] = (),
    cfg: ScopingConfig | None = None,
) -> PurityResult:
    """Apply the pure-campaign hard rule to an audit.

    For each audited campaign take its FULL advertised product set (the
    entity's ``product_ids``, else every product on its performance rows,
    with no segment filter) and each product's SET of segments from the
    catalogue:

    * PURE: every product maps to exactly this segment -> eligible;
    * MIXED: any product maps to another segment, or has no mapping ->
      excluded with the reason;
    * UNSCOPED: no product-level scope (reach formats) -> excluded.

    Campaigns beyond ``purity_campaign_cap`` are conservatively excluded
    and ``purity_incomplete`` is set.  Never guess.
    """
    cfg = cfg or _SC
    seg_of: dict[str, set[str]] = {}
    for p in products:
        if p.segment is not None:
            seg_of.setdefault(p.product_id, set()).add(p.segment)
        else:
            seg_of.setdefault(p.product_id, set())
    full: dict[str, set[str]] = {e.entity_id: set(e.product_ids) for e in entities if e.product_ids}
    from_rows: dict[str, set[str]] = {}
    for r in perf_rows:
        if r.product_id:
            from_rows.setdefault(r.entity_id, set()).add(r.product_id)
    pure: list[str] = []
    excluded: list[ExcludedCampaign] = []
    incomplete = False
    for i, c in enumerate(audit.campaigns):
        if i >= cfg.purity_campaign_cap:
            incomplete = True
            excluded.append(ExcludedCampaign(c.entity_id, c.spend, "not-checked", "beyond the purity-check cap"))
            continue
        pids = full.get(c.entity_id) or from_rows.get(c.entity_id) or set()
        if not pids:
            excluded.append(ExcludedCampaign(c.entity_id, c.spend, "unscoped", "no product-level scope"))
            continue
        unknown = sorted(p for p in pids if not seg_of.get(p))
        other = sorted({s for p in pids for s in seg_of.get(p, ()) if s != audit.segment})
        if unknown:
            excluded.append(ExcludedCampaign(c.entity_id, c.spend, "mixed",
                                             f"{len(unknown)} product(s) with no segment mapping"))
        elif other:
            excluded.append(ExcludedCampaign(c.entity_id, c.spend, "mixed",
                                             "also advertises: " + ", ".join(other[:3])))
        else:
            pure.append(c.entity_id)
    return PurityResult(tuple(pure), tuple(excluded), incomplete)


# --------------------------------------------------------------------------
# placement-label validity
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LabelCampaign:
    """A campaign and the placement labels it is configured on."""

    entity_id: str
    labels: Mapping[str, float | None]     # label -> configured bid
    start_date: date | None = None
    is_active: bool = True


@dataclass(frozen=True)
class LabelEvidence:
    """Delivery evidence for one placement label across campaigns."""

    label: str
    spend: float = 0.0
    stray: float = 0.0            # spend that fell under the within-campaign floor
    impressions: float = 0.0
    new: int = 0                  # new campaigns configured on the label
    n_campaigns: int = 0
    cpm: float | None = None
    bid: float | None = None      # median configured bid over active campaigns


@dataclass(frozen=True)
class LabelVerdict:
    label: str
    verdict: str                  # "VALID" | "UNCONFIRMED" | "DROP"
    why: str
    share: float | None           # spend / top label spend
    evidence: LabelEvidence


@dataclass(frozen=True)
class LabelOptions:
    """The evidence tier that decided, and what every tier showed."""

    tier: str | None
    decided: bool                 # True when the tier has a VALID label; False = fallback shown
    labels: tuple[LabelVerdict, ...] = ()
    tiers: Mapping[str, tuple[LabelVerdict, ...]] = field(default_factory=dict)


def label_delivery(perf_rows: Iterable[PerfRow], window: Window | None = None
                   ) -> dict[tuple[str, str], tuple[float, float]]:
    """``{(entity, label): (spend, impressions)}`` from rows whose targeting
    unit is the placement label (pass placement rows only)."""
    acc: dict[tuple[str, str], list[float]] = {}
    for r in perf_rows:
        if not r.targeting_unit or (window is not None and not (window.start <= r.date <= window.end)):
            continue
        a = acc.setdefault((r.entity_id, r.targeting_unit), [0.0, 0.0])
        a[0] += r.spend or 0.0
        a[1] += r.impressions or 0.0
    return {k: (a[0], a[1]) for k, a in acc.items()}


def label_evidence(
    campaigns: Iterable[LabelCampaign],
    delivery: Mapping[tuple[str, str], tuple[float, float]],
    as_of: date | None = None,
    cfg: ScopingConfig | None = None,
) -> dict[str, LabelEvidence]:
    """Aggregate delivery evidence per placement label.

    Per campaign ``top`` = its largest label spend; a label with
    ``spend < label_share_floor x top`` adds to ``stray`` instead of
    ``spend`` (the floor runs WITHIN each campaign first, so one
    campaign's stray label dies without killing a label another advertiser
    really serves on).  Per label: spend, stray, impressions, the number
    of new campaigns (started within ``label_new_days`` of ``as_of``),
    campaign count, ``cpm = 1000 x spend / impressions`` and the median
    bid over active campaigns.
    """
    cfg = cfg or _SC
    acc: dict[str, list[float]] = {}
    bids: dict[str, list[float]] = {}
    for c in campaigns:
        spends = {lb: delivery.get((c.entity_id, lb), (0.0, 0.0)) for lb in c.labels}
        top = max((s for s, _ in spends.values()), default=0.0)
        is_new = bool(as_of and c.start_date and (as_of - c.start_date).days <= cfg.label_new_days)
        for lb, (sp, im) in spends.items():
            a = acc.setdefault(lb, [0.0, 0.0, 0.0, 0.0, 0.0])
            if sp < cfg.label_share_floor * top:
                a[1] += sp
            else:
                a[0] += sp
                a[2] += im
            a[3] += 1 if is_new else 0
            a[4] += 1
            b = c.labels[lb]
            if c.is_active and b is not None:
                bids.setdefault(lb, []).append(float(b))
    out: dict[str, LabelEvidence] = {}
    for lb, a in acc.items():
        bs = sorted(bids.get(lb, []))
        med = None if not bs else (bs[len(bs) // 2] if len(bs) % 2 else (bs[len(bs) // 2 - 1] + bs[len(bs) // 2]) / 2)
        out[lb] = LabelEvidence(lb, a[0], a[1], a[2], int(a[3]), int(a[4]),
                                1000.0 * a[0] / a[2] if a[2] > 0 else None, med)
    return out


_VERDICT_ORDER = {"VALID": 0, "UNCONFIRMED": 1, "DROP": 2}


def classify_labels(evidence: Mapping[str, LabelEvidence], cfg: ScopingConfig | None = None) -> list[LabelVerdict]:
    """Decide which placement labels are real, first match:

    * ``VALID``: ``spend >= label_share_floor x top label spend`` (top > 0);
    * ``UNCONFIRMED``: no spend yet, but configured on a new campaign;
    * ``DROP``: stray spend only, or spend below the floor, or no spend
      and nothing new.

    Sorted VALID, UNCONFIRMED, DROP; spend descending.
    """
    cfg = cfg or _SC
    top = max((e.spend for e in evidence.values()), default=0.0)
    out: list[LabelVerdict] = []
    for e in evidence.values():
        share = e.spend / top if top > 0 else None
        if top > 0 and e.spend > 0 and e.spend >= cfg.label_share_floor * top:
            v, why = "VALID", "delivers a material share of placement spend"
        elif e.spend == 0 and e.stray == 0 and e.new > 0:
            v, why = "UNCONFIRMED", "no delivery yet, but configured on a recently started campaign"
        elif e.spend == 0 and e.stray > 0:
            v, why = "DROP", "stray spend only: negligible inside every campaign that carries it"
        elif e.spend > 0:
            v, why = "DROP", "spend below the floor against the top label: mislabelled or legacy"
        else:
            v, why = "DROP", "no spend and no new campaign"
        out.append(LabelVerdict(e.label, v, why, share, e))
    out.sort(key=lambda x: (_VERDICT_ORDER[x.verdict], -x.evidence.spend, x.label))
    return out


def label_options(
    tiers: Mapping[str, Iterable[LabelCampaign]],
    delivery: Mapping[tuple[str, str], tuple[float, float]],
    as_of: date | None = None,
    cfg: ScopingConfig | None = None,
) -> LabelOptions:
    """Pick placement labels for a product from tiered evidence.

    ``tiers`` is an ORDERED mapping from closest to farthest evidence (the
    source order: own campaigns, same-brand pure campaigns, other brands'
    pure campaigns over the product's peer set).  The closest tier with a
    VALID label decides; otherwise the first non-empty tier is shown as a
    fallback (``decided = False``).  Read-only advice.
    """
    per = {name: tuple(classify_labels(label_evidence(camps, delivery, as_of, cfg), cfg))
           for name, camps in tiers.items()}
    for name, verdicts in per.items():
        if any(v.verdict == "VALID" for v in verdicts):
            return LabelOptions(name, True, verdicts, per)
    for name, verdicts in per.items():
        if verdicts:
            return LabelOptions(name, False, verdicts, per)
    return LabelOptions(None, False, (), per)
