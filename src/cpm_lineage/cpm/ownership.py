"""Ownership guard: drop rows of a campaign filed under the wrong advertiser.

Some feeds occasionally file a campaign's rows under a sibling advertiser
account.  The guard decides one owner per campaign and discards the rest, so
no view double counts or misattributes spend.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable, Mapping, Sequence

from ..schema import PerfRow
from .config import CpmConfig

__all__ = ["assign_campaign_owner", "apply_ownership_guard", "leaked_rows"]


def assign_campaign_owner(
    rows: Iterable[PerfRow],
    *,
    anchor: date | None = None,
    lookback_days: int | None = None,
    cfg: CpmConfig | None = None,
    authoritative: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Decide which advertiser owns each campaign.

    Rule: ``owner(campaign) = argmax over advertisers of the number of
    distinct days`` the campaign appears under that advertiser within the
    trailing ``lookback_days`` (default 270) ending at ``anchor`` (default:
    latest row date).  Ties are
    broken deterministically by the most recent day seen, then
    by advertiser id.  ``authoritative`` (campaign -> advertiser), when
    supplied, overrides the vote: it is the preferred source if the platform
    exposes a real mapping.  Rows without an advertiser id do not vote.

    Returns ``{entity_id: advertiser_id}``.
    """
    cfg = cfg or CpmConfig()
    lookback = cfg.ownership_lookback_days if lookback_days is None else lookback_days
    rows = list(rows)
    if anchor is None:
        anchor = max((r.date for r in rows), default=None)
    owners: dict[str, str] = {}
    if anchor is not None:
        start = anchor - timedelta(days=lookback)
        days: dict[tuple[str, str], set[date]] = defaultdict(set)
        for r in rows:
            if r.advertiser_id is None or not (start <= r.date <= anchor):
                continue
            days[(r.entity_id, r.advertiser_id)].add(r.date)
        best: dict[str, tuple[int, date, str]] = {}
        for (entity, adv), ds in days.items():
            # rank: more days, then later last day, then smaller id (negated via sort below)
            cand = (len(ds), max(ds), adv)
            cur = best.get(entity)
            if cur is None or (cand[0], cand[1]) > (cur[0], cur[1]) or (
                (cand[0], cand[1]) == (cur[0], cur[1]) and adv < cur[2]
            ):
                best[entity] = cand
        owners = {entity: v[2] for entity, v in best.items()}
    if authoritative:
        owners.update(authoritative)
    return owners


def apply_ownership_guard(
    rows: Sequence[PerfRow],
    owners: Mapping[str, str] | None = None,
    *,
    anchor: date | None = None,
    cfg: CpmConfig | None = None,
) -> list[PerfRow]:
    """Keep only rows filed under their campaign's owner.

    A row is kept when ``(row.entity_id, row.advertiser_id)`` equals
    ``(entity_id, owner(entity_id))``.  Rows with no advertiser id, and rows
    of campaigns with no decided owner, are kept (nothing to guard).
    ``owners`` defaults to ``assign_campaign_owner(rows)``.
    """
    if owners is None:
        owners = assign_campaign_owner(rows, anchor=anchor, cfg=cfg)
    out = []
    for r in rows:
        owner = owners.get(r.entity_id)
        if r.advertiser_id is None or owner is None or r.advertiser_id == owner:
            out.append(r)
    return out


def leaked_rows(rows: Sequence[PerfRow], owners: Mapping[str, str] | None = None, **kwargs) -> list[PerfRow]:
    """Return the rows the guard would drop (for reporting the leak size)."""
    if owners is None:
        owners = assign_campaign_owner(rows, **kwargs)
    return [
        r for r in rows
        if r.advertiser_id is not None and owners.get(r.entity_id) not in (None, r.advertiser_id)
    ]
