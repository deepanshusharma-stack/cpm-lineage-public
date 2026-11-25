"""Pure conflict resolvers: signal-versus-signal by granularity, data-versus-data by source authority.

Two questions, two functions:

* ``resolve_action_conflict``: several reads at different grains recommend
  different verbs for the same thing.  One verb wins; they are never
  averaged ("monitor" as a compromise between "scale" and "pause" is an
  anti-pattern, because it is the one action nobody recommended).
* ``resolve_data_conflict``: several sources report different values for the
  same field.  The most authoritative source wins and the disagreement is
  cited so the reader can verify before acting.

Both return a citation string so the losing side is on the record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "Override", "ConflictConfig", "DEFAULT_GRAIN_ORDER", "DEFAULT_OVERRIDES",
    "is_cut", "grain_rank", "source_rank", "resolve_action_conflict", "resolve_data_conflict",
]

#: Coarsest to finest.  A more granular signal has seen more of the specific case.
DEFAULT_GRAIN_ORDER: tuple[str, ...] = ("portfolio", "segment", "product", "geo", "entity", "keyword")


@dataclass(frozen=True)
class Override:
    """A universal override: a signal tagged with ``name`` beats granularity.

    ``requires_opposing_cut``: the override only wins when at least one other
    signal is a cut (it protects something from being cut; with nothing to
    protect it is just another signal).
    """

    name: str
    requires_opposing_cut: bool = False
    citation: str = "{name} override wins regardless of grain"


DEFAULT_OVERRIDES: tuple[Override, ...] = (
    Override("availability", False, "availability block wins: fix supply before any demand action"),
    Override("acquisition", True, "new-customer acquisition gate wins over a cut"),
)


@dataclass(frozen=True)
class ConflictConfig:
    """Orderings used by the two resolvers."""

    grain_order: tuple[str, ...] = DEFAULT_GRAIN_ORDER        # coarsest -> finest; the finest signal wins
    grain_aliases: tuple[tuple[str, str], ...] = (            # common synonyms folded onto the order above
        ("category", "segment"), ("sku", "product"), ("city", "geo"), ("campaign", "entity"),
        ("targeting_unit", "keyword"), ("account", "portfolio"))
    overrides: tuple[Override, ...] = DEFAULT_OVERRIDES       # checked in order, before granularity
    cut_verbs: tuple[str, ...] = ("stop", "cap", "pause", "cut", "negative", "reduce", "tighten")  # substrings marking a cut
    source_authority: tuple[str, ...] = ("operational", "analytics", "warehouse")  # most authoritative first; real-time beats lagged
    unknown_source_rank: int = 9                              # unknown sources rank below every named one


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def is_cut(verb: str | None, cfg: ConflictConfig | None = None) -> bool:
    """True when the lower-cased verb contains any cut marker (stop, cap, pause, cut, ...)."""
    cfg = cfg or ConflictConfig()
    v = (verb or "").lower()
    return any(m in v for m in cfg.cut_verbs)


def grain_rank(grain: str | None, cfg: ConflictConfig | None = None) -> int:
    """Position of a grain in ``cfg.grain_order`` (aliases folded); unknown grains rank -1."""
    cfg = cfg or ConflictConfig()
    g = (grain or "").strip().lower()
    g = dict(cfg.grain_aliases).get(g, g)
    return cfg.grain_order.index(g) if g in cfg.grain_order else -1


def source_rank(source: str | None, cfg: ConflictConfig | None = None) -> int:
    """Authority rank of a source (0 = most authoritative; unknown = ``unknown_source_rank``)."""
    cfg = cfg or ConflictConfig()
    s = (source or "").strip().lower()
    order = [x.lower() for x in cfg.source_authority]
    return order.index(s) if s in order else cfg.unknown_source_rank


def resolve_action_conflict(
    signals: Sequence[Any],
    cfg: ConflictConfig | None = None,
) -> dict[str, Any] | None:
    """Pick the one verb that wins when signals at different grains disagree.

    ``signals`` are mappings (or objects) with ``grain`` and ``verb`` and
    optionally ``source``, ``reason`` and ``override``.  Rules, in order:

    1. Signals without a verb are dropped.  None left -> ``None``.  One left
       -> it wins with no citation.
    2. Universal overrides, in ``cfg.overrides`` order: a signal whose
       ``override`` names a registered override wins outright, except that an
       override with ``requires_opposing_cut`` only wins when some other
       signal's verb is a cut.
    3. Otherwise the most granular signal wins (``cfg.grain_order``; unknown
       grain ranks lowest; ties keep input order).

    Returns ``{"winner", "losers", "citation"}``.  The citation names the
    winner and the first loser:
    ``[CONFLICT: <grain>-><verb> vs <grain>-><verb>; <why>; action=<verb>]``.
    """
    cfg = cfg or ConflictConfig()
    sigs = [s for s in signals if _get(s, "verb")]
    if not sigs:
        return None
    if len(sigs) == 1:
        return {"winner": sigs[0], "losers": [], "citation": None}

    def cite(winner: Any, losers: list[Any], why: str) -> dict[str, Any]:
        first = losers[0]
        text = (f"[CONFLICT: {_get(winner, 'grain')}->{_get(winner, 'verb')} vs "
                f"{_get(first, 'grain')}->{_get(first, 'verb')}; {why}; action={_get(winner, 'verb')}]")
        return {"winner": winner, "losers": losers, "citation": text}

    for ov in cfg.overrides:
        for s in sigs:
            if _get(s, "override") != ov.name:
                continue
            others = [o for o in sigs if o is not s]
            if ov.requires_opposing_cut and not any(is_cut(_get(o, "verb"), cfg) for o in others):
                continue
            return cite(s, others, ov.citation.format(name=ov.name))

    ranked = sorted(sigs, key=lambda s: -grain_rank(_get(s, "grain"), cfg))
    winner = ranked[0]
    return cite(winner, ranked[1:], f"{_get(winner, 'grain')}-level wins: the most granular signal decides")


def resolve_data_conflict(
    rows: Sequence[Any],
    cfg: ConflictConfig | None = None,
) -> dict[str, Any] | None:
    """Pick the value to trust when sources disagree on one field.

    ``rows`` are mappings (or objects) with ``source``, ``field`` and
    ``value``.  Rows with a ``None`` value are dropped; the rest are sorted
    by ``cfg.source_authority`` (stable, unknown sources last) and the first
    wins.  ``disagreements`` are the lower-authority rows whose value
    differs.  When there are any, the citation says the winner's value beats
    the first disagreeing source, which is assumed to lag, and to verify
    before acting.

    Returns ``{"winner", "citation", "disagreements"}`` or ``None`` when no
    row has a value.
    """
    cfg = cfg or ConflictConfig()
    live = [r for r in rows if _get(r, "value") is not None]
    if not live:
        return None
    ranked = sorted(live, key=lambda r: source_rank(_get(r, "source"), cfg))
    winner = ranked[0]
    dis = [r for r in ranked[1:] if _get(r, "value") != _get(winner, "value")]
    citation = None
    if dis:
        d = dis[0]
        citation = (f"[DATA CONFLICT: {_get(winner, 'field')}: {_get(winner, 'source')}={_get(winner, 'value')} "
                    f"beats {_get(d, 'source')}={_get(d, 'value')} (lags); verify before acting]")
    return {"winner": winner, "citation": citation, "disagreements": dis}
