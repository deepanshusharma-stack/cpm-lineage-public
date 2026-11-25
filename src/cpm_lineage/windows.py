"""Windows and data-tier routing.

Every comparison in the library runs on an explicit ``WindowPair``.  The
builders here produce one; nothing reads the wall clock except
``reporting_window``, which takes ``today`` as an argument.

* ``trailing_windows`` -- last ``d`` days versus the ``d`` before, anchored
  to the **data's** latest date (never to "today").
* ``default_windows`` -- the same with the sparse-panel fallbacks used by
  the campaign diagnosis.
* ``resolve_window`` -- a custom range with a day / week / month comparison.
* ``reporting_window`` -- month-to-date complete weeks; the only
  calendar-anchored window.
* ``drill_buckets`` -- current / baseline buckets anchored to the scope's
  last active day.
* ``route_tier`` -- COLD / WARM / MATURE / FULL from the active-day count.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any, Iterable, Sequence, TypeVar

from .config import TierConfig, WindowDefaults
from .schema import DataTier, PerfRow, Window, WindowPair

__all__ = [
    "parse_iso_date", "latest_date", "earliest_date", "trailing_window",
    "trailing_windows", "default_windows", "resolve_window",
    "reporting_window", "drill_buckets", "is_active_day", "active_days",
    "route_tier", "tier_for_rows", "filter_rows", "split_rows",
    "recent_half_start", "tier_allows_move_model",
]

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_R = TypeVar("_R")
_WD = WindowDefaults()
_TC = TierConfig()


def parse_iso_date(value: Any) -> date | None:
    """Strict ``YYYY-MM-DD`` parse; anything else (including ``2026-7-1``) -> ``None``.

    ``date`` objects pass through.
    """
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not _ISO.match(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _dates(items: Iterable[Any]) -> list[date]:
    out = []
    for x in items:
        out.append(x if isinstance(x, date) else x.date)
    return out


def latest_date(items: Iterable[Any]) -> date | None:
    """Latest date among dates or dated records (``None`` if empty)."""
    ds = _dates(items)
    return max(ds) if ds else None


def earliest_date(items: Iterable[Any]) -> date | None:
    """Earliest date among dates or dated records (``None`` if empty)."""
    ds = _dates(items)
    return min(ds) if ds else None


def trailing_window(anchor: date, days: int, label: str = "") -> Window:
    """The ``days``-day window ending at ``anchor`` inclusive."""
    if days < 1:
        raise ValueError("days must be >= 1")
    return Window(anchor - timedelta(days=days - 1), anchor, label)


def trailing_windows(anchor: date, days: int = _WD.rca_days) -> WindowPair:
    """Current = last ``days`` days ending at ``anchor``; prior = the ``days`` before.

    ``cur = [anchor - (days - 1), anchor]``, ``pri = [cur.start - days,
    cur.start - 1]``.  ``anchor`` should be the latest date in the feed.
    """
    cur = trailing_window(anchor, days, "current")
    pri = Window(cur.start - timedelta(days=days), cur.start - timedelta(days=1), "prior")
    return WindowPair(prior=pri, current=cur, cmp="trailing")


def default_windows(
    items: Iterable[Any],
    days: int = _WD.identity_days,
    min_rows: int = _WD.min_rows,
) -> WindowPair | None:
    """Period-over-period windows for a (possibly sparse) day panel.

    ``items`` are the panel's dates or dated records, one per kept day.

    1. ``dmax`` = latest date.  Current = days with ``date > dmax - days``;
       prior = days in the ``days`` immediately before.
    2. If prior has fewer than ``min_rows`` days: prior = everything before
       the current window.
    3. If either side still has fewer than ``min_rows``: ``k = max(min_rows,
       n // 2)``; current = last ``k`` days, prior = first ``k`` days (on a
       very short panel the two can overlap; the note says so).

    Returns a ``WindowPair`` whose windows span the first and last day
    actually used on each side, or ``None`` for an empty panel.
    """
    ds = sorted(set(_dates(items)))
    if not ds:
        return None
    dmax = ds[-1]
    cur_start = dmax - timedelta(days=days - 1)
    pri_start = cur_start - timedelta(days=days)
    cur = [d for d in ds if d >= cur_start]
    pri = [d for d in ds if pri_start <= d < cur_start]
    cmp, note = "trailing", ""
    if len(pri) < min_rows:
        pri = [d for d in ds if d < cur_start]
        if pri:
            note = "prior window widened to all earlier days (too few days in the standard prior window)"
            cmp = "fallback"
    if len(pri) < min_rows or len(cur) < min_rows:
        k = max(min_rows, len(ds) // 2)
        cur, pri = ds[-k:], ds[:k]
        cmp = "fallback"
        note = f"sparse panel: comparing the first {len(pri)} and last {len(cur)} active days"
        if len(ds) < 2 * k:
            note += " (windows overlap)"
    elif cmp == "trailing":
        # Standard case: report the nominal calendar windows.
        return WindowPair(
            prior=Window(pri_start, cur_start - timedelta(days=1), "prior"),
            current=Window(cur_start, dmax, "current"),
            cmp="trailing",
        )
    return WindowPair(
        prior=Window(pri[0], pri[-1], "prior"), current=Window(cur[0], cur[-1], "current"),
        cmp=cmp, note=note,
    )


def _shift_month_back(d: date) -> date:
    y, m = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def resolve_window(
    date_from: Any,
    date_to: Any,
    cmp: str = "wow",
    clamp_to: date | None = None,
    cfg: WindowDefaults | None = None,
) -> WindowPair | None:
    """Resolve a custom date range and its comparison window.

    * Both dates must be ``date`` objects or strict ``YYYY-MM-DD`` strings,
      else ``None``.  Reversed dates are swapped.  ``n`` = inclusive days.
    * ``clamp_to`` (the feed's latest date): an end beyond it is clamped and
      the note says so.
    * ``cmp="dod"`` shifts back 1 day, ``"wow"`` 7 days.  If ``n`` exceeds
      the unit the shift is ``n`` instead (previous equal-length period), so
      windows never overlap; the note says so.
    * ``cmp="mom"``: the same day-of-month in the previous month, clamped to
      that month's length.  If the shifted end would reach the current start
      it falls back to the previous ``n``-day period, with a note.
    * Unknown ``cmp`` -> ``None``.

    Returns a ``WindowPair`` (``.as_dict()`` gives ``cur_from, cur_to,
    pri_from, pri_to, days, note, cmp``).
    """
    cfg = cfg or _WD
    a, b = parse_iso_date(date_from), parse_iso_date(date_to)
    if a is None or b is None or cmp not in ("dod", "wow", "mom"):
        return None
    if b < a:
        a, b = b, a
    notes: list[str] = []
    if clamp_to is not None and b > clamp_to:
        if a > clamp_to:
            return None
        b = clamp_to
        notes.append(f"end date clamped to the latest data date {clamp_to.isoformat()}")
    n = (b - a).days + 1
    if cmp == "mom":
        pa, pb = _shift_month_back(a), _shift_month_back(b)
        if pb >= a or pb < pa:
            pa, pb = a - timedelta(days=n), b - timedelta(days=n)
            notes.append(f"range spans about a month or more: compared with the previous {n}-day period")
    else:
        unit = cfg.dod_shift_days if cmp == "dod" else cfg.wow_shift_days
        shift = unit
        if n > unit:
            shift = n
            notes.append(f"range is longer than the comparison unit: compared with the previous {n}-day period")
        pa, pb = a - timedelta(days=shift), b - timedelta(days=shift)
    return WindowPair(
        prior=Window(pa, pb, "prior"), current=Window(a, b, "current"),
        cmp=cmp, note="; ".join(notes),
    )


def reporting_window(today: date, cfg: WindowDefaults | None = None) -> Window:
    """Budget reporting window: this month's complete weeks (calendar-anchored).

    1. ``end`` = the most recent completed week-end day strictly before
       ``today`` (a week-end "today" is still in progress and steps back a
       full week).
    2. ``start`` = first day of ``end``'s month.
    3. If that leaves less than one complete week (``end - start < 6``
       days), ``start = end - short_month_span_days`` (trailing 3 weeks).

    Returns an inclusive ``Window``.
    """
    cfg = cfg or _WD
    back = (today.isoweekday() - cfg.week_end_isoweekday) % 7 or 7
    end = today - timedelta(days=back)
    start = end.replace(day=1)
    if (end - start).days < 6:
        start = end - timedelta(days=cfg.short_month_span_days)
    return Window(start, end, "reporting")


def drill_buckets(active_dates: Iterable[Any], days: int = _WD.drill_days) -> WindowPair | None:
    """Drilldown buckets anchored to the scope's last active day.

    Current = ``(dmax - days, dmax]``; baseline = ``(dmax - 2*days, dmax -
    days]``.  ``active_dates`` are days with spend > 0 and impressions > 0
    for the scope.  ``None`` if there is no active day.
    """
    dmax = latest_date(active_dates)
    if dmax is None:
        return None
    cur = Window(dmax - timedelta(days=days - 1), dmax, "current")
    base = Window(dmax - timedelta(days=2 * days - 1), dmax - timedelta(days=days), "baseline")
    return WindowPair(prior=base, current=cur, cmp="trailing")


def is_active_day(spend: float, impressions: float, units: float | None = None, require_units: bool = True) -> bool:
    """Panel row filter: ``spend > 0 and impressions > 0`` (and ``units > 0``).

    The modelling panel keeps only days with conversions
    (``require_units=True``); drilldowns use spend and impressions only.
    """
    if not (spend > 0 and impressions > 0):
        return False
    return (units or 0) > 0 if require_units else True


def active_days(rows: Iterable[PerfRow], require_units: bool = True) -> list[date]:
    """Sorted dates on which the rows' **daily totals** pass ``is_active_day``."""
    tot: dict[date, list[float]] = {}
    for r in rows:
        t = tot.setdefault(r.date, [0.0, 0.0, 0.0])
        t[0] += r.spend
        t[1] += r.impressions
        t[2] += r.units or 0.0
    return sorted(d for d, (s, i, u) in tot.items() if is_active_day(s, i, u, require_units))


def route_tier(n_active_days: int, cfg: TierConfig | None = None) -> DataTier:
    """Route by active-day count: ``< 7`` COLD, ``7-20`` WARM, ``21-34`` MATURE, ``>= 35`` FULL.

    COLD and WARM get no move model and no saturation curve (state-mode
    predictions from account history only); MATURE runs the move model in
    short mode; FULL runs everything.
    """
    cfg = cfg or _TC
    if n_active_days < cfg.warm_min_days:
        return DataTier.COLD
    if n_active_days < cfg.mature_min_days:
        return DataTier.WARM
    if n_active_days < cfg.full_min_days:
        return DataTier.MATURE
    return DataTier.FULL


def tier_allows_move_model(tier: DataTier) -> bool:
    """True for MATURE and FULL."""
    return tier in (DataTier.MATURE, DataTier.FULL)


def tier_for_rows(rows: Iterable[PerfRow], cfg: TierConfig | None = None) -> DataTier:
    """``route_tier`` on the number of active days in ``rows``."""
    return route_tier(len(active_days(rows)), cfg)


def filter_rows(rows: Iterable[_R], window: Window) -> list[_R]:
    """Records whose ``.date`` falls inside ``window`` (inclusive)."""
    return [r for r in rows if window.start <= r.date <= window.end]  # type: ignore[attr-defined]


def split_rows(rows: Sequence[_R], pair: WindowPair) -> tuple[list[_R], list[_R]]:
    """``(prior_rows, current_rows)`` for a window pair."""
    return filter_rows(rows, pair.prior), filter_rows(rows, pair.current)


def recent_half_start(anchor: date, days: int) -> date:
    """First date of the "recent half" of a lookback: rows with ``date > anchor - max(days // 2, 1)``."""
    return anchor - timedelta(days=max(days // 2, 1)) + timedelta(days=1)
