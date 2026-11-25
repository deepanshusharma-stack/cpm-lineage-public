"""Growth family: ride a growing segment with its best campaign; add converting keywords."""

from __future__ import annotations

from typing import Any, Mapping

from ...schema import ActionCard, Change, KeywordRef
from ..primitives import (ActionContext, decision_roas, fmt_money, fmt_roas, live_campaigns,
                          new_card, segments_in_scope)

__all__ = ["growth"]

FAMILY = "growth"


def _suggestion(item: Any) -> KeywordRef | None:
    if isinstance(item, KeywordRef):
        return item
    if isinstance(item, Mapping) and item.get("kw"):
        vol = item.get("volume", item.get("vol"))
        return KeywordRef(kw=str(item["kw"]), volume=vol)
    return None


def growth(ctx: ActionContext) -> list[ActionCard]:
    """Decide where to add budget or keywords because the segment itself is growing.

    Budget ("Increase Budget"), per segment with both-window market totals:
    ``growth = 100 x (total_current / total_prior - 1)`` and ``dpp`` = own
    share change in points.  Fires when ``growth >= 5``, ``dpp >= -0.1`` and
    a live campaign has ``2 x be <= r < 12 x be`` (higher is treated as
    own-brand-carried).  The max-ROAS candidate gets +10%.  Stake:
    ``own_sales_current x growth / 100 x 0.1``.

    Keywords ("Add Keywords"), for the 2 biggest segments: the top-spend live
    campaign plus the first 5 well-formed suggestions (of the first 8) from
    ``ctx.keyword_suggestions`` keyed by entity id or segment.  Stake 0.
    Skipped when no suggestions were supplied.
    """
    cfg, be = ctx.cfg, ctx.be
    cards: list[ActionCard] = []
    segs = segments_in_scope(ctx)
    for seg in segs:
        m = ctx.segments.get(seg)  # type: ignore[arg-type]
        if m is None or not m.has_both_windows:
            continue
        g, dpp = m.growth_pct, m.share_delta_pp
        live = live_campaigns(ctx, seg)
        if g is None or dpp is None or g < cfg.growth_min_pct or dpp < cfg.growth_share_delta_min_pp:
            continue
        cands = []
        for c in live:
            r = decision_roas(ctx, c)
            if r is not None and cfg.growth_roas_min_mult * be <= r < cfg.scale_max_mult * be:
                cands.append((r, c))
        if not cands:
            continue
        r, c = sorted(cands, key=lambda rc: (-rc[0], rc[1].entity_id))[0]
        b = ctx.telemetry.get(c.entity_id)
        frm = (b.budget_latest or b.avg_budget) if b else None
        pct = cfg.growth_raise_pct
        cards.append(new_card(
            ctx, FAMILY, "budget", "Increase Budget", segment=seg, entity_id=c.entity_id,
            direction="up", change=Change(pct=pct, frm=frm, to=frm * (1 + pct / 100) if frm else None),
            trigger=(f"segment sales grew {g:.1f}% with own share {dpp:+.2f} pp; best campaign "
                     f"runs at {fmt_roas(r)}"),
            targets=f"raise {pct:g}% and re-read share after 7 days",
            evidence={"growth": g, "dpp": dpp, "roas": r, "spend": c.spend,
                      "own_sales": m.own_sales_current},
            stake=(m.own_sales_current or 0.0) * g / 100.0 * cfg.growth_stake_share,
            facts=f"growth={g:.1f} dpp={dpp:.2f} roas={r:.2f} own_sales={fmt_money(m.own_sales_current)}"))
    if ctx.keyword_suggestions:
        for seg in segs[: cfg.growth_keyword_segments]:
            live = live_campaigns(ctx, seg)
            if not live:
                continue
            c = sorted(live, key=lambda x: (-x.spend, x.entity_id))[0]
            raw = ctx.keyword_suggestions.get(c.entity_id) or ctx.keyword_suggestions.get(seg) or ()  # type: ignore[arg-type]
            refs = [s for s in (_suggestion(i) for i in list(raw)[: cfg.growth_keyword_ask]) if s]
            refs = refs[: cfg.growth_keyword_max_terms]
            if not refs:
                continue
            cards.append(new_card(
                ctx, FAMILY, "keyword", "Add Keywords", segment=seg, entity_id=c.entity_id,
                direction="up", keywords=refs,
                trigger=f"{len(refs)} converting term(s) in this segment are not bid on yet",
                targets="add at suggested bids; judge after 7 days",
                evidence={"n_kws": len(refs)}, stake=0.0, facts=f"n={len(refs)}"))
    return cards
