"""The worked example behind docs/how_it_works.md, step by step.

Everything printed here comes from running the library on its seeded
synthetic dataset (``SyntheticAdapter()``, seed 7, 90 days ending
2026-03-31).  The dataset is generated, has five planted events and does not
react to actions, so the numbers show what the library computes; they are
not results from a real account.

Run:

    python docs/guide/worked_example.py              # the seven steps
    python docs/guide/worked_example.py --eval 20    # planted-event recovery over seeds 0..19

The committed copy of the output is docs/guide/worked_example_output.txt.
"""

from __future__ import annotations

import argparse
from dataclasses import fields, replace
from datetime import timedelta

from cpm_lineage import (PipelineConfig, RunCheck, SyntheticAdapter, diagnose, plan, trailing_windows)
from cpm_lineage.actions.families import run_families
from cpm_lineage.actions.primitives import context_from_dataset
from cpm_lineage.adapters.synthetic import DEFAULT_SEED, generate
from cpm_lineage.budget import build_budget_telemetry
from cpm_lineage.outcomes import pair_outcomes
from cpm_lineage.store import ActionLedger

NO_HOLDOUT = PipelineConfig(holdout_pct=0)


def head(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def compact(record) -> str:
    """A record's non-empty fields on one line."""
    parts = []
    for f in fields(record):
        v = getattr(record, f.name)
        if v is None or v == () or v == "":
            continue
        parts.append(f"{f.name}={v!r}" if not hasattr(v, "isoformat") else f"{f.name}={v.isoformat()}")
    return ", ".join(parts)


def card_line(c) -> str:
    step = ""
    if c.change is not None and c.change.pct is not None:
        step = f" {c.change.pct:+.0f}%"
        if c.change.frm is not None and c.change.to is not None:
            step += f" ({c.change.frm:,.0f} -> {c.change.to:,.0f})"
    kws = ", ".join(c.keyword_values()[:2])
    lane = (c.verdict or {}).get("lane", "-")
    return (f"{c.id:<20} {c.family:<16} {c.type:<8} {c.title + step:<46} "
            f"[{c.entity_id}{': ' + kws if kws else ''}]  status={c.status} wave={c.wave or '-'} "
            f"conf={c.confidence} lane={lane}")


def fired(counts: dict) -> str:
    hit = {k: v for k, v in counts.items() if v}
    return ", ".join(f"{k} {v}" for k, v in sorted(hit.items())) or "none"


def steps() -> None:
    data = SyntheticAdapter().dataset
    d = diagnose(data)

    head("INPUT  the feeds (seed %d)" % DEFAULT_SEED)
    for name in ("perf", "entities", "targeting_units", "products", "market", "offtake", "shelf",
                 "budget_days", "change_events"):
        print(f"  {name:<16} {len(getattr(data, name)):>6} rows")
    print("  one PerfRow:      ", compact(data.perf[0]))
    print("  one BudgetDayRow: ", compact(data.budget_days[0]))

    head("STEP 1  identity: which of conversion, price or CPM moved")
    l1 = d.layer1
    print(f"  windows: current {d.windows.current.start} .. {d.windows.current.end}, "
          f"prior {d.windows.prior.start} .. {d.windows.prior.end}")
    print(f"  account ROAS {l1.roas_baseline:.4f} -> {l1.roas_comparison:.4f}   ln ratio {l1.raw_dln:+.4f}")
    for name, ch in l1.channels.items():
        print(f"    {name:<6} {ch.baseline:>9.3f} -> {ch.comparison:>9.3f}   signed log-points {ch.log_points:+.4f}"
              f"   share of move {ch.share_pct:+.1f}%")
    print(f"    sum of log-points {sum(c.log_points for c in l1.channels.values()):+.4f}   exact={l1.exact}")
    v = d.layer1_verdict
    print(f"  first read: code={v.code}  bid change {v.bid_change_pct:+.1f}%  -> {v.text}")
    g = d.entities["cmp-generic"]
    print(f"  cmp-generic: ln ratio {g.layer1.raw_dln:+.4f} = "
          + "  ".join(f"{n} {c.log_points:+.4f}" for n, c in g.layer1.channels.items()))

    head("STEP 2  attribution: which lever (cmp-generic)")
    a = g.attribution
    print(f"  verdict={a.verdict}  ({a.note})")
    print(f"  confidence={a.confidence}")
    print(f"  explained: net {a.explained['net']}%  gross {a.explained['gross']}%  residual {a.explained['residual']}%"
          f"  basis={a.explained['basis']}")
    print(f"  {'lever':<26}{'share':>7}{'level model':>13}{'day-to-day':>12}  {'jury':<8} from -> to")
    for dr in a.drivers:
        if dr.na:
            continue
        ch = dr.change or {}
        print(f"  {dr.lever:<26}{(dr.pct if dr.pct is not None else 0):>6.0f}%{dr.contrib:>13.4f}{dr.delta_contrib:>12.4f}"
              f"  {str(dr.jury):<8} {ch.get('from', float('nan')):.3f} -> {ch.get('to', float('nan')):.3f}")
    na = [dr.lever + (" (folded)" if dr.folded else "") for dr in a.drivers if dr.na]
    print(f"  not modelled: {', '.join(na)}")
    top = a.drivers[0].sizing
    print(f"  sizing of the top lever: {top['lever']} {top['direction']} {100 * top['step']:.0f}% "
          f"-> ROAS {top['d_roas']:+.2f} (estimator {top['estimator']})")
    for act in g.recommendation.actions:
        print(f"  recommendation: {act.verb} {', '.join(act.targets) or '(no target named)'}  [{act.headline}]")

    head("STEP 3  budget, keyword and CPM checks")
    w = d.entities["cmp-winner"]
    p, rv = w.probe, w.raise_verdict
    print(f"  budget probe cmp-winner: cap {p.daily_budget:,.0f}/day, avg spend {p.avg_spend:,.0f}, "
          f"capped {p.days_capped} of {p.act_days} days, exhausts at hour {p.exhaust_hour:.1f}")
    print(f"    marginal ROAS at the cap {p.mroas_raw:.2f} from the {p.mroas_src} curve (r2 {p.sat_r2:.2f}); "
          + (f"own curve not usable: daily spend barely varies (cv {p.spend_cv:.5f})" if p.var_starved
             else f"spend cv {p.spend_cv:.3f}"))
    print(f"    raise verdict: code={rv.code}  verb='{rv.verb}'  range {rv.rec_range[0]:,.0f}-{rv.rec_range[1]:,.0f}/day"
          f"  estimator={rv.estimator}")
    for m in d.cpm_moves:
        print(f"  CPM move '{m['keyword']}': {m['reference_cpm']:.1f} -> {m['current_cpm']:.1f} ({100 * m['pct']:+.0f}%)"
              f"  kind={m['kind']}  position delta {m['position_delta']:+.2f}  own SOV delta {m['sov_delta']:+.2f}"
              f"  rival {m['rival']} gain {m['rival_gain']:+.2f}")
    for c in d.keywords.cut.rows:
        print(f"  keyword cut: '{c.keyword}' role={c.role} spend {c.spend:,.0f} ROAS {c.roas:.2f} verdict={c.verdict}  ({c.why})")
    for f in d.findings:
        print(f"  finding [{f.kind}] from {f.source}: {f.summary}")

    head("STEP 4  rule families propose cards (before any gate)")
    windows = trailing_windows(data.latest_date, 30)
    telemetry = build_budget_telemetry(data.budget_days, anchor=data.latest_date, days=30, entities=data.entities)
    ctx = context_from_dataset(data, windows, telemetry=telemetry)
    run = run_families(ctx)
    for c in run.cards:
        print("  " + card_line(c))
        print(f"      why: {c.trigger}")
        print(f"      do:  {c.targets}   stake {c.stake:,.0f}")

    head("STEP 5  the pipeline resolves, scores and caps")
    for label, kwargs in (
        ("default (15% holdout)", {}),
        ("holdout off", {"pipeline_cfg": NO_HOLDOUT}),
        ("holdout off + run check ROAS >= 7", {"pipeline_cfg": NO_HOLDOUT, "check": RunCheck("roas", ">=", 7.0)}),
    ):
        pl = plan(data, **kwargs)
        print(f"  -- {label}: plan {pl.id}, {len(pl.actionable)} actionable of {len(pl.actions)} cards"
              + (f", holdout {', '.join(pl.holdout)}" if pl.holdout else ""))
        print(f"     pipeline gates fired: {fired(pl.telemetry['gates'])}"
              f"   spine: {fired(pl.telemetry['spine'])}")
        if pl.guard_note:
            print(f"     note: {pl.guard_note}")
        for c in pl.actions:
            print("     " + card_line(c))
            if c.veto_reason:
                print(f"         veto: {c.veto_reason}")
    pl = plan(data, pipeline_cfg=NO_HOLDOUT)
    c = pl.actions[0]
    print(f"  confidence of '{c.title}' on {c.entity_id}: 40 + "
          + " + ".join(f"{v:g} ({k})" for k, v in c.conf_parts) + f" = {c.confidence}")
    print(f"  expected value: est {c.est_day:,.0f}/day x p {c.p_success} = {c.ev_day:,.0f}/day"
          f"  kind={c.ev_kind} estimator={c.estimator}")

    head("STEP 6  verdicts")
    for label, kwargs in (("classic", {}), ("verdict rule profile", {"rule_profile": "verdict"})):
        pl = plan(data, pipeline_cfg=NO_HOLDOUT, **kwargs)
        print(f"  -- {label}: {pl.verdict_states}")
        for c in pl.actions:
            vd = c.verdict or {}
            print(f"     {vd.get('lane', '-'):<22} {c.title:<34} [{c.entity_id}]"
                  + (f"  ladder={c.ladder}" if c.ladder else "")
                  + (f"  asked {c.change.pct_requested:+.0f}%, shipped {c.change.pct:+.0f}%"
                     if c.change is not None and c.change.pct_requested is not None else ""))

    head("STEP 7  the ledger scores what happened")
    end = data.latest_date
    cut = end - timedelta(days=14)

    def upto(ds, day):
        def keep(rows, key=lambda r: r.date):
            return [r for r in rows if key(r) <= day]
        return replace(ds, perf=keep(ds.perf), market=keep(ds.market), offtake=keep(ds.offtake),
                       shelf=keep(ds.shelf), budget_days=keep(ds.budget_days),
                       change_events=keep(ds.change_events, lambda r: r.timestamp.date()))

    early = upto(data, cut)
    ledger = ActionLedger()
    name = early.profile.name
    pl = plan(early, ledger=ledger, account="demo", pipeline_cfg=NO_HOLDOUT)
    applied_on = cut + timedelta(days=1)
    print(f"  plan {pl.id} built on data to {cut}; every actionable card marked applied on {applied_on}")
    for c in pl.actions:
        ok = ledger.mark("demo", name, pl.id, c.id, "applied", today=applied_on) if c.is_actionable else False
        print(f"     {c.id:<20} {c.title:<34} [{c.entity_id}]  mark applied -> {ok}")
    for today in (cut + timedelta(days=5), cut + timedelta(days=8)):
        rep = pair_outcomes(ledger, "demo", name, [r for r in data.perf if r.date <= today], today=today,
                            offtake=data.offtake)
        print(f"  pair_outcomes(today={today}): paired {rep.paired}, deferred {rep.deferred}, {rep.outcomes}")
    print(f"  {'family':<17}{'entity':<14}{'dir':<6}{'h':>3}{'ROAS before':>13}{'ROAS after':>12}{'tide':>7}  outcome")
    for r in ledger.ledger_rows("demo", name):
        if r.get("outcome") is None:
            continue
        print(f"  {r['family']:<17}{r['entity_id']:<14}{r['direction']:<6}{r['horizon']:>3}"
              f"{r['roas_before']:>13.3f}{r['roas_after']:>12.3f}{r['tide']:>7.3f}  {r['outcome']}")
    print(f"  win rates (family: (rate, n)): {ledger.win_rates('demo', name, today=end)}")
    print("  note: the synthetic data does not react to an 'applied' mark, so these labels describe")
    print("        the untreated trajectory of each entity; they show the mechanics only.")


def evaluate(n_seeds: int) -> None:
    """Planted-event recovery: does diagnose() report each planted event on its planted target?"""
    head(f"EVALUATION  planted-event recovery, seeds 0..{n_seeds - 1}, default configuration")
    kinds = {"cpm_spike": "cpm_rise", "availability_drop": "availability_drop",
             "budget_capped_winner": "budget_bound_winner", "bleeding_keyword": "bleeding_keyword",
             "own_overlap": "own_collision"}
    hits = {k: 0 for k in kinds}
    extra_total = 0
    for seed in range(n_seeds):
        data = generate(seed=seed)
        d = diagnose(data)
        matched: set[int] = set()
        row = []
        for planted, kind in kinds.items():
            ev = data.events[planted]
            found = False
            for i, f in enumerate(d.findings):
                if f.kind != kind:
                    continue
                if ev.targeting_unit is not None and f.targeting_unit != ev.targeting_unit:
                    continue
                if planted == "availability_drop" and (f.geo != ev.geo or f.product_id != ev.product_id):
                    continue
                if planted == "budget_capped_winner" and tuple(f.entity_ids) != tuple(ev.entity_ids):
                    continue
                found = True
                matched.add(i)
            hits[planted] += found
            row.append("found" if found else "MISSED")
        extra = len(d.findings) - len(matched)
        extra_total += extra
        print(f"  seed {seed:>2}: " + "  ".join(f"{k}={r}" for k, r in zip(kinds, row))
              + f"  | other findings {extra}")
    print()
    for planted, n in hits.items():
        print(f"  {planted:<22} found on its planted target in {n} of {n_seeds} seeds")
    print(f"  findings that match no planted event: {extra_total} across {n_seeds} seeds")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--eval", type=int, default=0, metavar="N", help="run the recovery evaluation over seeds 0..N-1")
    args = ap.parse_args()
    if args.eval:
        evaluate(args.eval)
    else:
        steps()


if __name__ == "__main__":
    main()
