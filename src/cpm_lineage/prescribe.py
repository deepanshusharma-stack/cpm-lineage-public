"""Final one-row prescription per campaign, and one-word calls per drill row.

:func:`prescribe` turns one campaign's finished diagnosis into exactly one
``(verb, target, why)`` row.  A stack of framework gates runs *before*
average ROAS is consulted, so that a reach placement is never ROAS-judged,
a halo-positive conquest campaign is never cut, a cannibalising own-brand
campaign is never scaled and an out-of-stock campaign is never funded.
Only then does the ROAS ladder speak.

It is meant to be the single final stage after the recommendation engine
(``cpm_lineage.recommend``), not a second opinion: it consumes that
engine's top actions and passes its hold / investigate verdicts through.

Cut sizing here is the "(1 - ROAS) slice": a sub-breakeven campaign is
trimmed to ``average daily spend x ROAS / breakeven``.  The action queue
uses a different policy (fixed step, then verify); the two surfaces are
deliberately distinct.

ROAS bars are multiples of the effective breakeven (``Economics.be``), the
scale bar is ``Economics.scale_bar``; the defaults reproduce 1.0 / 2.0.
Cohort percentiles are a per-keyword tool and are never applied to a
campaign's blended ROAS here.  Availability is on the 0..1 scale.

:func:`keyword_row_call`, :func:`product_row_call` and :func:`geo_row_call`
give the one-word call shown next to a drilldown row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .config import AvailabilityPolicy, Economics
from .liveness import canonical_status
from .schema import CanonicalStatus

__all__ = [
    "PrescribeConfig", "TopAction", "PrescribeKeyword", "PrescribeBudget", "PrescribeInput", "Prescription",
    "RoleMix", "role_mix", "campaign_cann_rate", "demand_limited", "top_actions_from", "budget_from_probe",
    "prescribe", "DrillCallConfig", "keyword_row_call", "product_row_call", "geo_row_call",
    "GROW", "SHRINK", "HOLD", "NONE",
]

GROW, SHRINK, HOLD, NONE = "grow", "shrink", "hold", "none"


# --------------------------------------------------------------------------- config

@dataclass(frozen=True)
class PrescribeConfig:
    """Thresholds of the prescription gate stack.  ROAS bars are breakeven multiples."""

    halo_direct_max_be: float = 2.0          # halo gate: direct ROAS at or below the scale bar ...
    halo_total_min_be: float = 2.0           # ... total (direct + indirect) ROAS at or above it ...
    halo_total_to_direct: float = 1.5        # ... and total at least 1.5 x direct
    halo_direct_floor: float = 0.01          # floor on direct ROAS in the ratio test (avoids a zero divisor)
    mixed_role_share: float = 0.65           # dominant role under this spend share: blended ROAS cannot judge the campaign
    demand_min_searches: float = 50.0        # max searches per day below this: budget cannot create searches
    pause_roas: float = 0.0                  # at or below: pause (zero or negative return)
    min_trim_pct: int = 1                    # a trim smaller than this many percent is reported as a hold, not as an action
    marginal_ceiling_be: float = 1.0         # marginal ROAS below this at the cap: at the curve ceiling
    marginal_full_step_be: float = 1.5       # marginal ROAS at or above: full step; uncapped winner may scale
    held_days_min: int = 3                   # platform-held on this many days: a top-up, not a free scale
    availability_drop: float = 0.05          # a 5-point availability drop qualifies every scale
    budget_round_ref: float = 100.0          # trim targets are rounded to this money step
    cut_tokens: tuple[str, ...] = ("cut", "negative", "pause")          # top-action verbs that are surgical cuts
    launch_tokens: tuple[str, ...] = ("launch",)                        # top-action verbs that launch a replacement
    passthrough_tokens: tuple[str, ...] = (                             # engine verdicts passed through verbatim
        "investigate tracking", "investigate stop", "hold the budget raise", "audit before spending",
        "pre-fund", "hold lever changes")
    passthrough_any_action: bool = False                                # False: only the leading action is the engine verdict
    availability_tokens: tuple[str, ...] = ("availability", "supply")   # engine verbs that blame availability
    burn_tokens: tuple[str, ...] = ("burn",)                            # engine headlines flagging an auto-targeted burn
    own_brand_role: str = "own_brand"        # role value that marks an own-brand term
    conquest_role: str = "competitor"        # role value that marks a competitor-conquest term
    brand_name_tokens: tuple[str, ...] = ("brand", "branded")           # name-token fallback when no keyword data exists
    conquest_name_tokens: tuple[str, ...] = (
        "comp", "conq", "conquest", "competitor", "competition", "competitive")  # name tokens that mark a conquest campaign when no keyword data exists
    name_delimiters: str = r"[\s_\-|/:.,]+"  # how an entity name is split into tokens
    removed_statuses: tuple[str, ...] = (CanonicalStatus.REMOVED.value,)  # -> launch a replacement
    stopped_statuses: tuple[str, ...] = (    # -> investigate the stop
        CanonicalStatus.ENDED.value, CanonicalStatus.OPERATOR_PAUSED.value)


_PC = PrescribeConfig()
_ECON = Economics()
_AVAIL = AvailabilityPolicy()


# --------------------------------------------------------------------------- inputs

@dataclass(frozen=True)
class TopAction:
    """One of the recommendation engine's top actions, reduced to what the gates read."""

    verb: str = ""
    targets: tuple[str, ...] = ()
    verb_class: str | None = None
    headline: str = ""
    self_competed: bool = False


@dataclass(frozen=True)
class PrescribeKeyword:
    """One targeting unit of the campaign (window totals)."""

    value: str
    role: str = "generic"
    spend: float = 0.0
    cann_rate: float | None = None           # share of its sales that would have come organically, 0..1
    searches_per_day: float | None = None
    is_placement: bool = False


@dataclass(frozen=True)
class PrescribeBudget:
    """Budget probe facts.  Every field may be unknown (``None``)."""

    daily: float | None = None               # current daily cap
    capped: bool = False                     # the caller's budget-bound decision
    ladder: str | None = None                # "bid-constrained" | "volume-ceiling" | "severe" | "stepwise" | None
    mroas: float | None = None               # marginal ROAS at the cap
    sat_spend: float | None = None           # decision-grade curve ceiling (daily spend)
    rec_range: tuple[float, ...] | None = None   # (low, high[, recommended]) daily budget
    util: float | None = None                # spend / cap
    position: float | None = None            # current mean position (omitted from text when unknown)
    held_days: int | None = None             # days the platform held delivery on budget
    anomaly: bool = False                    # placeholder / nonsensical cap
    rev_at_stake: float | None = None
    unserved_hours: float | None = None


@dataclass(frozen=True)
class PrescribeInput:
    """Everything :func:`prescribe` reads about one campaign.

    ``diagnosis_ok`` False means the upstream diagnosis failed.  ``roas`` is
    the overall direct ROAS, ``total_roas`` direct + indirect (``None`` when
    the platform has no indirect attribution: the halo gate is then off).
    ``reach_judged`` marks an ad type judged on reach rather than sales;
    ``auto_targeted`` an ad type with no operator-chosen targeting units
    (both come from the adapter's capability matrix).
    """

    diagnosis_ok: bool = True
    roas: float | None = None
    total_roas: float | None = None
    spend: float = 0.0
    days: int = 0
    status: Any = None
    status_map: Mapping[str, Any] | None = None
    reach_judged: bool = False
    auto_targeted: bool = False
    auto_burn: bool = False                  # the engine flagged an auto-targeted burn
    actions: tuple[TopAction, ...] = ()
    keywords: tuple[PrescribeKeyword, ...] = ()
    entity_name: str | None = None
    account_cann_rate: float | None = None
    availability: float | None = None
    availability_prior: float | None = None
    budget: PrescribeBudget | None = None
    confidence: str | None = None            # "high" | "med" | "low" (any case)
    state_verb: str | None = None            # state-mode verb when the campaign is too young for a ROAS
    clearing_p50: float | None = None        # top-slot clearing bid percentiles, for the bid text
    clearing_p75: float | None = None


@dataclass(frozen=True)
class Prescription:
    """The one-row prescription.

    ``code`` is machine-readable; ``gate`` names the gate or ladder rung
    that fired; ``direction`` is ``"grow" | "shrink" | "hold" | "none"`` and
    never contradicts the verb; ``caveats`` are context lines (mixed
    campaign, thin demand) that did not change the decision.
    """

    verb: str
    target: str
    why: str
    code: str
    gate: str
    direction: str = NONE
    caveats: tuple[str, ...] = ()

    def as_tuple(self) -> tuple[str, str, str]:
        return self.verb, self.target, self.why


@dataclass(frozen=True)
class RoleMix:
    """Dominant role of a campaign by spend."""

    dominant: str | None = None
    share: float | None = None               # dominant role's spend share, 0..1
    is_branded: bool = False
    is_conquest: bool = False
    mixed: bool = False
    source: str = "none"                     # "keywords" | "name" | "none"


# --------------------------------------------------------------------------- helpers

def _g(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _has(text: str | None, tokens: Iterable[str]) -> bool:
    t = (text or "").lower()
    return any(tok in t for tok in tokens)


def _n(x: float | None, nd: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def _money(x: float) -> str:
    return f"{x:,.0f}"


def top_actions_from(actions: Iterable[Any] | None) -> tuple[TopAction, ...]:
    """Coerce recommendation-engine actions (dataclasses or mappings with
    ``verb`` / ``targets`` / ``verb_class`` / ``headline`` and an optional
    ``self_competed`` flag, directly or under ``extras``) to
    :class:`TopAction`.  Returns a tuple, order preserved."""
    out = []
    for a in actions or ():
        if isinstance(a, TopAction):
            out.append(a)
            continue
        extras = _g(a, "extras") or {}
        out.append(TopAction(
            verb=str(_g(a, "verb", "") or ""), targets=tuple(_g(a, "targets", ()) or ()),
            verb_class=_g(a, "verb_class"), headline=str(_g(a, "headline", "") or ""),
            self_competed=bool(_g(a, "self_competed", False) or _g(extras, "self_competed", False))))
    return tuple(out)


def budget_from_probe(probe: Any, held_days: int | None = None) -> PrescribeBudget | None:
    """Build a :class:`PrescribeBudget` from a budget-probe record (any
    object or mapping with ``daily_budget, capped, ladder, mroas_cap,
    sat_spend, rec_range, util, anomaly, rev_at_stake, unserved_hours``).
    ``held_days`` = days the platform held delivery on budget, when known."""
    if probe is None:
        return None
    return PrescribeBudget(
        daily=_g(probe, "daily_budget", _g(probe, "daily")), capped=bool(_g(probe, "capped", False)),
        ladder=_g(probe, "ladder"), mroas=_g(probe, "mroas_cap", _g(probe, "mroas")),
        sat_spend=_g(probe, "sat_spend"), rec_range=_g(probe, "rec_range"), util=_g(probe, "util"),
        position=_g(probe, "position"), held_days=held_days if held_days is not None else _g(probe, "held_days"),
        anomaly=bool(_g(probe, "anomaly", False)), rev_at_stake=_g(probe, "rev_at_stake"),
        unserved_hours=_g(probe, "unserved_hours"))


def role_mix(keywords: Sequence[PrescribeKeyword], entity_name: str | None = None,
             cfg: PrescribeConfig | None = None) -> RoleMix:
    """Classify a campaign as own-brand, conquest or other.

    The dominant role is the keyword role with the most spend (placements
    excluded); the campaign is ``mixed`` when that role holds less than
    ``mixed_role_share`` (65%) of the spend.  With no keyword spend, fall
    back to delimiter-split tokens of the entity name
    (``brand_name_tokens`` / ``conquest_name_tokens``).

    Returns a :class:`RoleMix`.
    """
    cfg = cfg or _PC
    by: dict[str, float] = {}
    for k in keywords:
        if not k.is_placement and k.spend > 0:
            by[k.role] = by.get(k.role, 0.0) + k.spend
    if by:
        total = sum(by.values())
        dom = max(sorted(by), key=lambda r: by[r])
        share = by[dom] / total
        return RoleMix(dominant=dom, share=share, is_branded=dom == cfg.own_brand_role,
                       is_conquest=dom == cfg.conquest_role, mixed=share < cfg.mixed_role_share, source="keywords")
    toks = {t for t in re.split(cfg.name_delimiters, (entity_name or "").lower()) if t}
    if toks & set(cfg.brand_name_tokens):
        return RoleMix(dominant=cfg.own_brand_role, is_branded=True, source="name")
    if toks & set(cfg.conquest_name_tokens):
        return RoleMix(dominant=cfg.conquest_role, is_conquest=True, source="name")
    return RoleMix()


def campaign_cann_rate(keywords: Sequence[PrescribeKeyword], account_rate: float | None = None,
                       cfg: PrescribeConfig | None = None) -> float | None:
    """Cannibalisation rate to judge an own-brand campaign with.

    The spend-weighted mean of the campaign's own-brand keywords' rates when
    any are known; else the account-wide rate; else ``None``.
    """
    cfg = cfg or _PC
    ks = [k for k in keywords if k.role == cfg.own_brand_role and k.cann_rate is not None and k.spend > 0]
    w = sum(k.spend for k in ks)
    if w > 0:
        return sum(k.spend * k.cann_rate for k in ks) / w  # type: ignore[operator]
    return account_rate


def demand_limited(keywords: Sequence[PrescribeKeyword], cfg: PrescribeConfig | None = None) -> bool:
    """True when every search keyword's daily searches is known and the
    largest is below ``demand_min_searches`` (50): budget cannot create
    searches.  False when there are no keywords or any volume is unknown."""
    cfg = cfg or _PC
    ks = [k for k in keywords if not k.is_placement]
    if not ks or any(k.searches_per_day is None for k in ks):
        return False
    return max(k.searches_per_day for k in ks) < cfg.demand_min_searches  # type: ignore[type-var]


# --------------------------------------------------------------------------- prescription

def prescribe(
    inp: PrescribeInput,
    *,
    cfg: PrescribeConfig | None = None,
    economics: Economics | None = None,
    availability_policy: AvailabilityPolicy | None = None,
) -> Prescription:
    """Decide the single action for one campaign.

    Gate stack, first match returns (``be`` = effective breakeven, ``bar``
    = scale bar = 2 x ``be`` by default):

    ====  =======================================================  ==========================
    gate  condition                                                verb
    ====  =======================================================  ==========================
    0a    diagnosis missing                                        Diagnose unavailable
    0b    status removed                                           Launch replacement
    0c    status ended / paused                                    Investigate stop & reactivate
    A     ad type judged on reach                                  Reach: judge on reach, not ROAS
    B     ``roas <= bar`` and ``total >= bar`` and ``total >=      Keep (total-return halo)
          1.5 x max(roas, 0.01)`` and (conquest or top action
          is a cut)
    C     own-brand, cannibalisation rate known, ``roas >= bar``,  Hold (branded cannibalisation)
          ``roas x (1 - rate) < bar``
    C'    own-brand, rate unknown, ``roas >= bar``                 Hold (verify incrementality)
    1     top action is a cut / negative / pause with targets      Cut bleeding keywords
    1b    top action is a launch                                   Launch replacement
    2     availability in the block band                           Fix availability first
    3     auto-targeted ad type and a flagged burn                 Cap the auto-targeted ad
    4     leading engine verb is a hold / investigate verdict      passed through verbatim
    5     budget configuration anomaly                             Fix budget configuration
    6     ``roas`` unknown                                         state verb, else Insufficient data
    ====  =======================================================  ==========================

    An availability "caution" band, or a drop of ``availability_drop`` (5
    points) versus the prior window, does not return: it prepends a
    mandatory qualifier to every growth prescription below.

    ROAS ladder: ``roas <= 0`` pause; ``roas < be`` never raise, trim the
    daily budget to ``avg_daily x roas / be`` (the cut is the ``1 - roas /
    be`` slice, not an arbitrary percentage; verb variants for a
    bid-constrained ladder or an availability verdict); ``be <= roas < bar``
    hold and monitor; ``roas >= bar``: (1) marginal ROAS below ``be`` or the
    curve ceiling at or under the cap -> hold; (2) demand-limited -> add
    keywords; (3) bid-constrained -> consolidate if a top action is
    self-competed, else raise bids, not budget; (4) volume ceiling -> add
    keywords; (5) capped -> top up (platform-held on ``held_days_min`` days)
    or scale, by the low end of the recommended range when marginal ROAS is
    under 1.5 x ``be`` or unknown, or confidence is low, else the high end,
    labelled "curve-fit" only when a curve ceiling exists; (6) uncapped
    with marginal ROAS >= 1.5 x ``be`` -> scale carefully; (7) otherwise
    maintain: an uncapped winner is not budget-bound.

    Returns a :class:`Prescription`; ``.as_tuple()`` is ``(verb, target, why)``.
    """
    cfg, econ, pol = cfg or _PC, economics or _ECON, availability_policy or _AVAIL
    be, bar = econ.be, econ.scale_bar
    roas, total = inp.roas, inp.total_roas
    b = inp.budget or PrescribeBudget()
    acts = top_actions_from(inp.actions)
    top = acts[0] if acts else None
    mix = role_mix(inp.keywords, inp.entity_name, cfg)
    cann = campaign_cann_rate(inp.keywords, inp.account_cann_rate, cfg)
    thin_demand = demand_limited(inp.keywords, cfg)
    avg_daily = inp.spend / max(inp.days, 1)

    caveats: list[str] = []
    if mix.mixed and mix.share is not None:
        caveats.append(f"Mixed campaign: the dominant role ({mix.dominant}) holds only {mix.share:.0%} of spend, "
                       "so blended ROAS cannot judge it; read per keyword.")
    if thin_demand:
        caveats.append(f"Demand ceiling: no keyword exceeds {cfg.demand_min_searches:.0f} searches a day.")

    def out(verb: str, target: str, why: str, code: str, gate: str, direction: str) -> Prescription:
        return Prescription(verb=verb, target=target, why=why, code=code, gate=gate, direction=direction,
                            caveats=tuple(caveats))

    # --- gate 0: diagnosis and status
    if not inp.diagnosis_ok:
        return out("Diagnose unavailable", "re-run the diagnosis", "The campaign diagnosis is missing or failed.",
                   "unavailable", "0a", NONE)
    status = canonical_status(inp.status, inp.status_map).value if inp.status is not None else None
    if status in cfg.removed_statuses:
        return out("Launch replacement", "new campaign on the same products",
                   "The campaign is removed and cannot be changed.", "launch_replacement", "0b", NONE)
    if status in cfg.stopped_statuses:
        return out("Investigate stop & reactivate", "campaign status",
                   f"The campaign is not running (status {status}); no lever applies until it is live again.",
                   "investigate_stop", "0c", NONE)

    # --- gate A: reach
    if inp.reach_judged:
        return out("Reach: judge on reach, not ROAS", "reach and cost per thousand",
                   "This ad type has no attributed sales; ROAS does not apply to it.", "reach", "A", HOLD)

    top_is_cut = bool(top and (_has(top.verb, cfg.cut_tokens) or (top.verb_class or "").lower() in cfg.cut_tokens))

    # --- gate B: total-return halo
    if roas is not None and total is not None:
        halo = (roas <= cfg.halo_direct_max_be * be and total >= cfg.halo_total_min_be * be
                and total >= cfg.halo_total_to_direct * max(roas, cfg.halo_direct_floor))
        if halo and (mix.is_conquest or top_is_cut):
            return out("Keep (total-return halo)", "cap the weakest bids, don't cut",
                       f"Direct ROAS {_n(roas)} looks weak but total ROAS {_n(total)} "
                       f"is at least {cfg.halo_total_to_direct:g}x direct and above {_n(cfg.halo_total_min_be * be, 1)}.",
                       "halo_keep", "B", HOLD)

    # --- gates C / C': branded cannibalisation
    if mix.is_branded and roas is not None and roas >= bar:
        if cann is not None:
            incr = roas * (1.0 - cann)
            if incr < bar:
                return out("Hold (branded cannibalisation)", "do not scale; defend the position only",
                           f"ROAS {_n(roas)} but {cann:.0%} of these sales would arrive organically: "
                           f"incremental ROAS {_n(incr)} is under {_n(bar, 1)}.", "hold_cannibalisation", "C", HOLD)
        else:
            return out("Hold (verify branded incrementality)", "probe tier for 7 days",
                       f"ROAS {_n(roas)} on own-brand terms with no cannibalisation measure: verify "
                       "incrementality before scaling.", "hold_verify_incrementality", "C'", HOLD)

    # --- gate 1: surgical cut (wins at any ROAS)
    if top is not None and top_is_cut and top.targets:
        return out("Cut bleeding keywords", ", ".join(top.targets),
                   (top.headline or "Named keywords lose money") + ": removing them is right at any campaign ROAS.",
                   "cut_keywords", "1", SHRINK)
    if top is not None and _has(top.verb, cfg.launch_tokens):
        return out("Launch replacement", ", ".join(top.targets) or "new campaign",
                   top.headline or "The engine recommends a replacement launch.", "launch_replacement", "1b", NONE)

    # --- gate 2: availability block
    band = pol.band(inp.availability)
    av = pol.normalise(inp.availability)
    av_prior = pol.normalise(inp.availability_prior)
    if band == "block":
        target = f"lift availability above {pol.block:.0%}"
        direction = HOLD
        if roas is not None and 0 < roas < be:
            target += f"; trim daily budget to ~{_money(_round_step(avg_daily * roas / be, cfg, econ))}/day meanwhile"
            direction = SHRINK
        return out("Fix availability first", target,
                   f"Availability {av:.0%} is under the {pol.block:.0%} block line: out-of-stock products "
                   "do not serve, so no bid or budget lever can work.", "fix_availability", "2", direction)

    # --- gate 3: auto-targeted burn
    burn = inp.auto_burn or any(_has(a.headline, cfg.burn_tokens) or _has(a.verb, cfg.burn_tokens) for a in acts)
    if inp.auto_targeted and burn:
        return out("Cap the auto-targeted ad", "reduce its daily budget",
                   "The auto-targeted ad is burning spend without a matching return.", "cap_auto", "3", SHRINK)

    # --- gate 4: engine verdict pass-through
    # The engine's verdict is its leading action.  A hold note that merely trails a
    # concrete lead action (for example "Top up" followed by "Hold lever changes")
    # must not override it; ``passthrough_any_action`` restores the wider scan.
    for a in (acts if cfg.passthrough_any_action else acts[:1]):
        if _has(a.verb, cfg.passthrough_tokens):
            return out(a.verb, ", ".join(a.targets) or "as stated", a.headline or "Engine verdict passed through.",
                       "passthrough", "4", NONE)

    # --- gate 5: budget anomaly
    if b.anomaly:
        return out("Fix budget configuration", "set a real daily budget",
                   "The daily budget looks like a placeholder; budget advice is meaningless until it is fixed.",
                   "fix_budget_config", "5", HOLD)

    # --- gate 6: no ROAS
    if roas is None:
        if inp.state_verb:
            return out(inp.state_verb, "see the state-mode actions", "Too young for a ROAS read; state-mode call.",
                       "state", "6", NONE)
        return out("Insufficient data, investigate", "delivery and tracking", "No spend-attributed ROAS is available.",
                   "insufficient", "6", NONE)

    qualifier = ""
    if band == "caution" or (av is not None and av_prior is not None and av_prior - av >= cfg.availability_drop - 1e-12):
        qualifier = f"Scale in-stock products only; lift availability toward {pol.target:.0%} first. "

    # --- ROAS ladder
    if roas <= cfg.pause_roas:
        return out("Pause", "the campaign", "Zero or negative return on spend.", "pause", "L0", SHRINK)

    if roas < be:
        trim_to = _round_step(avg_daily * roas / be, cfg, econ)
        cut_pct = int(min(max(round((1.0 - roas / be) * 100.0), 0), 100))
        if cut_pct < cfg.min_trim_pct:
            return out("Hold & monitor", "watch marginal ROAS for ~7 days",
                       f"ROAS {_n(roas)} is within {cfg.min_trim_pct}% of breakeven {_n(be, 1)}: a trim that small "
                       "is not an action.", "hold_monitor", "L1", HOLD)
        target = f"trim daily budget to ~{_money(trim_to)}/day (-{cut_pct}%)"
        why = (f"ROAS {_n(roas)} is under breakeven {_n(be, 1)}: only the {100 - cut_pct}% slice of spend that pays "
               "for itself is kept. Never raise.")
        if b.ladder == "bid-constrained":
            return out("Fix efficiency before bidding up", target, why, "trim_efficiency", "L1", SHRINK)
        if top is not None and _has(top.verb, cfg.availability_tokens):
            return out("Fix availability first", target, why, "trim_availability", "L1", SHRINK)
        return out("Trim toward break-even", target, why, "trim", "L1", SHRINK)

    if roas < bar:
        return out("Hold & monitor", "watch marginal ROAS for ~7 days",
                   f"ROAS {_n(roas)} is between breakeven {_n(be, 1)} and the scale bar {_n(bar, 1)}.",
                   "hold_monitor", "L2", HOLD)

    # roas >= bar
    m = b.mroas
    if (m is not None and m < cfg.marginal_ceiling_be * be) or (
            b.sat_spend is not None and b.daily is not None and b.sat_spend <= b.daily):
        return out("Hold (at curve ceiling)", "no budget change",
                   f"ROAS {_n(roas)} is healthy but the next unit of budget returns "
                   f"{'less than breakeven' if m is None else _n(m)}: the saturation curve is at its ceiling.",
                   "hold_ceiling", "L3.1", HOLD)
    if thin_demand:
        return out("Add keywords", "adjacent terms with real search volume",
                   qualifier + f"ROAS {_n(roas)} clears the bar but demand is the limit: budget cannot create searches.",
                   "add_keywords", "L3.2", GROW)
    if b.ladder == "bid-constrained":
        if any(a.self_competed for a in acts):
            return out("Consolidate overlapping campaigns first", "merge the overlapping targeting",
                       "Sibling campaigns bid on the same units; raising bids would outbid yourself.",
                       "consolidate", "L3.3", HOLD)
        pos = f" (position {_n(b.position, 1)})" if b.position is not None else ""
        tgt = "toward the top-slot clearing median"
        if inp.clearing_p50 is not None:
            tgt += f" {_n(inp.clearing_p50)}"
        tgt += "; step to the 75th percentile"
        if inp.clearing_p75 is not None:
            tgt += f" {_n(inp.clearing_p75)}"
        tgt += " only if rank does not move"
        return out("Raise bids (not budget)", tgt,
                   qualifier + f"ROAS {_n(roas)} with budget left unspent{pos}: the bid, not the cap, limits delivery.",
                   "raise_bids", "L3.3", GROW)
    if b.ladder == "volume-ceiling":
        return out("Add keywords", "new targeting units",
                   qualifier + f"ROAS {_n(roas)} and already on the top slot: more budget buys nothing here.",
                   "add_keywords", "L3.4", GROW)
    if b.capped:
        held = (b.held_days or 0) >= cfg.held_days_min
        verb = "Top up daily budget" if held else "Scale: raise the cap"
        small = (m is None or m < cfg.marginal_full_step_be * be or (inp.confidence or "").lower() == "low")
        rr = b.rec_range
        if rr and len(rr) >= 2:
            step_to = rr[0] if small else rr[1]
        elif b.daily:
            step_to = b.daily * (1.3 if small else 2.0)
        else:
            step_to = None
        if b.sat_spend is not None:
            basis = "curve-fit"
        else:
            # say the multiple that was actually computed; this is the one-line prescription's own
            # estimator and need not equal the step on the plan's card
            mult = f"{step_to / b.daily:.1f}x cap" if (step_to is not None and b.daily) else "step"
            basis = f"heuristic {mult}, verify on the curve"
        target = (f"raise daily budget to ~{_money(step_to)}/day ({basis})" if step_to is not None
                  else f"raise the daily budget in a measured step ({basis})")
        why = qualifier + f"ROAS {_n(roas)} and the budget binds"
        if held:
            why += f" (platform-held on {b.held_days} days)"
        why += f"; marginal ROAS {_n(m)}" if m is not None else "; marginal ROAS unverified"
        why += ": " + ("small step" if small else "full step") + "."
        return out(verb, target, why, "top_up" if held else "scale_cap", "L3.5", GROW)
    if m is not None and m >= cfg.marginal_full_step_be * be:
        return out("Scale carefully", "measured budget increase; watch marginal ROAS",
                   qualifier + f"ROAS {_n(roas)}, not capped, marginal ROAS {_n(m)}.", "scale_carefully", "L3.6", GROW)
    return out("Maintain (healthy, no action)", "no change",
               f"ROAS {_n(roas)} and the budget does not bind: an uncapped winner is not budget-bound.",
               "maintain", "L3.7", HOLD)


def _round_step(x: float, cfg: PrescribeConfig, econ: Economics) -> float:
    step = econ.money(cfg.budget_round_ref)
    return round(x / step) * step if step > 0 else x


# --------------------------------------------------------------------------- drill-row calls

@dataclass(frozen=True)
class DrillCallConfig:
    """Thresholds of the one-word drill-row calls.  ROAS bars are breakeven multiples."""

    stopped_spend_ref: float = 1.0           # current spend under this and baseline above it: already stopped
    cut_spend_ref: float = 20000.0           # material spend for a cut call
    cut_roas_be: float = 1.0                 # below breakeven with material spend -> cut
    scale_roas_be: float = 2.0               # at or above with a positive contribution -> scale
    contribution_band: float = 0.02          # |shift-share contribution| beyond this is a real move
    step_ratio: float = 1.1                  # bid or position 10% worse than baseline
    availability_fix: float = 0.7            # product availability under this: fix / gate
    availability_restock: float = 0.6        # geo availability under this: restock / gate
    availability_delisted: float = 0.02      # stopped product at or under this: possibly delisted
    availability_gap: float = -0.05          # own minus rival availability under this: trailing rivals


_DCC = DrillCallConfig()


def _stopped(row: Any, cfg: DrillCallConfig, econ: Economics) -> bool:
    lim = econ.money(cfg.stopped_spend_ref)
    return float(_g(row, "w_spend", 0.0) or 0.0) < lim < float(_g(row, "h_spend", 0.0) or 0.0)


def keyword_row_call(row: Any, *, lower_is_better: bool = True, cfg: DrillCallConfig | None = None,
                     economics: Economics | None = None) -> str:
    """One-word call for a keyword drill row.

    ``row`` is any object or mapping with ``w_*`` (current) and ``h_*``
    (baseline) ``spend, roas, bid, position`` and ``total`` (its exact
    shift-share contribution).  First match: current spend ~0 and baseline
    spend above it -> ``"already stopped"``; ``w_roas < be`` with spend
    above ``money(cut_spend_ref)`` -> ``"cut / negative"``; ``total <
    -0.02`` and bid up 10% and (ROAS unknown or under the scale bar) ->
    ``"cap bid"``; ``total < -0.02`` and position 10% worse -> ``"push
    rank"``; ``total > 0.02`` and ``w_roas >=`` scale bar -> ``"scale"``;
    else ``"hold"``.  ``lower_is_better`` states the position semantics.
    """
    cfg, econ = cfg or _DCC, economics or _ECON
    be = econ.be
    if _stopped(row, cfg, econ):
        return "already stopped"
    w_sp = float(_g(row, "w_spend", 0.0) or 0.0)
    w_roas, total = _g(row, "w_roas"), float(_g(row, "total", 0.0) or 0.0)
    if w_roas is not None and w_roas < cfg.cut_roas_be * be and w_sp > econ.money(cfg.cut_spend_ref):
        return "cut / negative"
    w_bid, h_bid = _g(row, "w_bid"), _g(row, "h_bid")
    if (total < -cfg.contribution_band and w_bid is not None and h_bid is not None and w_bid > cfg.step_ratio * h_bid
            and (w_roas is None or w_roas < cfg.scale_roas_be * be)):
        return "cap bid"
    w_pos, h_pos = _g(row, "w_position"), _g(row, "h_position")
    if total < -cfg.contribution_band and w_pos is not None and h_pos is not None:
        worse = w_pos > cfg.step_ratio * h_pos if lower_is_better else w_pos * cfg.step_ratio < h_pos
        if worse:
            return "push rank"
    if total > cfg.contribution_band and w_roas is not None and w_roas >= cfg.scale_roas_be * be:
        return "scale"
    return "hold"


def product_row_call(row: Any, *, cfg: DrillCallConfig | None = None, economics: Economics | None = None) -> str:
    """One-word call for a product drill row.

    Uses ``w_availability`` (0..1, optional).  First match: stopped (as for
    keywords) -> ``"possibly delisted, verify"`` when availability is at or
    under 0.02, else ``"already stopped"``; availability under 0.7 ->
    ``"fix availability / gate"``; ``w_roas < be`` with material spend ->
    ``"cut spend"``; ``total > 0.02`` and ``w_roas >=`` scale bar ->
    ``"scale"``; else ``"hold"``.
    """
    cfg, econ = cfg or _DCC, economics or _ECON
    be = econ.be
    av = AvailabilityPolicy.normalise(_g(row, "w_availability", _g(row, "availability")))
    if _stopped(row, cfg, econ):
        return "possibly delisted, verify" if av is not None and av <= cfg.availability_delisted else "already stopped"
    if av is not None and av < cfg.availability_fix:
        return "fix availability / gate"
    w_sp = float(_g(row, "w_spend", 0.0) or 0.0)
    w_roas, total = _g(row, "w_roas"), float(_g(row, "total", 0.0) or 0.0)
    if w_roas is not None and w_roas < cfg.cut_roas_be * be and w_sp > econ.money(cfg.cut_spend_ref):
        return "cut spend"
    if total > cfg.contribution_band and w_roas is not None and w_roas >= cfg.scale_roas_be * be:
        return "scale"
    return "hold"


def geo_row_call(own_availability: float | None, rival_availability: float | None = None, *,
                 cfg: DrillCallConfig | None = None) -> str:
    """One-word call for a geo drill row (availability on 0..1).

    ``gap = own - rival``; ``gap < -0.05`` -> ``"fix availability (trails
    rivals)"``; own availability under 0.6 -> ``"restock / gate"``; else
    ``"monitor"`` (also when availability is not reported).
    """
    cfg = cfg or _DCC
    own = AvailabilityPolicy.normalise(own_availability)
    rival = AvailabilityPolicy.normalise(rival_availability)
    if own is not None and rival is not None and own - rival < cfg.availability_gap:
        return "fix availability (trails rivals)"
    if own is not None and own < cfg.availability_restock:
        return "restock / gate"
    return "monitor"
