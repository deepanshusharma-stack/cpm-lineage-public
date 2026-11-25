"""The history store: long memory at the grain that actually persists.

Campaigns churn faster than a period-over-period model can fit, but the
keywords, products and geos they target persist.  ``build_entity_history``
pivots an account's trailing performance off the campaign grain onto those
three grains, so a days-old campaign is a cold start only at the campaign
grain: its keywords, products and geos already have months of history.

``HistoryStore`` caches one such document per account and platform behind a
``StorageBackend`` with a freshness rule.  The lookups return ``None`` for
an unseen key, which means "cold", never "zero".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Mapping

from ..adapters.base import PlatformProfile
from ..schema import MarketRow, PerfRow, ShelfRow, TargetingUnit
from .base import StorageBackend
from .memory import MemoryBackend

__all__ = [
    "HistoryConfig", "build_entity_history", "HistoryStore",
    "keyword_history", "product_history", "geo_history", "HISTORY",
]

HISTORY = "history"


@dataclass(frozen=True)
class HistoryConfig:
    """Depth and freshness of the entity history."""

    window_days: int = 90            # trailing history depth; long enough to outlive campaign churn
    fresh_hours: float = 24.0        # a cached document older than this is rebuilt
    availability_days: int = 30      # product availability is read over the latest N days only
    default_role: str = "generic"    # role of a keyword missing from the targeting-unit dimension
    round_dp: int = 2                # decimals kept on ratios


class _Agg:
    __slots__ = ("spend", "sales", "indirect", "new_users", "has_indirect", "has_new", "days", "entities", "last")

    def __init__(self) -> None:
        self.spend = self.sales = self.indirect = self.new_users = 0.0
        self.has_indirect = self.has_new = False
        self.days: set[date] = set()
        self.entities: set[str] = set()
        self.last: date | None = None

    def add(self, r: PerfRow) -> None:
        self.spend += r.spend or 0.0
        self.sales += r.sales or 0.0
        if r.indirect_sales is not None:
            self.indirect += r.indirect_sales
            self.has_indirect = True
        if r.new_users is not None:
            self.new_users += r.new_users
            self.has_new = True
        self.days.add(r.date)
        self.entities.add(r.entity_id)
        if self.last is None or r.date > self.last:
            self.last = r.date

    def row(self, dp: int, extended: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "spend": round(self.spend, 2), "roas": round(self.sales / self.spend, dp),
            "n_days": len(self.days), "n_entities": len(self.entities),
            "last_seen": self.last.isoformat() if self.last else None,
        }
        if extended:
            if self.has_new:
                out["new_users"] = self.new_users
                out["cost_per_new_user"] = round(self.spend / self.new_users, dp) if self.new_users > 0 else None
            if self.has_indirect:
                tot = self.indirect + self.sales
                out["halo"] = round(self.indirect / tot, 4) if tot > 0 else None
        return out


def build_entity_history(
    rows: Iterable[PerfRow],
    *,
    account: str = "",
    platform: str = "",
    anchor: date | None = None,
    built_at: datetime | None = None,
    profile: PlatformProfile | None = None,
    targeting_units: Iterable[TargetingUnit] = (),
    market_rows: Iterable[MarketRow] = (),
    shelf_rows: Iterable[ShelfRow] = (),
    cfg: HistoryConfig | None = None,
) -> dict[str, Any]:
    """Aggregate trailing performance by keyword, product and geo across all entities.

    Window: the ``cfg.window_days`` days ending at ``anchor`` (default: the
    latest row date).  Only keys with spend > 0 are kept.  Per key:

    * ``roas = sum(sales) / sum(spend)``;
    * ``n_days`` distinct days, ``n_entities`` distinct campaigns,
      ``last_seen`` the latest date;
    * keywords also get ``role`` (from ``targeting_units``, default
      ``"generic"``) and, when the platform reports them, ``new_users``,
      ``cost_per_new_user = spend / new_users`` and
      ``halo = indirect / (indirect + direct)`` sales;
    * optional keyword extras: ``relevance`` (from ``targeting_units``),
      ``own_organic_impressions`` / ``own_ad_impressions`` (own-brand rows of
      ``market_rows`` in the window);
    * optional product extra: ``availability = available / checks`` (0..1)
      over the latest ``cfg.availability_days`` of own ``shelf_rows``.

    Placement pseudo-targets and aggregate geo buckets (as the ``profile``
    defines them) are excluded.  A missing optional feed leaves its fields
    out rather than writing zeros.

    Returns ``{account, platform, built_at, anchor, window_days, keywords,
    products, geos}`` (plain, JSON-ready).
    """
    cfg = cfg or HistoryConfig()
    profile = profile or PlatformProfile()
    rows = list(rows)
    if anchor is None:
        anchor = max((r.date for r in rows), default=None)
    out: dict[str, Any] = {
        "account": account, "platform": platform,
        "built_at": built_at.isoformat(timespec="seconds") if built_at else None,
        "anchor": anchor.isoformat() if anchor else None, "window_days": cfg.window_days,
        "keywords": {}, "products": {}, "geos": {},
    }
    if anchor is None:
        return out
    start = anchor - timedelta(days=cfg.window_days - 1)
    kws: dict[str, _Agg] = {}
    prods: dict[str, _Agg] = {}
    geos: dict[str, _Agg] = {}
    for r in rows:
        if not (start <= r.date <= anchor):
            continue
        if profile.is_search_keyword(r.targeting_unit):
            kws.setdefault(str(r.targeting_unit).strip().lower(), _Agg()).add(r)
        if r.product_id:
            prods.setdefault(r.product_id, _Agg()).add(r)
        if r.geo and not profile.is_aggregate_geo(r.geo):
            geos.setdefault(r.geo, _Agg()).add(r)

    dp = cfg.round_dp
    units = {u.value.strip().lower(): u for u in targeting_units if u.value}
    own_org: dict[str, float] = {}
    own_ad: dict[str, float] = {}
    for m in market_rows:
        if m.is_own and start <= m.date <= anchor:
            k = m.targeting_unit.strip().lower()
            own_org[k] = own_org.get(k, 0.0) + (m.organic_impressions or 0.0)
            own_ad[k] = own_ad.get(k, 0.0) + (m.ad_impressions or 0.0)
    for k, a in kws.items():
        if a.spend <= 0:
            continue
        row = a.row(dp, extended=True)
        u = units.get(k)
        row["role"] = (u.role if u is not None and u.role else cfg.default_role)
        if u is not None and u.relevance is not None:
            row["relevance"] = u.relevance
        if k in own_org:
            row["own_organic_impressions"] = own_org[k]
            row["own_ad_impressions"] = own_ad[k]
        out["keywords"][k] = row

    av_start = anchor - timedelta(days=cfg.availability_days - 1)
    avail: dict[str, list[float]] = {}
    for s in shelf_rows:
        if s.is_own and av_start <= s.date <= anchor and s.check_count:
            t = avail.setdefault(s.product_id, [0.0, 0.0])
            t[0] += s.available_count or 0.0
            t[1] += s.check_count
    for k, a in prods.items():
        if a.spend <= 0:
            continue
        row = a.row(dp, extended=False)
        if k in avail and avail[k][1] > 0:
            row["availability"] = round(avail[k][0] / avail[k][1], 4)
        out["products"][k] = row
    for k, a in geos.items():
        if a.spend > 0:
            out["geos"][k] = a.row(dp, extended=False)
    return out


def keyword_history(history: Mapping[str, Any] | None, keyword: str | None) -> dict[str, Any] | None:
    """History row of a keyword (case- and space-insensitive); ``None`` = cold."""
    return ((history or {}).get("keywords") or {}).get((keyword or "").strip().lower())


def product_history(history: Mapping[str, Any] | None, product_id: str | None) -> dict[str, Any] | None:
    """History row of a product; ``None`` = cold."""
    return ((history or {}).get("products") or {}).get(str(product_id))


def geo_history(history: Mapping[str, Any] | None, geo: str | None) -> dict[str, Any] | None:
    """History row of a geo; ``None`` = cold."""
    return ((history or {}).get("geos") or {}).get(str(geo))


class HistoryStore:
    """Cache of one entity-history document per ``(account, platform)``."""

    def __init__(
        self,
        backend: StorageBackend | None = None,
        cfg: HistoryConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.backend: StorageBackend = backend if backend is not None else MemoryBackend()
        self.cfg = cfg or HistoryConfig()
        self._clock = clock or datetime.now

    def save(self, history: Mapping[str, Any], now: datetime | None = None) -> None:
        """Store a history document under its own ``account`` / ``platform``; stamps ``built_at`` when absent."""
        doc = dict(history)
        if not doc.get("built_at"):
            doc["built_at"] = (now or self._clock()).isoformat(timespec="seconds")
        self.backend.put(HISTORY, (str(doc.get("account") or ""), str(doc.get("platform") or "")), doc)

    def is_fresh(self, history: Mapping[str, Any] | None, now: datetime | None = None,
                 max_age_hours: float | None = None) -> bool:
        """True when the document was built at most ``max_age_hours`` (default ``cfg.fresh_hours``) ago."""
        if not history or not history.get("built_at"):
            return False
        age = (now or self._clock()) - datetime.fromisoformat(history["built_at"])
        return age <= timedelta(hours=self.cfg.fresh_hours if max_age_hours is None else max_age_hours)

    def load(
        self,
        account: str,
        platform: str,
        *,
        now: datetime | None = None,
        max_age_hours: float | None = None,
        rebuild: Callable[[], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Return the cached history when fresh; otherwise rebuild it.

        With a ``rebuild`` callable a stale or missing document is rebuilt,
        saved and returned.  Without one a stale document is returned as is
        (``is_fresh`` tells the caller) and a missing one gives ``None``.
        """
        doc = self.backend.get(HISTORY, (account, platform))
        if self.is_fresh(doc, now, max_age_hours) or rebuild is None:
            return doc
        fresh = dict(rebuild())
        fresh.setdefault("account", account)
        fresh.setdefault("platform", platform)
        fresh["built_at"] = (now or self._clock()).isoformat(timespec="seconds")
        self.save(fresh, now)
        return self.backend.get(HISTORY, (account, platform))
