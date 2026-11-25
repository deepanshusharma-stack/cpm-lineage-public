#!/usr/bin/env python3
"""Structural leak scan for this repository.  Exit status 1 on any HARD hit.

The scanner looks for *shapes* that should never be committed, whatever the
project: personal home-directory paths, private-key headers, fully
qualified warehouse object names, e-mail addresses, secrets, UUIDs and IP
addresses.  It carries **no list of terms**.

A project that also wants to block specific words (customer names, internal
codenames, ...) keeps that list in a private file *outside the repository*
and points the scanner at it::

    CPM_LINEAGE_LEAK_TERMS=/path/outside/the/repo/terms.txt python tools/leak_scan.py

One entry per line; blank lines and lines starting with ``#`` are ignored.
An entry is a regular expression matched case-insensitively.  Prefixes:
``cs:`` makes it case-sensitive, ``review:`` reports it in the REVIEW tier
(the two can be combined as ``review:cs:``).  The file is refused when it
sits inside a scanned tree, so it cannot be committed by accident, and its
entries are never printed: a hit shows the text found in the repository.

Two tiers:

* ``HARD``   must be zero.
* ``REVIEW`` allowed, but each hit deserves a second look (a generic
  secret, UUID, IP address or hard-coded currency amount).

Usage::

    python tools/leak_scan.py [path ...]     # default: the repository root
    python tools/leak_scan.py --history      # also scan git commit metadata
    python tools/leak_scan.py --strict       # REVIEW hits also fail
    python tools/leak_scan.py --quiet        # summary line only

A line containing ``leakscan: allow`` is skipped.  ``.gz`` files and zip
containers (including office documents) are scanned through their members;
file paths are scanned as well as contents.  ``--history`` reads author and
committer identities, subjects and bodies of every commit on every ref; an
e-mail address there is HARD unless it is a ``noreply`` address.

What this scan cannot see: paraphrased facts, figures lifted from private
data, and images.  It is a guard, not a proof.
"""

from __future__ import annotations

import argparse
import gzip
import io
import os
import re
import subprocess
import sys
import unicodedata
import zipfile
from typing import Iterable, Iterator

HERE = os.path.dirname(os.path.abspath(__file__))
TERMS_ENV = "CPM_LINEAGE_LEAK_TERMS"
ALLOW_PRAGMA = "leakscan: allow"
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache", ".pytest_cache",
             ".ruff_cache", "dist", "build", ".eggs", "htmlcov"}
SKIP_SUFFIXES = (".egg-info",)

HARD, REVIEW = "HARD", "REVIEW"

_HARD_SHAPES = re.compile(
    r"""(?x)
      /(?:Users|home)/[A-Za-z0-9._-]+/                  # a personal home-directory path
    | [A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\               # the same on Windows
    | -----BEGIN\s[A-Z ]*PRIVATE\sKEY                   # key material
    | \b[A-Z][A-Z0-9_]{2,}\.[A-Z][A-Z0-9_]{2,}\.[A-Z_][A-Z0-9_]{2,}\b   # database.schema.object, upper case
    """
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
_SAFE_EMAIL = re.compile(r"(?i)(?:^|[.@])(?:example\.(?:com|org|net)|[a-z0-9.-]*\.(?:test|invalid|example))$|^(?:[a-z0-9-]+\+)?noreply@|@(?:[a-z0-9-]+\.)*noreply\.")
_REVIEW_SHAPES = re.compile(
    r"""(?ix)
      (?:api[_-]?key|secret|password|passwd|token|bearer)\s*[=:]\s*['"][^'"\s]{8,}
    | \b(?:sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|AIza[0-9A-Za-z_-]{35}
         |xox[abprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})
    | \b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b
    | [₠-₿£¥]\s?\d                                   # a hard-coded currency amount
    | \b\d{1,3}(?:\.\d{1,3}){3}\b
    """
)


def normalise(text: str) -> str:
    """Strip accents and typographic apostrophes so one spelling of a term is enough."""
    text = text.replace("’", "'").replace("‘", "'")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def safe_email(address: str) -> bool:
    """True for placeholder and ``noreply`` addresses, which identify nobody."""
    return bool(_SAFE_EMAIL.search(address))


class Scanner:
    """Structural shapes plus optional private terms supplied at run time."""

    def __init__(self, terms: Iterable[str] = ()) -> None:
        self.terms: list[tuple[str, re.Pattern[str]]] = []
        self.terms_path: str | None = None
        for line in terms:
            self.add_term(line)

    def add_term(self, entry: str) -> None:
        """Add one term-file entry (see the module docstring for the syntax)."""
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            return
        level, flags = HARD, re.IGNORECASE
        while True:
            if entry.startswith("review:"):
                level, entry = REVIEW, entry[7:]
            elif entry.startswith("cs:"):
                flags, entry = 0, entry[3:]
            else:
                break
        self.terms.append((level, re.compile(normalise(entry), flags)))

    @classmethod
    def from_env(cls, environ: dict | None = None) -> "Scanner":
        """Scanner with the private term file named by ``CPM_LINEAGE_LEAK_TERMS``, if any."""
        path = (environ if environ is not None else os.environ).get(TERMS_ENV)
        return cls.from_file(path) if path else cls()

    @classmethod
    def from_file(cls, path: str) -> "Scanner":
        with open(path, encoding="utf-8") as fh:
            sc = cls(fh.read().splitlines())
        sc.terms_path = os.path.realpath(path)
        return sc

    def scan_line(self, line: str, *, emails: str = REVIEW) -> list[tuple[str, str]]:
        """All ``(level, matched text)`` hits on one line.

        ``emails`` is the tier of a real-looking e-mail address (placeholder
        and noreply addresses are never reported).
        """
        hits: list[tuple[str, str]] = []
        for m in _HARD_SHAPES.finditer(line):
            hits.append((HARD, m.group(0)))
        for m in _EMAIL.finditer(line):
            if not safe_email(m.group(0)):
                hits.append((emails, m.group(0)))
        for m in _REVIEW_SHAPES.finditer(line):
            hits.append((REVIEW, m.group(0)))
        if self.terms:
            text = normalise(line)
            for level, rx in self.terms:
                for m in rx.finditer(text):
                    if m.group(0):
                        hits.append((level, m.group(0)))
        seen: set[tuple[str, str]] = set()
        return [h for h in hits if not (h in seen or seen.add(h))]


# --------------------------------------------------------------------------
# File walking
# --------------------------------------------------------------------------

def _read(path: str) -> bytes | None:
    try:
        if path.endswith(".gz"):
            with gzip.open(path, "rb") as fh:
                return fh.read()
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if data[:4] == b"PK\x03\x04":                       # zip container: scan the members
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                return b"\n".join(zf.read(n) for n in zf.namelist())
        except (zipfile.BadZipFile, OSError):
            return data
    return data


def iter_files(root: str) -> Iterator[str]:
    if os.path.isfile(root):
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(SKIP_SUFFIXES))
        for name in sorted(filenames):
            yield os.path.join(dirpath, name)


def scan_text(text: str, scanner: Scanner, *, emails: str = REVIEW) -> Iterator[tuple[int, str, str]]:
    """Yield ``(line number, level, matched text)`` for a block of text."""
    for no, line in enumerate(text.split("\n"), 1):
        if ALLOW_PRAGMA in line:
            continue
        for level, found in scanner.scan_line(line, emails=emails):
            yield no, level, found


def _inside(path: str, root: str) -> bool:
    root = os.path.realpath(root)
    return os.path.commonpath([os.path.realpath(path), root]) == root if os.path.isdir(root) else False


def scan_paths(paths: Iterable[str], scanner: Scanner | None = None, *, out=None) -> tuple[int, int]:
    """Scan files and directories.  Returns ``(hard hits, review hits)``; prints each hit to ``out``."""
    scanner = scanner or Scanner()
    paths = list(paths)
    if scanner.terms_path and any(_inside(scanner.terms_path, p) for p in paths):
        raise ValueError(f"the private term file must live outside the scanned tree ({TERMS_ENV})")
    hard = review = 0
    for root in paths:
        base = root if os.path.isdir(root) else os.path.dirname(root) or "."
        for path in iter_files(root):
            rel = os.path.relpath(path, base)
            for level, found in scanner.scan_line(rel):                # file names leak too
                if level == HARD:
                    hard += 1
                    if out is not None:
                        print(f"{level}\t{path}\t<path>\t{found!r}", file=out)
            data = _read(path)
            if data is None:
                continue
            for no, level, found in scan_text(data.decode("utf-8", "replace"), scanner):
                if level == HARD:
                    hard += 1
                else:
                    review += 1
                if out is not None:
                    print(f"{level}\t{path}:{no}\t{found!r}", file=out)
    return hard, review


def scan_history(repo: str, scanner: Scanner | None = None, *, out=None, log_text: str | None = None) -> tuple[int, int]:
    """Scan commit metadata of every ref: identities, subjects and bodies.

    An e-mail address in history is HARD unless it is a noreply or
    placeholder address, because history is published with the tree and is
    not fixed by a later commit.  ``log_text`` replaces the ``git log`` call
    (used by the tests).
    """
    scanner = scanner or Scanner()
    if log_text is None:
        log_text = subprocess.run(
            ["git", "-C", repo, "log", "--all", "--format=%h%x09%an <%ae>%x09%cn <%ce>%x09%s%x09%b%x1e"],
            check=True, capture_output=True, text=True).stdout
    hard = review = 0
    for record in log_text.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        sha = record.split("\t", 1)[0]
        for _no, level, found in scan_text(record, scanner, emails=HARD):
            if level == HARD:
                hard += 1
            else:
                review += 1
            if out is not None:
                print(f"{level}\t<commit {sha}>\t{found!r}", file=out)
    return hard, review


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Structural leak scan (plus optional private terms).")
    ap.add_argument("paths", nargs="*", help="files or directories (default: the repository root)")
    ap.add_argument("--strict", action="store_true", help="fail on REVIEW hits as well")
    ap.add_argument("--quiet", action="store_true", help="print the summary line only")
    ap.add_argument("--history", action="store_true", help="also scan git commit metadata of every ref")
    ap.add_argument("--terms", default=None, help=f"private term file outside the repository (default: ${TERMS_ENV})")
    args = ap.parse_args(argv)
    scanner = Scanner.from_file(args.terms) if args.terms else Scanner.from_env()
    paths = args.paths or [os.path.dirname(HERE)]
    out = None if args.quiet else sys.stdout
    try:
        hard, review = scan_paths(paths, scanner, out=out)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.history:
        h, r = scan_history(paths[0] if os.path.isdir(paths[0]) else os.path.dirname(HERE), scanner, out=out)
        hard, review = hard + h, review + r
    extra = f", {len(scanner.terms)} private terms" if scanner.terms else ", structural shapes only"
    print(f"-- {hard} HARD, {review} REVIEW ({'tree + history' if args.history else 'tree'}{extra})", file=sys.stderr)
    return 1 if hard or (args.strict and review) else 0


if __name__ == "__main__":
    sys.exit(main())
