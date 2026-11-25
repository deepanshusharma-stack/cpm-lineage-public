"""The how-it-works guide quotes real output; these tests keep the quotes and the build honest."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "guide"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def steps_output() -> str:
    worked = _load("worked_example", GUIDE / "worked_example.py")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        worked.steps()
    return buf.getvalue()


def test_committed_worked_example_output_is_current(steps_output):
    committed = (GUIDE / "worked_example_output.txt").read_text(encoding="utf-8")
    assert committed.startswith(steps_output), (
        "docs/guide/worked_example_output.txt is stale: re-run docs/guide/worked_example.py "
        "(and --eval 20) and update the guide's quoted numbers")


def test_quickstart_lines_quoted_in_the_guide_are_real():
    from cpm_lineage import SyntheticAdapter, diagnose, plan, render_report

    data = SyntheticAdapter().dataset
    report = render_report(diagnose(data), plan(data))
    guide = (ROOT / "docs" / "how_it_works.md").read_text(encoding="utf-8")
    quoted = [
        "  ROAS 3.04 -> 2.79 (-8.2%)  [direct sales]",
        "    cpm       105.440 ->    114.184  log-points -0.080  share +93%",
        "  [cpm_rise] CPM on 'protein bar' rose 62% (149 -> 242): rival pressure",
        "  [availability_drop] availability of sku-a2 in geo-south fell 0.95 -> 0.58",
        "  [bleeding_keyword] 'rivalb bar' (competitor) returns ROAS 0.34 on 142,904 spend: cut",
        "  w3 ACT       pending   conf 79  Increase Budget +25%  [cmp-winner]",
        "  holdout (untouched this period): cmp-brand, cmp-conquest, cmp-reach",
    ]
    for line in quoted:
        assert line in report, f"the report no longer prints: {line!r}"
        assert line in guide, f"the guide no longer quotes: {line!r}"


def test_step_numbers_quoted_in_the_guide_are_real(steps_output):
    guide = (ROOT / "docs" / "how_it_works.md").read_text(encoding="utf-8")
    flat = re.sub(r"\s+", " ", steps_output)
    for fragment in (
        "account ROAS 3.0433 -> 2.7945 ln ratio -0.0853",
        "first read: code=self_inflicted_bid bid change +18.8%",
        "bid 86% -0.1885 -0.2046 confirm 137.336 -> 194.957",
        "kind=rival pressure",
        "raise verdict: code=top_up verb='Top up daily budget' range 8,255-12,700/day",
        "40 + 15 (30-day evidence window) + 9 (spend depth) + 15 (effect clarity: far from breakeven) = 79",
        "pair_outcomes(today=2026-03-25): paired 5, deferred 0, {'On Track': 3, 'Review': 1, 'No signal': 1}",
        "collision cmp-overlap down 7 1.757 1.656 1.027 Review",
    ):
        assert fragment in flat, f"the worked example no longer prints: {fragment!r}"
        assert fragment in re.sub(r"\s+", " ", guide), f"the guide no longer quotes: {fragment!r}"


def test_figures_exist_and_html_is_self_contained():
    guide = (ROOT / "docs" / "how_it_works.md").read_text(encoding="utf-8")
    figures = re.findall(r"\]\((guide/fig-[a-z]+\.svg)\)", guide)
    assert len(figures) >= 3
    for rel in figures:
        assert (ROOT / "docs" / rel).is_file(), rel
    page = (GUIDE / "how-it-works.html").read_text(encoding="utf-8")
    assert page.count("<svg") == len(figures)
    # no external stylesheet, script, image or font: the only URLs are XML namespaces
    assert not re.search(r'(?:src|href)="(?:https?:)?//', page)
    assert "<script" not in page and "<link" not in page and "@import" not in page
    assert (GUIDE / "cpm-lineage-how-it-works.pdf").read_bytes().startswith(b"%PDF")


def test_html_matches_the_markdown_source():
    pytest.importorskip("markdown")          # a documentation-only dependency
    build = _load("build_guide", GUIDE / "build_guide.py")
    assert (GUIDE / "how-it-works.html").read_text(encoding="utf-8") == build.build_html(), (
        "docs/guide/how-it-works.html is stale: run python docs/guide/build_guide.py --pdf")
