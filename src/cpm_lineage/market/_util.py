"""Small shared helpers for the keyword, market and geo modules (no I/O)."""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping, Sequence

from ..metrics import percentile
from ..schema import Window

__all__ = ["in_window", "ratio", "pct", "pct_growth", "diff", "percentile", "upper_pick", "get", "norm"]


def in_window(d: date, window: Window | None) -> bool:
    """True when ``window`` is ``None`` (no filter) or ``d`` is inside it."""
    return window is None or window.start <= d <= window.end


def ratio(a: float | None, b: float | None) -> float | None:
    """``a / b``; ``None`` when either side is missing or ``b`` is not positive."""
    if a is None or b is None or b <= 0:
        return None
    return a / b


def pct(a: float | None, b: float | None) -> float | None:
    """``100 * a / b`` (a share in percent) or ``None``."""
    r = ratio(a, b)
    return None if r is None else 100.0 * r


def pct_growth(cur: float | None, prev: float | None) -> float | None:
    """``100 * (cur / prev - 1)`` or ``None`` without a positive base."""
    r = ratio(cur, prev)
    return None if r is None else 100.0 * (r - 1.0)


def diff(a: float | None, b: float | None) -> float | None:
    """``a - b`` or ``None`` when either is missing."""
    if a is None or b is None:
        return None
    return a - b


def upper_pick(sorted_values: Sequence[float], divisor: int) -> float | None:
    """``sorted_values[len // divisor]``: an un-interpolated upper pick."""
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values) - 1, len(sorted_values) // divisor)]


def get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an object."""
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def norm(value: str | None) -> str:
    """Lower-case, trim and collapse whitespace (keyword join key)."""
    return " ".join((value or "").lower().split())
