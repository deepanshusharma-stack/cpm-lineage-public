"""The README is executable documentation: every fenced code block in it is run or checked here.

* ``python`` blocks are executed in order in one shared namespace, so a later
  block may use names an earlier one defined, exactly as a reader would.
* A ``text`` block shows output.  It must directly follow a ``python`` block,
  and every line of it must be a line that block printed.
* ``bash`` blocks (here and in ``CONTRIBUTING.md``) are checked command by
  command: scripts are run, install lines are checked against
  ``pyproject.toml``.  An unknown command fails.
* The ``bibtex`` block must agree with the package version, ``pyproject.toml``
  and ``CITATION.cff``.

A block in any other language fails the test, so nothing in the README can
go unchecked by accident.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

import cpm_lineage

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
FENCE = re.compile(r"^```([A-Za-z0-9_+-]*)[ \t]*\n(.*?)^```[ \t]*$", re.S | re.M)
HANDLED = {"python", "text", "bash", "bibtex"}


def _blocks(path: Path = README) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in FENCE.finditer(path.read_text(encoding="utf-8"))]


def _pyproject() -> str:
    return (ROOT / "pyproject.toml").read_text(encoding="utf-8")


def _homepage() -> str:
    match = re.search(r'^Homepage\s*=\s*"([^"]+)"', _pyproject(), re.M)
    assert match, "pyproject.toml has no Homepage url"
    return match.group(1)


@pytest.fixture(scope="module")
def blocks() -> list[tuple[str, str]]:
    found = _blocks()
    assert found, "no fenced code blocks found in README.md"
    return found


@pytest.fixture(scope="module")
def python_outputs(blocks, tmp_path_factory) -> dict[int, str]:
    """Run every python block in order; return what each printed, by block index."""
    namespace: dict[str, object] = {"__name__": "__readme__"}
    outputs: dict[int, str] = {}
    cwd = Path.cwd()
    os.chdir(tmp_path_factory.mktemp("readme"))      # a block that wrote a file would not touch the repo
    try:
        for index, (lang, code) in enumerate(blocks):
            if lang != "python":
                continue
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                exec(compile(code, f"README.md[block {index}]", "exec"), namespace)
            outputs[index] = buffer.getvalue()
    finally:
        os.chdir(cwd)
    return outputs


def test_every_block_has_a_handled_language(blocks):
    unknown = sorted({lang or "(none)" for lang, _ in blocks if lang not in HANDLED})
    assert not unknown, f"README code blocks in languages no test covers: {unknown}"
    # every opening fence was matched: an unclosed or malformed block would otherwise go unseen
    assert len(blocks) * 2 == len(re.findall(r"^```", README.read_text(encoding="utf-8"), re.M))


def test_python_blocks_run_and_print(blocks, python_outputs):
    assert python_outputs, "README has no python block"
    for index, text in python_outputs.items():
        following_text = index + 1 < len(blocks) and blocks[index + 1][0] == "text"
        if following_text:
            assert text.strip(), f"python block {index} is followed by an output block but printed nothing"


def test_output_blocks_match_what_the_code_prints(blocks, python_outputs):
    checked = 0
    for index, (lang, body) in enumerate(blocks):
        if lang != "text":
            continue
        assert index - 1 in python_outputs, f"text block {index} does not directly follow a python block"
        printed = {line.rstrip() for line in python_outputs[index - 1].splitlines()}
        for line in body.splitlines():
            if line.strip():
                assert line.rstrip() in printed, f"README shows a line the code does not print: {line!r}"
                checked += 1
    assert checked, "README has no output block to check"


def _check_pip_install(args: list[str]) -> None:
    targets = [a for a in args if not a.startswith("-")]
    assert "-e" in args and len(targets) == 1, f"unexpected install line: {args}"
    match = re.fullmatch(r"\.(?:\[([a-z,]+)\])?", targets[0])
    assert match, f"unexpected install target: {targets[0]}"
    for extra in filter(None, (match.group(1) or "").split(",")):
        assert re.search(rf"^{extra}\s*=\s*\[", _pyproject(), re.M), f"extra {extra!r} is not in pyproject.toml"


@pytest.mark.parametrize("document", ["README.md", "CONTRIBUTING.md"])
def test_bash_blocks(document):
    blocks = _blocks(ROOT / document)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(ROOT / "src"),
                                                                      os.environ.get("PYTHONPATH", "")]))}
    ran = 0
    for lang, body in blocks:
        if lang != "bash":
            continue
        for line in body.splitlines():
            args = shlex.split(line, comments=True)
            if not args:
                continue
            if args[:2] == ["git", "clone"]:
                assert args[2:] == [_homepage() + ".git"], "clone url differs from pyproject.toml Homepage"
            elif args[0] == "cd":
                assert args[1:] == [_homepage().rsplit("/", 1)[-1]]
            elif args[:2] == ["pip", "install"]:
                _check_pip_install(args[2:])
            elif args[:3] == ["python", "-m", "pytest"]:
                assert len(args) == 3          # this very process is that command; do not recurse
                continue
            elif args[0] == "python" and len(args) >= 2 and args[1].endswith(".py"):
                assert (ROOT / args[1]).is_file(), f"{document} runs a script that does not exist: {args[1]}"
                done = subprocess.run([sys.executable, *args[1:]], cwd=ROOT, env=env, capture_output=True,
                                      text=True, timeout=300)
                assert done.returncode == 0, f"`{line}` exited {done.returncode}:\n{done.stdout[-2000:]}{done.stderr[-2000:]}"
                ran += 1
            else:
                pytest.fail(f"{document} bash line is not covered by this test: {line!r}")
    assert ran, f"no shell command of {document} was executed"


def test_citation_block_matches_package_metadata(blocks):
    entries = [body for lang, body in blocks if lang == "bibtex"]
    assert len(entries) == 1
    entry = entries[0]
    cff = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
    assert f"version = {{{cpm_lineage.__version__}}}" in entry
    assert re.search(rf'^version:\s*"?{re.escape(cpm_lineage.__version__)}"?\s*$', cff, re.M)
    assert f"url     = {{{_homepage()}}}" in entry
    assert _homepage() in cff
    assert "Sharma, Deepanshu" in entry
    assert re.search(r"family-names:\s*Sharma", cff) and re.search(r"given-names:\s*Deepanshu", cff)


def test_relative_links_resolve():
    """Links to files in this repository must exist (documents written by other tools included)."""
    text = README.read_text(encoding="utf-8")
    targets = {t.split("#")[0] for t in re.findall(r"\]\(([^)\s]+)\)", text)}
    missing = sorted(t for t in targets
                     if t and not re.match(r"[a-z]+://", t) and not (ROOT / t).exists())
    assert not missing, f"README links to files that do not exist: {missing}"
