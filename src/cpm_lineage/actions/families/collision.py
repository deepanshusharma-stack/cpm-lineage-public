"""Collision family: two own campaigns bidding the same keyword in the same auction."""

from __future__ import annotations

from dataclasses import dataclass

from ...schema import ActionCard, KeywordRef
from ..primitives import (ActionContext, CollisionRow, fmt_money, is_aggregate_geo,
                          live_campaign_ids, money, new_card)

__all__ = ["collision", "find_collisions", "Collision", "PLACEHOLDER_KEYWORDS"]

FAMILY = "collision"

# Values that mean "no keyword" in a targeting column.
PLACEHOLDER_KEYWORDS = frozenset({"", "not targeted", "not_targeted", "na", "n/a", "none"})


@dataclass(frozen=True)
class Collision:
    """One keyword colliding in one or more auctions.

    ``groups`` maps each geo (``None`` on a single-auction platform) to its
    rows sorted by ROAS descending: the first row is the keeper.  ``waste`` is
    the spend of every non-best row across the colliding geos.
    """

    keyword: str
    waste: float
    groups: tuple[tuple[str | None, tuple[CollisionRow, ...]], ...]


def find_collisions(ctx: ActionContext) -> list[Collision]:
    """Detect self-competition.

    1. Keep rows of live campaigns with spend >= ``collision_min_spend``, a
       real keyword and a non-aggregate geo; group by (keyword, geo).
    2. A group needs at least 2 distinct campaigns.
    3. Product overlap: when at least 2 rows carry product sets, keep only
       campaigns whose set intersects another's in the group (rows without
       product data are kept: cannot judge, do not hide); re-check 2 campaigns.
       Different products on one keyword are shelf occupation, not a collision.
    4. Sort each group by ROAS descending; the first is the keeper.
    5. Per keyword, ``waste = sum(spend of non-best rows)``; output sorted by
       waste descending.
    """
    floor = money(ctx, ctx.cfg.collision_min_spend)
    live = live_campaign_ids(ctx) if ctx.campaigns else None
    groups: dict[tuple[str, str | None], list[CollisionRow]] = {}
    for r in ctx.collisions:
        kw = (r.keyword or "").strip().lower()
        if kw in PLACEHOLDER_KEYWORDS or r.spend < floor or (r.geo and is_aggregate_geo(ctx, r.geo)):
            continue
        if live is not None and r.entity_id not in live:
            continue
        groups.setdefault((r.keyword, r.geo), []).append(r)
    per_kw: dict[str, list[tuple[str | None, tuple[CollisionRow, ...]]]] = {}
    for (kw, g), rows in groups.items():
        if len({r.entity_id for r in rows}) < 2:
            continue
        with_products = [r for r in rows if r.product_ids]
        if len(with_products) >= 2:
            rows = [r for r in rows if not r.product_ids or any(
                o.entity_id != r.entity_id and r.product_ids & o.product_ids for o in with_products)]
            if len({r.entity_id for r in rows}) < 2:
                continue
        ordered = tuple(sorted(rows, key=lambda r: (-(r.sales / r.spend if r.spend > 0 else 0.0),
                                                    r.entity_id)))
        per_kw.setdefault(kw, []).append((g, ordered))
    out = [Collision(kw, sum(r.spend for _, rows in gs for r in rows[1:]),
                     tuple(sorted(gs, key=lambda x: str(x[0])))) for kw, gs in per_kw.items()]
    return sorted(out, key=lambda c: (-c.waste, c.keyword))


def collision(ctx: ActionContext) -> list[ActionCard]:
    """Decide where one campaign should stop bidding on a keyword another own campaign also buys.

    One card per colliding keyword (see ``find_collisions``).  The geo with
    the largest total spend is named; the best-converting campaign there
    keeps the keyword and the others negative it in that geo only.  The card
    is anchored on the highest-spend overlapping (non-keeper) campaign, the
    one the negative is applied to; the keeper leads ``evidence["cids"]``.
    Stake: ``waste x 0.15``.
    """
    cards: list[ActionCard] = []
    for col in find_collisions(ctx):
        geo, rows = sorted(col.groups, key=lambda g: (-sum(r.spend for r in g[1]), str(g[0])))[0]
        best, losers = rows[0], rows[1:]
        target = sorted(losers, key=lambda r: (-r.spend, r.entity_id))[0]
        cids = list(dict.fromkeys(r.entity_id for r in rows))
        where = f" in {geo}" if geo else ""
        cards.append(new_card(
            ctx, FAMILY, "keyword", "Resolve Keyword Collision", segment=target.segment or best.segment,
            entity_id=target.entity_id, direction="down",
            keywords=[KeywordRef(kw=col.keyword, spend=col.waste)],
            trigger=(f"{len(cids)} own campaigns bid '{col.keyword}'{where}; "
                     f"{fmt_money(col.waste)} sits on the weaker ones"),
            targets=(f"keep it on {best.entity_id}; negative-match it on the overlapping "
                     f"campaigns{where} only"),
            evidence={"n_campaigns": len(cids), "spend": col.waste, "geos_up": len(col.groups),
                      "cids": cids, "geo": geo, "keep": best.entity_id},
            stake=col.waste * ctx.cfg.collision_stake_share,
            facts=f"waste={col.waste:.0f} n={len(cids)} geos={len(col.groups)}"))
    return cards
