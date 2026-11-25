"""Bid family: raise bids on converting-but-unseen terms, lower them on visible bleeders,
hold or yield a head term under CPM pressure.

Bid cards are keyword-grain: they never enter the campaign budget-conflict table.
Per-keyword rules need a position signal where a lower number is better; on a
platform without one the family emits only the head-term cards.
"""

from __future__ import annotations

from ...schema import ActionCard, Change, KeywordRef, TargetingRole
from ..primitives import (ActionContext, KeywordMarketStat, bucket_of, fmt_money, fmt_roas,
                          is_aggregate_geo, keyword_demand, keyword_threshold, live_keywords, money,
                          new_card, proportional_step, roas_floor, segments_in_scope)

__all__ = ["bid", "head_term_cards", "keyword_owner_map"]

FAMILY = "bid"


def keyword_owner_map(ctx: ActionContext) -> dict[tuple[str, str | None], tuple[str, str | None]]:
    """``{(keyword, segment): (entity_id, role)}`` of the live campaign spending most on the keyword
    in that segment.  Keyed by segment so a keyword is never attributed to another segment's campaign.
    """
    cached = ctx._memo.get("kw_owner")
    if cached is not None:
        return cached
    seg_of = {c.entity_id: c.segment for c in ctx.campaigns}
    best: dict[tuple[str, str | None], tuple[float, str, str | None]] = {}
    for k in live_keywords(ctx):
        key = (k.keyword, k.segment if k.segment is not None else seg_of.get(k.entity_id))
        if key not in best or k.spend > best[key][0]:
            best[key] = (k.spend, k.entity_id, k.role)
    out = {key: (v[1], v[2]) for key, v in best.items()}
    ctx._memo["kw_owner"] = out
    return out


def head_term_cards(ctx: ActionContext, rows: list[KeywordMarketStat]) -> list[ActionCard]:
    """Head-term pressure: is the biggest keyword's auction inflating across geos?

    ``head`` = the max-spend row, spend >= ``head_min_spend``.  Using
    ``ctx.head_cpm_by_geo[head]``, count real geos whose current CPM exceeds
    prior CPM x 1.10; at least 3 are needed.  Direct ROAS unknown -> watch
    card (missing attribution is neither zero nor a shield).  Known:
    ``dr >= roas_floor("generic")`` -> "Hold Bid Under Pressure" (no change),
    else "Yield the Inflated Auction" (bid -15%).  Stake: ``head spend x 0.1``.
    Without the per-geo CPM input the rule abstains.
    """
    cfg = ctx.cfg
    if not rows or ctx.head_cpm_by_geo is None:
        return []
    head = sorted(rows, key=lambda r: (-r.spend, r.keyword, str(r.segment)))[0]
    if head.spend < money(ctx, cfg.head_min_spend):
        return []
    probe = ctx.head_cpm_by_geo.get(head.keyword) or {}
    ups = sorted(g for g, (cur, pri) in probe.items()
                 if not is_aggregate_geo(ctx, g) and cur is not None and pri and cur > pri * cfg.head_cpm_rise)
    if len(ups) < cfg.head_min_geos:
        return []
    owner = keyword_owner_map(ctx).get((head.keyword, head.segment))
    common = dict(segment=head.segment, entity_id=owner[0] if owner else None,
                  keywords=[KeywordRef(kw=head.keyword, spend=head.spend, roas=head.direct_roas,
                                       position=head.position)],
                  evidence={"spend": head.spend, "roas": head.direct_roas, "cpm": head.cpm,
                            "geos_up": ups, "n_geos_up": len(ups)},
                  stake=head.spend * cfg.head_stake_share,
                  facts=f"spend={head.spend:.0f} geos_up={len(ups)} dr={fmt_roas(head.direct_roas)}")
    trig = f"CPM on '{head.keyword}' rose more than {100 * (cfg.head_cpm_rise - 1):.0f}% in {len(ups)} geos"
    if head.direct_roas is None:
        return [new_card(ctx, FAMILY, "watch", "Head Term Under CPM Pressure (attribution incomplete)",
                         direction="none", trigger=trig,
                         targets="attribution is missing: neither hold nor yield can be argued yet",
                         **common)]
    floor = roas_floor(ctx, TargetingRole.GENERIC.value)
    if head.direct_roas >= floor:
        return [new_card(ctx, FAMILY, "bid", "Hold Bid Under Pressure", direction="none",
                         trigger=trig + f"; ROAS {fmt_roas(head.direct_roas)} still clears {fmt_roas(floor)}",
                         targets="hold the bid; the term can afford the fight", **common)]
    return [new_card(ctx, FAMILY, "bid", "Yield the Inflated Auction", direction="down",
                     change=Change(pct=-cfg.head_yield_pct),
                     trigger=trig + f"; ROAS {fmt_roas(head.direct_roas)} is below {fmt_roas(floor)}",
                     targets=f"lower the bid {cfg.head_yield_pct:g}%; do not chase the inflated price",
                     **common)]


def bid(ctx: ActionContext) -> list[ActionCard]:
    """Decide per keyword whether its bid should rise or fall.

    Rows: ``ctx.keyword_market`` in the segments in scope.  Skipped rows:
    spend below the profile's minimum (classic ``bid_min_spend_classic``,
    verdict ``bid_min_spend_verdict``), unknown direct ROAS, unknown
    position.  ``bavg`` = spend-weighted direct ROAS of the row's bucket,
    falling back to the book average.

    Classic:
      * up +15%: ``dr >= max(1.2 x bavg, 1.5 x be)`` and position > 5.
        Stake ``spend x (dr - bavg) x 0.15``.
      * down -15%: ``dr < min(0.8 x be, 0.5 x bavg)`` and position <= 2
        (never bid down a term already below the fold).  Stake ``spend x (1 - dr)``.

    Verdict (``thr = keyword_threshold(role, bavg)``; position = recent modal
    position when supplied, else the window mean):
      * raise: ``dr > thr`` and position > 6; ``headroom = dr / thr - 1``,
        no card under 8%; ``step = round(min(headroom, cap) x 100)`` with cap
        25%, or 40% when buried (position >= 11) unless a rival owns >= 60%
        of the term.  Pay-your-bid auctions: ROAS scales with 1 / bid, so the
        headroom is itself the ceiling and predicted ROAS is ``dr / (1 +
        step)``; on other auction types the step is additionally capped at
        ``bid_fixed_step_pct`` and no ceiling is quoted.  Stake ``spend x
        (dr - thr) x step / 100``.
      * cut: ``dr < thr``, position <= 9, spend >= ``bid_cut_min_spend_verdict``,
        not a new-user top term; ``needed = 1 - dr / thr`` must reach the
        dead-zone (10%, 20% for top-volume terms); step 30% when ``needed >
        0.40`` and ``dr < be`` (step 1 of exit) else ``clamp(needed, 0.10,
        0.30)``.  Stake ``spend x (1 - dr / thr)``.

    One keyword gets at most one card per campaign.  Returns head-term cards
    first, then per-keyword cards in input order.
    """
    cfg, be = ctx.cfg, ctx.be
    segs = set(segments_in_scope(ctx))
    rows = [r for r in ctx.keyword_market if r.segment in segs]
    cards = head_term_cards(ctx, rows)
    pos_sem = ctx.profile.position
    if not rows or not pos_sem.available or not pos_sem.lower_is_better:
        return cards

    tot_sp = sum(r.spend for r in rows)
    book = (sum(r.spend * (r.direct_roas or 0.0) for r in rows) / tot_sp) if tot_sp > 0 else None
    owners = keyword_owner_map(ctx)
    demand = keyword_demand(ctx)

    def role_of(r: KeywordMarketStat) -> str | None:
        owner = owners.get((r.keyword, r.segment))
        return (owner[1] if owner and owner[1] else None) or r.role

    acc: dict[str, list[float]] = {}
    for r in rows:
        if r.direct_roas is not None and r.spend > 0:
            a = acc.setdefault(bucket_of(role_of(r)), [0.0, 0.0])
            a[0] += r.spend * r.direct_roas
            a[1] += r.spend
    bucket_avg = {b: a[0] / a[1] for b, a in acc.items() if a[1] > 0}
    min_sp = money(ctx, cfg.bid_min_spend_verdict if ctx.verdict else cfg.bid_min_spend_classic)
    seen: set[tuple[str | None, str]] = set()

    for r in rows:
        if r.spend < min_sp or r.direct_roas is None or r.position is None:
            continue
        dr, sp = r.direct_roas, r.spend
        role = role_of(r)
        bucket = bucket_of(role)
        bavg = bucket_avg.get(bucket) or book
        owner = owners.get((r.keyword, r.segment))
        eid = owner[0] if owner else None
        if eid is not None and (eid, r.keyword) in seen:
            continue
        key = (r.keyword, r.segment)
        card: ActionCard | None = None
        ref = KeywordRef(kw=r.keyword, spend=sp, roas=dr, position=r.position, bucket=bucket, bench=bavg)
        if not ctx.verdict:
            if bavg is None:
                continue
            if dr >= max(cfg.bid_up_bucket_mult * bavg, cfg.bid_up_be_mult * be) and r.position > cfg.bid_up_min_position:
                card = new_card(
                    ctx, FAMILY, "bid", "Increase Bid", segment=r.segment, entity_id=eid,
                    direction="up", change=Change(pct=cfg.bid_step_pct), keywords=[ref],
                    trigger=(f"'{r.keyword}' converts at {fmt_roas(dr)} (its kind averages "
                             f"{fmt_roas(bavg)}) but sits at position {r.position:.1f}"),
                    targets=f"raise the bid {cfg.bid_step_pct:g}%; judge after 7 days",
                    evidence={"roas": dr, "spend": sp, "rank": r.position, "bucket": bucket,
                              "bucket_avg": bavg},
                    stake=sp * (dr - bavg) * cfg.bid_step_pct / 100.0,
                    facts=f"dr={dr:.2f} bavg={bavg:.2f} pos={r.position:.1f} spend={sp:.0f}")
            elif dr < min(cfg.bid_down_be_mult * be, cfg.bid_down_bucket_mult * bavg) and r.position <= cfg.bid_top_slot:
                card = new_card(
                    ctx, FAMILY, "bid", "Decrease Bid", segment=r.segment, entity_id=eid,
                    direction="down", change=Change(pct=-cfg.bid_step_pct), keywords=[ref],
                    trigger=(f"'{r.keyword}' holds position {r.position:.1f} at {fmt_roas(dr)} "
                             f"on {fmt_money(sp)} spend"),
                    targets=(f"lower the bid {cfg.bid_step_pct:g}%; settle near position "
                             f"{cfg.bid_settle_position:g}, never below the fold"),
                    evidence={"roas": dr, "spend": sp, "rank": r.position, "bucket": bucket,
                              "bucket_avg": bavg},
                    stake=sp * (1.0 - dr),
                    facts=f"dr={dr:.2f} bavg={bavg:.2f} pos={r.position:.1f} spend={sp:.0f}")
        else:
            thr = keyword_threshold(ctx, role, bavg)
            rank = r.position_mode if r.position_mode is not None else r.position
            deep = rank >= cfg.bid_deep_position
            if (deep and r.rival_owner_share is not None
                    and r.rival_owner_share >= cfg.rented_owner_share and not r.rival_owner_is_own):
                deep = False
            ev = {"roas": dr, "spend": sp, "rank": rank, "bucket": bucket, "threshold": thr,
                  "bucket_avg": bavg}
            facts = f"dr={dr:.2f} thr={thr:.2f} pos={rank:.1f} spend={sp:.0f}"
            if dr > thr and rank > cfg.bid_raise_min_position:
                head = dr / thr - 1.0
                if head < cfg.bid_headroom_min:
                    continue
                step = int(round(min(head, cfg.bid_raise_cap_deep if deep else cfg.bid_raise_cap) * 100))
                pay = ctx.profile.pay_your_bid
                if not pay:
                    step = int(min(step, cfg.bid_fixed_step_pct))
                change = Change(pct=float(step))
                ev["sizing"] = "headroom" if pay else "fixed_step"
                if pay:
                    ev["roas_after"] = dr / (1.0 + step / 100.0)
                    if r.live_bid:
                        change.frm = r.live_bid
                        change.to = r.live_bid * (1.0 + step / 100.0)
                        ev["bid_ceiling"] = r.live_bid * dr / thr
                card = new_card(
                    ctx, FAMILY, "bid", "Increase Bid (visibility buy)" if deep else "Increase Bid",
                    segment=r.segment, entity_id=eid, direction="up", change=change, keywords=[ref],
                    trigger=(f"'{r.keyword}' converts at {fmt_roas(dr)} against a bar of "
                             f"{fmt_roas(thr)} but sits at position {rank:.0f}"),
                    targets=f"raise the bid {step}%; re-read in {cfg.reread_days} days",
                    evidence=ev, stake=sp * (dr - thr) * step / 100.0, facts=facts + f" step={step}")
            elif dr < thr and rank <= cfg.bid_cut_max_position and sp >= money(ctx, cfg.bid_cut_min_spend_verdict):
                if key in demand.new_user_top:
                    continue
                needed = 1.0 - dr / thr
                zone = cfg.bid_dead_zone_top_volume if key in demand.volume_top else cfg.bid_dead_zone
                if needed < zone:
                    continue
                exit1 = needed > cfg.exit_needed_min and dr < be
                step = int(cfg.exit_step_pct) if exit1 else proportional_step(ctx, needed)
                top = rank <= cfg.bid_top_slot
                ev.update(needed=needed, exit=exit1)
                card = new_card(
                    ctx, FAMILY, "bid", "Decrease Bid (step 1 of exit)" if exit1 else "Decrease Bid",
                    segment=r.segment, entity_id=eid, direction="down", change=Change(pct=-float(step)),
                    keywords=[ref],
                    trigger=(f"'{r.keyword}' runs {100 * needed:.0f}% below its bar "
                             f"({fmt_roas(dr)} vs {fmt_roas(thr)}) at position {rank:.0f}"),
                    targets=((f"stop paying the top-slot premium; settle near position "
                              f"{cfg.bid_settle_position:g}; " if top else "")
                             + f"lower the bid {step}%; re-read in {cfg.reread_days} days"),
                    evidence=ev, stake=sp * (1.0 - dr / thr), facts=facts + f" step={step}")
            if card is not None:
                card.kw_priority = demand.priority.get((r.segment, bucket))
        if card is not None:
            seen.add((eid, r.keyword))
            cards.append(card)
    return cards
