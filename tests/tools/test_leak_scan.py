"""The leak scanner: structural shapes, optional private terms, pragmas, history, and a clean tree.

The repository carries no term list.  These tests exercise the term
machinery with made-up words written to a temporary file.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("leak_scan", ROOT / "tools" / "leak_scan.py")
leak_scan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(leak_scan)

HARD, REVIEW = leak_scan.HARD, leak_scan.REVIEW

TERMS = r"""
# made-up entries
\bzorblat\b
quix\s+corp
cs:\bFern\b
review:examplemart
cafe noir
"""


@pytest.fixture()
def scanner():
    return leak_scan.Scanner(TERMS.splitlines())


def levels(s, line, **kw):
    return {lvl for lvl, _ in s.scan_line(line, **kw)}


def test_structural_shapes():
    s = leak_scan.Scanner()
    assert levels(s, "open('/Users/" + "somebody/notes.txt')") == {HARD}
    assert levels(s, "cd /home/" + "somebody/project") == {HARD}
    assert levels(s, "-----BEGIN RSA " + "PRIVATE KEY-----") == {HARD}
    assert levels(s, "select * from WAREHOUSE" + ".SALES.ORDERS_DAILY") == {HARD}
    assert levels(s, 'api_key = "abcd' + 'efgh12345"') == {REVIEW}
    assert levels(s, "host 10.20" + ".30.40") == {REVIEW}
    assert levels(s, "aggregate.sum() of silver.orders") == set()
    assert levels(s, "cfg.be * ctx.cfg.scale and A.B") == set()
    assert levels(s, "version 1.2.3") == set()


def test_email_addresses():
    s = leak_scan.Scanner()
    real = "jane.doe@" + "corp-mail.io"
    assert levels(s, f"contact {real}") == {REVIEW}
    assert levels(s, f"contact {real}", emails=HARD) == {HARD}
    for ok in ("someone@example.com", "noreply@" + "vendor.io", "1234+name@" + "users.noreply.host.io"):
        assert levels(s, ok, emails=HARD) == set(), ok


def test_private_terms_from_a_file(scanner):
    assert len(scanner.terms) == 5
    assert levels(scanner, "the Zorblat account") == {HARD}
    assert levels(scanner, "zorblats") == set()
    assert levels(scanner, "from Quix   Corp today") == {HARD}
    assert levels(scanner, "the Fern launch") == {HARD} and levels(scanner, "a fern in the shade") == set()
    assert levels(scanner, "sold on ExampleMart") == {REVIEW}
    assert levels(scanner, "Café Noir") == {HARD}                  # accents are normalised
    assert levels(leak_scan.Scanner(), "the Zorblat account") == set()   # no terms without the file


def test_terms_come_from_the_environment_and_never_from_the_tree(tmp_path):
    outside = tmp_path / "private" / "terms.txt"
    outside.parent.mkdir()
    outside.write_text(TERMS)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("zorblat\n")
    assert leak_scan.Scanner.from_env({}).terms == []
    s = leak_scan.Scanner.from_env({leak_scan.TERMS_ENV: str(outside)})
    assert leak_scan.scan_paths([str(tree)], s) == (1, 0)
    inside = tree / "terms.txt"
    inside.write_text(TERMS)
    with pytest.raises(ValueError):
        leak_scan.scan_paths([str(tree)], leak_scan.Scanner.from_file(str(inside)))
    assert leak_scan.main(["--quiet", "--terms", str(inside), str(tree)]) == 2


def test_repository_ships_no_term_list():
    assert not (ROOT / "tools" / "leak_denylist.json").exists()
    # tools/ holds scripts only: the scanner and the thresholds-reference generator, no data file.
    assert sorted(p.name for p in (ROOT / "tools").iterdir() if p.is_file()) == ["gen_thresholds.py", "leak_scan.py"]
    source = (ROOT / "tools" / "leak_scan.py").read_text()
    assert "hashlib" not in source and "sha256" not in source.lower()


def test_pragma_and_file_walk(tmp_path, scanner):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("x = 'zorblat'\ny = 'zorblat'  # leakscan: allow\nz = 'examplemart'\n")
    (tmp_path / "pkg" / "zorblat_data.txt").write_text("nothing here\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "junk.txt").write_text("zorblat\n")
    out = io.StringIO()
    hard, review = leak_scan.scan_paths([str(tmp_path)], leak_scan.Scanner([r"zorblat", "review:examplemart"]), out=out)
    assert (hard, review) == (2, 1)                                # one content line, one file name
    text = out.getvalue()
    assert "a.py:1" in text and "<path>" in text and "a.py:2" not in text and "junk" not in text


def test_archives_are_scanned_through_their_members(tmp_path, scanner):
    import gzip
    import zipfile

    with gzip.open(tmp_path / "dump.txt.gz", "wb") as fh:
        fh.write(b"row,zorblat\n")
    with zipfile.ZipFile(tmp_path / "book.zip", "w") as zf:
        zf.writestr("sheet1.xml", "<c>Quix Corp</c>")
    hard, _ = leak_scan.scan_paths([str(tmp_path)], scanner)
    assert hard == 2


def test_history_scan_flags_identities_and_messages(scanner):
    corp = "First Last <first.last@" + "corp-mail.io>"
    ok = "First Last <1234+first@" + "users.noreply.host.io>"
    log = (f"aaaa111\t{corp}\tHost <noreply@" + "host.io>\tInitial commit\t\x1e\n"
           f"bbbb222\t{ok}\t{ok}\tAdd module\tbody mentions zorblat\x1e\n"
           f"cccc333\t{ok}\t{ok}\tTidy\t\x1e\n")
    out = io.StringIO()
    hard, review = leak_scan.scan_history(".", scanner, out=out, log_text=log)
    assert (hard, review) == (2, 0)
    text = out.getvalue()
    assert "<commit aaaa111>" in text and "<commit bbbb222>" in text and "cccc333" not in text


def test_repository_is_clean():
    out = io.StringIO()
    hard, _review = leak_scan.scan_paths([str(ROOT)], out=out)
    assert hard == 0, out.getvalue()
