"""Small shared helpers of the CPM subsystem (no I/O)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from ..adapters.base import PlatformProfile
from ..metrics import normalise_keyword
from ..schema import MetricCell, PerfRow, Window

__all__ = [
    "norm_kw", "to_local", "resolve_tz", "median", "quantile", "is_keyword_row",
    "cell_dict", "in_window", "pct", "position_config",
]

TzLike = "tzinfo | str | int | float | None"


def norm_kw(value: str | None) -> str:
    """Lower-case, trim and collapse internal whitespace of a keyword."""
    return normalise_keyword(value, collapse_whitespace=True)


def pct(a: float | None, b: float | None) -> float | None:
    """``a / b`` or ``None`` when ``b`` is zero or either side is missing."""
    if a is None or not b:
        return None
    return a / b


def resolve_tz(tz: Any) -> tzinfo:
    """Turn a timezone spec into a ``tzinfo``.

    Accepts a ``tzinfo``, an IANA name (``"UTC"``, ``"Europe/Paris"``), a
    number of minutes east of UTC, or ``None`` (UTC).
    """
    if tz is None:
        return timezone.utc
    if isinstance(tz, tzinfo):
        return tz
    if isinstance(tz, (int, float)):
        return timezone(timedelta(minutes=float(tz)))
    name = str(tz)
    if name.upper() == "UTC":
        return timezone.utc
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def to_local(ts: datetime, tz: Any = None) -> datetime:
    """Convert a decision timestamp to local time (naive timestamps are UTC)."""
    zone = resolve_tz(tz)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(zone)


def median(values: Iterable[float]) -> float | None:
    """Median of the values, ``None`` when there are none."""
    xs = [float(v) for v in values]
    return float(np.median(xs)) if xs else None


def quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile (``q`` in 0..1)."""
    return float(np.quantile(np.asarray(values, dtype=float), q))


def is_keyword_row(row: PerfRow, profile: PlatformProfile | None = None, keyword_type: str = "keyword") -> bool:
    """Decide whether a performance row is a biddable targeting-unit row.

    Rule: a non-empty targeting unit is required.  When the row carries a
    ``targeting_type`` it must equal ``keyword_type`` (case-insensitive) or
    be one of the profile's ``biddable_targeting_types`` (so a platform that
    bids on audience segments or contextual categories declares them once);
    otherwise the profile's capability for the row's ad type must be
    keyword-drillable and the target must not be a placement pseudo-target.
    """
    unit = (row.targeting_unit or "").strip()
    if not unit:
        return False
    if row.targeting_type:
        kind = row.targeting_type.strip().lower()
        return kind == keyword_type or (profile is not None and kind in profile.biddable_targeting_types)
    if profile is not None:
        if not profile.is_search_keyword(unit):
            return False
        if row.ad_type:
            return bool(profile.capability(row.ad_type).keyword_drillable)
    return True


def position_config(profile: PlatformProfile | None, position: Any = None) -> Any:
    """The position validity range in force: an explicit ``PositionConfig``, else the profile's, else defaults."""
    from ..config import PositionConfig

    if position is not None:
        return position
    if profile is not None:
        p = profile.position
        return PositionConfig(valid_min=p.valid_min, valid_max=p.valid_max, cap=p.cap, lower_is_better=p.lower_is_better)
    return PositionConfig()


def in_window(d: date, window: Window | None) -> bool:
    """True when ``d`` falls in the inclusive window."""
    return window is not None and window.start <= d <= window.end


def cell_dict(cell: MetricCell, **extra: Any) -> dict[str, Any]:
    """Plain-dict view of a metric cell: raw sums plus ``cpm``, ``position``,
    ``roas`` (ratios of sums, ``None`` on a zero denominator)."""
    out: dict[str, Any] = {
        "spend": cell.spend,
        "impressions": cell.impressions,
        "sales": cell.sales,
        "pos_weight": cell.pos_weight,
        "pos_impressions": cell.pos_impressions,
        "entities": cell.entities,
        "days": cell.days,
        "cpm": cell.cpm,
        "position": cell.position,
        "roas": cell.roas,
    }
    out.update(extra)
    return out


def get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an object."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)
