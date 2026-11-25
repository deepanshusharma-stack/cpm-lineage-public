"""Deterministic conflict resolution between candidate action cards.

Safety is owned by a fixed priority table, never by a model: when two cards
contradict each other on one entity the higher-priority family wins, unless
the losing side carries far more money than the winner, in which case the
table abstains and the whole conflict becomes a "tension group" in which a
human may apply exactly one card.  Abstention is a recorded state.

All functions mutate card status fields in place and return plain values.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

from ..schema import ActionCard
from ..verdict import contested_ratio as _contested_ratio
from .pipeline_config import (
    PipelineConfig, PlanContext, R_BID_CONFLICT, R_PARTITIONED, R_PRIORITY, R_SUPPLY, R_TENSION,
    is_live, pauses_entity, veto_card,
)

__all__ = [
    "supply_gate", "merge_duplicates", "resolve_bid_conflicts", "resolve_budget_conflicts",
    "resolve", "contested_ratio", "apply_contested_check",
]


def supply_gate(cards: Sequence[ActionCard], ctx: PlanContext) -> int:
    """Veto scale-ups into a segment whose shelf is too empty.

    Rule: an actionable card with ``direction == "up"`` and
    ``family != "defense"`` is vetoed when its segment has an availability
    reading below ``AvailabilityPolicy.caution`` (0.75).  Segments with no
    reading pass (the plan discloses that the gate was not armed).  The gate
    is off when ``ctx.supply_gate_on`` is false (verdict profile, or disabled
    by policy).

    Only actionable cards are touched, so the reason on an already vetoed
    card is never overwritten.  Returns the number vetoed.
    """
    if not ctx.supply_gate_on:
        return 0
    gate = ctx.availability_policy.caution
    n = 0
    for c in cards:
        if not is_live(c) or c.direction != "up" or c.family == "defense":
            continue
        a = ctx.availability(c.segment)
        if a is not None and a < gate:
            veto_card(c, R_SUPPLY, f"supply gate: availability {100 * a:.0f}% is below {100 * gate:.0f}% "
                                   "- scaling into an empty shelf buys impressions, not sales")
            n += 1
    return n


def merge_duplicates(cards: Sequence[ActionCard], cfg: PipelineConfig | None = None) -> list[ActionCard]:
    """Merge pending cards that say the same thing about one entity.

    Key: ``(entity_id, type, direction)`` over pending cards with an entity;
    for keyword-grain cards (type ``bid`` or ``keyword``) the key also
    carries the card's keyword set, so two cards about different terms on
    one entity are different statements and both survive.
    One card per key keeps; the others get ``status = "merged"``, their
    trigger text is appended to the keeper's with ``" · also: "``, and they
    are removed from the returned list.

    The keeper is the card of the highest-priority family
    (``cfg.priority``), so a defense card is never merged into a
    lower-priority one; within a family the higher-stake card with
    first-seen tiebreak (``merge_keeper = "higher_stake"``, the default) or
    simply the first card seen (``"first_seen"``).  A choice pair survives
    the merge: the keeper inherits a merged card's ``tension_group`` /
    ``tension_with``, and any card that pointed at a merged card is
    re-pointed at its keeper.
    Returns the surviving cards in input order.
    """
    cfg = cfg or PipelineConfig()
    groups: dict[tuple, list[ActionCard]] = {}
    for c in cards:
        if c.status == "pending" and c.entity_id is not None:
            key: tuple = (c.entity_id, c.type, c.direction)
            if c.type in ("bid", "keyword"):
                key += (tuple(sorted(k.kw.strip().lower() for k in c.keywords)),)
            groups.setdefault(key, []).append(c)
    moved: dict[str, str] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        top = max(cfg.priority.get(c.family, 0.0) for c in group)
        best = [c for c in group if cfg.priority.get(c.family, 0.0) == top]
        keeper = best[0]
        if cfg.merge_keeper == "higher_stake":
            keeper = max(best, key=lambda c: c.stake)  # max returns the first of equals
        for c in group:
            if c is keeper:
                continue
            c.status = "merged"
            c.superseded_by = keeper.id
            if c.id != keeper.id:
                moved[c.id] = keeper.id
            if c.tension_group and not keeper.tension_group:
                keeper.tension_group, keeper.tension_with = c.tension_group, c.tension_with
            if c.trigger:
                keeper.trigger = f"{keeper.trigger} · also: {c.trigger}" if keeper.trigger else c.trigger
    for c in cards:
        if c.tension_with in moved and c.status != "merged":
            c.tension_with = moved[c.tension_with]
    return [c for c in cards if c.status != "merged"]


def resolve_bid_conflicts(cards: Sequence[ActionCard]) -> int:
    """One term, one direction per entity.

    Over pending ``bid`` cards with an entity and at least one keyword, keyed
    by ``(entity_id, lower(first keyword))``: when two cards on a key point
    in different directions, the one with the larger ``evidence["spend"]``
    wins (ties favour the earlier card); the loser becomes ``superseded``.
    Returns the number superseded.
    """
    best: dict[tuple, ActionCard] = {}
    n = 0
    for c in cards:
        if c.status != "pending" or c.type != "bid" or c.entity_id is None or not c.keywords:
            continue
        key = (c.entity_id, c.keywords[0].kw.strip().lower())
        cur = best.get(key)
        if cur is None:
            best[key] = c
            continue
        if cur.direction == c.direction:
            continue
        s_cur = float(cur.evidence.get("spend") or 0.0)
        s_new = float(c.evidence.get("spend") or 0.0)
        winner, loser = (c, cur) if s_new > s_cur else (cur, c)
        loser.status = "superseded"
        loser.superseded_by = winner.id
        loser.veto_reason = (f"opposite bid move on the same term: the read on {max(s_cur, s_new):,.0f} spend "
                             f"outweighs the read on {min(s_cur, s_new):,.0f}")
        loser.extras["reason_code"] = R_BID_CONFLICT
        best[key] = winner
        n += 1
    return n


def _side(card: ActionCard) -> str:
    return "down" if (card.direction == "down" or pauses_entity(card)) else "up"


def resolve_budget_conflicts(cards: Sequence[ActionCard], cfg: PipelineConfig | None = None) -> tuple[int, int]:
    """Resolve budget-grain contradictions per entity with the priority table.

    Participants: pending cards with an entity that are either budget cards
    (direction up or down) or experiments that pause the entity (counted as
    a down-move).  Keyword and bid cards never participate: a budget cut,
    keyword pauses, keyword adds and a bid move on one entity are one
    coherent strategy sequenced by waves.

    For each entity with both an up side and a down side:

    * ``winner = argmax priority[family]`` (unknown family 0; ties resolve
      to list order).
    * ``escalate = [c != winner with c.stake > valve * max(winner.stake, floor)]``
      over the cards on the side opposing the winner.  Letting an ally
      of the winner trip the valve would put a group in tension although
      the losing side was small, so allies are ignored by default
      (``valve_counts_allies = True`` counts them).
    * If ``escalate`` is non-empty the table abstains: every card in the
      group gets ``status = "tension"``, ``tension_group = str(entity_id)``
      and ``tension_with`` (the highest-stake escalating card for the
      winner, the winner for everyone else).  The whole set is one group.
    * Otherwise cards on the side opposing the winner become ``superseded``
      (``superseded_by = winner.id``); allies stay pending.

    Returns ``(n_superseded, n_tension_cards)``.
    """
    cfg = cfg or PipelineConfig()
    groups: dict[str, list[ActionCard]] = {}
    for c in cards:
        if c.status != "pending" or c.entity_id is None:
            continue
        if (c.type == "budget" and c.direction in ("up", "down")) or pauses_entity(c):
            groups.setdefault(c.entity_id, []).append(c)
    n_sup = n_ten = 0
    for entity_id, group in groups.items():
        sides = {_side(c) for c in group}
        if len(sides) < 2:
            continue
        winner = max(group, key=lambda c: cfg.priority.get(c.family, 0.0))
        w_stake = max(winner.stake, cfg.valve_min_winner_stake)
        w_side = _side(winner)
        escalate = [c for c in group if c is not winner and c.stake > cfg.stake_ratio_valve * w_stake
                    and (cfg.valve_counts_allies or _side(c) != w_side)]
        if escalate:
            top = max(escalate, key=lambda c: c.stake)
            for c in group:
                c.status = "tension"
                c.tension_group = str(entity_id)
                c.tension_with = top.id if c is winner else winner.id
                c.veto_reason = (f"priority table favours {winner.family}, but the opposing {top.family} card "
                                 f"carries {top.stake / w_stake:.1f}x its stake - "
                                 f"apply exactly ONE of the {len(group)} cards on this entity")
                c.extras["reason_code"] = R_TENSION
                n_ten += 1
        else:
            for c in group:
                if c is winner or _side(c) == w_side:
                    continue
                c.status = "superseded"
                c.superseded_by = winner.id
                c.veto_reason = f"priority table: {winner.family} outranks {c.family}"
                c.extras["reason_code"] = R_PRIORITY
                n_sup += 1
    return n_sup, n_ten


def resolve(cards: Sequence[ActionCard], ctx: PlanContext) -> list[ActionCard]:
    """Run the conflict table: supply gate, duplicate merge, bid, budget.

    Order is fixed (``supply_gate`` -> ``merge_duplicates`` ->
    ``resolve_bid_conflicts`` -> ``resolve_budget_conflicts``).  Returns all
    cards except merged duplicates, statuses updated in place.
    """
    supply_gate(cards, ctx)
    kept = merge_duplicates(cards, ctx.cfg)
    resolve_bid_conflicts(kept)
    resolve_budget_conflicts(kept, ctx.cfg)
    return kept


def contested_ratio(days_by_entity: Mapping[str, Iterable]) -> float | None:
    """Share of live days on which two or more entities actually overlapped.

    ``days_by_entity`` maps each entity to the set of days it spent on the
    keyword (in the geo).  ``ratio = |days with >= 2 entities| / |union|``.
    Returns ``None`` when there are no live days.
    """
    sets = {k: set(v) for k, v in days_by_entity.items()}
    if not any(sets.values()):
        return None
    return _contested_ratio(sets)    # one implementation, in cpm_lineage.verdict


def apply_contested_check(
    cards: Sequence[ActionCard],
    days_by_card: Mapping[str, Mapping[str, Iterable]],
    cfg: PipelineConfig | None = None,
) -> int:
    """Veto collision cards whose entities take turns instead of colliding.

    For each actionable ``collision`` card with an entry in ``days_by_card``
    (card id -> ``{entity_id: days}``): if ``contested_ratio`` is below
    ``contested_min_ratio`` (0.30) the card is vetoed as "partitioned - a
    schedule, not a collision".  Cards without data are skipped (fail open).
    Returns the number vetoed.
    """
    cfg = cfg or PipelineConfig()
    n = 0
    for c in cards:
        if c.family != "collision" or not is_live(c) or c.id not in days_by_card:
            continue
        ratio = contested_ratio(days_by_card[c.id])
        if ratio is None:
            continue
        c.evidence["contested_ratio"] = round(ratio, 2)
        if ratio < cfg.contested_min_ratio:
            veto_card(c, R_PARTITIONED, f"partitioned - a schedule, not a collision "
                                        f"(contested on {100 * ratio:.0f}% of live days)")
            n += 1
    return n
