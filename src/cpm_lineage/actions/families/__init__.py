"""The rule families: pure ``ActionContext -> list[ActionCard]`` functions.

``FAMILY_RULES`` lists them in run order; ``run_families`` runs all (or a
subset) and returns the cards together with the verdict-profile scoreboard.
Nothing here vetoes, resolves conflicts, scores or caps: that is the plan
pipeline's job.  ``primitives.apply_playbook`` is the one gate provided here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from ...schema import ActionCard
from ..primitives import ActionContext
from .bid import bid, head_term_cards, keyword_owner_map
from .collision import PLACEHOLDER_KEYWORDS, Collision, collision, find_collisions
from .defense import defense, moat_campaign
from .efficiency import (classic_cut_card, classic_scale_card, efficiency, efficiency_cut,
                         efficiency_scale, ladder_cards, pause_cards, verdict_scoreboard)
from .experiment import experiment
from .geo import control_geos, expand_cards, geo, weak_geo_cards
from .growth import growth
from .sovgap import sov_gap

__all__ = [
    "FAMILY_RULES", "FamilyRun", "run_families",
    "efficiency", "efficiency_cut", "efficiency_scale", "growth", "defense", "bid", "experiment",
    "sov_gap", "geo", "collision",
    "classic_cut_card", "classic_scale_card", "ladder_cards", "pause_cards", "verdict_scoreboard",
    "head_term_cards", "keyword_owner_map", "moat_campaign", "control_geos", "weak_geo_cards",
    "expand_cards", "find_collisions", "Collision", "PLACEHOLDER_KEYWORDS",
]

FamilyRule = Callable[[ActionContext], list[ActionCard]]

# Run order.  "efficiency" emits both efficiency-cut and efficiency-scale cards.
FAMILY_RULES: dict[str, FamilyRule] = {
    "efficiency": efficiency,
    "growth": growth,
    "defense": defense,
    "bid": bid,
    "experiment": experiment,
    "sov-gap": sov_gap,
    "geo": geo,
    "collision": collision,
}


@dataclass
class FamilyRun:
    """Output of ``run_families``: candidate cards plus the verdict scoreboard."""

    cards: list[ActionCard] = field(default_factory=list)
    board: dict[str, dict[str, object]] = field(default_factory=dict)


def run_families(ctx: ActionContext, families: Iterable[str] | None = None) -> FamilyRun:
    """Run the rule families over one context.

    ``families`` names a subset of ``FAMILY_RULES`` (default: all, in run
    order).  Cards are returned as emitted: all ``pending``, duplicates not
    merged, no vetoes applied.  ``board`` is ``verdict_scoreboard(ctx)``
    (empty under the classic profile).  Identical contexts give identical
    card ids in identical order.
    """
    names = list(FAMILY_RULES) if families is None else list(families)
    unknown = [n for n in names if n not in FAMILY_RULES]
    if unknown:
        raise ValueError(f"unknown rule families: {unknown}")
    cards: list[ActionCard] = []
    for name in names:
        cards.extend(FAMILY_RULES[name](ctx))
    return FamilyRun(cards=cards, board=verdict_scoreboard(ctx))
