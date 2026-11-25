# Contributing to cpm-lineage

Thank you for looking at the code. Bug reports, platform adapters, clearer documentation and
better-argued thresholds are all welcome. Open an issue first for anything larger than a small
fix, so the approach can be agreed before you write it.

## Set up

```bash
git clone https://github.com/deepanshusharma-stack/cpm-lineage-public.git
cd cpm-lineage-public
pip install -e ".[dev]"
python -m pytest
```

Python 3.10 or later. The only runtime dependency is numpy; pandas is optional and needed only to
convert records back to a frame with `adapters.dataframe.to_dataframe` (`pip install -e ".[pandas]"`);
`DataFrameAdapter` itself reads any frame-like object or a list of dicts without importing it.

## Before you open a pull request

Run these from the repository root. All three must pass.

```bash
python -m pytest
python tools/leak_scan.py
python tools/gen_thresholds.py --check
```

* `python -m pytest` includes tests that execute every code block in `README.md` and compare
  `docs/thresholds.md` with the source.
* `python tools/leak_scan.py` must report `0 HARD`. It looks for shapes that should never be
  committed: home-directory paths, key headers, secrets, e-mail addresses and fully qualified
  warehouse names.
* If you add or change a config field, regenerate the threshold reference with
  `python tools/gen_thresholds.py` and commit the result.

## Ground rules for code

These are the design principles the library is held to. A change that breaks one needs a stated
reason.

1. **Pure functions.** Analysis code takes records and returns dataclasses. File I/O belongs in
   `adapters/` and `store/` only. No network access anywhere. No function outside `store/` reads
   the wall clock; "today" is the latest date in the data or an explicit argument.
2. **Every threshold is a named config field.** Do not put a number that changes a decision in a
   function body. Add a field to the frozen config dataclass next to the code, with a default and
   a one-line comment that says what it means and why that value.
3. **Nothing platform-specific.** Rules read capabilities from `PlatformProfile` and never test a
   platform name. If a rule needs something a platform may lack (a position field, a second-price
   auction, a click feed), it must abstain with a stated reason when the capability is absent.
4. **Abstain rather than guess.** A missing feed or too little data produces "not assessed",
   `thin-data` or a hold with a reason. It never produces a number.
5. **Exact where it claims to be exact.** A decomposition that is described as exact must add up
   in a test, including at the edges (zero impressions, zero units, an empty prior window).
6. **Deterministic.** Same input, same output. Seed any generator. Sort before you iterate over a
   set or a mapping whose order could reach the output.

## Ground rules for data and wording

* **Synthetic data only.** Do not commit exports, screenshots or numbers from a real ad account,
  and do not name real advertisers, brands or campaigns in code, tests, examples or documents.
  Invent names that are obviously invented.
* **No performance claims.** This repository contains no results from real accounts. Documentation
  may report only what a reader can reproduce by running the library on its synthetic data, with
  the command and the seed stated.
* **Name ad platforms generically** ("quick-commerce marketplaces, retail media networks, display
  exchanges"). An adapter for a specific platform is welcome as an example that uses that
  platform's public export format and invented data.
* **State limits.** If a rule rests on an assumption (an auction type, an attribution window, a
  money scale), say so in the docstring and in `docs/rule_catalogue.md`.

## Tests

* Every behaviour change needs a test. Every bug fix needs a test that fails without the fix.
* Tests build their own small inputs or use `SyntheticAdapter(seed=...)`. They do not read files
  outside the repository or depend on the current date.
* Keep the suite fast. It currently runs in seconds; a test that needs many seeds should use the
  fewest that make the point.

## Changing a default threshold

The defaults are judgment values, and a better-argued value is a welcome change. In the pull
request, say what the field decides, what the old and new values do on the synthetic data (which
findings and cards change, under which seeds), and the reasoning for the new value. "It works
better on my account" is useful context in an issue, but it cannot be verified here, so the
argument in the pull request has to stand on reasoning and on the synthetic data.

## Reporting a bug

Open an issue with the smallest input that shows the problem. A few hand-written `PerfRow`
records, or a `SyntheticAdapter` seed, is ideal. Include the output you got, the output you
expected, and the Python and numpy versions. Remove anything that identifies a real account before
you post.

Security problems should not go in a public issue. See [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contribution is released under the MIT license in `LICENSE`.
