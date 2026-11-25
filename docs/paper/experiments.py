"""Reproducible experiments for the cpm-lineage paper.

Everything here runs on synthetic data with planted events.  No real account
data is involved, so the numbers say what the library does on worlds whose
ground truth is known by construction; they are not evidence about any real
platform or advertiser.

Run from the repository root:

    pip install -e .
    python docs/paper/experiments.py                # all experiments, default seeds
    python docs/paper/experiments.py --seeds 10     # quicker

Output: ``docs/paper/results.json`` (read by ``build_paper.py``) and a short
summary on stdout.  Seeds are the integers 1..N (default N = 30); the figure
run uses the library's default seed 7.

Experiments
-----------
A  detection of the five planted events (first-price marketplace world)
B  null world: the same generator with every planted event removed
C  dose-response: detection rate against the size of each planted event
D  exactness residuals of the decompositions
E  determinism: repeated runs and shuffled input order
F  a second, differently shaped platform (second-price, clicks, no position)
G  saturation-curve parameter recovery on planted concave curves
H  the conflict table's stake-ratio valve
I  lever-attribution verdicts on the entity that carries the planted CPM spike

The generator exposes its event sizes as module constants.  Experiments B and
C change those constants for the duration of one ``generate`` call and restore
them afterwards (``patched``); nothing in the installed library is edited.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
import math
import random
import statistics
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from cpm_lineage import (Economics, EngineConfig, PipelineConfig, decompose, diagnose, identity,
                         make_card, plan)
from cpm_lineage.actions.resolve import resolve_budget_conflicts
from cpm_lineage.adapters import synthetic as syn
from cpm_lineage.budget import saturation

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIGURE_SEED = syn.DEFAULT_SEED          # 7
NO_HOLDOUT = PipelineConfig(holdout_pct=0)


# --------------------------------------------------------------------------- helpers

@contextlib.contextmanager
def patched(**values: Any) -> Iterator[None]:
    """Temporarily set module constants of the synthetic generator."""
    old = {k: getattr(syn, k) for k in values}
    try:
        for k, v in values.items():
            setattr(syn, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(syn, k, v)


def campaigns_with(*, winner_budget_factor: float | None = None, bleed_roas: float | None = None,
                   drop_overlap: bool = False) -> tuple[dict[str, Any], ...]:
    """A copy of the generator's campaign table with event sizes changed."""
    out = []
    for camp in copy.deepcopy(syn.CAMPAIGNS):
        if drop_overlap and camp["id"] == "cmp-overlap":
            continue
        if winner_budget_factor is not None and camp["id"] == "cmp-winner":
            camp["budget_factor"] = winner_budget_factor
            if winner_budget_factor >= 1.0:
                camp["status"] = "ACTIVE"
        if bleed_roas is not None and camp["id"] == "cmp-conquest":
            kws = []
            for kw in camp["keywords"]:
                if kw[0] == "rivalb bar":
                    price = 240.0                      # sku-a1, the product this keyword sells
                    icr = bleed_roas * kw[3] / (1000.0 * price)
                    kw = (kw[0], kw[1], kw[2], kw[3], icr, kw[5], kw[6])
                kws.append(kw)
            camp["keywords"] = tuple(kws)
        out.append(camp)
    return tuple(out)


def planted_hits(dg: Any) -> dict[str, bool]:
    """Strict match of each planted event against the typed findings."""
    hit = dict.fromkeys(("cpm_spike", "availability_drop", "budget_capped_winner",
                         "bleeding_keyword", "own_overlap"), False)
    for f in dg.findings:
        if f.kind == "cpm_rise" and f.targeting_unit == "protein bar" and "cmp-generic" in f.entity_ids:
            hit["cpm_spike"] = True
        elif f.kind == "availability_drop" and f.geo == "geo-south" and f.product_id == "sku-a2":
            hit["availability_drop"] = True
        elif f.kind == "budget_bound_winner" and "cmp-winner" in f.entity_ids:
            hit["budget_capped_winner"] = True
        elif f.kind == "bleeding_keyword" and f.targeting_unit == "rivalb bar":
            hit["bleeding_keyword"] = True
        elif f.kind == "own_collision" and f.targeting_unit == "energy bar":
            hit["own_overlap"] = True
    return hit


def is_planted(f: Any) -> bool:
    return ((f.kind == "cpm_rise" and f.targeting_unit == "protein bar")
            or (f.kind == "availability_drop" and f.geo == "geo-south" and f.product_id == "sku-a2")
            or (f.kind == "budget_bound_winner" and "cmp-winner" in f.entity_ids)
            or (f.kind == "bleeding_keyword" and f.targeting_unit == "rivalb bar")
            or (f.kind == "own_collision" and f.targeting_unit == "energy bar"))


def card_kws(card: Any) -> list[str]:
    return [k.kw for k in (card.keywords or [])]


def plan_hits(p: Any) -> dict[str, bool]:
    """Does the plan carry an ACT card that addresses each planted event?"""
    hit = dict.fromkeys(("cpm_spike", "budget_capped_winner", "bleeding_keyword", "own_overlap"), False)
    for c in p.actions:
        if not (c.verdict and c.verdict.get("state") == "ACT"):
            continue
        if c.entity_id == "cmp-winner" and c.type == "budget" and c.direction == "up":
            hit["budget_capped_winner"] = True
        if c.entity_id == "cmp-conquest" and c.direction == "down" and "rivalb bar" in card_kws(c):
            hit["bleeding_keyword"] = True
        if c.family == "collision" and "energy bar" in card_kws(c):
            hit["own_overlap"] = True
        if c.family == "bid" and c.entity_id == "cmp-generic" and "protein bar" in card_kws(c):
            hit["cpm_spike"] = True
    return hit


def card_label(c: Any) -> str:
    """``entity: title -> lane`` for tallying what a plan contains."""
    return f"{c.entity_id}: {c.title} -> {(c.verdict or {}).get('lane')}"


def rate(flags: list[bool]) -> dict[str, Any]:
    return {"hits": int(sum(flags)), "n": len(flags), "rate": sum(flags) / len(flags) if flags else None}


def digest(dg: Any, p: Any) -> str:
    """Hash of everything a reader sees: findings, identity table, the cards and their verdicts."""
    payload = {
        "findings": [(f.kind, f.summary, f.source, list(f.entity_ids)) for f in dg.findings],
        "layer1": {k: round(v.log_points, 9) for k, v in dg.layer1.channels.items()},
        "entities": {k: (e.attribution.verdict if e.attribution else None,
                         e.attribution.top_driver if e.attribution else None)
                     for k, e in sorted(dg.entities.items())},
        "plan_id": p.id,
        "cards": [(c.id, c.family, c.type, c.direction, c.entity_id, c.status, c.confidence,
                   (c.verdict or {}).get("lane"), round(c.stake, 2)) for c in p.actions],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# --------------------------------------------------------------------------- A, B, C

def exp_detection(seeds: list[int]) -> dict[str, Any]:
    per_event: dict[str, list[bool]] = {}
    per_plan: dict[str, list[bool]] = {}
    extra: list[int] = []
    extra_kinds: Counter[str] = Counter()
    cpm_kind: Counter[str] = Counter()
    states: Counter[str] = Counter()
    n_cards: list[int] = []
    tally: dict[str, Counter[str]] = {"classic": Counter(), "verdict": Counter()}
    rows = 0
    for s in seeds:
        data = syn.generate(seed=s)
        rows = len(data.perf)
        dg = diagnose(data)
        for k, v in planted_hits(dg).items():
            per_event.setdefault(k, []).append(v)
        others = [f for f in dg.findings if not is_planted(f)]
        extra.append(len(others))
        extra_kinds.update(f.kind for f in others)
        for f in dg.findings:
            if f.kind == "cpm_rise" and f.targeting_unit == "protein bar":
                cpm_kind[str(f.evidence.get("kind"))] += 1
        p = plan(data, pipeline_cfg=NO_HOLDOUT)
        for k, v in plan_hits(p).items():
            per_plan.setdefault(k, []).append(v)
        states.update(p.verdict_states or {})
        n_cards.append(len(p.actions))
        tally["classic"].update(card_label(c) for c in p.actions)
        pv = plan(data, pipeline_cfg=NO_HOLDOUT, rule_profile="verdict")
        tally["verdict"].update(card_label(c) for c in pv.actions)
    return {
        "seeds": seeds, "perf_rows_per_dataset": rows,
        "cards_by_profile": {k: dict(v.most_common()) for k, v in tally.items()},
        "findings": {k: rate(v) for k, v in per_event.items()},
        "plan_act_card": {k: rate(v) for k, v in per_plan.items()},
        "extra_findings_total": int(sum(extra)), "extra_findings_by_kind": dict(extra_kinds),
        "cpm_rise_classification": dict(cpm_kind),
        "plan_cards_mean": statistics.mean(n_cards), "plan_verdict_states": dict(states),
    }


def null_patch() -> dict[str, Any]:
    return dict(_SPIKE_MULT=1.0, _SPIKE_RIVAL_MULT=1.0, _AVAIL_DROP=syn._AVAIL_BASE,
                CAMPAIGNS=campaigns_with(winner_budget_factor=1.8, bleed_roas=2.5, drop_overlap=True))


def exp_null(seeds: list[int]) -> dict[str, Any]:
    kinds: Counter[str] = Counter()
    summaries: Counter[str] = Counter()
    datasets_with_any = 0
    cards: Counter[str] = Counter()
    for s in seeds:
        with patched(**null_patch()):
            data = syn.generate(seed=s)
        dg = diagnose(data)
        kinds.update(f.kind for f in dg.findings)
        summaries.update(f"{f.kind}:{f.targeting_unit or f.product_id or ','.join(f.entity_ids)}" for f in dg.findings)
        datasets_with_any += bool(dg.findings)
        p = plan(data, pipeline_cfg=NO_HOLDOUT)
        cards.update(f"{c.family}/{c.type}/{c.direction}:{(c.verdict or {}).get('state')}" for c in p.actions)
    return {"seeds": seeds, "findings_by_kind": dict(kinds), "findings_by_subject": dict(summaries),
            "datasets_with_any_finding": datasets_with_any, "n": len(seeds), "plan_cards": dict(cards)}


def exp_dose(seeds: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {"seeds": seeds}

    def sweep(event: str, levels: list[float], make_patch) -> list[dict[str, Any]]:
        res = []
        for lv in levels:
            flags = []
            for s in seeds:
                with patched(**make_patch(lv)):
                    data = syn.generate(seed=s)
                flags.append(planted_hits(diagnose(data))[event])
            res.append({"level": lv, **rate(flags)})
        return res

    out["cpm_spike_multiplier"] = sweep(
        "cpm_spike", [1.0, 1.03, 1.05, 1.08, 1.10, 1.20, 1.40, 1.60],
        lambda m: dict(_SPIKE_MULT=m))
    out["availability_level_after_drop"] = sweep(
        "availability_drop", [0.95, 0.92, 0.90, 0.85, 0.75, 0.60, 0.45],
        lambda a: dict(_AVAIL_DROP=a))
    out["bleed_keyword_true_roas"] = sweep(
        "bleeding_keyword", [0.35, 0.70, 0.90, 1.00, 1.20, 1.50],
        lambda r: dict(CAMPAIGNS=campaigns_with(bleed_roas=r)))
    out["winner_budget_factor"] = sweep(
        "budget_capped_winner", [0.33, 0.60, 0.80, 0.90, 1.00, 1.20],
        lambda b: dict(CAMPAIGNS=campaigns_with(winner_budget_factor=b)))
    return out


# --------------------------------------------------------------------------- D

def exp_exactness(seeds: list[int]) -> dict[str, Any]:
    res: dict[str, list[float]] = {}

    def add(name: str, value: float | None) -> None:
        if value is not None:
            res.setdefault(name, []).append(abs(float(value)))

    for s in seeds:
        data = syn.generate(seed=s)
        dg = diagnose(data)
        w = dg.windows
        add("log identity, account", dg.layer1.residual)
        for e in dg.entities.values():
            if e.layer1.exact:
                add("log identity, per entity", e.layer1.residual)
        add("shift-share by ad type", dg.ad_type_mix.residual)
        for label, key in (("shift-share by entity", "entity_id"),
                           ("shift-share by entity x targeting unit", ("entity_id", "targeting_unit")),
                           ("shift-share by geo", "geo"), ("shift-share by product", "product_id")):
            add(label, decompose.shift_share_rows(data.perf, w, key).residual)
        price = decompose.shift_share_rows(data.perf, w, "product_id", value="sales", weight="units",
                                           one_sided="carry")
        add("price mix / own-price split (units as weight)", price.residual)
        cpm = decompose.shift_share_rows(data.perf, w, "targeting_unit", value="spend", weight="impressions")
        add("cost-per-impression shift-share by targeting unit", cpm.residual)
        b, t = dg.layer1.baseline, dg.layer1.comparison
        u = identity.unit_decomposition(b.impressions, b.units, t.impressions, t.units)
        add("units = impressions x conversion (prior-rate split)", u.volume_effect + u.rate_effect - (t.units - b.units))
        v = identity.value_decomposition(b.units, b.asp, t.units, t.asp)
        add("value = units x price (prior-rate split)", v.volume_effect + v.rate_effect - (t.sales - b.sales))
        c = dg.composition
        if c is not None:
            add("entry / exit composition (mix term is the remainder)",
                c.cont_rate + c.cont_mix + c.entrants + c.exits - (c.roas_current - c.roas_prior))

    # randomised stress: arbitrary positive windows and item lists with one-sided items
    rng = np.random.default_rng(20260101)
    stress_l1, stress_ss = [], []
    for _ in range(20000):
        sp, sa, im, un = (float(x) for x in rng.lognormal(8, 3, size=4))
        sp2, sa2, im2, un2 = (float(x) for x in rng.lognormal(8, 3, size=4))
        r = identity.log_decomposition({"spend": sp, "sales": sa, "impressions": im, "units": un},
                                       {"spend": sp2, "sales": sa2, "impressions": im2, "units": un2})
        stress_l1.append(abs(r.residual))
    for _ in range(5000):
        n = int(rng.integers(2, 40))
        items = []
        for i in range(n):
            wp, wc = (float(x) for x in rng.lognormal(6, 2, size=2))
            if rng.random() < 0.15:
                wp = 0.0
            elif rng.random() < 0.15:
                wc = 0.0
            items.append({"key": f"i{i}", "weight_prior": wp, "value_prior": wp * float(rng.lognormal(0, 1)),
                          "weight_current": wc, "value_current": wc * float(rng.lognormal(0, 1))})
        ss = decompose.shift_share(items)
        scale = max(abs(ss.rate_prior), abs(ss.rate_current), 1e-12)
        stress_ss.append(abs(ss.residual) / scale)
    table = [{"decomposition": k, "n": len(v), "max_abs_residual": max(v), "median_abs_residual": statistics.median(v)}
             for k, v in res.items()]
    table.append({"decomposition": "log identity, 20,000 random window pairs (lognormal sums, sigma 3)",
                  "n": len(stress_l1), "max_abs_residual": max(stress_l1),
                  "median_abs_residual": statistics.median(stress_l1)})
    table.append({"decomposition": "shift-share, 5,000 random item lists, 15% one-sided items (relative to the blended rate)",
                  "n": len(stress_ss), "max_abs_residual": max(stress_ss),
                  "median_abs_residual": statistics.median(stress_ss)})
    return {"seeds": seeds, "rows": table}


# --------------------------------------------------------------------------- E

def shuffled(data: Any, seed: int) -> Any:
    rnd = random.Random(seed)
    out = copy.copy(data)
    for name in ("perf", "entities", "targeting_units", "products", "market", "offtake", "shelf",
                 "budget_days", "change_events"):
        rows = getattr(data, name, None)
        if rows:
            rows = list(rows)
            rnd.shuffle(rows)
            setattr(out, name, rows)
    return out


def exp_determinism(seeds: list[int]) -> dict[str, Any]:
    repeat_same = shuffle_same = shuffle_plan_id_same = shuffle_findings_same = 0
    diffs: list[str] = []
    for s in seeds:
        d1, d2 = syn.generate(seed=s), syn.generate(seed=s)
        h1 = digest(diagnose(d1), plan(d1))
        h2 = digest(diagnose(d2), plan(d2))
        repeat_same += h1 == h2
        d3 = shuffled(d1, 1000 + s)
        dg1, dg3 = diagnose(d1), diagnose(d3)
        p1, p3 = plan(d1), plan(d3)
        shuffle_same += digest(dg1, p1) == digest(dg3, p3)
        shuffle_plan_id_same += p1.id == p3.id
        f1 = sorted((f.kind, f.summary) for f in dg1.findings)
        f3 = sorted((f.kind, f.summary) for f in dg3.findings)
        shuffle_findings_same += f1 == f3
        if f1 != f3 and len(diffs) < 3:
            diffs.append(f"seed {s}: {sorted(set(f1) ^ set(f3))[:2]}")
    n = len(seeds)
    return {"seeds": seeds, "n": n, "repeat_identical_digest": repeat_same,
            "shuffled_identical_digest": shuffle_same, "shuffled_identical_plan_id": shuffle_plan_id_same,
            "shuffled_identical_findings": shuffle_findings_same, "example_differences": diffs}


# --------------------------------------------------------------------------- F

def load_second_platform() -> Any:
    path = ROOT / "examples" / "custom_platform_adapter.py"
    spec = importlib.util.spec_from_file_location("custom_platform_adapter", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def exp_second_platform(seeds: list[int]) -> dict[str, Any]:
    mod = load_second_platform()
    cfg = EngineConfig(economics=Economics(currency_unit=0.01))
    cpm_hit, bud_hit, extra, resid = [], [], [], []
    act_raise: dict[str, list[bool]] = {"classic": [], "verdict": []}
    lanes: dict[str, Counter[str]] = {"classic": Counter(), "verdict": Counter()}
    roas: dict[str, list[float]] = {}
    extra_subjects: Counter[str] = Counter()
    cpm_kind: Counter[str] = Counter()
    rows = 0
    notes: Counter[str] = Counter()
    for s in seeds:
        data = mod.StreamshopAdapter(mod.fetch_report(seed=s)).dataset
        rows = len(data.perf)
        dg = diagnose(data, config=cfg)
        c_hit = b_hit = False
        n_extra = 0
        for f in dg.findings:
            if f.kind == "cpm_rise" and f.targeting_unit == "trail shoes":
                c_hit = True
                cpm_kind[str(f.evidence.get("kind"))] += 1
            elif f.kind == "budget_bound_winner" and "li-hero" in f.entity_ids:
                b_hit = True
            else:
                n_extra += 1
                extra_subjects[f"{f.kind}:{f.targeting_unit or ','.join(f.entity_ids)}"] += 1
        cpm_hit.append(c_hit); bud_hit.append(b_hit); extra.append(n_extra)
        resid.append(abs(dg.layer1.residual))
        notes.update(dg.notes)
        for prof in ("classic", "verdict"):
            p = plan(data, config=cfg, rule_profile=prof, pipeline_cfg=NO_HOLDOUT)
            act_raise[prof].append(any(c.entity_id == "li-hero" and c.type == "budget" and c.direction == "up"
                                       and (c.verdict or {}).get("state") == "ACT" for c in p.actions))
            lanes[prof].update(card_label(c) for c in p.actions)
        for e in dg.entities.values():
            if e.roas is not None:
                roas.setdefault(e.entity_id, []).append(e.roas)
    return {"seeds": seeds, "perf_rows_per_dataset": rows,
            "cpm_rise_trail_shoes": rate(cpm_hit), "budget_bound_li_hero": rate(bud_hit),
            "plan_act_raise_li_hero": {k: rate(v) for k, v in act_raise.items()},
            "median_entity_roas": {k: statistics.median(v) for k, v in roas.items()},
            "extra_findings_total": int(sum(extra)),
            "extra_findings_by_subject": dict(extra_subjects),
            "cpm_rise_classification": dict(cpm_kind), "max_abs_identity_residual": max(resid),
            "cards_by_profile": {k: dict(v.most_common()) for k, v in lanes.items()},
            "diagnosis_notes": dict(notes)}


# --------------------------------------------------------------------------- G

TRUE_B, TRUE_THETA = 20000.0, 4000.0          # marginal-ROAS-1 spend = b - theta = 16000


def curve_points(seed: int, noise: float, n: int = 90) -> tuple[list[float], list[float]]:
    rng = np.random.default_rng(seed)
    spend = rng.uniform(1500.0, 14000.0, size=n)
    sales = TRUE_B * np.log1p(spend / TRUE_THETA) * rng.lognormal(0.0, noise, size=n)
    return [float(x) for x in spend], [float(y) for y in sales]


def exp_curve(seeds: list[int]) -> dict[str, Any]:
    out = []
    true_sat = TRUE_B - TRUE_THETA
    for noise in (0.05, 0.15, 0.30):
        fits, errs, r2s, m_err = 0, [], [], []
        for s in seeds:
            x, y = curve_points(s, noise)
            f = saturation.fit_curve(x, y)
            if not f.fit:
                continue
            fits += 1
            errs.append(abs(saturation.unit_marginal_spend(f) - true_sat) / true_sat)
            r2s.append(f.r2)
            m_true = TRUE_B / (TRUE_THETA + 8000.0)
            m_err.append(abs(saturation.marginal_raw(f, 8000.0) - m_true) / m_true)
        out.append({"noise_sigma": noise, "fitted": fits, "n": len(seeds),
                    "median_rel_err_unit_marginal_spend": statistics.median(errs) if errs else None,
                    "max_rel_err_unit_marginal_spend": max(errs) if errs else None,
                    "median_rel_err_marginal_at_8000": statistics.median(m_err) if m_err else None,
                    "median_r2": statistics.median(r2s) if r2s else None})
    x, y = curve_points(FIGURE_SEED, 0.15)
    f = saturation.fit_curve(x, y)
    figure = {"seed": FIGURE_SEED, "noise_sigma": 0.15, "spend": x, "sales": y, "b": f.b, "theta": f.theta,
              "r2": f.r2, "unit_marginal_spend": saturation.unit_marginal_spend(f),
              "true_b": TRUE_B, "true_theta": TRUE_THETA}
    return {"seeds": seeds, "true_b": TRUE_B, "true_theta": TRUE_THETA, "rows": out, "figure": figure}


# --------------------------------------------------------------------------- H

def exp_valve() -> dict[str, Any]:
    rows = []
    for ratio in (0.5, 1.0, 2.0, 3.0, 3.01, 5.0, 10.0):
        cut = make_card("efficiency-cut", "budget", "Decrease Budget", entity_id="e1", direction="down",
                        change={"pct": -20, "frm": 1000.0, "to": 800.0}, stake=1000.0)
        up = make_card("efficiency-scale", "budget", "Increase Budget", entity_id="e1", direction="up",
                       change={"pct": 25, "frm": 1000.0, "to": 1250.0}, stake=1000.0 * ratio)
        n_sup, n_ten = resolve_budget_conflicts([cut, up])
        rows.append({"opposing_stake_ratio": ratio, "cut_status": cut.status, "raise_status": up.status,
                     "superseded": n_sup, "tension_cards": n_ten})
    return {"winner_family": "efficiency-cut (priority 3.0)", "loser_family": "efficiency-scale (priority 1.5)",
            "valve": PipelineConfig().stake_ratio_valve, "rows": rows}


# --------------------------------------------------------------------------- I

def exp_attribution(seeds: list[int]) -> dict[str, Any]:
    def run(patch: dict[str, Any]) -> dict[str, Any]:
        verdicts: Counter[str] = Counter()
        top: Counter[str] = Counter()
        first: Counter[str] = Counter()
        bid_share = []
        for s in seeds:
            with patched(**patch):
                data = syn.generate(seed=s)
            dg = diagnose(data, entity_ids=["cmp-generic"])
            e = dg.entities["cmp-generic"]
            a = e.attribution
            verdicts[a.verdict] += 1
            top[str(a.top_driver)] += 1
            first[str(e.layer1_verdict.code or e.layer1_verdict.reason)] += 1
            for d in a.drivers:
                if d.lever == "bid" and d.pct is not None:
                    bid_share.append(d.pct)
        return {"verdict": dict(verdicts), "top_lever": dict(top), "first_read": dict(first),
                "median_bid_share_pct": statistics.median(bid_share) if bid_share else None}
    return {"seeds": seeds, "with_spike_1.6x": run({}), "without_spike": run(dict(_SPIKE_MULT=1.0, _SPIKE_RIVAL_MULT=1.0))}


# --------------------------------------------------------------------------- figure run

def figure_run() -> dict[str, Any]:
    data = syn.generate(seed=FIGURE_SEED)
    dg = diagnose(data)
    p = plan(data, pipeline_cfg=NO_HOLDOUT)
    e = dg.entities["cmp-generic"]

    def l1(r: Any) -> dict[str, Any]:
        return {"roas_prior": r.baseline.roas, "roas_current": r.comparison.roas, "raw_dln": r.raw_dln,
                "residual": r.residual,
                "channels": {k: {"log_points": v.log_points, "baseline": v.baseline, "comparison": v.comparison}
                             for k, v in r.channels.items()}}
    a = e.attribution
    return {
        "seed": FIGURE_SEED,
        "windows": [str(dg.windows.prior.start), str(dg.windows.prior.end),
                    str(dg.windows.current.start), str(dg.windows.current.end)],
        "account": l1(dg.layer1), "cmp_generic": l1(e.layer1),
        "attribution": {"verdict": a.verdict, "top": a.top_driver, "explained": a.explained, "skill": a.skill,
                        "move_skill": a.move_skill,
                        "drivers": [{"lever": d.lever, "contrib": d.contrib, "delta_contrib": d.delta_contrib,
                                     "pct": d.pct, "jury": d.jury, "na": d.na} for d in a.drivers]},
        "counts": {"perf_rows": len(data.perf), "entities": len(data.entities), "days": 90,
                   "findings": len(dg.findings), "cards": len(p.actions),
                   "verdict_states": p.verdict_states, "telemetry": p.telemetry},
        "cards": [{"title": c.title, "family": c.family, "entity": c.entity_id, "kw": card_kws(c),
                   "confidence": c.confidence, "lane": (c.verdict or {}).get("lane"), "wave": c.wave}
                  for c in p.actions],
        "findings": [f.summary for f in dg.findings],
    }


# --------------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", type=int, default=30, help="number of seeds (1..N)")
    ap.add_argument("--dose-seeds", type=int, default=15, help="seeds per level in the dose-response sweep")
    ap.add_argument("--out", default=str(HERE / "results.json"))
    args = ap.parse_args()
    seeds = list(range(1, args.seeds + 1))
    dose_seeds = list(range(1, min(args.dose_seeds, args.seeds) + 1))

    import cpm_lineage
    results: dict[str, Any] = {"library_version": cpm_lineage.__version__, "python": sys.version.split()[0],
                               "numpy": np.__version__, "command": "python docs/paper/experiments.py "
                               f"--seeds {args.seeds} --dose-seeds {args.dose_seeds}"}
    steps = [("A_detection", lambda: exp_detection(seeds)), ("B_null", lambda: exp_null(seeds)),
             ("C_dose", lambda: exp_dose(dose_seeds)), ("D_exactness", lambda: exp_exactness(seeds)),
             ("E_determinism", lambda: exp_determinism(seeds[:10])),
             ("F_second_platform", lambda: exp_second_platform(seeds)), ("G_curve", lambda: exp_curve(seeds)),
             ("H_valve", exp_valve), ("I_attribution", lambda: exp_attribution(seeds)),
             ("figure_run", figure_run)]
    for name, fn in steps:
        print(f"running {name} ...", flush=True)
        results[name] = fn()
    Path(args.out).write_text(json.dumps(results, indent=1, default=str) + "\n")

    a = results["A_detection"]
    print("\nA  findings:", {k: f"{v['hits']}/{v['n']}" for k, v in a["findings"].items()},
          "extra:", a["extra_findings_total"])
    print("A  plan ACT cards:", {k: f"{v['hits']}/{v['n']}" for k, v in a["plan_act_card"].items()})
    print("B  null findings:", results["B_null"]["findings_by_subject"])
    for k, v in results["C_dose"].items():
        if k != "seeds":
            print("C ", k, [(r["level"], f"{r['hits']}/{r['n']}") for r in v])
    print("D  max residual:", max(r["max_abs_residual"] for r in results["D_exactness"]["rows"]))
    print("E ", {k: v for k, v in results["E_determinism"].items() if k != "seeds"})
    f = results["F_second_platform"]
    print("F ", f["cpm_rise_trail_shoes"], f["budget_bound_li_hero"], f["plan_act_raise_li_hero"],
          "extra", f["extra_findings_by_subject"])
    print("F  cards:", f["cards_by_profile"])
    print("A  cards:", a["cards_by_profile"])
    print("G ", results["G_curve"]["rows"])
    print("H ", [(r["opposing_stake_ratio"], r["cut_status"], r["raise_status"]) for r in results["H_valve"]["rows"]])
    print("I ", results["I_attribution"]["with_spike_1.6x"], results["I_attribution"]["without_spike"])
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
