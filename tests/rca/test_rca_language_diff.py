"""Numeral guards, the optional language hook, enrichment cache, run-over-run diff."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from cpm_lineage.rca import (KeywordPoolRow, RcaConfig, Snapshot, analyse, ask_report, attribute, context_caption,
                             diff_snapshots, digit_tokens, enrich, fact_digest, fill_cache, frame_diff, is_lane,
                             lane_match, number_guard, previous_snapshot, render_markdown, rewrite_blocks,
                             same_numbers, since_last, snapshot, trim_to_sentence, update_history, verified_terms)


def test_number_guard_accepts_source_numbers_and_years_and_rejects_inventions():
    source = "Sales moved -12.4% while the segment moved +3.0%; 1,250 units across 4 geos."
    assert number_guard("A 12.4% fall against 3.0% growth, on 1250 units.", source)
    assert number_guard("Seasonal demand in 2026 usually softens; 2 holidays fell in the window.", source)
    assert not number_guard("Sales fell 15%.", source)
    assert not number_guard("About 1,300 units.", source)
    assert not number_guard("Back in 2019 this was different.", source)        # outside the free-year range
    assert number_guard("No numbers at all.", "")
    assert digit_tokens("1,250 and 12.4% and v2.") == ["1250", "12.4", "2"]
    assert number_guard("In 2040.", "x", replace(RcaConfig(), free_year_max=2040))


def test_same_numbers_needs_identical_multisets_both_ways():
    assert same_numbers("Share fell 2.5pp to 17.5%.", "At 17.5%, share is down 2.5pp.")
    assert not same_numbers("Share fell 2.5pp to 17.5%.", "Share fell 2.5pp.")             # dropped one
    assert not same_numbers("Share fell 2.5pp.", "Share fell 2.5pp, about 3 points.")      # invented one
    assert not same_numbers("5 and 5", "5")                                                # multiset, not set


def test_lane_matching():
    assert lane_match("high protein", "protein snacks")
    assert lane_match("kids lunchbox, vegan", "vegan bars")              # comma-separated lanes
    assert not lane_match("kids lunchbox", "protein snacks")
    assert not lane_match("unknown", "unknown") and not lane_match(None, "protein")
    assert not lane_match("on the go", "go to")                           # short words do not count
    assert is_lane("high protein") and is_lane("unknown") and not is_lane("a b c d e") and not is_lane(3)


def test_digest_is_brand_masked_and_currency_free_and_keeps_geos(b):
    f = b.tide_market()
    a = attribute(f)
    digest = fact_digest(f, a)
    assert "own-brand" not in digest and "rival-b" not in digest and "sku-" not in digest
    assert "geo-south" in digest and "MARKET_TIDE_PLUS_BRAND" in digest
    for money in ("1,000,000", "800,000", "1000000", "800000", "5,000,000"):
        assert money not in digest
    assert number_guard("The segment fell 18.0% and the brand 20.0%, worst in geo-south at -35%.", digest)
    assert not number_guard("Sales fell by 200,000.", digest)


def test_context_hook_gets_the_digest_and_fails_to_absence(b):
    f = b.tide_market()
    a = attribute(f)
    seen = []

    def good(prompt):
        seen.append(prompt)
        return "A late season and two regional holidays plausibly softened demand in geo-south."

    assert context_caption(good, f, a, market_description="a grocery marketplace").startswith("A late season")
    assert "a grocery marketplace" in seen[0] and fact_digest(f, a) in seen[0] and "own-brand" not in seen[0]
    assert context_caption(None, f, a) is None
    assert context_caption(lambda p: "Demand fell 47% across the region.", f, a) is None      # invented number
    assert context_caption(lambda p: "", f, a) is None

    def broken(prompt):
        raise TimeoutError("no answer")

    assert context_caption(broken, f, a) is None
    long = "Word " * 300 + "end."
    out = context_caption(lambda p: "A cold snap hit early. " + long, f, a)
    assert out == "A cold snap hit early."


def test_trim_to_sentence():
    assert trim_to_sentence("One two three.", 10) == "One two three."
    assert trim_to_sentence("One two. Three four five six", 4) == "One two."
    assert trim_to_sentence("one two three four five", 3) == "one two three"


def test_rewrite_is_guarded_block_by_block():
    blocks = ["Share fell 2.5pp to 17.5%.", "Availability held at 93%."]

    def rewriter(prompt):
        if "Share fell" in prompt:
            return "At 17.5%, share is down 2.5pp."
        return "Availability held near 95%."                 # invents a number: refused

    assert rewrite_blocks(rewriter, blocks) == ["At 17.5%, share is down 2.5pp.", blocks[1]]
    assert rewrite_blocks(None, blocks) == blocks
    assert rewrite_blocks(lambda p: "Share fell 2.5pp to 17.5%. " + "padding " * 20, blocks[:1]) == blocks[:1]


def test_ask_report_refuses_an_invented_number(b):
    report = render_markdown(analyse(b.tide_market()))
    assert ask_report(lambda p: "Own sales moved -20.0% while the segment moved -18.0%.", report, "how bad?")
    assert ask_report(lambda p: "Sales fell 41%.", report, "how bad?") is None
    assert ask_report(None, report, "how bad?") is None


def test_enrich_once_cache_asks_only_for_missing_keys():
    asked = []

    def ask(keys):
        asked.append(list(keys))
        return {"rival-b": "high protein", "rival-c": "this lane description is far too long", "stranger": "x"}

    cache = fill_cache({"rival-a": "kids lunchbox"}, ["rival-a", "rival-b", "rival-c", "rival-b"], ask, is_lane)
    assert asked == [["rival-b", "rival-c"]]
    assert cache == {"rival-a": "kids lunchbox", "rival-b": "high protein"}      # invalid and unasked keys dropped
    assert fill_cache(cache, ["rival-a", "rival-b"], ask, is_lane) == cache and len(asked) == 1
    assert fill_cache({}, ["x"], None) == {}

    def down(keys):
        raise RuntimeError

    assert fill_cache({"a": 1}, ["b"], down) == {"a": 1}


def test_expansion_terms_must_have_live_demand(b):
    v = b.voice(1.0, 1.0, 9.0, 9.0, keywords=(KeywordPoolRow("oat bar", 500.0, 400.0, 0.0, 0.0),
                                              KeywordPoolRow("dead term", 0.0, 90.0, 0.0, 0.0)))
    assert verified_terms(["Oat Bar", "moon bar", "dead term", "oat bar"], v) == ("oat bar",)
    f = enrich(b.facts(b.offtake(1.0, 1.0, 9.0, 9.0), voice=v), expansion=["moon bar", "oat bar"])
    assert f.voice.expansion == ("oat bar",)
    assert verified_terms(["oat bar"], None) == ()


# ---- run-over-run diff ------------------------------------------------------


def _snap(day: int, **kw) -> Snapshot:
    base = dict(feed_max=date(2026, 3, day), days=14, own_pct=-6.0, segment_pct=12.0, share_c=16.79,
                availability=0.78, sov=15.88, roas=3.0, quadrant="SHARE_LOSS_IN_GROWTH",
                causes=("AVAILABILITY_DRAG", "PROMO_PULLBACK"))
    base.update(kw)
    return Snapshot(**base)


def test_diff_names_moved_axes_new_and_resolved_causes_and_quadrant_flips():
    prev = _snap(17)
    cur = _snap(31, own_pct=-2.0, availability=0.90, quadrant="RIDING_GROWTH", causes=("AD_PULLBACK", "PROMO_PULLBACK"))
    lines = diff_snapshots(prev, cur)
    assert lines == ["own sales move (%): -6.00 -> -2.00", "availability: 0.78 -> 0.90",
                     "newly fired: Ad voice pulled back", "resolved: Availability drag",
                     "quadrant: SHARE_LOSS_IN_GROWTH -> RIDING_GROWTH"]


def test_identical_snapshots_and_first_runs_are_silent():
    assert diff_snapshots(_snap(17), _snap(31)) == []
    assert diff_snapshots(_snap(17), _snap(31, share_c=16.794)) == []          # same at 2 decimals
    assert diff_snapshots(_snap(17), _snap(31, share_c=16.80)) == ["share (%): 16.79 -> 16.80"]
    assert since_last([], _snap(31)) == []
    assert since_last([_snap(31, own_pct=5.0)], _snap(31)) == []               # same feed date is not "earlier"
    assert since_last([_snap(3, own_pct=9.0), _snap(17, own_pct=-1.0)], _snap(31)) == \
        ["own sales move (%): -1.00 -> -6.00"]
    assert diff_snapshots(_snap(17, roas=None), _snap(31)) == ["direct ROAS: n/a -> 3.00"]


def test_history_keeps_one_snapshot_per_feed_date_and_a_bounded_depth():
    history: list[Snapshot] = []
    for day in range(1, 21):
        history = update_history(history, _snap(day))
    assert len(history) == 12 and history[0].feed_max == date(2026, 3, 9)
    replaced = update_history(history, _snap(20, own_pct=1.0))
    assert len(replaced) == 12 and replaced[-1].own_pct == 1.0 and history[-1].own_pct == -6.0
    assert previous_snapshot(history, _snap(20)).feed_max == date(2026, 3, 19)
    assert Snapshot.from_dict(_snap(5).as_dict()) == _snap(5)


def test_analyse_carries_the_diff_and_a_guarded_framing(b):
    f = b.growth_market()
    snap = snapshot(f, attribute(f))
    assert snap.causes == tuple(sorted(set(attribute(f).cause_keys))) and snap.quadrant == "SHARE_LOSS_IN_GROWTH"
    earlier = replace(snap, feed_max=date(2026, 3, 17), quadrant="STABLE", causes=("PROMO_PULLBACK",))
    result = analyse(f, history=[earlier], complete=lambda p: "The quadrant flipped and 4 causes are new.")
    assert "quadrant: STABLE -> SHARE_LOSS_IN_GROWTH" in result.since_last
    assert result.since_last_framing == "The quadrant flipped and 4 causes are new."
    assert "## Since the last run" in render_markdown(result)
    refused = analyse(f, history=[earlier], complete=lambda p: "Share is down 73 points.")
    assert refused.since_last and refused.since_last_framing is None and refused.context is None
    assert frame_diff(lambda p: "x", []) is None
    assert analyse(f).since_last == ()
