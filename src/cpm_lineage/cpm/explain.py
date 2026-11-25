"""Why did CPM rise?  A four-way classifier for one keyword between a
reference period and a current period.

Kinds, first match wins:

``bought position``  position improved by >= 0.3 slots, or own ad share rose
                     by >= 5 points: we paid for a better spot / more share.
``rival pressure``   a rival's ad share rose by >= 3 points.
``worse spot``       position worsened by >= 0.3 slots: paying more for a
                     worse spot (a rival outbid us or our ceiling held).
``auction price``    same spot, higher price: the slot itself got dearer.

Direction matters: "bought position" fires when the position *improved* by
the step, "worse spot" on a worsening alone (no share-of-voice condition).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from ..adapters.base import PlatformProfile
from ..metrics import cell_from_rows
from ..schema import MarketRow, MetricCell, PerfRow, Window
from ._util import in_window, is_keyword_row, norm_kw, position_config
from .config import CpmConfig
from .sov import OWN_KEY, brand_ad_shares, share_of_voice

__all__ = ["KINDS", "PeriodReading", "reading_from_cell", "classify_cpm_move", "explain_cpm_moves"]

KINDS = ("bought position", "rival pressure", "worse spot", "auction price")


@dataclass(frozen=True)
class PeriodReading:
    """One keyword in one period.  ``None`` = not reported (never 0)."""

    cpm: float | None
    impressions: float = 0.0
    position: float | None = None
    sov: float | None = None            # own ad share, fraction 0..1
    roas: float | None = None
    brand_shares: Mapping[str, float] = field(default_factory=dict)  # ad share per brand; own keyed "~own"


def reading_from_cell(
    cell: MetricCell, sov: float | None = None, brand_shares: Mapping[str, float] | None = None
) -> PeriodReading:
    """Build a reading from a metric cell plus optional share-of-voice context."""
    return PeriodReading(
        cpm=cell.cpm, impressions=cell.impressions, position=cell.position, sov=sov,
        roas=cell.roas, brand_shares=dict(brand_shares or {}),
    )


def _fmt_pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def classify_cpm_move(
    reference: PeriodReading | None,
    current: PeriodReading | None,
    cfg: CpmConfig | None = None,
    *,
    lower_is_better: bool = True,
) -> dict | None:
    """Classify why a keyword's CPM rose between two periods.

    Steps (in order):

    1. ``None`` unless both periods exist with impressions > 0 and a CPM.
    2. ``pct = current.cpm / reference.cpm - 1``; ``None`` unless
       ``pct > cfg.explain_min_rise`` (0.05): only rises are explained.
    3. ``dpos`` = change in position expressed so that negative = better
       (``current - reference`` when lower is better, the reverse otherwise);
       0 when either side is missing.  ``dsov = current.sov - reference.sov``
       (0 when missing).
    4. Largest rival gain = ``max(share_cur - share_ref)`` over non-own
       brands seen in either period (missing = 0); it counts only when
       ``>= cfg.explain_rival_gain`` (0.03).
    5. Kind, first match: ``dpos <= -0.3 or dsov >= 0.05`` -> "bought
       position"; rival counts -> "rival pressure"; ``dpos >= 0.3`` ->
       "worse spot"; else "auction price".
    6. When both ROAS exist and the reference is non-zero the sentence gets
       ``ROAS a -> b`` and "(returns fell)" if
       ``current < cfg.explain_roas_fell * reference``.

    Returns ``{reference_cpm, current_cpm, pct, kind, why, position_delta,
    sov_delta, rival, rival_gain, returns_fell}`` or ``None``.
    """
    cfg = cfg or CpmConfig()
    if reference is None or current is None:
        return None
    if not (reference.impressions > 0 and current.impressions > 0):
        return None
    if not reference.cpm or current.cpm is None:
        return None
    pct = current.cpm / reference.cpm - 1.0
    if pct <= cfg.explain_min_rise:
        return None
    dpos = 0.0
    if reference.position is not None and current.position is not None:
        dpos = current.position - reference.position
        if not lower_is_better:
            dpos = -dpos
    dsov = 0.0
    if reference.sov is not None and current.sov is not None:
        dsov = current.sov - reference.sov

    rival: str | None = None
    rival_gain = 0.0
    brands = (set(reference.brand_shares) | set(current.brand_shares)) - {OWN_KEY}
    for b in sorted(brands):
        gain = current.brand_shares.get(b, 0.0) - reference.brand_shares.get(b, 0.0)
        if rival is None or gain > rival_gain:
            rival, rival_gain = b, gain
    rival_text = ""
    if rival is not None and rival_gain >= cfg.explain_rival_gain:
        rival_text = (
            f"{rival} ad share {_fmt_pct(reference.brand_shares.get(rival, 0.0))}"
            f" -> {_fmt_pct(current.brand_shares.get(rival, 0.0))}"
        )

    step = cfg.explain_position_step
    better = dpos <= -step
    more = dsov >= cfg.explain_sov_gain
    if better or more:
        kind = "bought position"
        why = "we paid for a better position / more ad share"
        if rival_text:
            why += f"; {rival_text}"
    elif rival_text:
        kind = "rival pressure"
        why = f"more competition: {rival_text}"
    elif dpos >= step:
        kind = "worse spot"
        why = "paying more for a worse spot: a rival outbid us or our bid ceiling held"
    else:
        kind = "auction price"
        why = "same spot, higher price: the slot itself got dearer"

    returns_fell = False
    if reference.roas is not None and current.roas is not None and reference.roas != 0:
        why += f"; ROAS {reference.roas:.2f} -> {current.roas:.2f}"
        if current.roas < cfg.explain_roas_fell * reference.roas:
            returns_fell = True
            why += " (returns fell)"
    return {
        "reference_cpm": reference.cpm,
        "current_cpm": current.cpm,
        "pct": pct,
        "kind": kind,
        "why": why,
        "position_delta": dpos,
        "sov_delta": dsov,
        "rival": rival if rival_text else None,
        "rival_gain": rival_gain if rival_text else None,
        "returns_fell": returns_fell,
    }


def explain_cpm_moves(
    perf_rows: Sequence[PerfRow],
    reference: Window,
    current: Window,
    *,
    market_rows: Sequence[MarketRow] | None = None,
    keywords: Iterable[str] | None = None,
    focus: Sequence[str] | None = None,
    profile: PlatformProfile | None = None,
    cfg: CpmConfig | None = None,
) -> list[dict]:
    """Explain every risen top keyword of each advertiser.

    Scope: keyword-targeted rows only.  Per advertiser the keyword set is
    ``keywords`` if given, else its ``cfg.top_n`` keywords by spend in the
    ``current`` window.  Each keyword gets a reading per period (CPM,
    impression-weighted position and ROAS from sums; share of voice and
    brand shares from ``market_rows`` when supplied) and is classified with
    ``classify_cpm_move``.  ``focus`` restricts and orders advertisers.

    Returns a list of ``{advertiser_id, keyword, ...classify fields}`` sorted
    by focus order (else advertiser id), then ``pct`` descending.
    """
    cfg = cfg or CpmConfig()
    lower = profile.position.lower_is_better if profile is not None else True
    pos_cfg = position_config(profile) if profile is not None else None   # the profile's validity range
    by: dict[tuple[str | None, str], dict[str, list[PerfRow]]] = defaultdict(lambda: {"ref": [], "cur": []})
    for r in perf_rows:
        if not is_keyword_row(r, profile, cfg.keyword_targeting_type):
            continue
        slot = "ref" if in_window(r.date, reference) else "cur" if in_window(r.date, current) else None
        if slot is None:
            continue
        by[(r.advertiser_id, norm_kw(r.targeting_unit))][slot].append(r)

    wanted = None if keywords is None else {norm_kw(k) for k in keywords}
    advertisers = sorted({a for a, _ in by}, key=lambda a: a or "")
    if focus is not None:
        advertisers = [a for a in focus if a in set(advertisers)]
    out: list[dict] = []
    for order, adv in enumerate(advertisers):
        kws = [k for (a, k) in by if a == adv]
        if wanted is not None:
            kws = [k for k in kws if k in wanted]
        else:
            spend = {k: sum(r.spend for r in by[(adv, k)]["cur"]) for k in kws}
            kws = sorted(kws, key=lambda k: (-spend[k], k))[: cfg.top_n]
        for k in kws:
            ref_cell = cell_from_rows(by[(adv, k)]["ref"], pos_cfg)
            cur_cell = cell_from_rows(by[(adv, k)]["cur"], pos_cfg)
            sov_r = sov_c = None
            shares_r: dict[str, float] = {}
            shares_c: dict[str, float] = {}
            if market_rows:
                sov_r = share_of_voice(market_rows, reference, [k])
                sov_c = share_of_voice(market_rows, current, [k])
                shares_r = brand_ad_shares(market_rows, reference, [k], top=cfg.rivals_top)
                shares_c = brand_ad_shares(market_rows, current, [k], top=cfg.rivals_top)
            res = classify_cpm_move(
                reading_from_cell(ref_cell, sov_r, shares_r),
                reading_from_cell(cur_cell, sov_c, shares_c),
                cfg, lower_is_better=lower,
            )
            if res is None:
                continue
            out.append({"advertiser_id": adv, "keyword": k, "_order": order, **res})
    out.sort(key=lambda d: (d["_order"], -d["pct"], d["keyword"]))
    for d in out:
        del d["_order"]
    return out
