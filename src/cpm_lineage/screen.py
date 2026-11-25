"""Correlation pre-screen: which market driver moves with market share?

A cheap first look before a full root-cause run.  Over a handful of weeks
it correlates the account's market share (own sell-out / segment sell-out)
with six candidate drivers, bands each as STRONG / MODERATE / WEAK, routes
the analyst to the dominant one (sign-aware: a strong *inverse* driver is a
drag to investigate, not a lever to add to), and reads the latest discount
push as "share bought / share kept / what it cost".

Own sell-out is deliberately *not* a driver: it is the numerator of the
target, so its correlation with share is tautological.

Correlations over six to eight weekly points are directional only; the
result always carries that caveat.

:func:`weekly_series` builds the weekly inputs from the neutral feeds
(``OfftakeRow``, ``ShelfRow``, ``MarketRow``); :func:`prescreen` is the
decision function; :func:`promo_read` is the discount-episode read.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .schema import MarketRow, OfftakeRow, ShelfRow

__all__ = [
    "ScreenConfig", "WeekPoint", "DriverRead", "PromoRead", "ScreenResult",
    "pearson", "band", "routing", "promo_read", "weekly_series", "prescreen",
    "STRONG", "MODERATE", "WEAK", "DRIVERS",
]

STRONG, MODERATE, WEAK = "STRONG", "MODERATE", "WEAK"

#: Drivers in test order: (field on :class:`WeekPoint`, label).
DRIVERS: tuple[tuple[str, str], ...] = (
    ("segment_offtake", "category size"),
    ("ad_sov", "ad share of voice"),
    ("organic_sov", "organic share of voice"),
    ("availability", "availability"),
    ("rival_ad_impressions", "competitor ad activity"),
    ("discount_gap", "discount gap"),
)


@dataclass(frozen=True)
class ScreenConfig:
    """Thresholds of the pre-screen."""

    weeks: int = 8                        # default window: eight weekly points
    min_weeks: int = 6                    # fewer paired weeks than this: no correlation is reported
    strong_r: float = 0.7                 # |r| at or above -> STRONG
    moderate_r: float = 0.4               # |r| at or above -> MODERATE, else WEAK
    rival_note_r: float = -0.5            # competitor activity at or below this r: competitive-response note
    min_segment_offtake: float = 1.0      # weeks with segment sell-out under this are skipped (no share defined)
    promo_min_sd_pp: float = 1.0          # a discount push must exceed the mean gap by at least 1 pp (or 1 sd)
    promo_baseline_weeks: int = 3         # share baseline before, and retention after, the push
    round_dp: int = 2                     # correlation rounding


_SC = ScreenConfig()


@dataclass(frozen=True)
class WeekPoint:
    """One week of pre-screen inputs.  ``None`` = not observed that week.

    ``availability`` may be 0..1 or percent (correlation is scale-free);
    share-of-voice values and the discount gap are in percentage points.
    """

    week_start: date | None = None
    own_offtake: float = 0.0
    segment_offtake: float = 0.0
    availability: float | None = None
    discount_gap: float | None = None            # own discount % minus segment discount %, pp
    ad_sov: float | None = None                  # 100 * own ad impressions / all ad impressions
    organic_sov: float | None = None             # 100 * own organic impressions / all impressions
    rival_ad_impressions: float | None = None    # all ad impressions minus own

    @property
    def share(self) -> float | None:
        """Market share in percent, ``None`` when the segment has no sell-out."""
        return 100.0 * self.own_offtake / self.segment_offtake if self.segment_offtake > 0 else None


@dataclass(frozen=True)
class DriverRead:
    """Correlation of one driver with market share."""

    driver: str
    r_contemp: float
    r_lag1: float | None                  # driver leading share by one week
    signal: str                           # STRONG | MODERATE | WEAK
    note: str | None = None


@dataclass(frozen=True)
class PromoRead:
    """Read of the hottest discount push in the window (an estimate)."""

    weeks: int                            # length of the contiguous push
    start_index: int
    end_index: int
    peak_gap: float                       # pp
    bought_pp: float                      # share during the push minus the baseline before it
    kept_pp: float | None                 # share after the push minus the baseline; None while it runs
    cost: float                           # sum(max(gap - mean gap, 0) / 100 * own sell-out): an estimate
    ended: bool


@dataclass(frozen=True)
class ScreenResult:
    """Output of :func:`prescreen`."""

    target: str = "market share (%)"
    weeks: int = 0
    drivers: tuple[DriverRead, ...] = ()
    routing: str | None = None
    promo: PromoRead | None = None
    series_share: tuple[float, ...] = ()
    caveats: tuple[str, ...] = ()
    reason: str | None = None             # set when no drivers could be computed


def _arr(values: Sequence[float | None]) -> np.ndarray:
    return np.array([np.nan if v is None else float(v) for v in values], dtype=float)


def pearson(x: Sequence[float | None], y: Sequence[float | None], *, min_n: int = _SC.min_weeks,
            ndigits: int | None = _SC.round_dp) -> float | None:
    """Pearson correlation over the positions where both series are
    observed.  Returns ``None`` when fewer than ``min_n`` pairs remain or
    either side has zero variance; rounded to ``ndigits`` (2) unless ``None``."""
    a, b = _arr(x), _arr(y)
    if a.shape != b.shape:
        return None
    m = ~(np.isnan(a) | np.isnan(b))
    a, b = a[m], b[m]
    if a.size < min_n or float(np.std(a)) == 0.0 or float(np.std(b)) == 0.0:
        return None
    r = float(np.corrcoef(a, b)[0, 1])
    if math.isnan(r):
        return None
    return round(r, ndigits) if ndigits is not None else r


def band(r: float, cfg: ScreenConfig | None = None) -> str:
    """Band a correlation: ``|r| >= 0.7`` STRONG, ``>= 0.4`` MODERATE, else WEAK."""
    cfg = cfg or _SC
    a = abs(r)
    return STRONG if a >= cfg.strong_r else MODERATE if a >= cfg.moderate_r else WEAK


def routing(drivers: Sequence[DriverRead]) -> str:
    """Where to look first, sign-aware.

    ``drivers`` must be sorted by ``|r|`` descending.  No STRONG driver ->
    run the full step sequence; top STRONG driver with ``r >= 0`` -> lead
    with it (a lever to lean on); with ``r < 0`` -> strongest but inverse:
    a drag / displacement read to investigate, not a lever to add to.
    """
    strong = [d for d in drivers if d.signal == STRONG]
    if not strong:
        return "no single dominant driver - run the full step sequence"
    top = strong[0]
    if top.r_contemp >= 0:
        return f"lead with {top.driver} (moves with share - a lever to lean on)"
    return (f"{top.driver} is strongest but INVERSE - a drag/displacement read to investigate, "
            "NOT a lever to add to")


def promo_read(gap: Sequence[float | None], share: Sequence[float | None], own_offtake: Sequence[float | None],
               cfg: ScreenConfig | None = None) -> PromoRead | None:
    """Did the latest discount push buy share, and did the share stay?

    ``gap`` = weekly discount gap (pp), ``share`` = weekly market share
    (%), ``own_offtake`` = weekly own sell-out; equal lengths.  Needs at
    least ``min_weeks`` observed gap weeks.

    ``thr = mean(gap) + max(sd(gap), 1 pp)``; hot weeks have ``gap >= thr``;
    the episode is the contiguous hot run around the hottest week.
    ``bought_pp = mean(share during) - mean(share over up to 3 observed
    weeks before)``; ``kept_pp = mean(share over the first 3 weeks after) -
    baseline`` (``None`` while the push still runs); ``cost = sum over the
    episode of max(gap - mean, 0) / 100 x own sell-out``.

    Returns a :class:`PromoRead` or ``None`` (too few weeks, no hot week, or
    no baseline before the push).
    """
    cfg = cfg or _SC
    if not (len(gap) == len(share) == len(own_offtake)):
        return None
    g, ms, off = _arr(gap), _arr(share), _arr(own_offtake)
    ok = ~np.isnan(g)
    if int(ok.sum()) < cfg.min_weeks:
        return None
    mu, sd = float(np.mean(g[ok])), float(np.std(g[ok]))
    thr = mu + max(sd, cfg.promo_min_sd_pp)
    hot = [i for i in range(len(g)) if ok[i] and g[i] >= thr]
    if not hot:
        return None
    peak = max(hot, key=lambda i: g[i])
    hs = set(hot)
    s = e = peak
    while s - 1 in hs:
        s -= 1
    while e + 1 in hs:
        e += 1
    before = [float(ms[i]) for i in range(s) if not np.isnan(ms[i])][-cfg.promo_baseline_weeks:]
    during = [float(ms[i]) for i in range(s, e + 1) if not np.isnan(ms[i])]
    if not before or not during:
        return None
    base = sum(before) / len(before)
    ended = e < len(g) - 1
    after = [float(ms[i]) for i in range(e + 1, len(g)) if not np.isnan(ms[i])][: cfg.promo_baseline_weeks]
    kept = (sum(after) / len(after) - base) if (ended and after) else None
    cost = sum(max(float(g[i]) - mu, 0.0) / 100.0 * (0.0 if np.isnan(off[i]) else float(off[i]))
               for i in range(s, e + 1))
    return PromoRead(weeks=e - s + 1, start_index=s, end_index=e, peak_gap=float(g[peak]),
                     bought_pp=sum(during) / len(during) - base, kept_pp=kept, cost=cost, ended=ended)


def weekly_series(
    offtake: Iterable[OfftakeRow],
    shelf: Iterable[ShelfRow] = (),
    market: Iterable[MarketRow] = (),
    *,
    anchor: date | None = None,
    weeks: int = _SC.weeks,
    segment: str | None = None,
) -> list[WeekPoint]:
    """Aggregate the market feeds into ``weeks`` seven-day buckets ending at ``anchor``.

    Per week: own and segment sell-out; ``availability = sum(available x
    weight) / sum(checks x weight)`` over own products; ``discount % = (1 -
    sell-out at selling price / sell-out at list price) x 100`` for own and
    for the segment, and their gap; ``ad_sov = 100 x own ad impressions /
    all ad impressions``; ``organic_sov = 100 x own organic impressions /
    all impressions``; ``rival_ad_impressions = all - own``.  A field is
    ``None`` for a week with no source rows (never 0).  ``segment`` scopes
    every feed when given.  ``anchor`` defaults to the latest sell-out date.

    Returns the list of :class:`WeekPoint`, oldest first (empty when there
    is no sell-out data).
    """
    off = [r for r in offtake if segment is None or r.segment == segment]
    if not off or weeks <= 0:
        return []
    anchor = anchor or max(r.date for r in off)
    start = anchor - timedelta(days=7 * weeks - 1)

    def idx(d: date) -> int | None:
        k = (d - start).days // 7
        return k if d >= start and d <= anchor and 0 <= k < weeks else None

    z = [dict(own=0.0, seg=0.0, own_list=0.0, own_sp=0.0, seg_list=0.0, seg_sp=0.0, av=0.0, chk=0.0,
              own_ad=0.0, all_ad=0.0, own_org=0.0, all_tot=0.0, has_sov=False) for _ in range(weeks)]
    for r in off:
        k = idx(r.date)
        if k is None:
            continue
        w = z[k]
        w["seg"] += r.sales
        if r.list_value is not None:
            w["seg_list"] += r.list_value
            w["seg_sp"] += r.sales
        if r.is_own:
            w["own"] += r.sales
            if r.list_value is not None:
                w["own_list"] += r.list_value
                w["own_sp"] += r.sales
    for s in shelf:
        k = idx(s.date)
        if k is None or not s.is_own or (segment is not None and s.segment not in (None, segment)):
            continue
        if s.available_count is None or s.check_count is None:
            continue
        z[k]["av"] += s.available_count * s.weight
        z[k]["chk"] += s.check_count * s.weight
    for m in market:
        k = idx(m.date)
        if k is None or (segment is not None and m.segment not in (None, segment)):
            continue
        w = z[k]
        w["has_sov"] = True
        w["all_ad"] += m.ad_impressions
        w["all_tot"] += m.impressions
        if m.is_own:
            w["own_ad"] += m.ad_impressions
            w["own_org"] += m.organic_impressions

    out: list[WeekPoint] = []
    for k, w in enumerate(z):
        own_disc = (1.0 - w["own_sp"] / w["own_list"]) * 100.0 if w["own_list"] > 0 else None
        seg_disc = (1.0 - w["seg_sp"] / w["seg_list"]) * 100.0 if w["seg_list"] > 0 else None
        sov = w["has_sov"]
        out.append(WeekPoint(
            week_start=start + timedelta(days=7 * k), own_offtake=w["own"], segment_offtake=w["seg"],
            availability=w["av"] / w["chk"] if w["chk"] > 0 else None,
            discount_gap=(own_disc - seg_disc) if own_disc is not None and seg_disc is not None else None,
            ad_sov=100.0 * w["own_ad"] / w["all_ad"] if sov and w["all_ad"] > 0 else None,
            organic_sov=100.0 * w["own_org"] / w["all_tot"] if sov and w["all_tot"] > 0 else None,
            rival_ad_impressions=(w["all_ad"] - w["own_ad"]) if sov else None))
    return out


def _point(p: WeekPoint | Mapping[str, Any]) -> WeekPoint:
    if isinstance(p, WeekPoint):
        return p
    names = WeekPoint.__dataclass_fields__
    return WeekPoint(**{k: v for k, v in p.items() if k in names})


def prescreen(points: Sequence[WeekPoint | Mapping[str, Any]], cfg: ScreenConfig | None = None) -> ScreenResult:
    """Screen the candidate drivers of market share.

    Steps:

    1. Drop weeks whose segment sell-out is under ``min_segment_offtake``
       (share undefined).  Fewer than ``min_weeks`` (6) weeks left -> no
       drivers and a "directional only" reason.
    2. For each driver in :data:`DRIVERS` (own sell-out excluded as
       tautological): ``r_contemp = pearson(driver, share)`` over jointly
       observed weeks (needs 6); ``r_lag1`` pairs ``driver[t]`` with
       ``share[t + 1]`` on the *full* series and masks afterwards, so a gap
       never pairs non-adjacent weeks.  A driver without ``r_contemp`` is
       skipped.
    3. Band each, add the competitive-response note (competitor activity
       with ``r <= -0.5``) and the supply note (availability not WEAK).
    4. Sort by ``|r_contemp|`` descending, route with :func:`routing`, and
       read the discount push with :func:`promo_read`.

    Returns a :class:`ScreenResult`.  Never raises on thin data: ``reason``
    says why ``drivers`` is empty.
    """
    cfg = cfg or _SC
    every = [_point(x) for x in points]
    # The full series is kept, with an undefined share on a thin week, so the lag below pairs
    # week t with week t + 1 and a dropped week breaks the pair instead of shifting the series.
    full_share = [p.share if p.segment_offtake >= cfg.min_segment_offtake else None for p in every]
    pts = [p for p in every if p.segment_offtake >= cfg.min_segment_offtake]
    share = [p.share for p in pts]
    caveats = [f"{len(pts)} weekly points: correlation is directional, not causal.",
               "Own sell-out is excluded as a driver: it is the numerator of market share."]
    series = tuple(float(s) for s in share if s is not None)
    if len(pts) < cfg.min_weeks:
        return ScreenResult(weeks=len(pts), series_share=series, caveats=tuple(caveats),
                            reason=f"{len(pts)} weeks is below the {cfg.min_weeks}-week minimum; directional only")
    reads: list[DriverRead] = []
    for fld, label in DRIVERS:
        xs = [getattr(p, fld) for p in every]
        r = pearson(xs, full_share, min_n=cfg.min_weeks, ndigits=cfg.round_dp)
        if r is None:
            continue
        lag = pearson(xs[:-1], full_share[1:], min_n=cfg.min_weeks, ndigits=cfg.round_dp)
        sig = band(r, cfg)
        note = None
        if fld == "rival_ad_impressions" and r <= cfg.rival_note_r:
            note = ("competitor scaling dilutes your share of voice - a competitive-response signal, "
                    "not own-bid failure")
        elif fld == "availability" and sig != WEAK:
            note = "supply - coupled with organic share of voice (one mechanism, not two); fix supply before ads"
        reads.append(DriverRead(driver=label, r_contemp=r, r_lag1=lag, signal=sig, note=note))
    reads.sort(key=lambda d: -abs(d.r_contemp))
    promo = promo_read([p.discount_gap for p in pts], share, [p.own_offtake for p in pts], cfg)
    if promo is not None:
        caveats.append("Promo cost is an estimate: excess discount depth times own sell-out.")
    return ScreenResult(weeks=len(pts), drivers=tuple(reads), routing=routing(reads), promo=promo,
                        series_share=series, caveats=tuple(caveats),
                        reason=None if reads else "no driver had enough paired weeks or any variance")
