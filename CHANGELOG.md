# Changelog

Every change a run of these hooks can show you: new and retired checks, what `--fix` rewrites, the command-line flags and `pyproject.toml` keys, and the output itself. [Releases](docs/releases.md) explains how a version number tells you whether an upgrade can change your results, and what to do about it.

Notes start at 0.0.50. Earlier tags shipped without them.

## [Unreleased]

### Changed

- `redundant-assignment` no longer treats a file differently for living under `tests/`/`test/` or being named `test_*.py`/`*_test.py`. Such a file used to get a quieter version of the check; it now reports exactly what the same code reports anywhere else, at both levels. To keep the check off your tests, list it under `[tool.ruff-extra-rules.per-file-ignores]`. See [ADR-0055](docs/adr/0055-redundant-assignment-ignores-the-file-path.md).

## [0.0.50] - 2026-08-15

### Changed

- A `--fix` run now reports a violation that some other fix removed along the way as `[RESOLVED INDIRECTLY]`, instead of leaving it out of the report. Every violation the run found is now accounted for in its output, so a run can print lines for the same files it used to stay silent about. See [ADR-0053](docs/adr/0053-indirect-resolution-outcome.md).
