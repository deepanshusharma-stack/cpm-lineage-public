"""Geo family: a campaign hiding a weak geography; under-penetrated geographies to expand into.

Aggregate roll-up labels (the adapter's long-tail buckets) are never a
treatment, a control or an expand target.  A platform with a single national
auction supplies no geo rows and the family returns nothing.
"""

from __future__ import annotations

import math

from ...schema import ActionCard
from ..primitives import (ActionContext, EntityGeoStat, GeoMarketStat, campaign_is_dead, fmt_money,
                          fmt_roas, is_aggregate_geo, money, new_card)

__all__ = ["geo", "control_geos", "weak_geo_cards", "expand_cards"]

FAMILY = "geo"


def _avail(ctx: ActionContext, value: float | None) -> float:
    if value is None:
        return ctx.cfg.geo_control_default_availability
    return ctx.availability_policy.normalise(value)


def control_geos(ctx: ActionContext, treatment: str, served: set[str], k: int | None = None) -> list[str]:
    """Nearest control geos for a geo-carve test.

    Candidates: real geos of ``ctx.geo_market`` other than the treatment and
    not served by the treated campaign.  Distance to the treatment:
    ``|ln(total_z / total_t)| + |share_z - share_t| / 10 + |avail_z - avail_t|
    x 100 / 20`` (shares in percent, availability 0..1, missing availability
    read as ``geo_control_default_availability``).  Returns the ``k`` nearest
    geo names (default ``geo_controls``); empty when the treatment is unknown.
    """
    k = ctx.cfg.geo_controls if k is None else k
    table = {z.geo: z for z in ctx.geo_market}
    t = table.get(treatment)
    if t is None or t.total_sales <= 0:
        return []
    scored = []
    for z in ctx.geo_market:
        if z.geo == treatment or z.geo in served or is_aggregate_geo(ctx, z.geo) or z.total_sales <= 0:
            continue
        d = (abs(math.log(z.total_sales / t.total_sales)) + abs(z.own_share_pct - t.own_share_pct) / 10.0
             + abs(_avail(ctx, z.availability) - _avail(ctx, t.availability)) * 100.0 / 20.0)
        scored.append((d, z.geo))
    return [g for _, g in sorted(scored)[:k]]


def weak_geo_cards(ctx: ActionContext) -> list[ActionCard]:
    """Find one weak geography per campaign.

    Scans the top ``geo_top_entities`` live pure campaigns by spend, using
    campaign x geo rows with spend > ``geo_row_min_spend``.  A campaign
    qualifies with total spend >= ``geo_min_spend``, at least 3 rows and
    average ROAS ``avg = sales / spend >= be``.  Weak rows: real geo, spend
    >= 15% of the campaign's, ROAS < ``0.6 x avg``; only the worst is carded:

    * ROAS < ``0.4 x avg`` -> ``geo`` card "Split / Retarget by Geo" (carve it
      out or exclude it).  Stake ``geo spend x (avg - geo ROAS)``.
    * otherwise -> ``experiment`` card "Launch Geo-Carve Experiment", 14 days,
      2 matched controls, success = treatment ROAS >= campaign average.
      Stake ``geo spend x 0.2``.
    """
    cfg, be = ctx.cfg, ctx.be
    by_entity: dict[str, list[EntityGeoStat]] = {}
    for row in ctx.entity_geo:
        if row.spend > money(ctx, cfg.geo_row_min_spend):
            by_entity.setdefault(row.entity_id, []).append(row)
    camps = [c for c in ctx.campaigns
             if c.is_pure is not False and not campaign_is_dead(ctx, c)
             and (ctx.selected_segments is None or c.segment in ctx.selected_segments)]
    camps = sorted(camps, key=lambda c: (-c.spend, c.entity_id))[: cfg.geo_top_entities]
    cards: list[ActionCard] = []
    for c in camps:
        rows = by_entity.get(c.entity_id, [])
        sp = sum(r.spend for r in rows)
        if sp < money(ctx, cfg.geo_min_spend) or len(rows) < cfg.geo_min_rows:
            continue
        avg = sum(r.sales for r in rows) / sp
        if avg < be:
            continue
        bad = [(r.sales / r.spend, r) for r in rows
               if r.spend >= cfg.geo_weak_spend_share * sp and r.sales / r.spend < cfg.geo_weak_roas_mult * avg
               and not is_aggregate_geo(ctx, r.geo)]
        if not bad:
            continue
        roas_w, w = sorted(bad, key=lambda x: (x[0], x[1].geo))[0]
        ev = {"geo": w.geo, "geo_roas": roas_w, "avg_roas": avg, "geo_spend": w.spend, "spend": sp}
        facts = f"geo_roas={roas_w:.2f} avg={avg:.2f} geo_spend={w.spend:.0f}"
        trig = (f"{w.geo} takes {100 * w.spend / sp:.0f}% of spend at {fmt_roas(roas_w)} against a "
                f"campaign average of {fmt_roas(avg)}")
        if roas_w < cfg.geo_decisive_roas_mult * avg:
            cards.append(new_card(
                ctx, FAMILY, "geo", "Split / Retarget by Geo", segment=c.segment, entity_id=c.entity_id,
                direction="down", trigger=trig,
                targets=f"carve {w.geo} into its own campaign or exclude it",
                evidence=ev, stake=w.spend * (avg - roas_w), facts=facts))
        else:
            controls = control_geos(ctx, w.geo, {r.geo for r in ctx.entity_geo if r.entity_id == c.entity_id})
            cards.append(new_card(
                ctx, "experiment", "experiment", "Launch Geo-Carve Experiment", segment=c.segment,
                entity_id=c.entity_id, direction="none", trigger=trig,
                targets=(f"{cfg.geo_experiment_days}-day carve test on {w.geo}"
                         + (f" against controls {', '.join(controls)}" if controls else "")
                         + "; success = treatment geo ROAS at or above the campaign average"),
                evidence=dict(ev, controls=controls, days=cfg.geo_experiment_days),
                stake=w.spend * cfg.geo_experiment_stake_share, facts=facts))
    return cards


def expand_cards(ctx: ActionContext) -> list[ActionCard]:
    """Find under-penetrated geographies worth targeting.

    ``blend = sum(own_share x total) / sum(total)`` over the account x geo
    table.  A real geo qualifies when its own share is at least 3 points
    under the blend and it holds >= 3% of total volume; the classic profile
    also requires known availability >= ``geo_expand_min_availability``
    (the verdict profile does not gate media on availability).  One card per
    geo, title carrying the geo so ids stay unique, no campaign, no segment.
    Stake: ``geo sales x (blend - share) / 100``.
    """
    cfg = ctx.cfg
    table: list[GeoMarketStat] = list(ctx.geo_market)
    total = sum(z.total_sales for z in table)
    if total <= 0:
        return []
    blend = sum(z.own_share_pct * z.total_sales for z in table) / total
    cards: list[ActionCard] = []
    for z in table:
        if is_aggregate_geo(ctx, z.geo):
            continue
        if z.own_share_pct > blend - cfg.geo_expand_share_gap_pp or z.total_sales < cfg.geo_expand_min_volume_share * total:
            continue
        if not ctx.verdict:
            if z.availability is None or ctx.availability_policy.normalise(z.availability) < cfg.geo_expand_min_availability:
                continue
        cards.append(new_card(
            ctx, FAMILY, "geo", f"Target Geo (expand): {z.geo}", direction="up",
            trigger=(f"own share {z.own_share_pct:.1f}% in {z.geo} vs blend {blend:.1f}% on "
                     f"{fmt_money(z.total_sales)} of segment sales"),
            targets=f"add {z.geo} as a targeted geo; judge share after 14 days",
            evidence={"geo": z.geo, "share": z.own_share_pct, "blend": blend,
                      "geo_sales": z.total_sales, "availability": z.availability},
            stake=z.total_sales * (blend - z.own_share_pct) / 100.0,
            facts=f"share={z.own_share_pct:.1f} blend={blend:.1f} sales={z.total_sales:.0f}"))
    return cards


def geo(ctx: ActionContext) -> list[ActionCard]:
    """Decide which geographies to carve out of a campaign and which to expand into.

    Returns ``weak_geo_cards(ctx) + expand_cards(ctx)``.
    """
    return weak_geo_cards(ctx) + expand_cards(ctx)
