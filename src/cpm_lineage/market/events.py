"""Break detection, same-day movers, launches and demand seasonality."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from ..config import Economics
from ..schema import MarketRow, OfftakeRow, Product, ShelfRow, Window
from ._util import in_window
from .config import MarketConfig

__all__ = [
    "BreakEvent", "Mover", "EventDating", "Launch", "Seasonality",
    "daily_share", "daily_ad_sov", "daily_availability", "daily_rival_ad_impressions",
    "break_scan", "event_dating", "launches", "demand_seasonality",
]

_MC = MarketConfig()
Series = Sequence[tuple[date, float]]


@dataclass(frozen=True)
class BreakEvent:
    """The single largest day-over-day break in a daily series."""

    date: date
    delta: float
    sigmas: float     # |delta| / sd of the other deltas (inf when they are all equal)


def break_scan(series: Series, cfg: MarketConfig | None = None) -> BreakEvent | None:
    """Find the one largest day-over-day break in a daily share series.

    Needs ``break_min_points`` points.  The candidate is the largest
    absolute day-over-day change; its yardstick ``sd`` is the population
    standard deviation of the OTHER deltas (the candidate is left out so a
    spike does not inflate its own yardstick; ``break_min_others`` needed).
    Returned only when ``|delta| >= max(break_sigma x sd, break_min_pp)``.
    A quiet window returns ``None``: never invent an event.
    """
    cfg = cfg or _MC
    ser = sorted(series)
    if len(ser) < cfg.break_min_points:
        return None
    deltas = [(ser[i][0], ser[i][1] - ser[i - 1][1]) for i in range(1, len(ser))]
    idx = max(range(len(deltas)), key=lambda i: abs(deltas[i][1]))
    others = [d for i, (_, d) in enumerate(deltas) if i != idx]
    if len(others) < cfg.break_min_others:
        return None
    mean = sum(others) / len(others)
    sd = (sum((d - mean) ** 2 for d in others) / len(others)) ** 0.5
    day, delta = deltas[idx]
    if abs(delta) < max(cfg.break_sigma * sd, cfg.break_min_pp):
        return None
    return BreakEvent(day, delta, abs(delta) / sd if sd > 0 else float("inf"))


def daily_share(offtake_rows: Iterable[OfftakeRow], window: Window | None = None) -> list[tuple[date, float]]:
    """Daily own market share in percent (days with no segment sales are skipped)."""
    acc: dict[date, list[float]] = {}
    for o in offtake_rows:
        if in_window(o.date, window):
            a = acc.setdefault(o.date, [0.0, 0.0])
            a[1] += o.sales or 0.0
            if o.is_own:
                a[0] += o.sales or 0.0
    return [(d, 100.0 * a[0] / a[1]) for d, a in sorted(acc.items()) if a[1] > 0]


def daily_ad_sov(market_rows: Iterable[MarketRow], window: Window | None = None) -> list[tuple[date, float]]:
    """Daily own ad share of voice in percent."""
    acc: dict[date, list[float]] = {}
    for m in market_rows:
        if in_window(m.date, window):
            a = acc.setdefault(m.date, [0.0, 0.0])
            a[1] += m.ad_impressions or 0.0
            if m.is_own:
                a[0] += m.ad_impressions or 0.0
    return [(d, 100.0 * a[0] / a[1]) for d, a in sorted(acc.items()) if a[1] > 0]


def daily_availability(shelf_rows: Iterable[ShelfRow], window: Window | None = None) -> list[tuple[date, float]]:
    """Daily own availability in percent points (0..100), weight-adjusted."""
    acc: dict[date, list[float]] = {}
    for s in shelf_rows:
        if s.is_own and s.check_count and s.available_count is not None and in_window(s.date, window):
            a = acc.setdefault(s.date, [0.0, 0.0])
            a[0] += s.available_count * s.weight
            a[1] += s.check_count * s.weight
    return [(d, 100.0 * a[0] / a[1]) for d, a in sorted(acc.items()) if a[1] > 0]


def daily_rival_ad_impressions(market_rows: Iterable[MarketRow], window: Window | None = None
                               ) -> dict[str, list[tuple[date, float]]]:
    """Daily ad impressions per rival brand."""
    acc: dict[str, dict[date, float]] = {}
    for m in market_rows:
        if not m.is_own and in_window(m.date, window):
            d = acc.setdefault(m.brand, {})
            d[m.date] = d.get(m.date, 0.0) + (m.ad_impressions or 0.0)
    return {b: sorted(d.items()) for b, d in acc.items()}


@dataclass(frozen=True)
class Mover:
    """Something else that moved on the break day."""

    kind: str          # "availability" | "ad_sov" | "rival_ad"
    name: str
    change: float      # points for availability / ad_sov, percent for rival_ad


@dataclass(frozen=True)
class EventDating:
    """A dated break and what moved with it (empty on a quiet window)."""

    event: BreakEvent | None = None
    movers: tuple[Mover, ...] = ()
    reason: str | None = None


def _day_change(series: Series | None, day: date) -> tuple[float, float] | None:
    if not series:
        return None
    m = dict(series)
    prev = day - timedelta(days=1)
    if day in m and prev in m:
        return m[prev], m[day]
    return None


def event_dating(
    share_series: Series,
    *,
    availability_series: Series | None = None,
    ad_sov_series: Series | None = None,
    rival_ad_series: Mapping[str, Series] | None = None,
    cfg: MarketConfig | None = None,
) -> EventDating:
    """Date the largest share break and list same-day movers.

    Runs :func:`break_scan` on the daily share; on the break day reports
    own availability moves of at least ``mover_availability_pp`` points,
    own ad share-of-voice moves of at least ``mover_ad_sov_pp`` points and
    rival ad-impression moves of at least ``mover_rival_ad_pct`` percent.
    Co-movement is a lead, not a cause.
    """
    cfg = cfg or _MC
    ev = break_scan(share_series, cfg)
    if ev is None:
        return EventDating(reason="quiet window: no break clears the threshold")
    movers: list[Mover] = []
    c = _day_change(availability_series, ev.date)
    if c and abs(c[1] - c[0]) >= cfg.mover_availability_pp:
        movers.append(Mover("availability", "own availability", c[1] - c[0]))
    c = _day_change(ad_sov_series, ev.date)
    if c and abs(c[1] - c[0]) >= cfg.mover_ad_sov_pp:
        movers.append(Mover("ad_sov", "own ad share of voice", c[1] - c[0]))
    for brand, ser in sorted((rival_ad_series or {}).items()):
        c = _day_change(ser, ev.date)
        if c and c[0] > 0:
            ch = 100.0 * (c[1] / c[0] - 1.0)
            if abs(ch) >= cfg.mover_rival_ad_pct:
                movers.append(Mover("rival_ad", brand, ch))
    return EventDating(ev, tuple(movers))


@dataclass(frozen=True)
class Launch:
    """A recently launched product in the segment."""

    product_id: str
    brand: str | None
    is_own: bool
    title: str | None
    first_seen: date
    days_alive: int
    sales_current: float
    rate_all: float          # lifetime sales per day
    rate_current: float      # current-window sales per day alive in it
    trend: str               # "ramping" | "steady" | "fading"


def launches(
    offtake_rows: Iterable[OfftakeRow],
    window: Window,
    *,
    products: Iterable[Product] = (),
    history_start: date | None = None,
    economics: Economics | None = None,
    cfg: MarketConfig | None = None,
) -> list[Launch]:
    """List products launched recently, with a ramping / steady / fading read.

    A launch is a product first seen (first sale in the feed, or the
    catalogue's ``first_sale_date`` when later evidence is absent) within
    ``launch_window_days`` of ``window.end`` AND more than
    ``launch_history_gate_days`` after the feed's history start (honesty
    gate: a product "first seen" on day one of the data is not a launch)
    AND with current-window sales of at least
    ``money(launch_min_sales_ref)``.  ``rate_all = lifetime sales / days
    alive``; ``rate_current = window sales / min(window days, days
    alive)``; ``ramping`` when current >= ``launch_ramping`` x lifetime,
    ``fading`` when below ``launch_fading`` x lifetime.
    """
    cfg, economics = cfg or _MC, economics or Economics()
    rows = [o for o in offtake_rows if o.product_id and o.date <= window.end]
    if not rows:
        return []
    start = history_start or min(o.date for o in rows)
    cat = {p.product_id: p for p in products}
    first: dict[str, date] = {}
    tot: dict[str, float] = {}
    cur: dict[str, float] = {}
    meta: dict[str, OfftakeRow] = {}
    for o in rows:
        pid = o.product_id or ""
        if (o.sales or 0) > 0 and (pid not in first or o.date < first[pid]):
            first[pid] = o.date
        tot[pid] = tot.get(pid, 0.0) + (o.sales or 0.0)
        if in_window(o.date, window):
            cur[pid] = cur.get(pid, 0.0) + (o.sales or 0.0)
        meta.setdefault(pid, o)
    floor = economics.money(cfg.launch_min_sales_ref)
    out = []
    for pid, f in first.items():
        alive = (window.end - f).days + 1
        if alive > cfg.launch_window_days or (f - start).days <= cfg.launch_history_gate_days:
            continue
        sc = cur.get(pid, 0.0)
        if sc < floor:
            continue
        rate_all = tot[pid] / alive
        rate_cur = sc / min(window.days, alive)
        trend = ("ramping" if rate_cur >= cfg.launch_ramping * rate_all
                 else "fading" if rate_cur < cfg.launch_fading * rate_all else "steady")
        p = cat.get(pid)
        out.append(Launch(pid, (p.brand if p else None) or meta[pid].brand, meta[pid].is_own,
                          p.title if p else None, f, alive, sc, rate_all, rate_cur, trend))
    out.sort(key=lambda x: (-x.sales_current, x.product_id))
    return out


@dataclass(frozen=True)
class Seasonality:
    """One observed cycle of monthly segment sales (not a seasonal index)."""

    months: tuple[str, ...]
    sales: tuple[float, ...]
    mean: float
    peak: str
    trough: str
    current_vs_mean_pct: float | None


def demand_seasonality(offtake_rows: Iterable[OfftakeRow], cfg: MarketConfig | None = None) -> Seasonality | None:
    """Monthly segment sales: peak, trough and the latest month versus the mean.

    Needs ``seasonality_min_months`` months; with more, the last (partial)
    month is dropped.  ``None`` otherwise.
    """
    cfg = cfg or _MC
    vol: dict[str, float] = {}
    for o in offtake_rows:
        k = f"{o.date.year:04d}-{o.date.month:02d}"
        vol[k] = vol.get(k, 0.0) + (o.sales or 0.0)
    months = sorted(vol)
    if len(months) < cfg.seasonality_min_months:
        return None
    if len(months) > cfg.seasonality_min_months:
        months = months[:-1]
    vals = [vol[m] for m in months]
    mean = sum(vals) / len(vals)
    return Seasonality(tuple(months), tuple(vals), mean, months[vals.index(max(vals))],
                       months[vals.index(min(vals))], None if mean <= 0 else 100.0 * (vals[-1] / mean - 1.0))
