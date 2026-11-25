"""Ad share of voice and the rival table used by the CPM views.

Share of voice is additive context: every consumer must render without it
when no impression-share feed exists (``None`` is "not reported", never 0).

The aggregation itself lives in :mod:`cpm_lineage.market.sov`; this module
is the CPM subsystem's view of it.  The only difference is the scale:
here share of voice is a 0..1 fraction, there it is a percentage.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from ..market.config import OWN_LABEL, MarketConfig
from ..market.sov import ad_impression_sums, brand_ad_shares, rival_table, tracked_keywords
from ..schema import MarketRow, Window
from ._util import pct
from .config import CpmConfig

__all__ = ["OWN_KEY", "tracked_keywords", "share_of_voice", "brand_ad_shares", "rivals"]

OWN_KEY = OWN_LABEL  # key of the own brand in rival tables


def share_of_voice(
    market_rows: Iterable[MarketRow],
    window: Window | None = None,
    keywords: Iterable[str] | None = None,
    *,
    like_for_like: Window | None = None,
) -> float | None:
    """Ad share of voice of the own brand, as a 0..1 fraction.

    Formula: ``sum(own ad impressions) / sum(all brands' ad impressions)``
    over every row in ``window`` (all rows when ``None``) and ``keywords``
    (all when ``None``); sums first, one division, never a mean of daily
    shares.  ``like_for_like``: when given, only keywords still tracked in
    that window (ad impressions > 0 there) are counted, so a keyword leaving
    the feed cannot fake a fall.  Returns ``None`` when the denominator is 0.
    """
    own, tot = ad_impression_sums(market_rows, window, keywords, like_for_like=like_for_like)
    return pct(own, tot)


def rivals(
    market_rows: Sequence[MarketRow],
    periods: Mapping[str, Window],
    keywords: Iterable[str],
    cfg: CpmConfig | None = None,
) -> dict[str, dict[str, dict[str, float]]]:
    """Rival table: ``{keyword: {period label: {brand: ad share}}}``.

    Keeps the ``cfg.rivals_top`` (6) largest brands per keyword and period;
    periods with no ad impressions are omitted.
    """
    cfg = cfg or CpmConfig()
    return rival_table(market_rows, periods, keywords, MarketConfig(rivals_top=cfg.rivals_top))
