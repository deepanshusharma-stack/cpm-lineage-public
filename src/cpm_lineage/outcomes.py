"""Outcome scoring: did an action do what its card predicted?

Two independent loops live here.

**Ledger pairing** (``pair_outcomes``).  Each applied action is measured at
fixed horizons: efficiency (ROAS = attributed sales / spend) over the
``horizon`` days after the action against the same number of days before.
The expectation is the "before" value adjusted for the market *tide* (the
segment's own sales ratio over the same two windows), damped by a square
root because ad efficiency does not follow market velocity one for one.
The result is a label: ``On Track``, ``Review``, ``Off Track``,
``No signal``, ``Too early`` or ``Unreadable``.  Cards that were *not* taken
(conflict losers, vetoed cards) are scored as counterfactuals:
``cf-right`` / ``cf-wrong`` / ``cf-nosignal``.

The asymmetry is deliberate: a cut that misses is ``Review`` (the money was
saved either way), a raise that misses is ``Off Track`` (money was spent).

**Difference-in-differences** (``evaluate``).  For a logged recommendation
on one entity: the change in the entity's ROAS from before to after, minus
the same change over the rest of the account, plus a check of whether the
recommended change was actually made (*taken-detection*).

Nothing here needs clicks; everything runs on spend and attributed sales.
All functions are pure except ``pair_outcomes``, which writes outcomes to
the ledger object it is handed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .config import Economics
from .schema import AppliedAction, ChangeEvent, OfftakeRow, PerfRow
from .store.actionlog import ExpectedChange, LoggedAction

__all__ = [
    "OutcomeConfig", "PairMeasure", "PairingReport", "DidResult",
    "damp", "outcome_verdict", "counterfactual_verdict", "early_call",
    "daily_by_entity", "pair_measure", "tide", "is_stacked", "pair_outcomes",
    "window_roas", "detect_taken", "did_status", "evaluate",
]


@dataclass(frozen=True)
class OutcomeConfig:
    """Thresholds for outcome labels, pairing and the difference-in-differences read."""

    # ---- tide ----
    damp_min: float = 0.5                  # floor of the tide multiplier
    damp_max: float = 2.0                  # ceiling of the tide multiplier
    # ---- outcome labels ----
    too_early_days: int = 3                # no label before day 3
    flat_band: float = 0.15                # watch / freeze: within 15% of expectation confirms "flat"
    cut_hold: float = 0.95                 # cut is on track when efficiency holds >= 95% of expectation
    revenue_retention: float = 0.60        # ... and at least 60% of revenue is kept (no celebrating a collapse)
    raise_hold: float = 0.85               # raise is on track at >= 85% of expectation (scale dilutes a little)
    cf_cut_ratio: float = 0.95             # counterfactual cut was right when efficiency fell below 95% of expectation
    # ---- early stop-loss (display only) ----
    early_from_day: int = 3                # early call window opens ...
    early_to_day: int = 7                  # ... and closes at the first horizon
    early_stop_ratio: float = 0.5          # under half of expectation: losers stop early, winners run the window
    # ---- pairing ----
    pairing_cap: int = 20                  # realised and counterfactual rows paired per run, each
    edge_guard: str = "feed"               # "feed": the feed's latest date must reach the horizon end (an entity that stopped serving is scored); "entity": the entity's own data must
    # ---- difference-in-differences ----
    did_min_age_days: int = 7              # younger entries are "too-early"
    did_max_window_days: int = 14          # W = min(14, age)
    did_band: float = 0.10                 # +/- ROAS points separating landed / flat / worsened
    did_min_post_spend: float = 1000.0     # money: below this the entity stopped or is negligible -> no-data
    # ---- taken-detection ----
    budget_fields: tuple[str, ...] = ("daily_budget",)            # change-log fields that are a budget change
    bid_fields: tuple[str, ...] = ("bid",)  # change-log fields that are a bid change
    status_fields: tuple[str, ...] = ("status",)  # change-log fields that are a status change
    keyword_cut_fields: tuple[str, ...] = ("keyword_removed", "negative_keyword_added")  # change-log fields that record a keyword cut
    keyword_add_fields: tuple[str, ...] = ("keyword_added",)  # change-log fields that record a keyword addition
    taken_max_lines: int = 4               # evidence lines kept per entry
    taken_budget_epsilon: float = 1.0      # money: budget differences at or below this are not a change
    taken_share_min: float = 0.01          # target keywords must hold > 1% of spend before the action to infer anything
    taken_cut_ratio: float = 0.5           # spend share fell below half: the cut was made
    taken_scale_ratio: float = 1.2         # spend share rose above 1.2x: the scale was made


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

def damp(tide_ratio: float | None, cfg: OutcomeConfig | None = None) -> float:
    """Turn a market tide ratio into an expectation multiplier.

    ``clamp(sqrt(tide), damp_min, damp_max)``; a missing or zero tide is
    1.0.  A market running 2x raises the expectation about 1.4x, because a
    faster market pulls in broader, lower-intent traffic.
    """
    cfg = cfg or OutcomeConfig()
    return max(cfg.damp_min, min(cfg.damp_max, math.sqrt(max(tide_ratio or 1.0, 0.0))))


def outcome_verdict(
    direction: str | None,
    roas_after: float | None,
    roas_before: float | None,
    days_since: int,
    tide_ratio: float | None = 1.0,
    revenue_after: float | None = None,
    revenue_before: float | None = None,
    cfg: OutcomeConfig | None = None,
) -> str:
    """Label a realised action against its own prediction.

    With ``exp = roas_before * damp(tide)``:

    * before day ``too_early_days``: ``Too early``;
    * watch / freeze (direction neither up nor down; the prediction is
      "flat"): ``No signal`` without a positive baseline, else ``On Track``
      when ``|after - exp| <= flat_band * exp``, otherwise ``Review``;
    * missing either side: ``No signal``; baseline <= 0: ``No signal`` if
      the after value is also <= 0, else ``Review`` (a zero baseline never
      judges);
    * ``down``: ``On Track`` when ``after >= cut_hold * exp`` *and* revenue
      was retained (``revenue_after >= revenue_retention * revenue_before``
      when both are known); otherwise ``Review``.  A cut is never
      ``Off Track``;
    * ``up``: ``On Track`` when ``after >= raise_hold * exp``, else
      ``Off Track``.
    """
    cfg = cfg or OutcomeConfig()
    if days_since < cfg.too_early_days:
        return "Too early"
    if direction not in ("up", "down"):
        if roas_before is None or roas_after is None or roas_before <= 0:
            return "No signal"
        exp = roas_before * damp(tide_ratio, cfg)
        return "On Track" if abs(roas_after - exp) <= cfg.flat_band * exp else "Review"
    if roas_before is None or roas_after is None:
        return "No signal"
    if roas_before <= 0:
        return "No signal" if roas_after <= 0 else "Review"
    exp = roas_before * damp(tide_ratio, cfg)
    if direction == "down":
        if roas_after >= cfg.cut_hold * exp:
            if revenue_before and revenue_after is not None and revenue_after < cfg.revenue_retention * revenue_before:
                return "Review"
            return "On Track"
        return "Review"
    return "On Track" if roas_after >= cfg.raise_hold * exp else "Off Track"


def counterfactual_verdict(
    direction: str | None,
    roas_after: float | None,
    roas_before: float | None,
    tide_ratio: float | None = 1.0,
    cfg: OutcomeConfig | None = None,
) -> str:
    """Score a card that was *not* taken: did reality move the way it said it would?

    ``cf-nosignal`` without both values and a positive baseline.  Otherwise
    with ``exp = roas_before * damp(tide)``: a ``down`` card was right when
    efficiency went on to fall (``after < cf_cut_ratio * exp``); any other
    card was right when efficiency held (``after >= exp``).  Returns
    ``cf-right``, ``cf-wrong`` or ``cf-nosignal``.
    """
    cfg = cfg or OutcomeConfig()
    if roas_after is None or roas_before is None or roas_before <= 0:
        return "cf-nosignal"
    exp = roas_before * damp(tide_ratio, cfg)
    right = roas_after < cfg.cf_cut_ratio * exp if direction == "down" else roas_after >= exp
    return "cf-right" if right else "cf-wrong"


def early_call(
    direction: str | None,
    roas_after: float | None,
    roas_before: float | None,
    days_since: int,
    tide_ratio: float | None = 1.0,
    cfg: OutcomeConfig | None = None,
) -> str | None:
    """Sequential stop-loss for display: flag a clear loser before the first horizon.

    Only between day ``early_from_day`` and ``early_to_day`` (exclusive) and
    with a positive baseline: when ``after < early_stop_ratio * before *
    damp(tide)`` return ``"Review (early)"`` for a cut and
    ``"Off Track (early)"`` otherwise.  Winners are never called early, and
    the result must not be written to the ledger, so win rates stay
    comparable across horizons.  Returns ``None`` when no call is made.
    """
    cfg = cfg or OutcomeConfig()
    if not (cfg.early_from_day <= days_since < cfg.early_to_day):
        return None
    if roas_after is None or roas_before is None or roas_before <= 0:
        return None
    if roas_after < cfg.early_stop_ratio * roas_before * damp(tide_ratio, cfg):
        return "Review (early)" if direction == "down" else "Off Track (early)"
    return None


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

Daily = Mapping[date, tuple[float, float]]


def daily_by_entity(rows: Iterable[PerfRow]) -> dict[str, dict[date, tuple[float, float]]]:
    """Index performance rows as ``{entity_id: {day: (spend, sales)}}``."""
    out: dict[str, dict[date, tuple[float, float]]] = {}
    for r in rows:
        d = out.setdefault(r.entity_id, {})
        s, v = d.get(r.date, (0.0, 0.0))
        d[r.date] = (s + (r.spend or 0.0), v + (r.sales or 0.0))
    return out


def _sum(daily: Daily, start: date, end: date) -> tuple[float, float]:
    """Spend and sales over ``start <= day < end``."""
    sp = sa = 0.0
    for d, (s, v) in daily.items():
        if start <= d < end:
            sp += s
            sa += v
    return sp, sa


@dataclass(frozen=True)
class PairMeasure:
    """Horizon-exact before / after read of one entity."""

    roas_before: float | None
    roas_after: float | None
    revenue_before: float
    revenue_after: float
    spend_before: float
    spend_after: float


def pair_measure(
    daily: Daily | None,
    d0: date,
    horizon: int,
    cfg: OutcomeConfig | None = None,
    feed_latest: date | None = None,
) -> PairMeasure | None:
    """Measure an entity over ``after = [d0, d0 + h)`` against ``before = [d0 - h, d0)``.

    ``roas = sales / spend`` per window (``None`` when spend is 0); the
    revenue sums are returned as well.

    Data-edge guard: pairing is permanent, so a row is only measured once
    the data covers the whole after-window.  With ``edge_guard="feed"``
    (default) the supplied ``feed_latest`` must be at least ``d0 + h - 1``,
    so an entity that stopped serving after the action (the usual fate of a
    cut) is measured with no after-spend and resolves to ``No signal``
    instead of waiting forever.  With ``"entity"``, or when no
    ``feed_latest`` is supplied, the entity's own latest day inside
    ``[d0 - h, d0 + h)`` must reach it.  Otherwise ``None`` is returned and
    the row stays due.
    """
    cfg = cfg or OutcomeConfig()
    daily = daily or {}
    start, end = d0 - timedelta(days=horizon), d0 + timedelta(days=horizon)
    last_needed = end - timedelta(days=1)
    if cfg.edge_guard == "feed" and feed_latest is not None:
        latest = feed_latest
    else:
        latest = max((d for d in daily if start <= d < end), default=None)
    if latest is None or latest < last_needed:
        return None
    sb, vb = _sum(daily, start, d0)
    sa, va = _sum(daily, d0, end)
    return PairMeasure(roas_before=vb / sb if sb > 0 else None, roas_after=va / sa if sa > 0 else None,
                       revenue_before=vb, revenue_after=va, spend_before=sb, spend_after=sa)


def tide(offtake: Iterable[OfftakeRow], segment: str | None, d0: date, horizon: int) -> float:
    """Market tide of a segment around an action date.

    ``a`` = the segment's total sales (all brands) over ``[d0, d0 + h)``,
    ``b`` = over ``[d0 - h, d0)``; ``tide = a / b`` when both are positive,
    else 1.0.  The windows have equal length, so this is a per-day rate
    ratio.  No segment or no market feed gives 1.0.
    """
    if not segment:
        return 1.0
    a = b = 0.0
    lo, hi = d0 - timedelta(days=horizon), d0 + timedelta(days=horizon)
    for r in offtake:
        if r.segment != segment or not (lo <= r.date < hi):
            continue
        if r.date >= d0:
            a += r.sales or 0.0
        else:
            b += r.sales or 0.0
    return a / b if a > 0 and b > 0 else 1.0


def is_stacked(entity_id: str | None, d0: date, horizon: int, applied: Iterable[AppliedAction]) -> bool:
    """True when another applied action on the same entity lands inside the measurement window.

    Stacked means some applied action on ``entity_id`` has a date ``da``
    with ``d0 < da <= d0 + horizon``.  Such a read cannot be attributed to
    either action and is labelled ``Unreadable``.  An action applied on the
    same day as ``d0`` is not treated as stacked.
    """
    d1 = d0 + timedelta(days=horizon)
    return any(a.entity_id == entity_id and d0 < a.applied_on <= d1 for a in applied)


@dataclass
class PairingReport:
    """What one pairing run did."""

    paired: int = 0
    deferred: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)

    def __int__(self) -> int:
        return self.paired


def pair_outcomes(
    ledger: Any,
    account: str,
    platform: str,
    rows: Iterable[PerfRow],
    *,
    today: date,
    offtake: Iterable[OfftakeRow] = (),
    applied: Sequence[AppliedAction] | None = None,
    cfg: OutcomeConfig | None = None,
) -> PairingReport:
    """Measure every due ledger row and store its outcome.  Run once a day.

    ``ledger`` is an ``ActionLedger`` (anything with ``due_pairs``,
    ``store_pair`` and ``applied_actions``).  Every due row is visited,
    oldest first, realised then counterfactual; each list stops after
    ``pairing_cap`` rows have actually been *paired*.  A row that has to be
    deferred does not use up the cap, so rows that cannot be measured yet
    never starve the measurable ones behind them.  For each row, with anchor
    ``d0`` and horizon ``h``:

    1. **Unreadable guard**: ``is_stacked`` -> outcome ``Unreadable``,
       tide 1.0, no ROAS.
    2. **Measure**: ``pair_measure``; ``None`` (data short of the horizon)
       leaves the row due for the next run.
    3. **Market tide**: ``tide(offtake, segment, d0, h)`` when the row has a
       segment, else 1.0.
    4. **Score**: counterfactual rows with ``counterfactual_verdict``,
       realised rows with ``outcome_verdict`` at ``days_since = h``.
    5. **Store** with ``ledger.store_pair``.

    Returns a ``PairingReport`` (``paired``, ``deferred``, label counts).
    """
    cfg = cfg or OutcomeConfig()
    daily = daily_by_entity(rows)
    feed_latest = max((d for e in daily.values() for d in e), default=None)
    offtake = list(offtake)
    if applied is None:
        applied = ledger.applied_actions(account, platform)
    report = PairingReport()
    for is_cf in (False, True):
        _pair_rows(ledger, account, platform, ledger.due_pairs(account, platform, cf=is_cf, today=today),
                   daily, feed_latest, offtake, applied, cfg, report)
    return report


def _pair_rows(ledger: Any, account: str, platform: str, work: Sequence[Mapping[str, Any]], daily: Mapping[str, Daily],
               feed_latest: date | None, offtake: Sequence[OfftakeRow], applied: Sequence[AppliedAction],
               cfg: OutcomeConfig, report: PairingReport) -> None:
    done = 0
    for row in work:
        if done >= cfg.pairing_cap:
            break
        d0 = date.fromisoformat(row["d0"])
        hz = int(row["horizon"])
        eid = row["entity_id"]
        cf = bool(row.get("counterfactual"))
        t = 1.0
        before = after = None
        if is_stacked(eid, d0, hz, applied):
            outcome = "Unreadable"
        else:
            m = pair_measure(daily.get(eid), d0, hz, cfg, feed_latest)
            if m is None:
                report.deferred += 1
                continue
            t = tide(offtake, row.get("segment"), d0, hz)
            before, after = m.roas_before, m.roas_after
            if cf:
                outcome = counterfactual_verdict(row.get("direction"), after, before, t, cfg)
            else:
                outcome = outcome_verdict(row.get("direction"), after, before, hz, t,
                                          m.revenue_after, m.revenue_before, cfg)
        if ledger.store_pair(account, platform, row["plan_id"], row["action_id"], hz, outcome=outcome,
                             tide=round(t, 4), roas_before=before, roas_after=after):
            done += 1
            report.paired += 1
            report.outcomes[outcome] = report.outcomes.get(outcome, 0) + 1


# --------------------------------------------------------------------------
# Difference-in-differences
# --------------------------------------------------------------------------

@dataclass
class DidResult:
    """One evaluated log entry.

    ``did = (roas_post - roas_pre) - (control_post - control_pre)``;
    ``control_delta`` is the second bracket.  ``taken`` lists the evidence
    that the recommended change was made (empty = no evidence found).
    ``status``: ``too-early``, ``no-data``, ``landed``, ``flat``,
    ``worsened`` or ``error``.
    """

    entry: LoggedAction
    age: int
    status: str
    window: int | None = None
    roas_pre: float | None = None
    roas_post: float | None = None
    spend_post: float | None = None
    control_delta: float | None = None
    did: float | None = None
    taken: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


def window_roas(daily: Daily, start: date, end: date) -> tuple[float | None, float]:
    """``(sales / spend, spend)`` over ``start <= day < end``; ROAS is ``None`` at zero spend."""
    sp, sa = _sum(daily, start, end)
    return (sa / sp if sp > 0 else None), sp


def did_status(did: float | None, roas_post: float | None, spend_post: float | None,
               min_post_spend: float, band: float = 0.10) -> str:
    """Classify a difference-in-differences read.

    ``no-data`` when there is no post ROAS, post spend is below
    ``min_post_spend``, or ``did`` could not be measured ("flat" asserts a
    measured null; unmeasurable is not flat).  Otherwise ``landed`` when
    ``did > band``, ``worsened`` when ``did < -band``, else ``flat``.
    """
    if roas_post is None or (spend_post or 0.0) < min_post_spend or did is None:
        return "no-data"
    if did > band:
        return "landed"
    if did < -band:
        return "worsened"
    return "flat"


def _num(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def detect_taken(
    entry: LoggedAction,
    window_days: int,
    *,
    change_events: Iterable[ChangeEvent] = (),
    rows: Iterable[PerfRow] = (),
    current_budget: float | None = None,
    economics: Economics | None = None,
    cfg: OutcomeConfig | None = None,
) -> list[dict[str, Any]]:
    """Find evidence that a logged recommendation was actually executed.

    **Canonical** (authoritative when it finds anything): change-log events
    on the entity with ``d0 <= day < d0 + W``, not marked unverified,
    excluding bid changes made by an automation.  Events are aggregated per
    ``(field, source, targeting unit)`` with a count and the latest old /
    new values, and a group counts as taken when it matches an expected
    change:

    * budget: the entry expects a budget change and the observed direction
      (new versus old) matches the expected one; an unparseable observed
      direction, or an expectation without a direction, counts as taken;
    * keyword removed / negative added / keyword added: the keyword is among
      the expected targets;
    * bid: the entry expects a bid change;
    * status: the entry expects a status change.

    At most ``taken_max_lines`` lines are kept.

    **Fallback inference**, only when the canonical read found nothing:

    * budget: ``current_budget`` differs from the logged budget by more than
      ``money(taken_budget_epsilon)`` in the expected direction;
    * keyword: the expected targets' share of the entity's spend, before
      (``[d0 - W, d0)``) versus after (``(d0, d0 + W]``).  Needs a pre-share
      above ``taken_share_min``; a ``down`` expectation is taken when the
      share fell below ``taken_cut_ratio`` of itself, an ``up`` expectation
      when it rose above ``taken_scale_ratio``.

    Returns a list of evidence dicts, each with a ``basis`` of
    ``"change_log"`` or ``"inferred"``.
    """
    cfg = cfg or OutcomeConfig()
    economics = economics or Economics()
    d0 = entry.date
    end = d0 + timedelta(days=window_days)
    exp: Sequence[ExpectedChange] = entry.expected
    budget_exp = [e for e in exp if e.lever == "budget"]
    targets = {_norm(t) for e in exp for t in e.targets}
    wants_bid = any(e.lever == "bid" for e in exp)
    wants_status = any(e.lever == "status" for e in exp)

    groups: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    for ev in sorted(change_events, key=lambda e: e.timestamp):
        if ev.entity_id != entry.entity_id or not (d0 <= ev.timestamp.date() < end) or ev.verified is False:
            continue
        if ev.field in cfg.bid_fields and ev.automated:
            continue
        g = groups.setdefault((ev.field, ev.source, _norm(ev.targeting_unit)), {
            "field": ev.field, "source": ev.source, "targeting_unit": ev.targeting_unit, "count": 0,
            "basis": "change_log"})
        g["count"] += 1
        g["old"], g["new"] = ev.old, ev.new
    taken: list[dict[str, Any]] = []
    for (fld, _src, unit), g in groups.items():
        ok = False
        if fld in cfg.budget_fields:
            if budget_exp:
                old, new = _num(g["old"]), _num(g["new"])
                seen = None if old is None or new is None or new == old else ("up" if new > old else "down")
                ok = seen is None or any(e.direction in (None, seen) for e in budget_exp)
        elif fld in cfg.keyword_cut_fields or fld in cfg.keyword_add_fields:
            ok = bool(unit) and unit in targets
        elif fld in cfg.bid_fields:
            ok = wants_bid
        elif fld in cfg.status_fields:
            ok = wants_status
        if ok:
            taken.append(g)
    if taken:
        return taken[: cfg.taken_max_lines]

    eps = economics.money(cfg.taken_budget_epsilon)
    if current_budget is not None and entry.budget_daily_at is not None and budget_exp:
        diff = current_budget - entry.budget_daily_at
        if abs(diff) > eps and any(e.direction == ("up" if diff > 0 else "down") for e in budget_exp):
            taken.append({"field": cfg.budget_fields[0] if cfg.budget_fields else "budget", "basis": "inferred",
                          "old": entry.budget_daily_at, "new": current_budget, "count": 1})
    kw_exp = [e for e in exp if e.targets and e.direction in ("up", "down") and e.lever in ("keyword", "bid")]
    if kw_exp:
        pre_lo = d0 - timedelta(days=window_days)
        post_lo, post_hi = d0 + timedelta(days=1), d0 + timedelta(days=window_days + 1)
        tot = {"pre": 0.0, "post": 0.0}
        by_kw: dict[str, dict[str, float]] = {}
        for r in rows:
            if r.entity_id != entry.entity_id:
                continue
            side = "pre" if pre_lo <= r.date < d0 else ("post" if post_lo <= r.date < post_hi else None)
            if side is None:
                continue
            tot[side] += r.spend or 0.0
            k = by_kw.setdefault(_norm(r.targeting_unit), {"pre": 0.0, "post": 0.0})
            k[side] += r.spend or 0.0
        for e in kw_exp:
            pre = sum(by_kw.get(_norm(t), {}).get("pre", 0.0) for t in e.targets)
            post = sum(by_kw.get(_norm(t), {}).get("post", 0.0) for t in e.targets)
            if tot["pre"] <= 0 or tot["post"] <= 0:
                continue
            sh_pre, sh_post = pre / tot["pre"], post / tot["post"]
            if sh_pre <= cfg.taken_share_min:
                continue
            hit = (sh_post < cfg.taken_cut_ratio * sh_pre if e.direction == "down"
                   else sh_post > cfg.taken_scale_ratio * sh_pre)
            if hit:
                taken.append({"field": "keyword_spend_share", "basis": "inferred", "targets": list(e.targets),
                              "old": round(sh_pre, 4), "new": round(sh_post, 4), "count": 1})
    return taken[: cfg.taken_max_lines]


def evaluate(
    entries: Iterable[LoggedAction],
    rows: Iterable[PerfRow],
    *,
    today: date,
    change_events: Iterable[ChangeEvent] = (),
    current_budgets: Mapping[str, float] | None = None,
    control_entity_ids: Iterable[str] | None = None,
    economics: Economics | None = None,
    cfg: OutcomeConfig | None = None,
) -> list[DidResult]:
    """Difference-in-differences read of each logged recommendation, newest first.

    For an entry issued on ``d0`` with ``age = today - d0``:

    1. ``age < did_min_age_days`` -> ``too-early``.
    2. ``W = min(did_max_window_days, age)``.
    3. Windows exclude the issue day: pre ``[d0 - W, d0)``, post
       ``(d0, d0 + W]``.
    4. Treated: ``pre`` and ``post`` ROAS (sales / spend) and post spend.
    5. Control: the same two ratios over every *other* entity in ``rows``
       (or over ``control_entity_ids`` when given), always excluding the
       treated entity.  A large entity left in its own baseline drags it and
       shrinks the measured effect toward zero.
    6. ``control_delta = round(c_post - c_pre, 2)``;
       ``did = round((post - pre) - (c_post - c_pre), 2)``; ``None`` when any
       input is missing.
    7. ``taken`` from ``detect_taken``.
    8. Status from ``did_status`` with ``money(did_min_post_spend)``.

    An entry that raises is returned with status ``error`` and a truncated
    message; one bad entry never stops the rest.

    Caveat: the rest of the account is not a clean control, because other
    entities share keywords, geos and auctions with the treated one.
    """
    cfg = cfg or OutcomeConfig()
    economics = economics or Economics()
    rows = list(rows)
    events = list(change_events)
    daily = daily_by_entity(rows)
    control = set(control_entity_ids) if control_entity_ids is not None else None
    min_spend = economics.money(cfg.did_min_post_spend)
    out: list[DidResult] = []
    for entry in sorted(entries, key=lambda e: e.date, reverse=True):
        age = (today - entry.date).days
        if age < cfg.did_min_age_days:
            out.append(DidResult(entry=entry, age=age, status="too-early"))
            continue
        try:
            w = min(cfg.did_max_window_days, age)
            d0 = entry.date
            pre_lo, post_lo, post_hi = d0 - timedelta(days=w), d0 + timedelta(days=1), d0 + timedelta(days=w + 1)
            mine = daily.get(entry.entity_id or "", {})
            pre, _ = window_roas(mine, pre_lo, d0)
            post, spend_post = window_roas(mine, post_lo, post_hi)
            rest: dict[date, tuple[float, float]] = {}
            for eid, days in daily.items():
                if eid == entry.entity_id or (control is not None and eid not in control):
                    continue
                for d, (s, v) in days.items():
                    a, b = rest.get(d, (0.0, 0.0))
                    rest[d] = (a + s, b + v)
            c_pre, _ = window_roas(rest, pre_lo, d0)
            c_post, _ = window_roas(rest, post_lo, post_hi)
            control_delta = round(c_post - c_pre, 2) if c_pre is not None and c_post is not None else None
            did = (round((post - pre) - (c_post - c_pre), 2)
                   if None not in (pre, post, c_pre, c_post) else None)
            taken = detect_taken(entry, w, change_events=events, rows=rows,
                                 current_budget=(current_budgets or {}).get(entry.entity_id or ""),
                                 economics=economics, cfg=cfg)
            out.append(DidResult(
                entry=entry, age=age, window=w, roas_pre=pre, roas_post=post, spend_post=spend_post,
                control_delta=control_delta, did=did, taken=taken,
                status=did_status(did, post, spend_post, min_spend, cfg.did_band)))
        except Exception as exc:  # one unreadable entry must not stop the rest
            out.append(DidResult(entry=entry, age=age, status="error", error=str(exc)[:200]))
    return out
