# Security policy

## Supported versions

Only the latest release on the `main` branch receives fixes. The current version is 0.1.0.

## What counts as a security problem here

`cpm_lineage` is an offline analysis library. It makes no network calls and has one runtime
dependency (numpy). It reads CSV files or in-memory records that you supply, and it writes only
when you give the action ledger a SQLite file path. The realistic security problems are therefore:

* a crafted input file or record that makes the library execute code, or read or write a file
  other than the ones you named;
* SQL injection through values that reach the SQLite ledger backend;
* a secret, credential, private path or personal data committed to the repository or its history.

A wrong number, a rule that fires when it should not, or a crash on unusual input is an ordinary
bug. Please open a normal issue for those.

## Reporting

Please do not open a public issue for a security problem. Use GitHub's private vulnerability
reporting instead: on the repository page, open the **Security** tab and choose **Report a
vulnerability**. Include the version, a minimal input that reproduces the problem, and what you
observed.

This project is maintained by one person in spare time, so there is no guaranteed response time.
Reports are read, and a fix or a statement of why no fix is needed will follow as soon as is
practical. You will be credited in the release notes unless you ask not to be.

## Handling your own data

The library processes ad account data on your machine and sends it nowhere. Two things remain your
responsibility:

* A SQLite ledger file holds your plans, entity names and spend figures in plain form. Store it
  as you would any other business data.
* If you attach data to an issue, remove anything that identifies a real account first. The
  synthetic adapter (`SyntheticAdapter(seed=...)`) reproduces most problems without real data.
