"""The verdict spine: every candidate action leaves with a typed decision.

A card proposed by a rule family is not shipped or dropped silently.  It is
walked through an ordered stack of gates and leaves carrying
``verdict = {"state", "why", "lane"}`` where ``state`` is one of

* ``ACT``        ship it;
* ``OBSERVE``    the read is not trustworthy enough yet (contaminated cohort,
  too few impressions, confidence below the bar, card cannot be filled);
* ``HOLD``       the read is fine but something time-bound must pass first
  (launch grace, a price event, an open monitoring window);
* ``ESCALATE``   a human must decide (an automation owns the lever);
* ``NO-ACTION``  already decided elsewhere (conflict loser, upstream gate,
  applied / skipped by an operator, partitioned collision).

``lane`` is the state plus its reason code, for example
``HOLD(launch-grace)``.  Three invariants hold:

1. **Walk order is law.**  Integrity, lifecycle, evidence, identity, action
   lock, automation, risk bars, terminals.  The first gate to claim a card
   wins, so a contaminated read is named "quarantine" and never
   "insufficient evidence".
2. **Abstention is a recorded verdict.**  Not acting is never silence.
3. **Emittability.**  An ``ACT`` that cannot fill its required fields
   degrades to ``OBSERVE(unfillable)``.

Besides the gate stack this module holds the pre-spine shaping step
(``graduate``: cheapest reversible lever first, staircase raises, the
cumulative-loss sweep, the unowned-ramp freeze), two extra detectors
(``detect_families``), bounded exploration probes (``probes``) and the
contested-day arithmetic that separates a real own-versus-own collision from
a schedule (``contested_ratio`` / ``partition_collisions``).

Everything is a pure function of the cards and a ``SpineContext``.  Cards are
mutated in place (the card type is deliberately mutable); nothing here reads
a clock, a file or a network.  Every missing optional input makes its gate
inert: the spine fails open.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Mapping, MutableSequence, Sequence

from .config import Economics
from .schema import ACTIONABLE_STATUSES, ActionCard, AppliedAction, BudgetTelemetry, Change, PerfRow, Window

__all__ = [
    "ACT", "OBSERVE", "HOLD", "ESCALATE", "NO_ACTION", "STATES", "STAGE_ORDER", "LANES",
    "VerdictConfig", "SpineContext", "SpineResult",
    "demote", "reversibility_class", "risk_bar",
    "stage_integrity", "stage_lifecycle", "stage_evidence", "stage_identity", "stage_action_lock",
    "stage_automation", "stage_risk_bars", "stage_terminals",
    "apply_spine", "graduate", "detect_families", "probes",
    "contested_ratio", "contested_days", "partition_collisions",
    "verdict_states", "run_spine",
    "open_entities", "recent_bid_downs", "owned_entities",
]

ACT = "ACT"
OBSERVE = "OBSERVE"
HOLD = "HOLD"
ESCALATE = "ESCALATE"
NO_ACTION = "NO-ACTION"
STATES: tuple[str, ...] = (ACT, OBSERVE, HOLD, ESCALATE, NO_ACTION)

#: Lanes with a fixed name.  ``NO-ACTION(<status>)`` is open-ended by design.
LANES: tuple[str, ...] = (
    "ACT",
    "OBSERVE(quarantine)", "OBSERVE(bad-budget-field)", "OBSERVE(insufficient)",
    "OBSERVE(below-bar)", "OBSERVE(unfillable)",
    "HOLD(launch-grace)", "HOLD(price-event)", "HOLD(monitoring-window)",
    "ESCALATE(automation-owned)",
    "NO-ACTION(conflict)", "NO-ACTION(gate)", "NO-ACTION(partitioned)",
)

#: Reversibility classes used by the risk bars, cheapest-to-undo first.
REVERSIBILITY_CLASSES: tuple[str, ...] = ("restore", "watch", "experiment", "bid", "budget_up", "cut", "other")


@dataclass(frozen=True)
class VerdictConfig:
    """Every threshold of the verdict spine.  Money values are reference amounts
    scaled by ``Economics.money`` at use, so no currency is baked in."""

    # ---- integrity ----
    quarantine_min_cohort: int = 8             # same-ad-type entities alive exactly one day that mark an ingestion leak
    quarantine_min_window_days: int = 14       # one live day is normal in a short window, so only check long ones
    quarantine_live_days: int = 1              # the "alive exactly N days" shape of a phantom cohort
    placeholder_budget: float = 100.0          # money: a daily cap below this is a placeholder, not a decision
    # ---- lifecycle ----
    launch_grace_days: int = 5                 # CPM delivery starts on day one, so signal lands fast; no cuts before this
    # ---- evidence ----
    evidence_min_impressions: float = 1000.0   # minimum window impressions to judge an entity at all
    evidence_exempt_families: tuple[str, ...] = ("experiment",)  # a probe exists to buy significance
    # ---- identity / price event ----
    price_swing_pp: float = 8.0                # segment discount swing (percentage points) that holds media verdicts
    price_hold_types: tuple[str, ...] = ("budget", "bid", "keyword")  # levers held during a price event
    price_exempt_families: tuple[str, ...] = ("defense",)        # defending the shelf is not a media-efficiency call
    # ---- action lock ----
    lock_days: int = 7                         # one open action per entity until the first measurement horizon
    bid_down_memory_days: int = 14             # the bid ladder is not repeated inside this many days
    # ---- risk bars: confidence an ACT must clear, by reversibility class ----
    bar_restore: int = 30                      # restore / freeze: near-free to undo
    bar_watch: int = 25                        # a watch costs attention only
    bar_experiment: int = 30                   # bounded downside by construction (see ``risk_bar``)
    bar_bid: int = 45                          # reversible within a day
    bar_budget_up: int = 55                    # spends real money before it can be read
    bar_cut: int = 65                          # cuts lose position and history; hardest to undo
    bar_default: int = 50                      # anything unclassified
    cold_start_max_days: int = 21              # a book younger than this has not derived its own parameters
    cold_start_bump: int = 10                  # added to every bar while the book is cold
    restore_title_markers: tuple[str, ...] = ("Restore", "Freeze")  # fallback when no explicit reversibility is set
    # ---- graduate: ladder, staircase, sweeps ----
    ladder_bid_step_pct: float = -25.0         # first response to a keyword cut: the cheapest reversible lever
    ladder_reread_days: int = 5                # then re-read before the original call ships
    staircase_step_pct: float = 25.0           # largest one-shot budget raise; blanket raises decay at the margin
    staircase_reread_days: int = 7             # re-read marginal efficiency before the next step
    bleeder_book_share: float = 0.02           # cumulative loss >= 2% of book spend: small daily bleeds never trip a run rate
    bleeder_confidence: int = 55               # just above the bid bar; a sweep is a prior, not a measurement
    bleeder_conf_part: float = 15.0            # labelled part behind that confidence
    freeze_ramp_mult: float = 1.5              # last-7-day daily spend above 1.5x the window daily average is a ramp
    freeze_min_live_days: int = 14             # need a stable window average to call a ramp
    freeze_recent_days: int = 7                # length of the "recent" spend read
    freeze_confidence: int = 60                # freezing is near-free; drift is not
    freeze_conf_part: float = 20.0  # labelled part behind the freeze confidence
    # ---- extra detectors ----
    dark_roas_mult: float = 3.0                # a "winner" earns at least 3x breakeven
    dark_min_live_days: int = 7                # enough history to call it proven
    dark_min_spend: float = 10_000.0           # money: enough spend to call it proven
    dark_roas_cap: float = 6.0                 # caps the ROAS used in the stake so one outlier cannot dominate
    dark_confidence: int = 60                  # absence of serving is not "nothing"
    dark_conf_part: float = 20.0  # labelled part behind the dark-winner confidence
    untracked_min_spend: float = 50_000.0      # money: untracked spend large enough to need a justification
    untracked_confidence: int = 55  # confidence of a justify-or-kill card: a prompt to explain the spend, not a measurement
    untracked_conf_part: float = 15.0  # labelled part behind that confidence
    # ---- probes ----
    probe_weekly_cap: float = 500.0            # money per week; a parameter, not a law: re-derive per account
    probe_weeks: int = 2                       # stake = weekly cap x weeks
    probe_max_per_plan: int = 2                # richest candidates first
    probe_min_live_days: int = 7               # entity had time to serve and still did not reach the floor
    probe_confidence: int = 40                 # capped downside, unproven upside
    probe_conf_part: float = 15.0  # labelled part behind the probe confidence
    # ---- contested days ----
    partition_max_ratio: float = 0.3           # below this share of contested days it is a schedule, not a collision
    partition_min_entities: int = 2            # a collision needs at least two entities


@dataclass
class SpineContext:
    """Everything the spine reads besides the cards.  Every field may be empty.

    * ``telemetry``: ``{entity_id: BudgetTelemetry}`` for the entities in the
      plan's scope (live days, spend, impressions, average budget, ROAS,
      recent-half ROAS, ad type).  Sweeps and detectors only ever mint cards
      for entities in this map, so scoping it scopes them.
    * ``book_max_live_days``: maximum live days over the *whole* book, taken
      before any scoping; cold start reads the book's age, not the
      selection's.  ``None`` falls back to the scoped telemetry.
    * ``price_index``: ``{segment: (current, prior)}`` effective-discount
      index in percentage points for the current and prior window.
    * ``open_entities``: entities with an applied action still inside its
      monitoring window (see ``open_entities``).
    * ``bid_down_entities``: entities with a recently applied bid cut.
    * ``owned_entities``: entities an operator has acted on recently.
    * ``automation``: ``{entity_id: {lever: owner_name}}`` where lever is a
      card type (``"bid"``, ``"keyword"``, ``"budget"``).
    * ``spend_recent``: ``{entity_id: spend over the last
      cfg.freeze_recent_days}``; without it the unowned-ramp freeze is inert.
    * ``untracked_ad_types``: ad types whose revenue the platform does not
      attribute (``PlatformProfile.no_attribution_types()``).  Their ROAS
      is "cannot judge", never zero, so the ROAS sweeps skip them.
    * ``allowed_levers``: ``ad_type -> levers`` (pass
      ``PlatformProfile.allowed_levers``); a sweep never mints a card whose
      lever the entity's ad type lacks.  ``None`` allows every lever.

    The spine writes its findings back into ``quarantined``,
    ``budget_distrust``, ``price_events`` and ``cold_start``.
    """

    days: int = 30
    telemetry: Mapping[str, BudgetTelemetry] = field(default_factory=dict)
    book_max_live_days: int | None = None
    price_index: Mapping[str, tuple[float | None, float | None]] = field(default_factory=dict)
    open_entities: frozenset[str] = frozenset()
    bid_down_entities: frozenset[str] = frozenset()
    owned_entities: frozenset[str] = frozenset()
    automation: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    spend_recent: Mapping[str, float] = field(default_factory=dict)
    untracked_ad_types: frozenset[str] = frozenset()
    allowed_levers: Callable[[str | None], Iterable[str]] | None = None
    economics: Economics = field(default_factory=Economics)
    cfg: VerdictConfig = field(default_factory=VerdictConfig)
    # ---- written by the spine ----
    quarantined: frozenset[str] = frozenset()
    budget_distrust: frozenset[str] = frozenset()
    price_events: dict[str, float] = field(default_factory=dict)
    cold_start: bool = False

    @property
    def be(self) -> float:
        """Effective breakeven ROAS."""
        return self.economics.be

    def money(self, reference_amount: float) -> float:
        """Scale a reference money threshold into account currency."""
        return self.economics.money(reference_amount)

    def scoped(self, entity_ids: Iterable[str] | None) -> "SpineContext":
        """Copy of the context with telemetry limited to ``entity_ids``.

        The whole-book age is captured first so cold start still reads the
        book.  An empty or ``None`` scope keeps the full map.
        """
        book = self.book_max_live_days
        if not book:
            book = max((t.n_days for t in self.telemetry.values()), default=0) or None
        ids = set(entity_ids or ())
        tel = {k: v for k, v in self.telemetry.items() if k in ids} if ids else dict(self.telemetry)
        return replace(self, telemetry=tel, book_max_live_days=book)


@dataclass
class SpineResult:
    """What ``run_spine`` returns: the full card list, per-gate fire counts and the state tally."""

    cards: list[ActionCard]
    telemetry: dict[str, int]
    verdict_states: dict[str, int]
    context: SpineContext

    def zero_fire_gates(self) -> list[str]:
        """Gates that never fired this run: each is either unneeded or broken, so worth an audit."""
        return sorted(k for k, v in self.telemetry.items() if v == 0)


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _live(card: ActionCard) -> bool:
    return card.status in ACTIONABLE_STATUSES


def demote(card: ActionCard, lane: str, why: str) -> bool:
    """Claim a card for a non-ACT lane.  First claim wins.

    If the card already has a verdict nothing changes (this is what makes
    stage order load-bearing).  Otherwise a ``pending`` / ``tension`` card
    becomes ``vetoed`` with ``veto_reason = "<lane>: <why>"``, and the card
    gets ``verdict = {state, why, lane}`` where ``state`` is the lane up to
    its first ``(``.  Returns True when the claim was made.
    """
    if card.verdict:
        return False
    if _live(card):
        card.status = "vetoed"
        card.veto_reason = f"{lane}: {why}"
    card.verdict = {"state": lane.split("(")[0], "why": why, "lane": lane}
    return True


def reversibility_class(card: ActionCard, cfg: VerdictConfig | None = None) -> str:
    """Classify how costly a card is to undo.

    Reads the explicit ``card.extras["reversibility"]`` when present (one of
    ``restore``, ``freeze``, ``watch``, ``experiment``, ``bid``,
    ``budget_up``, ``cut``).  Otherwise, first match wins: a title containing
    one of ``cfg.restore_title_markers`` -> ``restore``; type ``watch``;
    type ``bid``; budget raise -> ``budget_up``; any ``down`` move ->
    ``cut``; type ``experiment``; else ``other``.

    The title fallback keys on headline text for cards built elsewhere;
    emitters should set the explicit attribute instead.
    """
    cfg = cfg or VerdictConfig()
    explicit = (card.extras or {}).get("reversibility")
    if explicit:
        e = str(explicit).lower()
        return "restore" if e == "freeze" else (e if e in REVERSIBILITY_CLASSES else "other")
    title = card.title or ""
    if any(m and m in title for m in cfg.restore_title_markers):
        return "restore"
    if card.type == "watch":
        return "watch"
    if card.type == "bid":
        return "bid"
    if card.type == "budget" and card.direction == "up":
        return "budget_up"
    if card.direction == "down":
        return "cut"
    if card.type == "experiment":
        return "experiment"
    return "other"


def risk_bar(card: ActionCard, cfg: VerdictConfig | None = None) -> int:
    """Confidence a card must reach to ship, before any cold-start bump.

    The bar scales with irreversibility: restore / freeze 30, watch 25,
    bid 45, budget raise 55, any cut 65, otherwise 50.

    A probe is minted at confidence 40, so under the default bar of 50 it
    could never ship.  Probes have a capped downside by construction, so
    experiments get their own bar (``bar_experiment``, default 30, which
    also clears under the cold-start bump).
    """
    cfg = cfg or VerdictConfig()
    return {
        "restore": cfg.bar_restore, "watch": cfg.bar_watch, "experiment": cfg.bar_experiment,
        "bid": cfg.bar_bid, "budget_up": cfg.bar_budget_up, "cut": cfg.bar_cut,
    }.get(reversibility_class(card, cfg), cfg.bar_default)


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:,.2f}"


# --------------------------------------------------------------------------
# The gate stack
# --------------------------------------------------------------------------

def stage_integrity(cards: Sequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Stage 0: refuse to judge contaminated reads.

    a. *Phantom cohort.*  When the window is at least
       ``quarantine_min_window_days`` long, entities with spend that were
       alive exactly ``quarantine_live_days`` are bucketed by ad type.  A
       bucket of ``quarantine_min_cohort`` or more is an ingestion artefact,
       not a set of real one-day campaigns; all its members are quarantined.
    b. *Placeholder budget.*  A configured daily budget above zero and below
       ``money(placeholder_budget)`` is not a real cap.

    A card on a quarantined entity gets ``OBSERVE(quarantine)``; a *budget*
    card on a placeholder-budget entity gets ``OBSERVE(bad-budget-field)``.
    This stage looks at every card: a card that is no longer pending still
    receives the verdict but keeps its status.

    Returns ``{"quarantine": n, "bud_distrust": n}`` and stores the two
    entity sets on the context.
    """
    cfg = ctx.cfg
    buckets: dict[str, list[str]] = {}
    if int(ctx.days or 30) >= cfg.quarantine_min_window_days:
        for eid, t in ctx.telemetry.items():
            if t.n_days == cfg.quarantine_live_days and (t.spend or 0) > 0:
                buckets.setdefault(t.ad_type or "?", []).append(eid)
    ctx.quarantined = frozenset(e for ids in buckets.values() if len(ids) >= cfg.quarantine_min_cohort for e in ids)
    floor = ctx.money(cfg.placeholder_budget)
    ctx.budget_distrust = frozenset(
        eid for eid, t in ctx.telemetry.items() if t.avg_budget and t.avg_budget < floor)
    out = {"quarantine": 0, "bud_distrust": 0}
    for c in cards:
        if c.entity_id in ctx.quarantined:
            out["quarantine"] += demote(c, "OBSERVE(quarantine)",
                                        "entity belongs to a one-live-day cohort that looks like an ingestion "
                                        "artefact; the read is contaminated, not thin")
        elif c.entity_id in ctx.budget_distrust and c.type == "budget":
            out["bud_distrust"] += demote(c, "OBSERVE(bad-budget-field)",
                                          f"configured daily budget is below {floor:,.0f}, a placeholder; "
                                          "set a real budget before sizing a budget move")
    return out


def stage_lifecycle(cards: Sequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Launch grace with an expiry: no cuts on an entity younger than ``launch_grace_days``.

    A live ``down`` card whose entity has ``0 < live_days < launch_grace_days``
    gets ``HOLD(launch-grace)``.  The reason states when the grace expires and
    that the cut then ships with no further grace.  Raises are never held.
    Returns ``{"launch_grace": n}``.
    """
    n = 0
    g = ctx.cfg.launch_grace_days
    for c in cards:
        if not _live(c) or c.direction != "down":
            continue
        t = ctx.telemetry.get(c.entity_id) if c.entity_id else None
        if t is not None and 0 < t.n_days < g:
            n += demote(c, "HOLD(launch-grace)",
                        f"{t.n_days} live day(s); grace EXPIRES at {g} live days, after which this cut ships "
                        "with no further grace")
    return {"launch_grace": n}


def stage_evidence(cards: Sequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Stage 1: the evidence floor.

    A live non-watch card whose entity has fewer than
    ``evidence_min_impressions`` window impressions gets
    ``OBSERVE(insufficient)``.  Cards without entity telemetry
    (account- or segment-level) carry their own evidence and are skipped;
    families in ``evidence_exempt_families`` are exempt.
    Returns ``{"insufficient": n}``.
    """
    n = 0
    cfg = ctx.cfg
    for c in cards:
        if not _live(c) or c.type == "watch":
            continue
        t = ctx.telemetry.get(c.entity_id) if c.entity_id else None
        if t is None or c.family in cfg.evidence_exempt_families:
            continue
        if (t.impressions or 0) < cfg.evidence_min_impressions:
            n += demote(c, "OBSERVE(insufficient)",
                        f"{t.impressions or 0:,.0f} impressions in the window, below the "
                        f"{cfg.evidence_min_impressions:,.0f} floor")
    return {"insufficient": n}


def stage_identity(cards: Sequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Stage 2: hold media verdicts while the price moved.

    For each segment with both a current and a prior discount index,
    ``swing = current - prior``; ``|swing| >= price_swing_pp`` marks a price
    event (stored on ``ctx.price_events``).  A live card in such a segment
    whose type is in ``price_hold_types`` gets ``HOLD(price-event)``, because
    efficiency moved for a reason media did not cause.  Families in
    ``price_exempt_families`` are spared.  Returns ``{"price_event": n}``.
    """
    cfg = ctx.cfg
    hot: dict[str, float] = {}
    for seg, pair in (ctx.price_index or {}).items():
        cur, pri = (pair[0], pair[1]) if pair else (None, None)
        if cur is not None and pri is not None and abs(cur - pri) >= cfg.price_swing_pp:
            hot[seg] = cur - pri
    ctx.price_events = hot
    n = 0
    for c in cards:
        if (_live(c) and c.segment in hot and c.type in cfg.price_hold_types
                and c.family not in cfg.price_exempt_families):
            n += demote(c, "HOLD(price-event)",
                        f"segment discount moved {hot[c.segment]:+.1f} pp between windows; "
                        "re-read once price is stable")
    return {"price_event": n}


def stage_action_lock(cards: Sequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Stage 3: one open action per entity.

    A live card whose entity is in ``ctx.open_entities`` gets
    ``HOLD(monitoring-window)``: a second move inside ``lock_days`` would make
    both unreadable.  Returns ``{"action_lock": n}``.
    """
    n = 0
    for c in cards:
        if _live(c) and c.entity_id and c.entity_id in ctx.open_entities:
            n += demote(c, "HOLD(monitoring-window)",
                        f"an applied action on this entity is inside its {ctx.cfg.lock_days}-day monitoring window")
    return {"action_lock": n}


def stage_automation(cards: Sequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Stage 3b: do not fight an automation that owns the same lever.

    Matched by lever: a ``bid`` or ``keyword`` card is blocked by an owner of
    the entity's ``bid`` or ``keyword`` lever, a ``budget`` card by an owner
    of its ``budget`` lever, and no other card type is ever blocked.  So a
    budget move on an entity whose bids are automated still ships.  A blocked
    card gets ``ESCALATE(automation-owned)`` naming the owner and the lever:
    it is a hand-off, not a silent veto.  Inert without automation data.
    Returns ``{"automation_conflict": n}``.
    """
    n = 0
    if not ctx.automation:
        return {"automation_conflict": 0}
    for c in cards:
        if not _live(c) or c.verdict or not c.entity_id:
            continue
        owners = ctx.automation.get(c.entity_id) or {}
        if c.type in ("bid", "keyword"):
            owner = owners.get(c.type) or owners.get("keyword") or owners.get("bid")
        elif c.type == "budget":
            owner = owners.get("budget")
        else:
            owner = None
        if owner:
            n += demote(c, "ESCALATE(automation-owned)",
                        f"automation '{owner}' owns the {c.type} lever on this entity; pause or re-scope it "
                        "first, otherwise it will overwrite this change")
    return {"automation_conflict": n}


def stage_risk_bars(cards: Sequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Risk bars: the cost of being wrong sets the confidence required.

    ``bar = risk_bar(card) + cold`` where ``cold = cold_start_bump`` when the
    book's maximum live days (``ctx.book_max_live_days``, else the maximum
    over scoped telemetry) is non-zero and below ``cold_start_max_days``.
    A live card with ``confidence < bar`` gets ``OBSERVE(below-bar)``.
    Returns ``{"below_bar": n}`` and sets ``ctx.cold_start``.
    """
    cfg = ctx.cfg
    ref = ctx.book_max_live_days or max((t.n_days for t in ctx.telemetry.values()), default=0)
    cold = cfg.cold_start_bump if ref and ref < cfg.cold_start_max_days else 0
    ctx.cold_start = bool(cold)
    n = 0
    for c in cards:
        if not _live(c) or c.verdict:
            continue
        bar = risk_bar(c, cfg) + cold
        conf = c.confidence or 0
        if conf < bar:
            note = f" (includes +{cold} cold-start bump)" if cold else ""
            n += demote(c, "OBSERVE(below-bar)",
                        f"confidence {conf} is under the {bar} bar for a {reversibility_class(c, cfg)} action{note}")
    return {"below_bar": n}


def stage_terminals(cards: Sequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Terminals: type every card that no gate claimed.

    * live card: it is *fillable* when it names a subject (entity, keywords
      or segment), has ``targets``, has ``evidence`` and has a confidence.
      Fillable -> ``ACT`` (``why`` = the card's trigger); otherwise
      ``OBSERVE(unfillable)``.
    * ``superseded`` -> ``NO-ACTION(conflict)``; ``vetoed`` upstream ->
      ``NO-ACTION(gate)``; anything else (``applied``, ``skipped``,
      ``merged`` ...) -> ``NO-ACTION(<status>)``.

    Returns ``{"unfillable": n}``.
    """
    n = 0
    for c in cards:
        if c.verdict:
            continue
        if _live(c):
            filled = bool((c.entity_id or c.keywords or c.segment) and c.targets and c.evidence
                          and c.confidence is not None)
            if filled:
                c.verdict = {"state": ACT, "why": c.trigger or "", "lane": ACT}
            else:
                n += demote(c, "OBSERVE(unfillable)", "card is missing a subject, targets, evidence or confidence")
        elif c.status == "superseded":
            c.verdict = {"state": NO_ACTION, "why": c.veto_reason or "conflict", "lane": "NO-ACTION(conflict)"}
        elif c.status == "vetoed":
            c.verdict = {"state": NO_ACTION, "why": c.veto_reason or "", "lane": "NO-ACTION(gate)"}
        else:
            c.verdict = {"state": NO_ACTION, "why": c.status, "lane": f"NO-ACTION({c.status})"}
    return {"unfillable": n}


Stage = Callable[[Sequence[ActionCard], SpineContext], dict[str, int]]

#: The literal walk order.  Reordering it changes outcomes; that is the point.
STAGE_ORDER: tuple[tuple[str, Stage], ...] = (
    ("integrity", stage_integrity),
    ("lifecycle", stage_lifecycle),
    ("evidence", stage_evidence),
    ("identity", stage_identity),
    ("action_lock", stage_action_lock),
    ("automation", stage_automation),
    ("risk_bars", stage_risk_bars),
    ("terminals", stage_terminals),
)

_TELEMETRY_KEYS = ("quarantine", "bud_distrust", "launch_grace", "insufficient", "price_event",
                   "action_lock", "automation_conflict", "below_bar", "unfillable")


def apply_spine(
    cards: Sequence[ActionCard],
    ctx: SpineContext,
    stages: Sequence[tuple[str, Stage]] | None = None,
) -> dict[str, int]:
    """Run the ordered gate stack over the cards and type every one.

    Decision: for each card, which lane it leaves in.  Stages run in
    ``STAGE_ORDER`` (or the ``stages`` given, for audits of the order) and
    the first stage to claim a card wins.  Cards are mutated in place.

    Returns the per-gate fire counts
    ``{quarantine, bud_distrust, launch_grace, insufficient, price_event,
    action_lock, automation_conflict, below_bar, unfillable}``.  A gate that
    never fires is either unneeded or broken; surface the zeros.
    """
    out = {k: 0 for k in _TELEMETRY_KEYS}
    for _name, stage in (stages or STAGE_ORDER):
        for k, v in stage(cards, ctx).items():
            out[k] = out.get(k, 0) + int(v)
    return out


# --------------------------------------------------------------------------
# Pre-spine shaping
# --------------------------------------------------------------------------

def _mint(ctx: SpineContext, *, id: str, family: str, type: str, direction: str, title: str,
          entity_id: str, trigger: str, targets: str, evidence: dict[str, Any], stake: float,
          confidence: int, part: tuple[str, float], change: Change | None = None,
          reversibility: str | None = None, ladder: str | None = None) -> ActionCard:
    card = ActionCard(
        id=id, family=family, type=type, title=title, entity_id=entity_id, direction=direction,
        change=change, trigger=trigger, targets=targets, evidence=evidence, stake=round(float(stake), 0),
        status="pending", rule_profile="verdict", confidence=confidence, conf_parts=[part], ladder=ladder,
    )
    if reversibility:
        card.extras["reversibility"] = reversibility
    card.extras["minted_by"] = "verdict"
    return card


def _sweepable(ctx: SpineContext, t: BudgetTelemetry, lever: str) -> bool:
    """May a ROAS sweep mint a ``lever`` card on this entity?

    No when the entity's ad type has no attributed sales (its ROAS is
    unknown, not zero) or when the ad type does not have the lever.
    """
    ad_type = (t.ad_type or "").strip().lower()
    if ad_type and ad_type in {a.strip().lower() for a in ctx.untracked_ad_types}:
        return False
    if ctx.allowed_levers is not None and lever not in set(ctx.allowed_levers(t.ad_type)):
        return False
    return True


def is_act(card: ActionCard) -> bool:
    """True when the card is live and the spine typed it ``ACT`` (or has not typed it yet)."""
    return _live(card) and (not card.verdict or card.verdict.get("state") == ACT)


def graduate(cards: MutableSequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Reshape cards toward the cheapest reversible lever, then sweep for what the families missed.

    Runs before ``apply_spine``; minted cards are appended to ``cards`` and
    walk the spine like any other.  ``be`` is ``ctx.be``.

    1. **Bid-down before pause.**  A live ``efficiency-cut`` keyword ``down``
       card on an entity with no recent applied bid cut is retyped in place
       to a bid card: ``change.pct = ladder_bid_step_pct`` (-25),
       ``ladder = "bid-down-first"``, re-read in ``ladder_reread_days``; the
       original call ships if the re-read still fails.  If the plan already
       holds a live bid cut on that entity, the keyword cut is ``superseded``
       instead (it queues behind the bid card).  Keyword grain only; budget
       cuts are never retyped.
    2. **Staircase.**  A live budget raise above ``staircase_step_pct`` (25)
       is trimmed to it: ``pct_requested`` keeps the ask and
       ``to = round(frm * 1.25, 2)`` when ``frm`` is known.
    Sweeps 3 and 4 skip entities whose ad type has no attributed sales or
    lacks the lever (see ``SpineContext.allowed_levers``).

    3. **Cumulative-loss sweep.**  For an entity with no card at all,
       ``roas < be`` and spend > 0:
       ``loss = spend * (1 - min(roas / be, 1))``; if
       ``loss >= bleeder_book_share * book_spend`` mint a -25% bid card with
       ``stake = loss``.
    4. **Unowned-ramp freeze.**  For an entity with no card, ``roas < be``,
       ``live_days >= freeze_min_live_days``, a recent-spend read ``s`` over
       ``r = freeze_recent_days`` with ``s / r > freeze_ramp_mult * (spend /
       live_days)``, and nobody on record as owning it: mint a freeze-budget
       card (``change = {pct: 0, frm: budget, to: budget}``,
       ``stake = max(s - spend / live_days * r, 0)``).

    Returns ``{bid_ladder, staircase, bleeder, unowned_freeze}`` counts.
    """
    cfg = ctx.cfg
    be = ctx.be
    out = {"bid_ladder": 0, "staircase": 0, "bleeder": 0, "unowned_freeze": 0}
    plan_bids = {c.entity_id for c in cards
                 if _live(c) and c.type == "bid" and c.direction == "down" and c.entity_id}
    covered: set[str] = set()
    step = cfg.ladder_bid_step_pct
    for c in list(cards):
        if c.entity_id:
            covered.add(c.entity_id)
        if not _live(c):
            continue
        if (c.family == "efficiency-cut" and c.direction == "down" and c.type == "keyword"
                and c.entity_id not in ctx.bid_down_entities):
            if c.entity_id and c.entity_id in plan_bids:
                c.status = "superseded"
                c.veto_reason = "bid-down first: a bid cut on this entity is already in the plan; this cut queues behind it"
            else:
                original = c.targets
                c.type = "bid"
                c.change = Change(pct=step)
                c.title = "Decrease Bid (first response)"
                c.ladder = "bid-down-first"
                c.extras["reversibility"] = "bid"
                c.targets = (f"{step:+.0f}% bid, re-read in {cfg.ladder_reread_days} days; if the re-read still "
                             f"fails, the original call ships: {original}")
                if c.entity_id:
                    plan_bids.add(c.entity_id)
                out["bid_ladder"] += 1
            continue
        if (c.type == "budget" and c.direction == "up" and c.change is not None
                and (c.change.pct or 0) > cfg.staircase_step_pct):
            c.change.pct_requested = c.change.pct
            c.change.pct = cfg.staircase_step_pct
            if c.change.frm:
                c.change.to = round(c.change.frm * (1 + cfg.staircase_step_pct / 100.0), 2)
            c.targets = (f"+{cfg.staircase_step_pct:.0f}% now, re-read marginal ROAS in "
                         f"{cfg.staircase_reread_days} days; take the next step only if it holds above target")
            out["staircase"] += 1

    book = sum((t.spend or 0.0) for t in ctx.telemetry.values())
    for eid, t in ctx.telemetry.items():
        if t.roas is None or t.roas >= be or (t.spend or 0) <= 0 or eid in covered or eid in plan_bids:
            continue
        if not _sweepable(ctx, t, "bid"):
            continue
        loss = t.spend * (1 - min(t.roas / be, 1.0))
        if book > 0 and loss >= cfg.bleeder_book_share * book:
            cards.append(_mint(
                ctx, id=f"bleed-{eid}", family="efficiency-cut", type="bid", direction="down",
                title="Decrease Bid (cumulative-loss stop)", entity_id=eid, change=Change(pct=step),
                trigger=(f"ROAS {_fmt(t.roas)} under the {_fmt(be)} floor has cost {loss:,.0f} over the window, "
                         f"{loss / book:.1%} of book spend"),
                targets=f"{step:+.0f}% bid, re-read in {cfg.ladder_reread_days} days",
                evidence={"roas": t.roas, "spend": t.spend, "cum_loss": round(loss, 2)},
                stake=loss, confidence=cfg.bleeder_confidence,
                part=("cumulative loss vs book", cfg.bleeder_conf_part), ladder="bid-down-first",
                reversibility="bid"))
            out["bleeder"] += 1

    r = cfg.freeze_recent_days
    for eid, t in ctx.telemetry.items():
        s = ctx.spend_recent.get(eid) if ctx.spend_recent else None
        if (s is None or t.roas is None or t.roas >= be or t.n_days < cfg.freeze_min_live_days
                or (t.spend or 0) <= 0 or eid in ctx.owned_entities or eid in covered
                or not _sweepable(ctx, t, "budget")):
            continue
        daily = t.spend / t.n_days
        if s / r > cfg.freeze_ramp_mult * daily:
            cards.append(_mint(
                ctx, id=f"freeze-{eid}", family="efficiency-cut", type="budget", direction="down",
                title="Freeze Budget (unowned ramp)", entity_id=eid,
                change=Change(pct=0.0, frm=t.avg_budget, to=t.avg_budget),
                trigger=(f"spend is ramping ({s / r:,.0f}/day recently vs {daily:,.0f}/day over the window) at ROAS "
                         f"{_fmt(t.roas)} under the {_fmt(be)} floor, and no one is on record as owning the ramp"),
                targets="hold the daily budget at its current value until someone owns the ramp or it clears the floor",
                evidence={"roas": t.roas, "spend": t.spend, "spend_recent": s},
                stake=max(s - daily * r, 0.0), confidence=cfg.freeze_confidence,
                part=("unowned ramp at sub-floor ROAS", cfg.freeze_conf_part), reversibility="freeze"))
            out["unowned_freeze"] += 1
    return out


def detect_families(cards: MutableSequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Two detectors for things the rule families cannot see; both mint ``watch`` cards.

    For each entity in ``ctx.telemetry`` that has no card at all:

    1. **Winner went dark**: ``roas >= dark_roas_mult * be``, no recent-half
       ROAS (it stopped serving), ``live_days >= dark_min_live_days`` and
       ``spend >= money(dark_min_spend)``.  Stake is a week of the proven
       profit rate: ``spend * (min(roas, dark_roas_cap) - 1) / live_days * 7``.
       Absence is not "nothing".
    2. **Justify or kill**: the entity's ad type is in
       ``ctx.untracked_ad_types`` and ``spend >= money(untracked_min_spend)``.
       Stake is the spend.  The card forbids judging the spend on ROAS:
       untracked is not zero.

    The two checks are independent; one entity can receive both cards.
    Returns ``{winner_dark, justify_or_kill}`` counts.
    """
    cfg = ctx.cfg
    be = ctx.be
    out = {"winner_dark": 0, "justify_or_kill": 0}
    have = {c.entity_id for c in cards if c.entity_id}
    untracked = {a.strip().lower() for a in ctx.untracked_ad_types}
    for eid, t in ctx.telemetry.items():
        if eid in have:
            continue
        sp = t.spend or 0.0
        if (t.roas is not None and t.roas >= cfg.dark_roas_mult * be and t.roas_recent is None
                and t.n_days >= cfg.dark_min_live_days and sp >= ctx.money(cfg.dark_min_spend)):
            cards.append(_mint(
                ctx, id=f"dark-{eid}", family="efficiency-scale", type="watch", direction="none",
                title="Winner Went Dark - Restore or Explain", entity_id=eid,
                trigger=(f"ROAS {_fmt(t.roas)} over {t.n_days} live days, then zero serving in the recent half "
                         "of the window"),
                targets="restore serving or record why it was stopped; a proven winner going quiet is a decision, "
                        "not a non-event",
                evidence={"roas": t.roas, "spend": sp, "live_days": t.n_days},
                stake=sp * (min(t.roas, cfg.dark_roas_cap) - 1) / max(t.n_days, 1) * 7,
                confidence=cfg.dark_confidence, part=("proven ROAS, zero recent serving", cfg.dark_conf_part),
                reversibility="restore"))
            out["winner_dark"] += 1
        if (t.ad_type or "").strip().lower() in untracked and untracked and sp >= ctx.money(cfg.untracked_min_spend):
            cards.append(_mint(
                ctx, id=f"untracked-{eid}", family="sov-gap", type="watch", direction="none",
                title="Justify or Kill (untracked spend)", entity_id=eid,
                trigger=f"{sp:,.0f} spent on an ad type whose revenue the platform does not attribute",
                targets="state the reach or share-of-voice goal this spend serves and how it will be read, or stop "
                        "it; do NOT judge it on ROAS: untracked is not zero",
                evidence={"spend": sp, "ad_type": t.ad_type},
                stake=sp, confidence=cfg.untracked_confidence,
                part=("untracked spend at stake", cfg.untracked_conf_part), reversibility="watch"))
            out["justify_or_kill"] += 1
    return out


def probes(cards: MutableSequence[ActionCard], ctx: SpineContext) -> dict[str, int]:
    """Mint bounded exploration probes for entities stuck under the evidence floor.

    Candidates: entities with no card, ``0 < impressions <
    evidence_min_impressions``, spend > 0 and ``live_days >=
    probe_min_live_days``.  The richest ``probe_max_per_plan`` by spend each
    get an ``experiment`` card: cap about ``money(probe_weekly_cap)`` per
    week for ``probe_weeks`` weeks to cross the impression floor;
    ``stake = cap * weeks``; confidence ``probe_confidence``.

    Returns ``{"probes": n}``.
    """
    cfg = ctx.cfg
    have = {c.entity_id for c in cards if c.entity_id}
    cand = [(eid, t) for eid, t in ctx.telemetry.items()
            if eid not in have and 0 < (t.impressions or 0) < cfg.evidence_min_impressions
            and (t.spend or 0) > 0 and t.n_days >= cfg.probe_min_live_days]
    cand.sort(key=lambda p: -(p[1].spend or 0.0))
    cap = ctx.money(cfg.probe_weekly_cap)
    n = 0
    for eid, t in cand[: cfg.probe_max_per_plan]:
        cards.append(_mint(
            ctx, id=f"probe-{eid}", family="experiment", type="experiment", direction="none",
            title="Exploration Probe (buy significance)", entity_id=eid,
            trigger=(f"{t.impressions:,.0f} impressions in {t.n_days} live days: too few to judge, "
                     "and waiting will not fix it"),
            targets=(f"cap about {cap:,.0f} per week for {cfg.probe_weeks} weeks to cross the "
                     f"{cfg.evidence_min_impressions:,.0f}-impression floor, then judge"),
            evidence={"impressions": t.impressions, "spend": t.spend, "live_days": t.n_days},
            stake=cap * cfg.probe_weeks, confidence=cfg.probe_confidence,
            part=("bounded probe, capped downside", cfg.probe_conf_part), reversibility="experiment"))
        n += 1
    return {"probes": n}


# --------------------------------------------------------------------------
# Contested days
# --------------------------------------------------------------------------

def contested_ratio(days_by_entity: Mapping[str, Iterable[Any]]) -> float:
    """Share of active days on which at least two entities were both spending.

    ``days_by_entity`` maps each entity to the days it spent on one targeting
    unit in one geo.  With fewer than two non-empty day sets, or an empty
    union, the ratio is 0.0.  Otherwise::

        contested = days present in at least two sets
        ratio = len(contested) / len(union of all sets)

    Returns a float in [0, 1].
    """
    sets = [set(v) for v in days_by_entity.values() if v]
    if len(sets) < 2:
        return 0.0
    union: set[Any] = set().union(*sets)
    if not union:
        return 0.0
    seen: dict[Any, int] = {}
    for s in sets:
        for d in s:
            seen[d] = seen.get(d, 0) + 1
    return sum(1 for k in seen.values() if k >= 2) / len(union)


def contested_days(
    rows: Iterable[PerfRow],
    entity_ids: Iterable[str],
    targeting_unit: str,
    geo: str | None = None,
    window: Window | None = None,
) -> dict[str, set[date]]:
    """Days each entity spent on one targeting unit (in one geo), from performance rows.

    A day counts when the entity has spend > 0 on that targeting unit (case-
    and space-insensitive match) in that geo (any geo when ``geo`` is None)
    inside ``window``.  Returns ``{entity_id: set(days)}`` ready for
    ``contested_ratio``.
    """
    ids = set(entity_ids)
    kw = targeting_unit.strip().lower()
    out: dict[str, set[date]] = {e: set() for e in ids}
    for r in rows:
        if r.entity_id not in ids or (r.spend or 0) <= 0:
            continue
        if (r.targeting_unit or "").strip().lower() != kw:
            continue
        if geo is not None and r.geo != geo:
            continue
        if window is not None and not window.contains(r.date):
            continue
        out[r.entity_id].add(r.date)
    return out


def partition_collisions(
    cards: Sequence[ActionCard],
    days_for: Callable[[ActionCard], Mapping[str, Iterable[Any]] | None],
    cfg: VerdictConfig | None = None,
) -> int:
    """Veto collision cards whose entities rarely spend on the same day.

    For each live ``collision`` card with keywords and at least two entity
    ids in ``evidence["entity_ids"]`` (or ``evidence["cids"]``, the key the
    collision family writes), ``days_for(card)`` supplies the
    per-entity day sets (return ``None`` to leave the card alone, e.g. when
    the data is unavailable).  The ratio is stored at
    ``evidence["contested_ratio"]`` (2 dp).  When it is below
    ``partition_max_ratio`` the card becomes ``vetoed`` with verdict
    ``NO-ACTION(partitioned)``: two entities that take turns on a term are a
    schedule, not a collision.

    Returns the number of cards partitioned.
    """
    cfg = cfg or VerdictConfig()
    n = 0
    for c in cards:
        if not _live(c) or c.family != "collision" or not c.keywords:
            continue
        ids = c.evidence.get("entity_ids") or c.evidence.get("cids") or []
        if len(ids) < cfg.partition_min_entities:
            continue
        try:
            days = days_for(c)
        except Exception:  # fail open: an unreadable schedule never blocks a card
            days = None
        if days is None:
            continue
        ratio = contested_ratio(days)
        c.evidence["contested_ratio"] = round(ratio, 2)
        if ratio < cfg.partition_max_ratio:
            why = (f"schedule, not collision: the entities overlap on only {ratio:.0%} of their active days "
                   f"(under {cfg.partition_max_ratio:.0%})")
            c.status = "vetoed"
            c.veto_reason = f"NO-ACTION(partitioned): {why}"
            c.verdict = {"state": NO_ACTION, "why": why, "lane": "NO-ACTION(partitioned)"}
            n += 1
    return n


# --------------------------------------------------------------------------
# Orchestration and context builders
# --------------------------------------------------------------------------

def verdict_states(cards: Iterable[ActionCard]) -> dict[str, int]:
    """Count cards per verdict state (cards without a verdict count under ``"untyped"``)."""
    out: dict[str, int] = {}
    for c in cards:
        s = (c.verdict or {}).get("state") or "untyped"
        out[s] = out.get(s, 0) + 1
    return out


def run_spine(
    cards: Iterable[ActionCard],
    ctx: SpineContext,
    *,
    days_for: Callable[[ActionCard], Mapping[str, Iterable[Any]] | None] | None = None,
    mint: bool = True,
) -> SpineResult:
    """Run the whole verdict pass in its load-bearing order.

    ``detect_families`` -> ``probes`` -> ``partition_collisions`` (only when
    ``days_for`` is given) -> ``graduate`` -> ``apply_spine``.  With
    ``mint=False`` the detectors, probes and sweeps are skipped and only
    in-place shaping and the gates run.

    Returns a ``SpineResult`` with the (possibly longer) card list, the merged
    fire counts and the tally of verdict states.
    """
    out_cards = list(cards)
    tel: dict[str, int] = {}
    if mint:
        tel.update(detect_families(out_cards, ctx))
        tel.update(probes(out_cards, ctx))
    tel["partitioned"] = partition_collisions(out_cards, days_for, ctx.cfg) if days_for else 0
    if mint:
        tel.update(graduate(out_cards, ctx))
    else:
        scratch = replace(ctx, telemetry={})
        tel.update(graduate(out_cards, scratch))
    tel.update(apply_spine(out_cards, ctx))
    return SpineResult(cards=out_cards, telemetry=tel, verdict_states=verdict_states(out_cards), context=ctx)


def open_entities(applied: Iterable[AppliedAction], today: date, lock_days: int = 7) -> frozenset[str]:
    """Entities with an applied action still inside its monitoring window.

    An entity is open when any applied action on it has
    ``applied_on >= today - lock_days``.
    """
    cut = today - timedelta(days=lock_days)
    return frozenset(a.entity_id for a in applied if a.entity_id and a.applied_on >= cut)


def recent_bid_downs(applied: Iterable[AppliedAction], today: date, days: int = 14) -> frozenset[str]:
    """Entities with a bid cut applied in the last ``days`` days.

    A bid cut is an applied action of kind ``"bid"`` with direction
    ``"down"``, or of kind ``"biddown"``.
    """
    cut = today - timedelta(days=days)
    return frozenset(
        a.entity_id for a in applied
        if a.entity_id and a.applied_on >= cut
        and (a.kind == "biddown" or (a.kind == "bid" and a.direction == "down")))


def owned_entities(applied: Iterable[AppliedAction]) -> frozenset[str]:
    """Entities an operator has acted on at all in the supplied history."""
    return frozenset(a.entity_id for a in applied if a.entity_id)
