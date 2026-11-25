"""docs/thresholds.md is generated from the config dataclasses and must match the code."""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("gen_thresholds", ROOT / "tools" / "gen_thresholds.py")
gen = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gen          # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(gen)


def test_committed_file_is_up_to_date():
    committed = (ROOT / "docs" / "thresholds.md").read_text(encoding="utf-8")
    assert committed == gen.render(), "docs/thresholds.md is stale: run python tools/gen_thresholds.py"


def test_check_mode_agrees(capsys):
    assert gen.main(["--check"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_render_is_deterministic():
    assert gen.render() == gen.render()


def test_every_field_of_every_listed_class_is_documented():
    """The AST reading agrees with the real dataclasses: same fields, none without a meaning."""
    classes = gen.collect()
    assert len(classes) >= 30
    for doc in classes:
        cls = getattr(importlib.import_module(doc.module), doc.name)
        assert dataclasses.is_dataclass(cls)
        assert [f.name for f in doc.fields] == [f.name for f in dataclasses.fields(cls)], doc.name
    known = {c.name for c in classes}
    missing = [f"{c.name}.{f.name}" for c in classes for f in c.fields
               if not f.meaning and f.default.removesuffix("()") not in known]
    assert not missing, f"config fields without an inline comment: {missing}"


def test_main_engine_configs_are_listed():
    names = {c.name for c in gen.collect()}
    for expected in ("EngineConfig", "Economics", "OrchestrationConfig", "ActionsConfig", "PipelineConfig",
                     "VerdictConfig", "CpmConfig", "BudgetConfig", "AttributionConfig", "OutcomeConfig",
                     "LedgerConfig", "PlatformProfile"):
        assert expected in names


def test_plain_defaults_match_the_runtime_values():
    """Spot-check the documented default text against live objects."""
    from cpm_lineage import Economics, PipelineConfig
    from cpm_lineage.verdict import VerdictConfig

    by = {c.name: {f.name: f.default for f in c.fields} for c in gen.collect()}
    assert float(by["PipelineConfig"]["holdout_pct"]) == PipelineConfig().holdout_pct
    assert int(by["PipelineConfig"]["pending_cap"]) == PipelineConfig().pending_cap
    assert float(by["Economics"]["breakeven"]) == Economics().breakeven
    assert int(by["VerdictConfig"]["bar_cut"]) == VerdictConfig().bar_cut
