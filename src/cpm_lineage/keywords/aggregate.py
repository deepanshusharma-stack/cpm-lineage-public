"""Keyword-grain aggregation shared by the recommenders and the leak screens."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Collection, Iterable

from ..adapters.base import PlatformProfile
from ..market._util import in_window, norm
from ..schema import PerfRow, Window

__all__ = ["KeywordStat", "keyword_stats", "scope_filter"]


@dataclass
class KeywordStat:
    """Window sums for one keyword across the entities in scope.

    ``new_users`` / ``indirect_sales`` stay ``None`` when no row reports
    them ("platform does not report it"), never 0.
    """

    keyword: str
    spend: float = 0.0
    sales: float = 0.0
    impressions: float = 0.0
    units: float = 0.0
    new_users: float | None = None
    indirect_sales: float | None = None
    pos_weight: float = 0.0          # sum(position x impressions) over valid positions
    pos_impressions: float = 0.0
    bid_weight: float = 0.0          # sum(bid x spend)
    bid_spend: float = 0.0
    entity_ids: tuple[str, ...] = ()

    @property
    def roas(self) -> float | None:
        """Direct sales / spend, ``None`` without spend."""
        return self.sales / self.spend if self.spend > 0 else None

    @property
    def cpm(self) -> float | None:
        """``1000 * spend / impressions``."""
        return 1000.0 * self.spend / self.impressions if self.impressions > 0 else None

    @property
    def position(self) -> float | None:
        """Impression-weighted position over valid positions only."""
        return self.pos_weight / self.pos_impressions if self.pos_impressions > 0 else None

    @property
    def bid(self) -> float | None:
        """Spend-weighted bid."""
        return self.bid_weight / self.bid_spend if self.bid_spend > 0 else None

    @property
    def cost_per_new_user(self) -> float | None:
        """Spend / new users when at least one new user was acquired."""
        if self.new_users is None or self.new_users < 1:
            return None
        return self.spend / self.new_users

    @property
    def n_entities(self) -> int:
        return len(self.entity_ids)


def scope_filter(entity_ids: str | Collection[str] | None) -> "frozenset[str] | None":
    """Normalise a scope: ``None`` or ``"*"`` = every entity, one id, or a
    collection of ids (an empty collection matches nothing)."""
    if entity_ids is None or entity_ids == "*":
        return None
    if isinstance(entity_ids, str):
        return frozenset({entity_ids})
    return frozenset(entity_ids)


def keyword_stats(
    perf_rows: Iterable[PerfRow],
    window: Window | None = None,
    *,
    entity_ids: str | Collection[str] | None = None,
    profile: PlatformProfile | None = None,
    position_min: float = 1.0,
    position_max: float = 500.0,
) -> dict[str, KeywordStat]:
    """Sum performance rows to one :class:`KeywordStat` per keyword.

    Keeps rows inside ``window`` and ``entity_ids`` whose targeting unit is
    a search keyword (``profile.is_search_keyword``; without a profile any
    non-empty unit).  Position is impression-weighted over rows whose
    position lies in ``position_min..position_max``; bid is spend-weighted.
    Returns ``{normalised keyword: KeywordStat}``.
    """
    scope = scope_filter(entity_ids)
    out: dict[str, KeywordStat] = {}
    ents: dict[str, set[str]] = {}
    for r in perf_rows:
        if not in_window(r.date, window):
            continue
        if scope is not None and r.entity_id not in scope:
            continue
        kw = norm(r.targeting_unit)
        if not kw or (profile is not None and not profile.is_search_keyword(kw)):
            continue
        s = out.get(kw)
        if s is None:
            s = out[kw] = KeywordStat(kw)
            ents[kw] = set()
        ents[kw].add(r.entity_id)
        s.spend += r.spend or 0.0
        s.sales += r.sales or 0.0
        s.impressions += r.impressions or 0.0
        s.units += r.units or 0.0
        if r.new_users is not None:
            s.new_users = (s.new_users or 0.0) + r.new_users
        if r.indirect_sales is not None:
            s.indirect_sales = (s.indirect_sales or 0.0) + r.indirect_sales
        if r.position is not None and position_min <= r.position <= position_max and (r.impressions or 0) > 0:
            s.pos_weight += r.position * r.impressions
            s.pos_impressions += r.impressions
        if r.bid is not None and (r.spend or 0) > 0:
            s.bid_weight += r.bid * r.spend
            s.bid_spend += r.spend
    for kw, s in out.items():
        s.entity_ids = tuple(sorted(ents[kw]))
    return out
