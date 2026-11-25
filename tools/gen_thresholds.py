#!/usr/bin/env python3
"""Generate ``docs/thresholds.md`` from the config dataclasses in the source.

Every tunable number in the library is a field on a frozen config dataclass,
with its default and a one-line comment next to it.  This script reads those
classes straight from the source files (``ast`` for the fields and defaults,
``tokenize`` for the comments) and writes one table per class, so the
reference cannot drift from the code.  Nothing is imported from the package
and nothing is executed.

Usage::

    python tools/gen_thresholds.py            # rewrite docs/thresholds.md
    python tools/gen_thresholds.py --check    # exit 1 when the file is out of date
    python tools/gen_thresholds.py --stdout   # print instead of writing

A class is included when it is a dataclass and its name ends in ``Config``,
``Policy``, ``Defaults`` or ``Options``, or is one of the named extras
(``Economics``, ``TradingDay`` and the three platform-declaration classes).
``EXCLUDE`` lists the two classes that match the naming rule but are data
records, not threshold sets.
"""

from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "cpm_lineage"
OUT = ROOT / "docs" / "thresholds.md"

SUFFIXES = ("Config", "Policy", "Defaults", "Options")
EXTRA = ("Economics", "TradingDay")
PLATFORM = ("PlatformProfile", "AdTypeCapability", "PositionSemantics")
# Named like a config but really a data record (a rule's bid cap, a result row): not a threshold set.
EXCLUDE = ("GuardrailPolicy", "LabelOptions")

# Shared configuration first, then the two entry-point configs, then the rest by module path.
LEAD_MODULES = ("config.py", "engine.py")


@dataclass
class FieldDoc:
    name: str
    type: str
    default: str
    meaning: str
    group: str = ""


@dataclass
class ClassDoc:
    name: str
    module: str            # dotted module path
    path: str              # repo-relative source path
    summary: str
    fields: list[FieldDoc] = field(default_factory=list)


def _is_dataclass(node: ast.ClassDef) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name == "dataclass":
            return True
    return False


def _wanted(name: str) -> bool:
    if name in EXCLUDE:
        return False
    return name.endswith(SUFFIXES) or name in EXTRA or name in PLATFORM


def _comments(source: str) -> tuple[dict[int, str], set[int]]:
    """``{line: comment text}`` and the set of lines that hold nothing but a comment."""
    by_line: dict[int, str] = {}
    alone: set[int] = set()
    lines = source.splitlines()
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            line = tok.start[0]
            by_line[line] = tok.string.lstrip("#").strip()
            if lines[line - 1].strip().startswith("#"):
                alone.add(line)
    return by_line, alone


def _default(value: ast.expr | None) -> str:
    """Source text of a field default; ``field(default_factory=X)`` is shown as what it builds."""
    if value is None:
        return "(required)"
    if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "field":
        for kw in value.keywords:
            if kw.arg == "default":
                return ast.unparse(kw.value)
            if kw.arg == "default_factory":
                factory = kw.value
                if isinstance(factory, ast.Lambda):
                    return ast.unparse(factory.body)
                return f"{ast.unparse(factory)}()"
        return "(required)"
    return ast.unparse(value)


def _summary(node: ast.ClassDef) -> str:
    doc = ast.get_docstring(node) or ""
    first = doc.split("\n\n")[0]
    return " ".join(first.split())


def _group_label(text: str) -> str:
    """``---- holdout ----`` -> ``holdout``; a plain sentence is not a group heading."""
    stripped = text.strip("-= ").strip()
    return stripped if text.startswith(("-", "=")) and stripped else ""


def constants() -> dict[str, str]:
    """Module-level UPPER_CASE constants of the package: ``{name: source text of its value}``.

    Used to spell out defaults such as ``dict(FAMILY_CAPS)``.  An alias
    (``PRIORITY = FAMILY_PRIORITY``) is followed to the value it names.
    """
    raw: dict[str, ast.expr] = {}
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value = node.target, node.value
            else:
                continue
            if isinstance(target, ast.Name) and target.id.isupper():
                raw.setdefault(target.id, value)
    out: dict[str, str] = {}
    for name, value in raw.items():
        hops = 0
        while isinstance(value, ast.Name) and value.id in raw and hops < 5:
            value, hops = raw[value.id], hops + 1
        out[name] = ast.unparse(value)
    return out


def _names(expr: str) -> list[str]:
    """UPPER_CASE names referenced by a default expression, in order of appearance."""
    seen: list[str] = []
    for node in ast.walk(ast.parse(expr, mode="eval")):
        if isinstance(node, ast.Name) and node.id.isupper() and node.id not in seen:
            seen.append(node.id)
    return seen


def collect() -> list[ClassDoc]:
    out: list[ClassDoc] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        by_line, alone = _comments(source)
        rel = path.relative_to(ROOT).as_posix()
        module = "cpm_lineage." + path.relative_to(SRC).with_suffix("").as_posix().replace("/", ".")
        module = module.removesuffix(".__init__")
        for node in tree.body:
            if not (isinstance(node, ast.ClassDef) and _is_dataclass(node) and _wanted(node.name)):
                continue
            cls = ClassDoc(name=node.name, module=module, path=rel, summary=_summary(node))
            group = ""
            prev_end = node.lineno
            for stmt in node.body:
                if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                    prev_end = getattr(stmt, "end_lineno", prev_end)
                    continue
                # A comment-only line of dashes above the field starts a group.
                for line in range(prev_end + 1, stmt.lineno):
                    if line in alone:
                        label = _group_label(by_line[line])
                        if label:
                            group = label
                end = stmt.end_lineno or stmt.lineno
                parts = [by_line[line] for line in range(stmt.lineno, end + 1) if line in by_line]
                # Comment-only continuation lines directly under the field belong to it.
                line = end + 1
                while line in alone and not _group_label(by_line[line]):
                    parts.append(by_line[line])
                    line += 1
                cls.fields.append(FieldDoc(
                    name=stmt.target.id, type=ast.unparse(stmt.annotation), default=_default(stmt.value),
                    meaning=" ".join(p for p in parts if p), group=group))
                prev_end = line - 1
            out.append(cls)

    def order(c: ClassDoc) -> tuple[int, str, str]:
        tail = c.path.rsplit("/", 1)[-1]
        if c.name in PLATFORM:
            return (3, c.path, c.name)
        if c.path.count("/") == 2 and tail in LEAD_MODULES:
            return (LEAD_MODULES.index(tail), c.path, "")     # keep source order inside the file
        return (2, c.path, "")

    indexed = sorted(enumerate(out), key=lambda t: (order(t[1]), t[0]))
    return [c for _, c in indexed]


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _code(text: str) -> str:
    """Inline code that survives a table cell (pipes break GitHub tables even inside backticks)."""
    return "`" + text.replace("|", "\\|") + "`" if text else ""


def _anchor(name: str) -> str:
    return name.lower()


def render() -> str:
    classes = collect()
    known = {c.name for c in classes}
    n_fields = sum(len(c.fields) for c in classes)
    tuning = [c for c in classes if c.name not in PLATFORM]
    platform = [c for c in classes if c.name in PLATFORM]
    L: list[str] = []
    L.append("# Threshold reference")
    L.append("")
    L.append("<!-- GENERATED FILE. Do not edit by hand: run `python tools/gen_thresholds.py`. -->")
    L.append("")
    L.append(f"Every tunable value in `cpm_lineage`: {n_fields} fields on {len(classes)} config dataclasses. "
             "This file is generated from the source by `tools/gen_thresholds.py`; the default and the "
             "meaning of each field are copied from the dataclass definition and its inline comment. "
             "A test (`tests/tools/test_gen_thresholds.py`) fails when this file and the code disagree.")
    L.append("")
    L.append("## How to read and change a value")
    L.append("")
    L.append("* **The defaults are judgment values.** They are starting points, not calibrated constants. "
             "No field here has been fitted to, or validated on, data from a real account in this repository.")
    L.append("* **Every class is frozen.** Build a changed copy and pass it to the function that takes it:")
    L.append("")
    L.append("  ```python")
    L.append("  from dataclasses import replace")
    L.append("  from cpm_lineage import EngineConfig, Economics, PipelineConfig, plan")
    L.append("")
    L.append("  cfg = EngineConfig(economics=Economics(breakeven=1.2, currency_unit=0.01))")
    L.append("  pipe = replace(PipelineConfig(), holdout_pct=0, pending_cap=40)")
    L.append("  plan(data, config=cfg, pipeline_cfg=pipe)")
    L.append("  ```")
    L.append("")
    L.append("* **Per-account overrides from a mapping** go through `cpm_lineage.apply_overrides(cfg, {...})`, "
             "which returns `(new_config, applied, unknown)` and never mutates the input.")
    L.append("* **Money fields** are written in reference units (an account whose material campaigns spend "
             "thousands of currency units a day) and are multiplied by `Economics.currency_unit` at use. "
             "Change the unit once instead of editing each money field.")
    L.append("* **ROAS bars** written as \"x breakeven\" are multiplied by the effective breakeven "
             "`Economics.be = max(breakeven / max(ltv_mult, 0.1), 0.1)`.")
    L.append("* The **Default** column shows the default as written in the source. `X()` means "
             "\"a default instance of `X`\", documented in its own section.")
    L.append("")
    L.append("## Where each class is passed")
    L.append("")
    L.append("| Class | Passed as |")
    L.append("|---|---|")
    L.append("| `EngineConfig` (holds `Economics`, `AvailabilityPolicy`, `BudgetBoundPolicy`, `LivenessConfig`, "
             "`PositionConfig`, `TierConfig`, `WindowDefaults`, `TradingDay`) | `diagnose(data, config=...)`, "
             "`plan(data, config=...)` |")
    L.append("| `OrchestrationConfig` | `diagnose(..., options=...)`, `plan(..., options=...)` |")
    L.append("| `CpmConfig` | `diagnose(..., cpm_cfg=...)` and the functions in `cpm_lineage.cpm` |")
    L.append("| `ActionsConfig` | `plan(..., actions_cfg=...)` |")
    L.append("| `PipelineConfig` | `plan(..., pipeline_cfg=...)` |")
    L.append("| `VerdictConfig` | `plan(..., verdict_cfg=...)` |")
    L.append("| `OutcomeConfig` | `outcomes.pair_outcomes(..., cfg=...)`, `outcomes.evaluate(..., cfg=...)` |")
    L.append("| `LedgerConfig` | `store.ActionLedger(cfg=...)` |")
    L.append("| every other class | the `cfg=` (or similarly named) argument of the functions in its own module; "
             "`diagnose()` and `plan()` run those subsystems on their defaults |")
    L.append("| `PlatformProfile`, `AdTypeCapability`, `PositionSemantics` | declared by the adapter "
             "(`Dataset.profile`), or `diagnose(data, profile=...)` |")
    L.append("")
    L.append("## Contents")
    L.append("")
    for c in tuning:
        L.append(f"* [`{c.name}`](#{_anchor(c.name)}) - `{c.module}` ({len(c.fields)} fields)")
    L.append("* Platform declaration: " + ", ".join(f"[`{c.name}`](#{_anchor(c.name)})" for c in platform))
    L.append("")

    def section(c: ClassDoc) -> None:
        L.append(f"## {c.name}")
        L.append("")
        L.append(f"`{c.module}.{c.name}` · defined in `{c.path}`")
        L.append("")
        if c.summary:
            L.append(c.summary)
            L.append("")
        L.append("| Field | Default | Type | Meaning |")
        L.append("|---|---|---|---|")
        group = ""
        for f in c.fields:
            if f.group != group:
                group = f.group
                L.append(f"| **{_cell(group)}** | | | |")
            meaning = f.meaning
            nested = f.default.removesuffix("()")
            if not meaning and f.default.endswith("()") and nested in known:
                meaning = f"The embedded [`{nested}`](#{_anchor(nested)}) (see its own section)."
            L.append(f"| `{f.name}` | {_code(f.default)} | {_code(f.type)} | {_cell(meaning)} |")
        L.append("")

    for c in tuning:
        section(c)
    consts = constants()
    used: list[str] = []
    for c in classes:
        for f in c.fields:
            if f.default == "(required)":
                continue
            for name in _names(f.default):
                if name in consts and name not in used:
                    used.append(name)
    if used:
        L.append("## Constants used as defaults")
        L.append("")
        L.append("Some defaults above are written as a named module constant. Their values:")
        L.append("")
        for name in sorted(used):
            L.append(f"**`{name}`**")
            L.append("")
            L.append("```python")
            L.append(consts[name])
            L.append("```")
            L.append("")
    L.append("# Platform declaration")
    L.append("")
    L.append("These three classes describe the platform, not a threshold. They are listed here because "
             "several of their fields (`currency_unit`, the trading-day hours, the position range) feed "
             "the thresholds above.")
    L.append("")
    for c in platform:
        section(c)
    return "\n".join(L).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when docs/thresholds.md is out of date")
    ap.add_argument("--stdout", action="store_true", help="print the document instead of writing it")
    args = ap.parse_args(argv)
    text = render()
    if args.stdout:
        sys.stdout.write(text)
        return 0
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT.relative_to(ROOT)} is out of date: run python tools/gen_thresholds.py", file=sys.stderr)
            return 1
        print(f"{OUT.relative_to(ROOT)} is up to date")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
