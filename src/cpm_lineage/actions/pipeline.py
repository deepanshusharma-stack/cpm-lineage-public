"""The plan core: from raw candidate cards to a reviewable plan.

Order matters and is fixed (``plan_actions``):

1. holdout scrub and dead-entity scrub (cards removed);
2. under-test veto, fundability veto, capability-playbook veto;
3. conflict table (supply gate, duplicate merge, bid and budget conflicts,
   abstention into tension groups);
4. confidence and expected value for every card;
5. run guardrail (ROAS floor or spend ceiling), equity annotation and gate;
6. per-family caps - after every gate, so a vetoed card never uses a slot;
7. annotations that never veto: market posture, demand priority, term
   owner, waves, coordination notes;
8. telemetry: how often each gate fired.

``build_plan`` then applies optional marginal sizing and budget-neutral
rebalancing, sorts, applies the global reviewability cap and assembles the
``Plan``.  Nothing is auto-applied and nothing here does I/O: the clock, the
history and every market reading are injected through ``PlanContext``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..liveness import is_dead
from ..schema import ActionCard, AppliedAction, TargetingUnit
from .guardrail import GuardrailResult, annotate_equity, apply_guardrail, equity_gate
from .pipeline_config import (
    GOAL_BIAS, PipelineConfig, PlanContext, R_CAPABILITY, R_EQUITY, R_GUARDRAIL, R_SUPPLY,
    R_UNDER_TEST, R_UNFUNDABLE, R_UNSTATED, change_bounds, is_live, reason_code, veto_card,
)
from .rebalance import BoardEntry, rebalance
from .resolve import apply_contested_check, resolve
from .scoring import apply_marginal, expected_value, marginal_roas_by_entity, score_cards

__all__ = [
    "holdout_ids", "under_test_entities", "donor_exclusions", "scope_telemetry", "scrub", "veto_under_test", "veto_unfundable",
    "veto_capability", "family_caps", "pending_cap", "annotate_posture", "demand_priority",
    "annotate_demand_priority", "annotate_term_owner", "assign_waves", "coordinate",
    "PlanResult", "plan_actions", "plan_rank", "apply_polish", "Plan", "build_plan", "default_plan_id",
]

Polisher = Callable[[Sequence[ActionCard]], Mapping[str, Mapping[str, str]]]


# --------------------------------------------------------------------------
# Holdout and history-driven memory
# --------------------------------------------------------------------------

def _period_key(today: date, period: str) -> str:
    if period == "week":
        iso = today.isocalendar()
        return f"{iso[0]:04d}-W{iso[1]:02d}"
    return f"{today.year:04d}-{today.month:02d}"


def holdout_ids(entity_ids: Iterable[str], today: date, pct: float = 15.0, period: str = "month") -> list[str]:
    """Pick the standing counterfactual group: entities that get no cards.

    ``h = int(sha1(f"{entity_id}|{period_key}").hexdigest()[:8], 16) % 100``;
    held when ``h < pct``.  ``period_key`` is ``YYYY-MM`` (or ISO
    ``YYYY-Www``), so the draw is stable within a period and rotates after.
    Returns the held ids sorted.  ``pct = 0`` holds nothing.
    """
    key = _period_key(today, period)
    held = []
    for eid in entity_ids:
        h = int(hashlib.sha1(f"{eid}|{key}".encode()).hexdigest()[:8], 16) % 100
        if h < pct:
            held.append(str(eid))
    return sorted(set(held))


def under_test_entities(applied: Iterable[AppliedAction], today: date, days: int = 14) -> frozenset[str]:
    """Entities with an applied experiment in the last ``days`` days.

    An applied action counts when its ``kind`` or ``family`` is
    ``"experiment"`` and ``0 <= today - applied_on <= days``.  A concurrent
    move would confound the test, so the pipeline vetoes other cards there.
    """
    out = set()
    for a in applied:
        if a.entity_id is None or "experiment" not in (a.kind, a.family):
            continue
        if 0 <= (today - a.applied_on).days <= days:
            out.add(a.entity_id)
    return frozenset(out)


def donor_exclusions(applied: Iterable[AppliedAction], today: date, days: int = 14) -> frozenset[str]:
    """Entities that must not fund a budget-neutral swap.

    An entity is excluded when an experiment or a defense move was applied
    on it within the last ``days`` days: taking budget away would confound
    the test or undo the defense.  Pass the result as ``donor_excluded``.
    """
    out = set()
    for a in applied:
        if a.entity_id is None or not {"experiment", "defense"} & {a.kind, a.family}:
            continue
        if 0 <= (today - a.applied_on).days <= days:
            out.add(a.entity_id)
    return frozenset(out)


def scope_telemetry(telemetry: Mapping[str, Any], scope: Iterable[str] | None) -> dict[str, Any]:
    """Restrict budget telemetry to the operator's selection.

    Returns only the entries whose entity is in ``scope`` so that a sweep
    never cards an entity outside the selection.  An empty or missing scope
    keeps the map whole.
    """
    ids = set(scope or ())
    if not ids:
        return dict(telemetry)
    return {k: v for k, v in telemetry.items() if k in ids}


# --------------------------------------------------------------------------
# Scrubs and vetoes
# --------------------------------------------------------------------------

def scrub(cards: Sequence[ActionCard], ctx: PlanContext, held: Iterable[str] = ()) -> tuple[list[ActionCard], int, int]:
    """Remove cards that must not exist: held-out and dead entities.

    A card is dropped when its entity is in ``held`` or when the entity is
    dead by the shared liveness rule (hard-dead status, or soft-dead status
    with no spend in the last few days).  Entities with no state record or
    no status are treated as live.  Returns
    ``(kept, n_holdout_excluded, n_dead_excluded)``.
    """
    held_set = set(held)
    kept: list[ActionCard] = []
    n_held = n_dead = 0
    for c in cards:
        if c.entity_id is not None and c.entity_id in held_set:
            n_held += 1
            continue
        ent = ctx.entities.get(c.entity_id) if c.entity_id is not None else None
        if ent is not None and ent.status is not None and is_dead(
                ent.status, ent.spend_3d, status_map=ctx.profile.status_map):
            n_dead += 1
            continue
        kept.append(c)
    return kept, n_held, n_dead


def veto_under_test(cards: Sequence[ActionCard], ctx: PlanContext) -> int:
    """Veto every non-watch card on an entity that is under a running experiment."""
    n = 0
    for c in cards:
        if c.status == "pending" and c.entity_id in ctx.under_test and c.type != "watch":
            veto_card(c, R_UNDER_TEST, "under experiment: a concurrent move would confound the test")
            n += 1
    return n


def veto_unfundable(cards: Sequence[ActionCard], ctx: PlanContext) -> int:
    """Veto budget raises that cannot be stated or would buy nothing.

    For each pending budget card with ``direction == "up"``: veto when
    ``change.frm`` or ``change.to`` is missing (the raise cannot be stated
    in currency), or when the entity is not budget-bound
    (``BudgetTelemetry.is_bound``; no telemetry = not bound).  The bound
    definition is ``cfg.budget_bound_method`` or, by rule profile,
    early-exhaustion (classic, marginal) or 3-day sufficiency (verdict; when
    the platform reports no served-time telemetry the verdict profile falls
    back to early-exhaustion, the same rule the families use).
    Returns the number vetoed.
    """
    method = ctx.cfg.budget_bound_method or ("auto" if ctx.verdict else "exhaustion")
    n = 0
    for c in cards:
        if c.status != "pending" or c.type != "budget" or c.direction != "up":
            continue
        if change_bounds(c) is None:
            veto_card(c, R_UNSTATED, "this raise cannot be stated in currency (no current budget on record)")
            n += 1
            continue
        tel = ctx.telemetry.get(c.entity_id) if c.entity_id is not None else None
        if tel is None or not tel.is_bound(ctx.budget_policy, method):
            veto_card(c, R_UNFUNDABLE, "budget-sufficient: a bigger cap buys nothing; "
                                       "the lever is bids, targeting or coverage")
            n += 1
    return n


def veto_capability(cards: Sequence[ActionCard], ctx: PlanContext) -> int:
    """Veto cards whose lever does not exist on the entity's ad type.

    The ad type comes from budget telemetry first, the entity record as
    fallback; an unknown type gets the profile's default (fully levered)
    class.  ``card.type`` must be in ``profile.allowed_levers(ad_type)``.
    Returns the number vetoed.
    """
    n = 0
    for c in cards:
        if c.status != "pending" or c.entity_id is None:
            continue
        ad_type = ctx.ad_type(c.entity_id)
        cap = ctx.profile.capability(ad_type)
        if c.type not in cap.allowed_levers(ctx.profile.playbook):
            veto_card(c, R_CAPABILITY, f"capability playbook: {cap.capability_class} entities have no "
                                       f"{c.type} lever")
            n += 1
    return n


# --------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------

def family_caps(cards: Sequence[ActionCard], cfg: PipelineConfig | None = None) -> tuple[list[ActionCard], int]:
    """Keep the plan reviewable: cap pending cards per (family, type).

    Cards are visited by stake descending.  Non-pending cards are always
    kept and never count.  A pending card is kept while its
    ``(family, type)`` count is within the cap (default 99).  A dropped card
    whose ``tension_group`` matches a kept card's is re-admitted so a choice
    pair is never stranded.  Returns ``(kept + rescued, n_dropped)``.
    """
    cfg = cfg or PipelineConfig()
    counts: dict[tuple[str, str], int] = {}
    kept: list[ActionCard] = []
    dropped: list[ActionCard] = []
    for c in sorted(cards, key=lambda c: -c.stake):
        if c.status != "pending":
            kept.append(c)
            continue
        key = (c.family, c.type)
        counts[key] = counts.get(key, 0) + 1
        (kept if counts[key] <= cfg.family_caps.get(key, cfg.family_cap_default) else dropped).append(c)
    groups = {c.tension_group for c in kept if c.tension_group}
    rescued = [c for c in dropped if c.tension_group and c.tension_group in groups]
    return kept + rescued, len(dropped) - len(rescued)


def pending_cap(cards: Sequence[ActionCard], cap: int = 25) -> tuple[list[ActionCard], int]:
    """Global reviewability cap on an already sorted plan.

    Pending cards beyond the ``cap``-th in order are cut, except those whose
    ``tension_group`` still has a surviving pending or tension member.
    Returns ``(kept, n_cut)``.
    """
    pend = [c for c in cards if c.status == "pending"]
    if len(pend) <= cap:
        return list(cards), 0
    over = {id(c) for c in pend[cap:]}
    survivors = {c.tension_group for c in cards if c.tension_group and is_live(c) and id(c) not in over}
    kept = [c for c in cards if id(c) not in over or (c.tension_group and c.tension_group in survivors)]
    return kept, len(cards) - len(kept)


# --------------------------------------------------------------------------
# Annotations (never vetoes)
# --------------------------------------------------------------------------

def annotate_posture(cards: Sequence[ActionCard], ctx: PlanContext) -> int:
    """Stamp a market-posture note on actionable scale-ups.

    With ``ms = 100 * own_sales / total_sales`` and
    ``growth = 100 * (total / total_prior - 1)``: ``ms >= 60`` -> a
    saturation note; else ``growth < -10`` -> a declining-segment note.
    Stored in ``card.posture`` and appended to the trigger.  Never changes
    status.  Returns the number stamped.
    """
    cfg = ctx.cfg
    n = 0
    for c in cards:
        if not is_live(c) or c.direction != "up" or c.segment is None:
            continue
        seg = ctx.segments.get(c.segment)
        if seg is None or not seg.total_sales or not seg.total_sales_prior or seg.own_sales is None:
            continue
        ms = 100.0 * seg.own_sales / seg.total_sales
        growth = 100.0 * (seg.total_sales / seg.total_sales_prior - 1.0)
        note = None
        if ms >= cfg.posture_saturation_share:
            note = f"saturation: own share is {ms:.0f}% of the segment - each extra point costs steeply more"
        elif growth < cfg.posture_decline_pct:
            note = f"declining segment ({growth:+.0f}%): harvest efficiently rather than expand"
        if note:
            c.posture = note
            c.trigger = f"{c.trigger} · {note}" if c.trigger else note
            n += 1
    return n


def demand_priority(units: Iterable[TargetingUnit]) -> dict[tuple[str | None, str], int]:
    """Rank each keyword by the search demand of its role bucket.

    Search volume is summed per ``(segment, role)``; within a segment the
    buckets are ranked by total volume (0 = highest).  Returns
    ``{(segment, keyword): rank}`` for use as ``PlanContext.keyword_priority``.
    """
    volume: dict[tuple[str | None, str], float] = {}
    role_of: dict[tuple[str | None, str], str] = {}
    for u in units:
        volume[(u.segment, u.role)] = volume.get((u.segment, u.role), 0.0) + (u.search_volume or 0.0)
        role_of[(u.segment, u.value.strip().lower())] = u.role
    ranks: dict[tuple[str | None, str], int] = {}
    for seg in {s for s, _ in volume}:
        roles = sorted((r for s, r in volume if s == seg), key=lambda r: (-volume[(seg, r)], r))
        for i, r in enumerate(roles):
            ranks[(seg, r)] = i
    return {key: ranks[(key[0], role)] for key, role in role_of.items()}


def annotate_demand_priority(cards: Sequence[ActionCard], ctx: PlanContext) -> None:
    """Stamp ``kw_priority`` (0 = highest demand bucket) on keyword-bearing
    actionable cards; also ``evidence["bucket_sv_rank"] = kw_priority + 1``.
    Used for ordering only."""
    if not ctx.keyword_priority:
        return
    for c in cards:
        if not is_live(c) or not c.keywords:
            continue
        rank = ctx.keyword_priority.get((c.segment, c.keywords[0].kw.strip().lower()))
        if rank is not None:
            c.kw_priority = int(rank)
            c.evidence["bucket_sv_rank"] = int(rank) + 1


def annotate_term_owner(cards: Sequence[ActionCard], ctx: PlanContext) -> int:
    """Stamp who holds the first keyword of each keyword-bearing actionable card.

    Notes: another brand takes >= 60% of the term's organic impressions; we
    own the term; a rival's ad impressions exceed ours; >= 8 brands
    advertise on it.  Stored in ``card.lever_field``, appended to the
    trigger, and in ``evidence`` (``term_owner``, ``advertisers``,
    ``owner_share``).  Fail-open: no data, no stamp.  Returns the number
    stamped.
    """
    cfg = ctx.cfg
    n = 0
    for c in cards:
        if not is_live(c) or not c.keywords:
            continue
        term = c.keywords[0].kw.strip().lower()
        t = ctx.term_owners.get(term)
        if t is None:
            continue
        notes = []
        if t.owned_by_us:
            notes.append("we own this term organically")
        elif t.owner and t.owner_share is not None and t.owner_share >= cfg.term_owner_share:
            notes.append(f"{t.owner} takes {100 * t.owner_share:.0f}% of this term's organic impressions "
                         "- rented attention")
        if (t.rival_ad_impressions is not None and t.own_ad_impressions is not None
                and t.rival_ad_impressions > t.own_ad_impressions):
            notes.append(f"{t.rival or 'a rival'} out-advertises us on it")
        if t.advertisers is not None and t.advertisers >= cfg.term_crowded_advertisers:
            notes.append(f"{t.advertisers} brands advertise on it")
        c.evidence.update({"term_owner": t.owner, "advertisers": t.advertisers, "owner_share": t.owner_share})
        if notes:
            c.lever_field = "; ".join(notes)
            c.trigger = f"{c.trigger} · {c.lever_field}" if c.trigger else c.lever_field
            n += 1
    return n


def assign_waves(cards: Sequence[ActionCard]) -> None:
    """Sequence actionable cards so outcomes stay attributable.

    ``direction == "down"`` -> wave 1; else type bid or experiment, or
    ``direction == "none"`` -> wave 2; else wave 3.  Cuts settle before
    scale-ups.
    """
    for c in cards:
        if not is_live(c):
            continue
        if c.direction == "down":
            c.wave = 1
        elif c.type in ("bid", "experiment") or c.direction == "none":
            c.wave = 2
        else:
            c.wave = 3


def coordinate(cards: Sequence[ActionCard]) -> None:
    """Write sibling-coordination notes (``card.coordination``).

    * An entity with >= 2 actionable cards: each names its siblings with
      their wave and says to act in wave order and re-measure between waves.
    * A keyword appearing in >= 2 actionable cards from >= 2 families: each
      names the other families so the corrections do not compound.
    """
    live = [c for c in cards if is_live(c)]
    by_entity: dict[str, list[ActionCard]] = {}
    for c in live:
        if c.entity_id is not None:
            by_entity.setdefault(c.entity_id, []).append(c)
    for group in by_entity.values():
        if len(group) < 2:
            continue
        for c in group:
            sibs = ", ".join(f"{s.title} (wave {s.wave})" for s in group if s is not c)
            c.coordination = f"also on this entity: {sibs} - act in wave order and re-measure between waves"
    by_kw: dict[str, list[ActionCard]] = {}
    for c in live:
        for kw in {k.kw.strip().lower() for k in (c.keywords or [])}:
            by_kw.setdefault(kw, []).append(c)
    for kw, group in by_kw.items():
        fams = {c.family for c in group}
        if len(group) < 2 or len(fams) < 2:
            continue
        for c in group:
            others = ", ".join(sorted(fams - {c.family}))
            if not others:
                continue
            note = f"'{kw}' is also touched by {others} - coordinate so the corrections don't compound"
            c.coordination = f"{c.coordination} · {note}" if c.coordination else note


# --------------------------------------------------------------------------
# Plan core
# --------------------------------------------------------------------------

@dataclass
class PlanResult:
    """Output of ``plan_actions``."""

    cards: list[ActionCard]
    dropped: int                 # pending cards cut by per-family caps
    guard_note: str
    telemetry: dict[str, Any]
    holdout: list[str]
    guardrail: GuardrailResult = field(default_factory=GuardrailResult)


def _gate_counts(cards: Sequence[ActionCard]) -> dict[str, int]:
    codes: dict[str, int] = {}
    for c in cards:
        code = reason_code(c)
        if c.status == "vetoed" and code:
            codes[code] = codes.get(code, 0) + 1
    return {
        "capability_playbook": codes.get(R_CAPABILITY, 0),
        "under_test": codes.get(R_UNDER_TEST, 0),
        "unfundable": codes.get(R_UNSTATED, 0) + codes.get(R_UNFUNDABLE, 0),
        "supply_gate": codes.get(R_SUPPLY, 0),
        "guardrail": codes.get(R_GUARDRAIL, 0),
        "equity_gate": codes.get(R_EQUITY, 0),
        "superseded": sum(1 for c in cards if c.status == "superseded"),
        "tension": sum(1 for c in cards if c.status == "tension"),
    }


def plan_actions(
    cards: Sequence[ActionCard],
    ctx: PlanContext,
    check: Any = None,
    *,
    holdout: Iterable[str] | None = None,
) -> PlanResult:
    """Decide which candidate cards are actionable (the plan core).

    Runs, in this order: holdout and dead scrubs, under-test veto,
    fundability veto, capability veto, the conflict table, confidence and
    expected value, the run guardrail, equity annotation and gate,
    per-family caps, posture / demand / term-owner annotations, waves,
    coordination notes, telemetry.

    ``holdout`` is the held-out entity list; when ``None`` it is drawn from
    the eligible entities in ``ctx.entities`` with ``holdout_ids`` (needs
    ``ctx.today``; no clock = no holdout).  Families should already have
    excluded those entities; the scrub here catches stragglers.

    Cards are mutated in place.  Returns a ``PlanResult``; the telemetry is
    ``{families, capped, gates, holdout, holdout_excluded, dead_excluded}``
    with gates counted by reason code.
    """
    cfg = ctx.cfg
    if holdout is None:
        held = (holdout_ids((e.entity_id for e in ctx.entities.values() if e.eligible), ctx.today,
                            cfg.holdout_pct, cfg.holdout_period) if ctx.today is not None else [])
    else:
        held = sorted(set(holdout))
    fam_counts: dict[str, int] = {}
    for c in cards:
        fam_counts[c.family] = fam_counts.get(c.family, 0) + 1

    acts, n_held, n_dead = scrub(cards, ctx, held)
    veto_under_test(acts, ctx)
    veto_unfundable(acts, ctx)
    veto_capability(acts, ctx)
    acts = resolve(acts, ctx)
    score_cards(acts, ctx)
    guard = apply_guardrail(acts, check, ctx)
    annotate_equity(acts, ctx)
    equity_gate(acts, ctx)
    gates = _gate_counts(acts)
    acts, dropped = family_caps(acts, cfg)
    annotate_posture(acts, ctx)
    annotate_demand_priority(acts, ctx)
    annotate_term_owner(acts, ctx)
    assign_waves(acts)
    coordinate(acts)

    notes = [guard.note] if guard.note else []
    if ctx.supply_gate_on and not any(s.availability is not None for s in ctx.segments.values()):
        notes.append("supply gate NOT armed: no availability data")
    telemetry = {"families": fam_counts, "capped": dropped, "gates": gates, "holdout": len(held),
                 "holdout_excluded": n_held, "dead_excluded": n_dead}
    return PlanResult(acts, dropped, " · ".join(notes), telemetry, list(held), guard)


_STATUS_ORDER = {"pending": 0, "tension": 1, "superseded": 2, "vetoed": 3}


def plan_rank(card: ActionCard, goal_metric: str | None = None,
              goal_bias: Mapping[str, Mapping[str, float]] | None = None) -> tuple:
    """Sort key of a card in the plan (ascending).

    ``(status order (pending 0, tension 1, superseded 2, vetoed and unknown
    3), -goal bias of the family, kw_priority or 0, 0 if ev_kind == "money"
    else 1, -ev_day, -confidence, -stake)``.  The goal only reorders; real
    money always sorts ahead of opportunity-unit numbers.
    """
    bias = (goal_bias or GOAL_BIAS).get(goal_metric or "", {}).get(card.family, 0.0)
    return (_STATUS_ORDER.get(card.status, 3), -bias, card.kw_priority or 0,
            0 if card.ev_kind == "money" else 1, -(card.ev_day or 0), -(card.confidence or 0), -card.stake)


# --------------------------------------------------------------------------
# Optional prose polish
# --------------------------------------------------------------------------

_NUM = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {m.rstrip("0").rstrip(".") if "." in m else m for m in _NUM.findall(text.replace(",", ""))}


def apply_polish(cards: Sequence[ActionCard], polisher: Polisher | None, cfg: PipelineConfig | None = None) -> int:
    """Let an optional rewriter improve card wording, under two guards.

    ``polisher`` receives the first ``polish_batch`` (12) pending cards and
    returns ``{card_id: {"why": ..., "caution": ...}}``.  A rewrite is
    accepted per card only if every number in it already appears in the
    card's own ``facts + trigger`` and the first keyword's name (when the
    card has keywords) is still present.  Accepted text goes to
    ``card.wording`` (300 chars) and ``card.caution`` (200 chars).  Any
    failure is silence.  It never adds, removes, resizes or reorders cards.
    Returns the number of cards rewritten.
    """
    if polisher is None:
        return 0
    cfg = cfg or PipelineConfig()
    batch = [c for c in cards if c.status == "pending"][: cfg.polish_batch]
    try:
        out = polisher(batch) or {}
    except Exception:
        return 0
    n = 0
    for c in batch:
        item = out.get(c.id) if isinstance(out, Mapping) else None
        if not isinstance(item, Mapping):
            continue
        why, caution = str(item.get("why") or ""), str(item.get("caution") or "")
        text = f"{why} {caution}"
        if not why or not _numbers(text) <= _numbers(f"{c.facts} {c.trigger}"):
            continue
        if c.keywords and c.keywords[0].kw.lower() not in text.lower():
            continue
        c.wording = why[: cfg.polish_why_chars]
        c.caution = caution[: cfg.polish_caution_chars] or c.caution
        n += 1
    return n


# --------------------------------------------------------------------------
# Plan assembly
# --------------------------------------------------------------------------

@dataclass
class Plan:
    """The emitted plan document: sorted cards plus provenance and telemetry."""

    id: str
    created: str                      # ISO date
    generator: str                    # rule profile that actually produced the plan
    actions: list[ActionCard]
    economics: dict[str, float]
    days: int
    holdout: list[str]
    telemetry: dict[str, Any]
    guard_note: str = ""
    coverage_note: str = ""
    engine_version: str = ""
    status_source: str = "ok"
    n_segments: int = 0
    n_entities: int = 0
    segment: str | None = None
    goal: dict[str, Any] | None = None
    check: dict[str, Any] | None = None
    context: str = ""
    verdict_states: dict[str, int] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Plain nested dict (cards included) for storage or JSON."""
        return asdict(self)

    @property
    def actionable(self) -> list[ActionCard]:
        """The cards an operator can still act on (pending or in tension)."""
        return [c for c in self.actions if is_live(c)]


def _check_dict(check: Any) -> dict[str, Any] | None:
    if check is None:
        return None
    if isinstance(check, Mapping):
        return dict(check)
    return {k: getattr(check, k, None) for k in ("metric", "op", "value", "mode")}


def default_plan_id(created: str, generator: str, cards: Sequence[ActionCard], *, budget_neutral: bool = False,
                    check: Any = None, scope: Iterable[str] | None = None, plan_key: str = "") -> str:
    """Deterministic plan id: 12 hex characters of a SHA-1 over what defines the plan.

    Hashes the creation date, the generator (rule profile), the
    budget-neutral flag, the run check, the entity scope, ``plan_key`` (a
    caller-supplied run label, e.g. a timestamp or sequence number, when
    every generation should be a new plan) and the card ids.  The same data
    and options give the same id; a different profile or option does not.
    """
    chk = _check_dict(check)
    parts = [created, generator, "neutral" if budget_neutral else "",
             ",".join(f"{k}={chk[k]}" for k in sorted(chk)) if chk else "",
             ",".join(sorted(scope)) if scope is not None else "", plan_key,
             ",".join(c.id for c in cards)]
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]


def build_plan(
    cards: Sequence[ActionCard],
    ctx: PlanContext,
    *,
    goal: Any = None,
    check: Any = None,
    holdout: Iterable[str] | None = None,
    marginal_readings: Mapping[str, float] | None = None,
    day_rows: Iterable[Any] | None = None,
    board: Sequence[BoardEntry] | None = None,
    budget_neutral: bool = False,
    donor_excluded: frozenset[str] = frozenset(),
    demote: Callable[[ActionCard], bool] | None = None,
    contested_days: Mapping[str, Mapping[str, Iterable]] | None = None,
    polisher: Polisher | None = None,
    plan_id: str | None = None,
    plan_key: str = "",
    engine_version: str = "",
    context_text: str = "",
    segment: str | None = None,
) -> Plan:
    """Assemble the reviewable plan from raw candidate cards.

    1. ``plan_actions`` (gates, conflicts, scores, caps, annotations).
    2. Under the ``"marginal"`` rule profile: ``apply_marginal`` with
       ``marginal_readings`` (or readings computed from ``day_rows``).  With
       neither, a warning is disclosed and the plan is labelled
       ``"classic"`` so it never contaminates a comparison of profiles.
       With ``contested_days`` (card id -> ``{entity_id: days}``):
       collision cards whose entities take turns are vetoed as partitioned.
    3. With ``budget_neutral`` and a ``board``: donor cuts from
       ``rebalance`` are appended and disclosed.
    4. Entity name and status are stamped; optional prose polish;
       ``goal_aligned`` is set from the goal bias table.
    5. Cards are sorted by ``plan_rank`` and the global pending cap (25) is
       applied, keeping tension groups whole.
    6. Disclosures are joined into ``guard_note``: guardrail enforcement,
       unarmed supply gate, degraded status source, operator floors,
       non-default economics.

    ``goal`` and ``check`` are ``RunCheck``-like objects or mappings.  The
    plan id defaults to :func:`default_plan_id`: a deterministic hash of the
    date, the generator, the options that change a plan (budget-neutral,
    check, scope), the caller's ``plan_key`` and the card ids, so plans of
    different rule profiles never share an id;
    ``created`` is ``ctx.today``.  Returns a ``Plan``.
    """
    cfg = ctx.cfg
    res = plan_actions(cards, ctx, check, holdout=holdout)
    acts = res.cards
    notes = [res.guard_note] if res.guard_note else []
    telemetry = dict(res.telemetry)
    generator = ctx.rule_profile

    if ctx.rule_profile == "marginal":
        readings = marginal_readings
        if readings is None and day_rows is not None:
            readings = marginal_roas_by_entity(day_rows, ctx)
        if readings is None:
            notes.append("marginal sizing unavailable (no entity x day data): plan produced by the classic profile")
            generator = "classic"
        else:
            telemetry["gates"] = dict(telemetry["gates"], marginal_gate=apply_marginal(acts, readings, ctx))

    if contested_days:
        telemetry["gates"] = dict(telemetry["gates"],
                                  partitioned=apply_contested_check(acts, contested_days, cfg))

    if budget_neutral and board is not None:
        # A held-out entity is untouched this period: it can never be a donor either.
        rb = rebalance(acts, board, ctx, excluded=frozenset(donor_excluded) | frozenset(res.holdout),
                       demote=demote)
        for d in rb.donors:
            expected_value(d, ctx)
        acts = acts + rb.donors
        telemetry["rebalance"] = rb.telemetry()
        if rb.note:
            notes.append(rb.note)

    goal_d = _check_dict(goal)
    goal_metric = goal_d.get("metric") if goal_d else None
    for c in acts:
        ent = ctx.entities.get(c.entity_id) if c.entity_id is not None else None
        if ent is not None:
            c.entity_name = c.entity_name or ent.name
            c.entity_status = ent.status
        c.goal_aligned = bool(GOAL_BIAS.get(goal_metric or "", {}).get(c.family))
    acts = sorted(acts, key=lambda c: plan_rank(c, goal_metric))
    apply_polish(acts, polisher, cfg)
    acts, cut = pending_cap(acts, cfg.pending_cap)
    dropped = res.dropped + cut
    telemetry["capped"] = dropped

    if ctx.status_source != "ok":
        notes.append(f"entity status source is {ctx.status_source}: liveness may be stale")
    if ctx.operator_floors:
        notes.append("operator floors in force: "
                     + ", ".join(f"{k} >= {v:g}" for k, v in sorted(ctx.operator_floors.items())))
    econ = ctx.economics
    if econ.breakeven != 1.0 or econ.ltv_mult != 1.0:
        notes.append(f"economics: breakeven {econ.breakeven:g}, lifetime-value multiple {econ.ltv_mult:g}, "
                     f"effective floor {econ.be:.2f}")

    created = ctx.today.isoformat() if ctx.today is not None else ""
    if plan_id is None:
        plan_id = default_plan_id(created, generator, acts, budget_neutral=budget_neutral, check=check,
                                  scope=ctx.scope_entity_ids, plan_key=plan_key)
    coverage = (f"{dropped} further candidate(s) beyond the caps - act on these, then regenerate"
                if dropped else "")
    return Plan(
        id=plan_id, created=created, generator=generator, actions=acts,
        economics={"breakeven": econ.breakeven, "ltv_mult": econ.ltv_mult, "be": econ.be},
        days=ctx.lookback_days, holdout=res.holdout, telemetry=telemetry,
        guard_note=" · ".join(notes), coverage_note=coverage, engine_version=engine_version,
        status_source=ctx.status_source,
        n_segments=len({c.segment for c in acts if c.segment is not None}),
        n_entities=len({c.entity_id for c in acts if c.entity_id is not None}),
        segment=segment, goal=goal_d, check=_check_dict(check), context=context_text[:300],
    )
