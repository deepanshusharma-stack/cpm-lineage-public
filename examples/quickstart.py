"""Quick start: synthetic data -> diagnose -> plan -> printed report.

The synthetic adapter generates 90 days for one advertiser on an imaginary
first-price CPM platform with no click data, and plants five known events:
a CPM spike on one keyword, an availability drop for one product in one
geo, a budget-capped winner, a bleeding conquest keyword and two own
campaigns bidding the same keyword.  The engine should find all five.

Run:  python examples/quickstart.py
"""

from __future__ import annotations

from cpm_lineage import SyntheticAdapter, diagnose, plan, render_report


def main() -> None:
    data = SyntheticAdapter().dataset           # a Dataset: every feed plus the platform profile

    diagnosis = diagnose(data)                  # what happened and why
    action_plan = plan(data)                    # what to do about it (classic rule profile)

    print(render_report(diagnosis, action_plan))

    # Everything printed above is plain data you can read directly:
    print()
    print("PLANTED vs FOUND")
    found = {f.kind: f for f in diagnosis.findings}
    expected = {
        "cpm_spike": "cpm_rise", "availability_drop": "availability_drop",
        "budget_capped_winner": "budget_bound_winner", "bleeding_keyword": "bleeding_keyword",
        "own_overlap": "own_collision",
    }
    for planted, kind in expected.items():
        hit = found.get(kind)
        print(f"  {planted:<22} {'found' if hit else 'MISSED':<7} {hit.summary if hit else ''}")

    # One entity in depth: the exact identity, then the lever attribution.
    generic = diagnosis.entities["cmp-generic"]
    print()
    print("cmp-generic, exact identity  d ln ROAS = d ln ICR + d ln ASP - d ln CPM")
    for name, channel in generic.layer1.channels.items():
        print(f"  {name:<6} {channel.log_points:+.4f}")
    print(f"  sum    {sum(c.log_points for c in generic.layer1.channels.values()):+.4f}"
          f"   (ln ROAS ratio {generic.layer1.raw_dln:+.4f})")
    print(f"  attribution verdict: {generic.attribution.verdict}, top lever: {generic.attribution.top_driver}")
    for action in generic.recommendation.actions:
        print(f"  -> {action.verb} {', '.join(action.targets)}  ({action.headline})")


if __name__ == "__main__":
    main()
