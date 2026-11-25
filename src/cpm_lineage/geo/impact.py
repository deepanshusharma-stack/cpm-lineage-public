"""Money at stake per geo: what is bleeding, blocked or capturable.

Sizes each geo's market position in money per analysis window so geos
can be ranked.  Figures are labelled with how they were derived; the
"capturable" numbers are simple models, not forecasts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from ..config import AvailabilityPolicy
from ..market._util import get
from .config import GeoConfig

__all__ = ["TIERS", "ImpactCandidate", "GeoImpact", "ImpactResult", "geo_candidates", "impact_sizing"]

_GC = GeoConfig()
TIERS: tuple[str, ...] = ("CRITICAL", "NEEDS ATTENTION", "ON TRACK")
_TIER_ORDER = {t: i for i, t in enumerate(TIERS)}


@dataclass(frozen=True)
class ImpactCandidate:
    """One sized reading of a geo."""

    money: float
    tier: str            # "CRITICAL" | "NEEDS ATTENTION" | "ON TRACK"
    kind: str            # "Bleed" | "Blocked" | "Capturable"
    code: str            # "share-loss" | "share-loss-supply" | "recall-gap" | "run-rate" | "segment-growth"
    basis: str           # how the number was derived, with an origin label


@dataclass(frozen=True)
class GeoImpact:
    """A geo with its primary candidate and every other candidate."""

    geo: str
    primary: ImpactCandidate
    candidates: tuple[ImpactCandidate, ...]


@dataclass(frozen=True)
class ImpactResult:
    """Ranked geos plus a portfolio roll-up of PRIMARY money per kind."""

    geos: tuple[GeoImpact, ...] = ()
    by_kind: Mapping[str, float] = field(default_factory=dict)
    tier_counts: Mapping[str, int] = field(default_factory=dict)
    highest_impact: GeoImpact | None = None
    reason: str | None = None


def _order(c: ImpactCandidate) -> tuple[int, float]:
    return (_TIER_ORDER[c.tier], -c.money)


def geo_candidates(
    geo: Any,
    segment_growth_pct: float | None = None,
    cfg: GeoConfig | None = None,
) -> list[ImpactCandidate]:
    """Size one geo (a :class:`~cpm_lineage.geo.quadrants.GeoRow` or a
    mapping with ``segment_sales, share_pct, share_trend_pp, availability,
    organic_sov_pct``).

    =====================================  ==================================  ==========================
    condition                              money                               tier / kind / code
    =====================================  ==================================  ==========================
    share falling, availability low        ``|trend| / 100 x segment sales``   CRITICAL / Bleed / share-loss-supply
    share falling otherwise                ``|trend| / 100 x segment sales``   CRITICAL / Bleed / share-loss
    organic voice above share              ``(organic - share) / 100 x sales``  NEEDS ATTENTION / Blocked / recall-gap
    share rising                           ``trend / 100 x sales x 2``         ON TRACK / Capturable / run-rate
    segment growth above the capture line  ``sales x growth / 100 x share / 100``  ON TRACK / Capturable / segment-growth
    =====================================  ==================================  ==========================

    "Availability low" is below ``supply_bleed_availability`` (0..1; a
    value above 1.5 is read as percent).  Candidates are sorted tier first,
    then money: a bleed is never masked by a larger capturable on the same
    geo.
    """
    cfg = cfg or _GC
    size = get(geo, "segment_sales") or 0.0
    share = get(geo, "share_pct")
    trend = get(geo, "share_trend_pp")
    av = AvailabilityPolicy.normalise(get(geo, "availability"))
    org = get(geo, "organic_sov_pct")
    out: list[ImpactCandidate] = []
    if size <= 0:
        return out
    if trend is not None and trend < 0:
        money = abs(trend) / 100.0 * size
        if av is not None and av < cfg.supply_bleed_availability:
            out.append(ImpactCandidate(money, "CRITICAL", "Bleed", "share-loss-supply",
                                       f"[DERIVED] share fell {abs(trend):.2f} pp with availability at "
                                       f"{100 * av:.0f}%: supply-driven"))
        else:
            out.append(ImpactCandidate(money, "CRITICAL", "Bleed", "share-loss",
                                       f"[DERIVED] share fell {abs(trend):.2f} pp of a segment worth {size:.0f}"))
    if org is not None and share is not None and org > share:
        out.append(ImpactCandidate((org - share) / 100.0 * size, "NEEDS ATTENTION", "Blocked", "recall-gap",
                                   f"[DERIVED] organic voice {org:.1f}% exceeds share {share:.1f}%: "
                                   f"seen but not bought"))
    if trend is not None and trend > 0:
        out.append(ImpactCandidate(trend / 100.0 * size * cfg.run_rate_multiplier, "ON TRACK", "Capturable",
                                   "run-rate", f"[MODEL: run-rate x{cfg.run_rate_multiplier:g}] share is gaining "
                                               f"{trend:.2f} pp per window"))
    if segment_growth_pct is not None and segment_growth_pct > cfg.segment_growth_capture_pct and share:
        out.append(ImpactCandidate(size * segment_growth_pct / 100.0 * share / 100.0, "ON TRACK", "Capturable",
                                   "segment-growth", f"[MODEL: hold share] segment growing "
                                                     f"{segment_growth_pct:.1f}% at a {share:.1f}% share"))
    out.sort(key=_order)
    return out


def impact_sizing(
    geos: Iterable[Any] | None,
    segment_growth_pct: float | None = None,
    cfg: GeoConfig | None = None,
) -> ImpactResult:
    """Rank geos by money at stake.

    Each geo's candidates come from :func:`geo_candidates`; its primary is
    the first by (tier, money).  Geos are sorted the same way.  The
    roll-up sums PRIMARY money per kind and counts geos per tier;
    ``highest_impact`` is the first geo.  Money is per analysis window.
    Returns an :class:`ImpactResult` with ``reason`` set when the geo layer
    is absent or no geo yields a candidate.
    """
    rows = list(geos or ())
    if not rows:
        return ImpactResult(reason="geo layer absent")
    out: list[GeoImpact] = []
    for geo in rows:
        cands = geo_candidates(geo, segment_growth_pct, cfg)
        if cands:
            out.append(GeoImpact(str(get(geo, "geo")), cands[0], tuple(cands)))
    if not out:
        return ImpactResult(reason="no geo yields a candidate")
    out.sort(key=lambda gi: (*_order(gi.primary), gi.geo))
    by_kind: dict[str, float] = {}
    counts: dict[str, int] = {}
    for gi in out:
        by_kind[gi.primary.kind] = by_kind.get(gi.primary.kind, 0.0) + gi.primary.money
        counts[gi.primary.tier] = counts.get(gi.primary.tier, 0) + 1
    return ImpactResult(tuple(out), by_kind, counts, out[0])
