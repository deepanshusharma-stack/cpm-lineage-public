"""Watchdog tripwires: a handful of cheap alerts per (advertiser, segment).

:func:`tripwires` is a pure function over three pre-computed market reads
(rival share moves, own availability / share of voice, rival launches) and
returns at most five ``(severity, line)`` alerts, severity 1 = worst.

The watchlist and the sweep are kept pure too: :func:`arm` returns a new
watchlist, and :func:`sweep` takes an injected loader, so persistence and
data access stay in the caller (adapters / store).

Units: share and share-of-voice changes are in percentage points;
availability is on the 0..1 scale (values above 1.5 are read as percent)
and its change is a 0..1 fraction (``-0.03`` = three points down).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import AvailabilityPolicy

__all__ = ["WatchdogConfig", "Alert", "tripwires", "arm", "sweep", "alert_lines"]


@dataclass(frozen=True)
class WatchdogConfig:
    """Tripwire thresholds."""

    rival_share_gain_pp: float = 0.5        # a rival gaining this many share points is a severity-1 alert
    availability_floor: float = 0.85        # own availability under this: ad money is hitting empty shelves
    availability_drop: float = -0.03        # else a drop of three points or more: severity 2
    ad_sov_drop_pp: float = -5.0            # own ad share of voice down five points or more: severity 2
    launch_trend: str = "ramping"           # rival launches with this trend are flagged (severity 3)
    max_alerts: int = 5                     # alert lines per pair
    window_days: int = 30                   # data window the sweep loader is asked for


_WC = WatchdogConfig()


@dataclass(frozen=True, order=True)
class Alert:
    """One tripwire line.  Orders by severity, then text."""

    severity: int
    line: str

    def as_tuple(self) -> tuple[int, str]:
        return self.severity, self.line


def _g(obj: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        v = obj.get(key) if isinstance(obj, Mapping) else getattr(obj, key, None)
        if v is not None:
            return v
    return default


def tripwires(
    competition: Any = None,
    own: Any = None,
    launches: Any = None,
    cfg: WatchdogConfig | None = None,
) -> list[Alert]:
    """Decide which market alerts to raise for one advertiser and segment.

    Inputs (objects or mappings; every block is optional):

    * ``competition``: ``rows`` of ``{brand, is_own, is_house (or
      is_sibling), share_delta_pp}`` plus optional ``segment_sales`` (the
      segment's sell-out, to size a share move in money);
    * ``own``: ``{availability, availability_delta, ad_sov_delta_pp}``;
      availability may be on the 0..1 or the percent scale, the delta on
      the same scale as the level (fraction, or percentage points);
    * ``launches``: ``rows`` of ``{brand, is_own, trend, title}``.

    Rules, in order:

    1. each rival that is neither own nor same-house with a share gain of
       ``rival_share_gain_pp`` (0.5 pp) or more -> severity 1, sized as
       ``segment_sales / 100 x gain`` when the segment size is known;
    2. own availability under ``availability_floor`` (0.85) -> severity 1;
       *else* an availability change of ``availability_drop`` (-0.03) or
       worse -> severity 2;
    3. own ad share of voice change of ``ad_sov_drop_pp`` (-5 pp) or worse
       -> severity 2;
    4. each rival launch whose trend is ``ramping`` -> severity 3.

    Returns the alerts sorted by severity then text, capped at
    ``max_alerts`` (5).
    """
    cfg = cfg or _WC
    out: list[Alert] = []
    seg = _g(competition, "segment_sales") if competition is not None else None
    for r in (_g(competition, "rows", default=()) if competition is not None else ()):
        if _g(r, "is_own", default=False) or _g(r, "is_house", "is_sibling", default=False):
            continue
        d = _g(r, "share_delta_pp")
        if d is not None and d >= cfg.rival_share_gain_pp:
            line = f"{_g(r, 'brand', default='a rival')} gained {d:+.1f} pp of share"
            if seg:
                line += f" (about {seg / 100.0 * d:,.0f} of segment sales)"
            out.append(Alert(1, line))
    if own is not None:
        raw_av = _g(own, "availability")
        av = AvailabilityPolicy.normalise(raw_av)
        av_d = _g(own, "availability_delta")
        # The delta rides the scale of the level: a level given in percent (> 1.5) means the
        # delta is in percentage points too.  A delta larger than 1 in size can only be points.
        if av_d is not None and ((raw_av is not None and raw_av > 1.5) or abs(av_d) > 1.0):
            av_d = av_d / 100.0
        if av is not None and av < cfg.availability_floor:
            out.append(Alert(1, f"own availability {av:.0%} is under the {cfg.availability_floor:.0%} floor: "
                                "ad spend is landing on empty shelves"))
        elif av_d is not None and av_d <= cfg.availability_drop:
            out.append(Alert(2, f"own availability fell {abs(av_d) * 100.0:.1f} pp"))
        sov_d = _g(own, "ad_sov_delta_pp")
        if sov_d is not None and sov_d <= cfg.ad_sov_drop_pp:
            out.append(Alert(2, f"own ad share of voice fell {abs(sov_d):.1f} pp"))
    for r in (_g(launches, "rows", default=()) if launches is not None else ()):
        if _g(r, "is_own", default=False) or _g(r, "trend") != cfg.launch_trend:
            continue
        out.append(Alert(3, f"{_g(r, 'brand', default='a rival')} launch is ramping: "
                            f"{_g(r, 'title', 'product_id', default='new product')}"))
    return sorted(out)[: cfg.max_alerts]


def arm(watchlist: Iterable[tuple[str, str | None]], advertiser_id: str,
        segment: str | None = None) -> list[tuple[str, str | None]]:
    """Add ``(advertiser_id, segment)`` to a watchlist if absent.  Returns a
    new list (order preserved); the caller persists it."""
    pairs = list(watchlist)
    pair = (advertiser_id, segment)
    if pair not in pairs:
        pairs.append(pair)
    return pairs


def sweep(
    watchlist: Iterable[tuple[str, str | None]],
    loader: Callable[[str, str | None, int], tuple[Any, Any, Any]],
    cfg: WatchdogConfig | None = None,
) -> dict[tuple[str, str | None], list[Alert]]:
    """Run the tripwires for every watched pair.

    ``loader(advertiser_id, segment, window_days)`` returns the
    ``(competition, own, launches)`` reads (data access is the caller's).
    A pair whose loader raises yields one severity-3 "sweep failed" alert
    and the sweep continues.

    Returns ``{(advertiser_id, segment): [Alert, ...]}``.
    """
    cfg = cfg or _WC
    out: dict[tuple[str, str | None], list[Alert]] = {}
    for adv, seg in watchlist:
        try:
            comp, own, launches = loader(adv, seg, cfg.window_days)
            out[(adv, seg)] = tripwires(comp, own, launches, cfg)
        except Exception as exc:  # noqa: BLE001 - one bad pair must not stop the sweep
            out[(adv, seg)] = [Alert(3, f"sweep failed: {type(exc).__name__}")]
    return out


def alert_lines(alerts: Sequence[Alert]) -> list[str]:
    """Render alerts as ``"[severity] line"`` strings."""
    return [f"[{a.severity}] {a.line}" for a in alerts]
