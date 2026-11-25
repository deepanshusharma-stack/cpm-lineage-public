"""Metric algebra: additive sums and the ratios derived from them.

The invariant everything else relies on: **only sums are combined; ratios
are always recomputed from sums and never averaged.**

Identity (impression count cancels)::

    ROAS  = sales / spend
    CPM   = 1000 * spend / impressions
    ICR   = units / impressions            (impression conversion rate)
    ASP   = sales / units                  (average selling price)
    RPM   = 1000 * sales / impressions = 1000 * ICR * ASP
    ROAS  = RPM / CPM = 1000 * ICR * ASP / CPM

ICR replaces the click funnel because a CPM platform may report no clicks;
where clicks exist ``ICR = CTR x CVR`` and the three-term result is
unchanged.

Position is combined as an impression-weighted mean over rows whose
position lies in the valid range only.  Rows with an invalid position (feeds
carry sentinel values) still count toward spend, impressions and CPM.
"""

from __future__ import annotations

import math
import re
from typing import Any, Callable, Hashable, Iterable, Sequence

from .config import PositionConfig
from .schema import MetricCell, PerfRow

__all__ = [
    "fmt_money",
    "safe_div", "share", "pct_change", "cpm", "roas", "icr", "icr1k", "asp",
    "rpm", "ctr", "cvr", "roas_from_identity", "identity_gap",
    "position_is_valid", "clip_position", "position_bucket",
    "normalise_keyword", "empty_cell", "cell_from_rows", "sum_cells",
    "group_cells", "daily_cells", "total_sales", "is_additive",
    "ADDITIVE_FIELDS", "percentile",
]

# Fields of PerfRow that may be summed across rows.
ADDITIVE_FIELDS: tuple[str, ...] = (
    "spend", "impressions", "sales", "units", "clicks", "indirect_sales", "new_users",
)

_WS = re.compile(r"\s+")
_DEFAULT_POSITION = PositionConfig()


def is_additive(field_name: str) -> bool:
    """True when a ``PerfRow`` field may be summed (ratios and ranks may not)."""
    return field_name in ADDITIVE_FIELDS


def safe_div(a: float | None, b: float | None) -> float | None:
    """``a / b``, or ``None`` when ``b`` is zero / missing or ``a`` is missing."""
    if a is None or not b:
        return None
    return a / b


def share(part: float | None, whole: float | None) -> float | None:
    """Share ``part / whole`` in 0..1, ``None`` when the whole is zero or missing."""
    return safe_div(part, whole)


def percentile(values: Iterable[float], q: float) -> float | None:
    """Linear-interpolation percentile shared by every cohort and quartile rule.

    ``q`` is in 0..1.  With the values sorted ascending and ``pos = q *
    (n - 1)``, returns ``x[floor(pos)] + (x[floor(pos) + 1] - x[floor(pos)])
    * frac(pos)`` (the same convention as ``numpy.quantile``'s default).
    ``None`` when there are no values.
    """
    xs = sorted(float(v) for v in values)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def pct_change(current: float | None, prior: float | None) -> float | None:
    """Relative change ``current / prior - 1``; ``None`` without a positive-or-negative base."""
    if current is None or not prior:
        return None
    return current / prior - 1.0


def cpm(spend: float, impressions: float) -> float | None:
    """Cost per thousand impressions: ``1000 * spend / impressions`` (``None`` at 0 impressions)."""
    return 1000.0 * spend / impressions if impressions and impressions > 0 else None


def roas(sales: float, spend: float) -> float | None:
    """Return on ad spend: ``sales / spend`` (``None`` at 0 spend -- "cannot judge", never 0)."""
    return sales / spend if spend and spend > 0 else None


def icr(units: float, impressions: float) -> float | None:
    """Impression conversion rate: ``units / impressions``."""
    return units / impressions if impressions and impressions > 0 else None


def icr1k(units: float, impressions: float) -> float | None:
    """``1000 * units / impressions``."""
    v = icr(units, impressions)
    return None if v is None else 1000.0 * v


def asp(sales: float, units: float) -> float | None:
    """Average selling price: ``sales / units``."""
    return sales / units if units and units > 0 else None


def rpm(sales: float, impressions: float) -> float | None:
    """Revenue per thousand impressions: ``1000 * sales / impressions``."""
    return 1000.0 * sales / impressions if impressions and impressions > 0 else None


def ctr(clicks: float | None, impressions: float) -> float | None:
    """``clicks / impressions``; ``None`` when the platform reports no clicks."""
    if clicks is None or not impressions or impressions <= 0:
        return None
    return clicks / impressions


def cvr(units: float, clicks: float | None) -> float | None:
    """``units / clicks``; ``None`` when the platform reports no clicks."""
    if not clicks:
        return None
    return units / clicks


def roas_from_identity(icr_value: float | None, asp_value: float | None, cpm_value: float | None) -> float | None:
    """Rebuild ROAS from its three channels: ``1000 * ICR * ASP / CPM``.

    Returns ``None`` if any channel is missing or CPM is zero.
    """
    if icr_value is None or asp_value is None or not cpm_value:
        return None
    return 1000.0 * icr_value * asp_value / cpm_value


def identity_gap(cell: MetricCell) -> float | None:
    """``roas - 1000 * icr * asp / cpm`` for a cell; ~0 by construction.

    ``None`` when the identity is undefined (no spend, impressions or
    units).  Used by verifiers to assert exactness.
    """
    rebuilt = roas_from_identity(cell.icr, cell.asp, cell.cpm)
    if rebuilt is None or cell.roas is None:
        return None
    return cell.roas - rebuilt


def position_is_valid(position: float | None, cfg: PositionConfig | None = None) -> bool:
    """True when ``position`` is a number inside ``[valid_min, valid_max]``."""
    cfg = cfg or _DEFAULT_POSITION
    if position is None:
        return False
    try:
        p = float(position)
    except (TypeError, ValueError):
        return False
    if p != p:  # NaN
        return False
    return cfg.valid_min <= p <= cfg.valid_max


def clip_position(position: float | None, cfg: PositionConfig | None = None) -> float | None:
    """Return the position if valid, else ``None`` (sentinels never enter a mean)."""
    return float(position) if position_is_valid(position, cfg) else None  # type: ignore[arg-type]


def position_bucket(position: float | None, cfg: PositionConfig | None = None) -> str | None:
    """Label a position for per-position tables.

    Valid positions are rounded to the nearest slot; slots at or beyond
    ``cfg.cap`` collapse into ``"<cap>+"``.  Only observed positions are
    ever bucketed -- nothing is interpolated.  Invalid -> ``None``.
    """
    cfg = cfg or _DEFAULT_POSITION
    p = clip_position(position, cfg)
    if p is None:
        return None
    slot = int(round(p))
    return f"{cfg.cap}+" if slot >= cfg.cap else str(slot)


def normalise_keyword(value: str | None, collapse_whitespace: bool = False) -> str:
    """``lower(trim(value))``; optionally collapse inner whitespace runs (user-typed terms)."""
    s = (value or "").strip().lower()
    return _WS.sub(" ", s) if collapse_whitespace else s


def empty_cell() -> MetricCell:
    """A cell of zeros."""
    return MetricCell(entities=0, days=0)


def _opt_add(acc: float | None, v: float | None) -> float | None:
    if v is None:
        return acc
    return (acc or 0.0) + v


def cell_from_rows(rows: Iterable[PerfRow], position: PositionConfig | None = None) -> MetricCell:
    """Collapse fact rows into one ``MetricCell``.

    Sums ``spend, impressions, sales, units`` (and ``clicks``,
    ``indirect_sales``, ``new_users`` where any row carries them; they stay
    ``None`` when no row does).  ``pos_weight`` / ``pos_impressions``
    accumulate only rows with a valid position; ``bid_weight`` /
    ``bid_spend`` only rows with a bid.  ``entities`` = distinct entities
    with summed spend > 0; ``days`` = distinct dates present.
    """
    cfg = position or _DEFAULT_POSITION
    spend = impr = sales = units = pw = pi = bw = bs = 0.0
    clicks = indirect = new_users = None
    ent_spend: dict[str, float] = {}
    days: set[Any] = set()
    for r in rows:
        spend += r.spend
        impr += r.impressions
        sales += r.sales or 0.0
        units += r.units or 0.0
        if position_is_valid(r.position, cfg):
            pw += float(r.position) * r.impressions  # type: ignore[arg-type]
            pi += r.impressions
        if r.bid is not None:
            bw += r.bid * r.spend
            bs += r.spend
        clicks = _opt_add(clicks, r.clicks)
        indirect = _opt_add(indirect, r.indirect_sales)
        new_users = _opt_add(new_users, r.new_users)
        ent_spend[r.entity_id] = ent_spend.get(r.entity_id, 0.0) + r.spend
        days.add(r.date)
    return MetricCell(
        spend=spend, impressions=impr, sales=sales, units=units,
        pos_weight=pw, pos_impressions=pi, bid_weight=bw, bid_spend=bs,
        clicks=clicks, indirect_sales=indirect, new_users=new_users,
        entities=sum(1 for v in ent_spend.values() if v > 0), days=len(days),
    )


def sum_cells(cells: Iterable[MetricCell]) -> MetricCell:
    """Combine cells: add every raw sum, ``days = max(days)``, ``entities = None``.

    Groups share days, so days are never summed; the distinct-entity count
    cannot be recovered from parts, so it becomes ``None``.  Ratios follow
    from the summed raw values.
    """
    spend = impr = sales = units = pw = pi = bw = bs = 0.0
    clicks = indirect = new_users = None
    days = 0
    for c in cells:
        spend += c.spend
        impr += c.impressions
        sales += c.sales
        units += c.units
        pw += c.pos_weight
        pi += c.pos_impressions
        bw += c.bid_weight
        bs += c.bid_spend
        clicks = _opt_add(clicks, c.clicks)
        indirect = _opt_add(indirect, c.indirect_sales)
        new_users = _opt_add(new_users, c.new_users)
        days = max(days, c.days)
    return MetricCell(
        spend=spend, impressions=impr, sales=sales, units=units,
        pos_weight=pw, pos_impressions=pi, bid_weight=bw, bid_spend=bs,
        clicks=clicks, indirect_sales=indirect, new_users=new_users,
        entities=None, days=days,
    )


def group_cells(
    rows: Iterable[PerfRow],
    key: str | Sequence[str] | Callable[[PerfRow], Hashable],
    position: PositionConfig | None = None,
) -> dict[Hashable, MetricCell]:
    """Group rows and return ``{group_key: MetricCell}``.

    ``key`` is a ``PerfRow`` field name, a sequence of field names (the
    group key is then a tuple), or a callable.  Insertion order follows
    first appearance.
    """
    if callable(key):
        keyfn = key
    elif isinstance(key, str):
        name = key
        keyfn = lambda r: getattr(r, name)  # noqa: E731
    else:
        names = tuple(key)
        keyfn = lambda r: tuple(getattr(r, n) for n in names)  # noqa: E731
    buckets: dict[Hashable, list[PerfRow]] = {}
    for r in rows:
        buckets.setdefault(keyfn(r), []).append(r)
    return {k: cell_from_rows(v, position) for k, v in buckets.items()}


def daily_cells(rows: Iterable[PerfRow], position: PositionConfig | None = None) -> dict[Any, MetricCell]:
    """``{date: MetricCell}`` sorted by date -- the entity x day panel for one scope."""
    by_day = group_cells(rows, "date", position)
    return {d: by_day[d] for d in sorted(by_day)}  # type: ignore[type-var]


def total_sales(cell: MetricCell) -> float:
    """Direct plus indirect (halo) sales; equals direct sales without halo data."""
    return cell.sales + (cell.indirect_sales or 0.0)


def fmt_money(value: float | None) -> str:
    """Neutral money formatter: thousands separators, no currency symbol.

    Decimals follow the magnitude so low-magnitude currencies stay readable:
    whole units at 100 and above, one decimal from 10, two decimals below
    (``3.5`` prints as ``3.50``, not ``4``).  ``None`` prints as ``n/a``.
    """
    if value is None:
        return "n/a"
    a = abs(value)
    if a >= 100 or a == 0:
        return f"{value:,.0f}"
    return f"{value:,.1f}" if a >= 10 else f"{value:,.2f}"
