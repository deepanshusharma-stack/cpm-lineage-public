"""Bid-ceiling guardrail and the audit of a position-targeting bidder.

A position-targeting bidder is a closed loop: it reads the ad's current
position for a keyword and moves the bid to hold a target range, under a
ceiling.  For every active rule set that configures one, the audit answers:

* how high may it bid (``ceiling = base bid x (1 + guardrail% / 100)``),
* how often is it pinned at that ceiling (``cap``),
* how often does it reach the target (``hit``),
* how often can it see its position at all (``seen``),

and flags rule sets that are **blind** (bidding without seeing the position)
or **hot** (pinned at the ceiling and still missing the target), then rolls
spend up by guardrail-permissiveness band without double counting.

Budget guardrails are not CPM levers and are out of scope.

Two definitions of "target hit" exist and are exposed separately on
``DecisionCounts``: ``hit_or_better`` (position at or better than the worst
acceptable target; the audit's rate) and ``in_band`` (inside the target
range; the overpay check's rate), with ``overshoot`` = better than needed.

The thresholds ``hot_cap``, ``low_hit`` and ``blind_seen`` are judgment
values (see ``CpmConfig``).  Caveats: the guardrail percentage of a rule set
is the maximum across its rules while type, target and bid limits are
last-wins, so heterogeneous rule sets are summarised lossily; ``up`` uses
the live bid of today while spend and CPM belong to the chosen window.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Hashable, Iterable, Mapping, Sequence

from ..adapters.base import PlatformProfile
from ..config import PositionConfig
from ..metrics import cell_from_rows, empty_cell, sum_cells
from ..schema import BidDecision, BidRecord, Entity, MetricCell, PerfRow, Window
from ._util import cell_dict, get, in_window, is_keyword_row, median, norm_kw, position_config, to_local
from .config import CpmConfig
from .rules import (
    Rule, RuleSchema, Ruleset, RulesetMember, dedupe_members, position_conditions, select_rulesets,
)

__all__ = [
    "ABSOLUTE_BAND", "GuardrailPolicy", "guardrail_policy", "ceiling_multiplier", "ceiling_bid",
    "band_labels", "guardrail_band", "position_seen", "DecisionCounts", "decision_counts",
    "decision_rates", "audit_guardrails", "ruleset_members", "live_state",
]

ABSOLUTE_BAND = "absolute cap"


@dataclass(frozen=True)
class GuardrailPolicy:
    """How a position-targeting rule caps the bid.

    ``kind`` is ``"percentage_of_base"`` (ceiling = base x (1 + percentage /
    100)) or ``"absolute_cap"`` (ceiling = ``max_bid``).
    """

    kind: str = "absolute_cap"
    percentage: float | None = None
    max_bid: float | None = None
    min_bid: float | None = None


def guardrail_policy(condition: Mapping[str, Any], schema: RuleSchema | None = None) -> GuardrailPolicy:
    """Read the guardrail policy of one position-targeting condition.

    Percentage mode applies when the guardrail type equals
    ``schema.percentage_type`` **and** the percentage is non-zero; anything
    else is an absolute cap at the rule's max bid.
    """
    schema = schema or RuleSchema()
    gp = condition.get(schema.guardrail_pct_key)
    gt = condition.get(schema.guardrail_type_key)
    try:
        gp = float(gp) if gp not in (None, "") else None
    except (TypeError, ValueError):
        gp = None
    kind = "percentage_of_base" if (gt == schema.percentage_type and gp) else "absolute_cap"
    return GuardrailPolicy(
        kind=kind, percentage=gp, max_bid=_f(condition.get(schema.max_bid_key)),
        min_bid=_f(condition.get(schema.min_bid_key)),
    )


def _f(x: Any) -> float | None:
    try:
        return None if x in (None, "") else float(x)
    except (TypeError, ValueError):
        return None


def ceiling_multiplier(policy: GuardrailPolicy) -> float | None:
    """Ceiling as a multiple of the base bid: ``1 + percentage / 100`` in
    percentage mode (300% -> 4.0x), ``None`` for an absolute cap."""
    if policy.kind == "percentage_of_base" and policy.percentage:
        return 1.0 + policy.percentage / 100.0
    return None


def ceiling_bid(base_bid: float | None, policy: GuardrailPolicy, cfg: CpmConfig | None = None) -> float | None:
    """Highest bid the bidder may set.

    Percentage mode: ``base_bid x (1 + percentage / 100)`` where the base
    bid is the bid before the bidder began moving it (``None`` without a
    positive base).  By default the rule's max bid is *not*
    a cap in this mode; set ``cfg.percentage_ignores_max_bid = False`` for
    platforms where both apply (ceiling = the lower of the two).
    Absolute mode: ``max_bid``.
    """
    cfg = cfg or CpmConfig()
    mult = ceiling_multiplier(policy)
    if mult is None:
        return policy.max_bid
    if base_bid is None or base_bid <= 0:
        return None
    ceil = base_bid * mult
    if not cfg.percentage_ignores_max_bid and policy.max_bid:
        ceil = min(ceil, policy.max_bid)
    return ceil


def band_labels(cfg: CpmConfig | None = None) -> list[str]:
    """Labels of the permissiveness bands in ascending order, e.g.
    ``["<=50%", "51-100%", "101-200%", "201-300%", ">300%"]``."""
    cfg = cfg or CpmConfig()
    out, prev = [], None
    for ub in cfg.guardrail_bands:
        out.append(f"<={ub:g}%" if prev is None else f"{prev + 1:g}-{ub:g}%")
        prev = ub
    out.append(f">{prev:g}%" if prev is not None else "any%")
    return out


def guardrail_band(percentage: float | None, guardrail_type: str | None = "percentage",
                   cfg: CpmConfig | None = None, *, percentage_type: str = "percentage") -> str:
    """Permissiveness band of a guardrail.

    ``"absolute cap"`` unless the type is percentage with a non-zero
    percentage; otherwise the first band whose upper bound is >= the
    percentage (the open band above the last bound).
    """
    cfg = cfg or CpmConfig()
    if guardrail_type != percentage_type or not percentage:
        return ABSOLUTE_BAND
    labels = band_labels(cfg)
    for ub, label in zip(cfg.guardrail_bands, labels):
        if percentage <= ub:
            return label
    return labels[-1]


def position_seen(position: float | None, cfg: CpmConfig | None = None) -> bool:
    """True when the bidder found its position: inside
    ``cfg.rank_visible_min..rank_visible_max`` (1..60).  ``None`` and
    out-of-range sentinels mean "not found"."""
    cfg = cfg or CpmConfig()
    return position is not None and cfg.rank_visible_min <= position <= cfg.rank_visible_max


@dataclass(frozen=True)
class DecisionCounts:
    """Counters over a group of bidder decisions.  ``None`` = the log does
    not carry the field needed (never treated as 0)."""

    n: int = 0
    cap: int | None = None            # decisions pinned at the ceiling
    seen: int | None = None           # position found
    hit_or_better: int | None = None  # found and at or better than the worst acceptable target
    in_band: int | None = None        # found and inside the target range
    overshoot: int | None = None      # found and better than the best target bound


def _count(ds: Sequence[BidDecision], cfg: CpmConfig, lower: bool, position_reported: bool) -> DecisionCounts:
    n = len(ds)
    if n == 0:
        return DecisionCounts()
    cap = None
    if any(d.at_ceiling is not None for d in ds):
        cap = sum(1 for d in ds if d.at_ceiling)
    if not position_reported:
        return DecisionCounts(n=n, cap=cap)
    seen = hit = band = over = 0
    has_worst = has_best = False
    for d in ds:
        worst = d.target_position_max if lower else d.target_position_min
        best = d.target_position_min if lower else d.target_position_max
        has_worst = has_worst or worst is not None
        has_best = has_best or best is not None
        p = d.observed_position
        if not position_seen(p, cfg):
            continue
        seen += 1
        ok_worst = worst is not None and (p <= worst if lower else p >= worst)
        ok_best = best is not None and (p >= best if lower else p <= best)
        if ok_worst:
            hit += 1
        if ok_worst and ok_best:
            band += 1
        if best is not None and (p < best if lower else p > best):
            over += 1
    return DecisionCounts(
        n=n, cap=cap, seen=seen,
        hit_or_better=hit if has_worst else None,
        in_band=band if (has_worst and has_best) else None,
        overshoot=over if has_best else None,
    )


def decision_counts(
    decisions: Iterable[BidDecision],
    *,
    window: Window | None = None,
    by: Sequence[str] = ("rule_id",),
    tz: Any = None,
    log_from: date | None = None,
    position_reported: bool = True,
    lower_is_better: bool = True,
    cfg: CpmConfig | None = None,
    ids_as_str: bool = False,
) -> dict[Hashable, DecisionCounts]:
    """Count bidder decisions per group.

    Scope: decisions whose **local** date (``tz``) falls in ``window`` and is
    not before ``log_from`` (the first day the log exists).  Grouped by the
    ``by`` fields of ``BidDecision`` (key is the value for one field, a
    tuple for several).

    Counters: ``n``; ``cap`` = at the ceiling; ``seen`` = position found
    (in 1..60); ``hit_or_better`` = found and ``position <= target max``;
    ``in_band`` = found and ``target min <= position <= target max``;
    ``overshoot`` = found and ``position < target min`` (comparisons flip
    when a higher position number is better).  A counter is ``None`` when no
    decision of the group carries the needed field; ``position_reported=
    False`` declares that the log has no position at all (an observed
    position of ``None`` otherwise means "not found").  ``ids_as_str``
    turns every non-missing key value into a string, so a rule id of ``10``
    in the log and ``"10"`` in the rule store land in one group.
    """
    cfg = cfg or CpmConfig()

    def norm(v: Any) -> Any:
        return str(v) if (ids_as_str and v is not None) else v

    groups: dict[Hashable, list[BidDecision]] = defaultdict(list)
    for d in decisions:
        day = to_local(d.timestamp, tz).date()
        if window is not None and not in_window(day, window):
            continue
        if log_from is not None and day < log_from:
            continue
        key = norm(getattr(d, by[0])) if len(by) == 1 else tuple(norm(getattr(d, f)) for f in by)
        groups[key].append(d)
    return {k: _count(v, cfg, lower_is_better, position_reported) for k, v in groups.items()}


def decision_rates(counts: Iterable[DecisionCounts]) -> dict[str, Any]:
    """Decision-weighted rates over several counter groups.

    ``n = sum(n)``; each rate = ``sum(counter) / sum(n of the groups that
    carry that counter)`` (never a mean of group rates, and never diluted by
    a group whose log lacks the field).  All rates are ``None`` when
    ``n == 0``; one rate is ``None`` when none of the groups carries its
    counter.

    Returns ``{n, cap, hit, seen, in_band, overshoot}`` where ``hit`` is the
    ``hit_or_better`` rate.
    """
    counts = list(counts)
    n = sum(c.n for c in counts)

    def rate(name: str) -> float | None:
        vals = [(getattr(c, name), c.n) for c in counts if getattr(c, name) is not None]
        covered = sum(k for _, k in vals)
        if n == 0 or not vals or covered == 0:
            return None
        return sum(v for v, _ in vals) / covered

    return {
        "n": n, "cap": rate("cap"), "hit": rate("hit_or_better"), "seen": rate("seen"),
        "in_band": rate("in_band"), "overshoot": rate("overshoot"),
    }


def _sum(cells: Iterable[MetricCell]) -> MetricCell:
    cells = list(cells)
    return sum_cells(cells) if cells else empty_cell()


def _flags(rates: Mapping[str, Any], cfg: CpmConfig) -> tuple[bool, bool]:
    seen, cap, hit = rates["seen"], rates["cap"], rates["hit"]
    blind = seen is not None and seen < cfg.blind_seen
    hot = (not blind) and cap is not None and cap >= cfg.hot_cap and (hit or 0.0) < cfg.low_hit
    return blind, hot


def audit_guardrails(
    rulesets: Sequence[Ruleset],
    rules: Sequence[Rule],
    members: Sequence[RulesetMember],
    bids: Sequence[BidRecord],
    decisions: Sequence[BidDecision],
    perf_rows: Sequence[PerfRow],
    a: Window,
    b: Window | None = None,
    *,
    advertiser_id: str | None = None,
    umbrella_advertisers: Iterable[str] = (),
    tz: Any = None,
    log_from: date | None = None,
    position_reported: bool = True,
    profile: PlatformProfile | None = None,
    position: PositionConfig | None = None,
    schema: RuleSchema | None = None,
    cfg: CpmConfig | None = None,
) -> dict:
    """Audit every active position-targeting rule set.

    Selection: rule sets that are active, not archived, not templates, not
    deleted and hold at least one position-targeting condition.

    Per rule set:

    * ``guardrail_pct`` = the **maximum** guardrail percentage across its
      rules (most permissive); ``guardrail_type``, ``min_bid``, ``max_bid``
      last non-empty wins; ``target`` = ``"min–max"`` or ``"<= N"``.
    * members = its bids (a campaign rule set contributes every bid of its
      campaigns); negative and archived bids are excluded.  A pair is
      (campaign, normalised keyword).
    * ``a`` / ``b`` = sum of the pair cells in window A / B over keyword
      rows; ``n_keywords``, ``n_entities``, ``n_live`` (pairs with A spend),
      ``overlap`` (pairs also held by another audited rule set).
    * ``up`` = median over members of ``live_bid / base_bid`` (both > 0).
    * ``ceiling_multiplier`` = ``1 + guardrail_pct / 100`` in percentage mode.
    * ``cap, hit, seen`` = decision-weighted rates over its rules' decisions
      in window A (local time).
    * ``blind = seen < cfg.blind_seen``;
      ``hot = not blind and cap >= cfg.hot_cap and hit < cfg.low_hit``
      (a blind rule set is never hot).
    * ``owner`` = the rule set's advertiser, or for an advertiser listed in
      ``umbrella_advertisers`` (or a missing one) the advertiser with the
      most A + B spend among its pairs.

    Rows are ordered by A spend desc, B spend desc, name; ``advertiser_id``
    keeps one owner's rows and everything downstream follows that filter.

    Band roll-up: each pair is counted once, under the band of its most
    permissive holder still in scope (largest percentage among percentage-
    type holders; other holders rank below all of them).  Per band:
    ``n`` rule sets, ``pairs``, decision-weighted rates over those rule
    sets' rules, ``a`` / ``b`` over the band's pairs.  Bands with no rule
    set are omitted.  ``total`` is the same over everything in scope.

    Returns ``{rows: [...], bands: [...], total: {...}}``; see the row keys
    above plus ``ruleset_id, name, advertiser_id, start, rule_ids, members,
    n_decisions, band``.
    """
    cfg = cfg or CpmConfig()
    schema = schema or RuleSchema()
    lower = position.lower_is_better if position is not None else (
        profile.position.lower_is_better if profile is not None else True)
    if tz is None and profile is not None:
        tz = profile.timezone
    umbrella = set(umbrella_advertisers)

    rules_by_rs: dict[str, list[Rule]] = defaultdict(list)
    for r in rules:
        rules_by_rs[str(r.ruleset_id)].append(r)
    bid_by_id = {str(bd.bid_id): bd for bd in bids if bd.bid_id is not None}
    bids_by_entity: dict[str, list[BidRecord]] = defaultdict(list)
    for bd in bids:
        bids_by_entity[bd.entity_id].append(bd)
    mem_by_rs: dict[str, list[str]] = defaultdict(list)
    for m in dedupe_members(members):
        mem_by_rs[str(m.ruleset_id)].append(str(m.member_id))

    # --- select rule sets and summarise their configuration -----------------
    selected: list[dict] = []
    for rs in select_rulesets(rulesets, "active"):
        gp: float | None = None
        gt = target = minb = maxb = None
        rule_ids: list[str] = []
        for r in rules_by_rs.get(str(rs.id), []):
            conds = position_conditions(r.conditions, schema)
            if not conds:
                continue
            rule_ids.append(str(r.id))       # ids are compared as strings everywhere (stores and logs differ in type)
            for c in conds:
                pol = guardrail_policy(c, schema)
                if pol.percentage is not None:
                    gp = pol.percentage if gp is None else max(gp, pol.percentage)
                gt = c.get(schema.guardrail_type_key) or gt
                t = c.get(schema.position_target_key)
                if t is not None:
                    target = (f"{_g(t.get('min'))}–{_g(t.get('max'))}" if isinstance(t, Mapping) else f"<= {_g(t)}")
                minb = pol.min_bid if pol.min_bid is not None else minb
                maxb = pol.max_bid if pol.max_bid is not None else maxb
        if not rule_ids:
            continue
        member_bids: list[BidRecord] = []
        for mid in mem_by_rs.get(str(rs.id), []):
            if rs.entity_type == "campaign":
                member_bids.extend(bids_by_entity.get(mid, []))
            elif mid in bid_by_id:
                member_bids.append(bid_by_id[mid])
        member_bids = [bd for bd in member_bids if not bd.is_negative and not bd.is_archived]
        selected.append({
            "rs": rs, "rule_ids": rule_ids, "gp": gp, "gt": gt, "target": target, "minb": minb, "maxb": maxb,
            "bids": member_bids,
            "pairs": list(dict.fromkeys((bd.entity_id, norm_kw(bd.targeting_unit)) for bd in member_bids)),
        })

    # --- pair performance ---------------------------------------------------
    all_pairs = {p for s in selected for p in s["pairs"]}
    kw_type = cfg.keyword_targeting_type
    pa: dict[tuple[str, str], list[PerfRow]] = defaultdict(list)
    pb: dict[tuple[str, str], list[PerfRow]] = defaultdict(list)
    adv_spend: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for r in perf_rows:
        in_a = in_window(r.date, a)
        in_b = b is not None and in_window(r.date, b)
        if not (in_a or in_b) or not is_keyword_row(r, profile, kw_type):
            continue
        key = (r.entity_id, norm_kw(r.targeting_unit))
        if key not in all_pairs:
            continue
        if in_a:
            pa[key].append(r)
        if in_b:
            pb[key].append(r)
        if r.advertiser_id is not None:
            adv_spend[key][r.advertiser_id] += r.spend
    pos_cfg = position_config(profile, position) if (profile is not None or position is not None) else None
    cell_a = {k: cell_from_rows(v, pos_cfg) for k, v in pa.items()}
    cell_b = {k: cell_from_rows(v, pos_cfg) for k, v in pb.items()}
    spent = {k for k in all_pairs if cell_a.get(k, empty_cell()).spend > 0 or cell_b.get(k, empty_cell()).spend > 0}

    counts = decision_counts(
        decisions, window=a, by=("rule_id",), tz=tz, log_from=log_from,
        position_reported=position_reported, lower_is_better=lower, cfg=cfg, ids_as_str=True,
    )

    def rates(rule_ids: Iterable[str]) -> dict[str, Any]:
        return decision_rates(counts[r] for r in rule_ids if r in counts)

    # --- rows -----------------------------------------------------------------
    rows: list[dict] = []
    for s in selected:
        rs: Ruleset = s["rs"]
        pairs = s["pairs"]
        owner = rs.advertiser_id
        if owner is None or owner in umbrella:
            tot: dict[str, float] = defaultdict(float)
            for p in pairs:
                for adv, sp in adv_spend.get(p, {}).items():
                    tot[adv] += sp
            if tot:
                owner = sorted(tot.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        ups = [bd.live_bid / bd.base_bid for bd in s["bids"]
               if bd.live_bid is not None and bd.base_bid is not None and bd.live_bid > 0 and bd.base_bid > 0]
        pct_mode = s["gt"] == schema.percentage_type and bool(s["gp"])
        rt = rates(s["rule_ids"])
        blind, hot = _flags(rt, cfg)
        ca = _sum(cell_a[p] for p in pairs if p in spent and p in cell_a)
        cb = _sum(cell_b[p] for p in pairs if p in spent and p in cell_b)
        rows.append({
            "ruleset_id": rs.id, "name": rs.name, "advertiser_id": rs.advertiser_id, "owner": owner,
            "start": rs.start_date, "rule_ids": list(s["rule_ids"]),
            "no_decisions": rt["n"] == 0,       # an active rule set with no matched decision: not "healthy", unmeasured
            "guardrail_pct": s["gp"], "guardrail_type": s["gt"], "target": s["target"],
            "min_bid": s["minb"], "max_bid": s["maxb"],
            "members": [
                {"entity_id": bd.entity_id, "keyword": norm_kw(bd.targeting_unit), "bid_id": bd.bid_id,
                 "live_bid": bd.live_bid, "base_bid": bd.base_bid}
                for bd in s["bids"]
            ],
            "a": cell_dict(ca), "b": cell_dict(cb),
            "n_keywords": len({k for _, k in pairs}), "n_entities": len({e for e, _ in pairs}),
            "n_live": sum(1 for p in pairs if cell_a.get(p, empty_cell()).spend > 0),
            "overlap": 0,
            "up": median(ups),
            "ceiling_multiplier": (1.0 + s["gp"] / 100.0) if pct_mode else None,
            "cap": rt["cap"], "hit": rt["hit"], "seen": rt["seen"], "in_band": rt["in_band"],
            "overshoot": rt["overshoot"], "n_decisions": rt["n"],
            "blind": blind, "hot": hot,
            "band": guardrail_band(s["gp"], s["gt"], cfg, percentage_type=schema.percentage_type),
            "_pairs": pairs, "_rank": s["gp"] if pct_mode else -1.0,
        })
    holders_all: dict[tuple[str, str], set[Any]] = defaultdict(set)
    for row in rows:
        for p in row["_pairs"]:
            holders_all[p].add(row["ruleset_id"])
    for row in rows:
        row["overlap"] = sum(1 for p in row["_pairs"] if len(holders_all[p]) > 1)
    rows.sort(key=lambda d: (-d["a"]["spend"], -d["b"]["spend"], str(d["name"])))
    if advertiser_id is not None:
        rows = [d for d in rows if d["owner"] == advertiser_id]

    # --- bands: each pair once, under its most permissive holder in scope -----
    best: dict[tuple[str, str], dict] = {}
    for row in rows:
        for p in row["_pairs"]:
            cur = best.get(p)
            if cur is None or row["_rank"] > cur["_rank"]:
                best[p] = row
    order = band_labels(cfg) + [ABSOLUTE_BAND]
    bands = []
    for label in order:
        rs_rows = [d for d in rows if d["band"] == label]
        if not rs_rows:
            continue
        pairs = [p for p, holder in best.items() if holder["band"] == label]
        rt = rates(rid for d in rs_rows for rid in d["rule_ids"])
        bands.append({
            "band": label, "n": len(rs_rows), "pairs": len(pairs),
            "cap": rt["cap"], "hit": rt["hit"], "seen": rt["seen"], "n_decisions": rt["n"],
            "a": cell_dict(_sum(cell_a[p] for p in pairs if p in spent and p in cell_a)),
            "b": cell_dict(_sum(cell_b[p] for p in pairs if p in spent and p in cell_b)),
        })
    rt = rates(rid for d in rows for rid in d["rule_ids"])
    total = {
        "n": len(rows), "pairs": len(best),
        "cap": rt["cap"], "hit": rt["hit"], "seen": rt["seen"], "n_decisions": rt["n"],
        "a": cell_dict(_sum(cell_a[p] for p in best if p in spent and p in cell_a)),
        "b": cell_dict(_sum(cell_b[p] for p in best if p in spent and p in cell_b)),
    }
    for row in rows:
        del row["_pairs"], row["_rank"]
    return {"rows": rows, "bands": bands, "total": total}


def _g(x: Any) -> str:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return str(int(x)) if float(x) == int(x) else f"{float(x):g}"
    return str(x)


def ruleset_members(
    row: Mapping[str, Any],
    decisions: Sequence[BidDecision],
    perf_rows: Sequence[PerfRow],
    a: Window,
    b: Window | None = None,
    *,
    entities: Iterable[Entity] = (),
    tz: Any = None,
    log_from: date | None = None,
    position_reported: bool = True,
    profile: PlatformProfile | None = None,
    position: PositionConfig | None = None,
    cfg: CpmConfig | None = None,
) -> dict:
    """Per-member drill of one audited rule set (a row of ``audit_guardrails``).

    Per member bid: keyword, campaign, live and base bid, ``up = live /
    base`` (``None`` when the base is missing or <= 0), cells for A and B of
    its (campaign, keyword) pair, and the bid's own ``cap``, ``hit``,
    ``seen`` rates with counters summed across the rule set's rules (a bid
    can sit under several rules).  Sorted by A spend desc, B spend desc,
    keyword.

    Returns ``{ruleset: row, members: [{keyword, entity_id, entity_name,
    entity_status, bid_id, bid, base, up, a, b, cap, hit, seen, n}]}``.
    """
    cfg = cfg or CpmConfig()
    lower = position.lower_is_better if position is not None else (
        profile.position.lower_is_better if profile is not None else True)
    if tz is None and profile is not None:
        tz = profile.timezone
    ent = {e.entity_id: e for e in entities}
    rule_ids = {str(r) for r in row["rule_ids"]}
    if profile is not None or position is not None:
        position = position_config(profile, position)
    counts = decision_counts(
        (d for d in decisions if str(d.rule_id) in rule_ids), window=a, by=("rule_id", "bid_id"), tz=tz,
        log_from=log_from, position_reported=position_reported, lower_is_better=lower, cfg=cfg, ids_as_str=True,
    )
    per_bid: dict[Any, list[DecisionCounts]] = defaultdict(list)
    for (_, bid_id), c in counts.items():
        per_bid[bid_id].append(c)
    pairs = {(m["entity_id"], m["keyword"]) for m in row["members"]}
    pa: dict[tuple[str, str], list[PerfRow]] = defaultdict(list)
    pb: dict[tuple[str, str], list[PerfRow]] = defaultdict(list)
    for r in perf_rows:
        if not is_keyword_row(r, profile, cfg.keyword_targeting_type):
            continue
        key = (r.entity_id, norm_kw(r.targeting_unit))
        if key not in pairs:
            continue
        if in_window(r.date, a):
            pa[key].append(r)
        if b is not None and in_window(r.date, b):
            pb[key].append(r)
    out = []
    for m in row["members"]:
        key = (m["entity_id"], m["keyword"])
        rt = decision_rates(per_bid.get(None if m["bid_id"] is None else str(m["bid_id"]), []))
        live, base = m["live_bid"], m["base_bid"]
        e = ent.get(m["entity_id"])
        out.append({
            "keyword": m["keyword"], "entity_id": m["entity_id"],
            "entity_name": e.name if e else None, "entity_status": e.status if e else None,
            "bid_id": m["bid_id"], "bid": live, "base": base,
            "up": (live / base) if (live is not None and base is not None and base > 0) else None,
            "a": cell_dict(cell_from_rows(pa.get(key, []), position)),
            "b": cell_dict(cell_from_rows(pb.get(key, []), position)),
            "cap": rt["cap"], "hit": rt["hit"], "seen": rt["seen"], "n": rt["n"],
        })
    out.sort(key=lambda d: (-d["a"]["spend"], -d["b"]["spend"], d["keyword"]))
    return {"ruleset": dict(row), "members": out}


def live_state(
    states: Iterable[Any],
    as_of: Any,
    *,
    cfg: CpmConfig | None = None,
) -> dict[Any, dict]:
    """Live state of the bidder per rule set, from its latest state rows.

    ``states`` are mappings or objects with ``ruleset_id`` (or ``rule_id``),
    ``bid_id``, ``geo``, ``position``, ``bid`` and ``checked_at`` (a
    datetime).  Only rows checked within ``cfg.live_state_days`` (3) days
    before ``as_of`` count.  Per rule set: number of distinct bids and geos,
    median position over found positions (1..60), ``found`` = share of rows
    with a found position, median bid and the last check time.

    Returns ``{ruleset_id: {bids, geos, position_median, found, bid_median,
    last_check}}``.
    """
    from datetime import timedelta

    cfg = cfg or CpmConfig()
    cutoff = as_of - timedelta(days=cfg.live_state_days)
    groups: dict[Any, list[Any]] = defaultdict(list)
    for s in states:
        at = get(s, "checked_at")
        if at is None or at < cutoff or at > as_of:
            continue
        key = get(s, "ruleset_id")
        groups[key if key is not None else get(s, "rule_id")].append(s)
    out: dict[Any, dict] = {}
    for key, rows in groups.items():
        found = [get(s, "position") for s in rows if position_seen(get(s, "position"), cfg)]
        bid_values = [get(s, "bid") for s in rows if get(s, "bid") is not None]
        out[key] = {
            "bids": len({get(s, "bid_id") for s in rows}),
            "geos": len({get(s, "geo") for s in rows if get(s, "geo") is not None}),
            "position_median": median(found),
            "found": len(found) / len(rows),
            "bid_median": median(bid_values),
            "last_check": max(get(s, "checked_at") for s in rows),
        }
    return out
