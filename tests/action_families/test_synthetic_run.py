"""End-to-end over the synthetic dataset: the planted events surface as the expected cards."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from _helpers import one  # noqa: E402

from cpm_lineage.actions.families import find_collisions, run_families  # noqa: E402
from cpm_lineage.actions.primitives import (apply_playbook, context_from_dataset,  # noqa: E402
                                            decision_roas, live_campaigns)
from cpm_lineage.schema import AppliedAction  # noqa: E402


@pytest.fixture(scope="module")
def classic_ctx(synthetic_dataset, windows_30):
    return context_from_dataset(synthetic_dataset, windows_30)


@pytest.fixture(scope="module")
def verdict_ctx(synthetic_dataset, windows_30):
    return context_from_dataset(synthetic_dataset, windows_30, rule_profile="verdict")


def test_context_shapes(classic_ctx, synthetic_dataset):
    ids = {c.entity_id for c in classic_ctx.campaigns}
    assert {"cmp-winner", "cmp-conquest", "cmp-reach", "cmp-reco"} <= ids
    live = {c.entity_id for c in live_campaigns(classic_ctx, "snack-bars")}
    assert "cmp-winner" in live                       # raw status says held, but it is spending
    assert "cmp-paused" not in live
    reach = classic_ctx.campaign("cmp-reach")
    assert decision_roas(classic_ctx, reach) is None  # no attributed sales: cannot judge, never 0
    reco = classic_ctx.campaign("cmp-reco")
    assert decision_roas(classic_ctx, reco) == pytest.approx(reco.roas_primary * 0.6)
    assert all(k.entity_id != "cmp-reco" for k in classic_ctx.keywords)
    seg = classic_ctx.segments["snack-bars"]
    assert 0 < seg.market_share_pct < 100 and 0 < seg.sov_current < 100
    assert sum(p.share_current for p in seg.players) == pytest.approx(100.0, abs=1e-9)
    assert sum(p.share_prior for p in seg.players) == pytest.approx(100.0, abs=1e-9)
    assert classic_ctx.telemetry["cmp-winner"].sufficiency_3d < 0.9


def test_classic_run_finds_the_planted_events(classic_ctx, synthetic_events):
    run = run_families(classic_ctx)
    cards = run.cards
    assert run.board == {}
    scale = one(cards, family="efficiency-scale")
    assert scale.entity_id == "cmp-winner" and scale.direction == "up" and 5 <= scale.change.pct <= 50
    pause = one(cards, family="efficiency-cut", type="keyword")
    assert pause.entity_id == "cmp-conquest"
    assert [k.kw for k in pause.keywords] == [synthetic_events["bleeding_keyword"].targeting_unit]
    leak = sum(k.spend * max(0.0, 1 - k.roas) for k in pause.keywords)
    assert pause.stake == pytest.approx(leak, abs=1)
    col = one(cards, family="collision")
    assert [k.kw for k in col.keywords] == ["energy bar"]
    assert col.evidence["keep"] == "cmp-generic" and col.entity_id == "cmp-overlap"
    assert all(c.status == "pending" for c in apply_playbook(cards, classic_ctx))
    assert not [c for c in cards if c.entity_id in ("cmp-paused", "cmp-reach")]
    assert len({c.id for c in cards}) == len(cards)


def test_verdict_run(verdict_ctx):
    run = run_families(verdict_ctx)
    assert {"cmp-winner", "cmp-overlap", "cmp-generic"} <= set(run.board)
    assert "cmp-reco" not in run.board and "cmp-reach" not in run.board
    raise_ = one(run.cards, family="efficiency-scale")
    assert raise_.entity_id == "cmp-winner"
    row = run.board["cmp-winner"]
    assert raise_.stake == pytest.approx(row["spend"] * (row["r"] - row["thr"]) * raise_.change.pct / 100, abs=1)
    pair = [c for c in run.cards if c.tension_group == "cmp-overlap"]
    assert sorted(c.type for c in pair) == ["bid", "budget"]
    assert pair[0].tension_with == pair[1].id and pair[1].tension_with == pair[0].id
    for c in run.cards:
        assert c.rule_profile == "verdict" and c.stake >= 0
    again = run_families(verdict_ctx)
    assert [(c.id, c.status) for c in again.cards] == [(c.id, c.status) for c in run.cards]


def test_collision_waste_decomposes(classic_ctx):
    for col in find_collisions(classic_ctx):
        assert col.waste == pytest.approx(sum(r.spend for _, rows in col.groups for r in rows[1:]), abs=1e-9)
        for _, rows in col.groups:
            roas = [r.sales / r.spend for r in rows]
            assert roas == sorted(roas, reverse=True)


def test_cooldown_and_selection_reach_the_context(synthetic_dataset, windows_30):
    applied = [AppliedAction("cut", "cmp-conquest", windows_30.current.end)]
    c = context_from_dataset(synthetic_dataset, windows_30, applied=applied,
                             selected_entities=["cmp-conquest", "cmp-winner"], rule_profile="verdict")
    assert ("cut", "cmp-conquest") in c.recent_applied
    assert c.scoped and {x.entity_id for x in c.campaigns} == {"cmp-conquest", "cmp-winner"}
    assert run_families(c).board["cmp-winner"]["thr"] == pytest.approx(c.be)   # thin scoped cohort abstains
