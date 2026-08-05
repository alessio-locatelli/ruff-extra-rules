# Configure from `pyproject.toml`, resolved once per run

Until now every setting came from CLI arguments alone: ADR-0019 recorded that no config-file parsing, environment-variable precedence, or project-root discovery existed anywhere in this pipeline. That put each check's configuration in `.pre-commit-config.yaml`'s `args`, where it is invisible to anyone running the checks directly and has to be repeated per hook.

`ruff` and `ty` are the reference points, but they do not agree on how configuration is discovered, so "match ruff and ty" was not one target. `ruff` resolves hierarchically: the closest config file wins for each individual file, with no merging across files. `ty` walks up from the project directory once and uses a single project-level config. This decision follows `ty`.

## Decision

A run resolves exactly one configuration, from the nearest `pyproject.toml` carrying a `[tool.ruff-extra-rules]` table, searching upward from the working directory. A `pyproject.toml` without the table does not halt the search, so a monorepo root still configures a subpackage that has its own packaging metadata but no opinion about this tool. The search never leaves the enclosing git repository, which is what keeps a stray `pyproject.toml` in a home directory from silently configuring every run beneath it — the "unrelated parent directory" case chapter 28 of the behavioral contract forbids, and a case `ruff`'s own walk to the filesystem root does not cover.

One resolved configuration per process, never per file. This is what lets checks stay instantiated once per run, leaving the cache identity ADR-0044 established untouched: per-check settings arrive as constructor arguments, so they already reach `hook_name` through the existing instance fingerprint and no new invalidation mechanism is needed.

`pyproject.toml` is the only supported source. A standalone config file, as `ruff.toml` and `ty.toml` are, would double the discovery surface and add a same-directory precedence rule; it can be added later without breaking anything.

The table carries `select`, `ignore`, `exclude`, and `fix`, plus one sub-table per check keyed by its `check_id` (`[tool.ruff-extra-rules.meaningless-vars]`). There is no intermediate `lint` section, since unlike `ruff` this tool has no formatter half to disambiguate from. Command-line arguments override the file, which overrides each option's declared default.

`--config` names a `pyproject.toml` explicitly and skips the search; `--isolated` ignores configuration files entirely. `ruff`'s inline `--config "key = value"` form is deliberately not implemented: it exists there mainly for settings with no dedicated flag, and every option here generates its own flag (ADR-0047), so it would add a precedence tier with no capability behind it.

Nothing is restricted per entry point — not check selection, and not a check's own options. Both published hooks accept `--select`/`--ignore` and honor the configured `select`/`ignore`, each intersected against the checks that entry point actually runs; both likewise accept every check's own flag and sub-table, applying only those belonging to the checks they run. A setting for a check an entry point can't run is accepted and ignored, never rejected, so one set of options and one configuration file work with either hook. Neither `ruff-pre-commit` nor `ty-pre-commit` restricts rule selection, and one project-wide configuration has to serve both hooks: rejecting a check id the other hook owns would make an obviously-correct configuration fail. A selection that leaves an entry point with nothing to run is therefore a legitimate outcome that exits 0, matching `ruff check` with `select = []` and `ruff format` given a lint-only configuration.

Invalid configuration exits 2 — a new tier, distinct from 1 (the run completed and found something) and 0. It covers malformed TOML, an unknown field or value, an unknown check id from either source, and an unusable `--config` path. Every message names where the value came from: the absolute path of the file, or the CLI.

Validation runs over the whole file, before precedence is applied and independently of it. Validating only the settings the command line leaves unset would let an override launder an invalid file into an accepted one, and for `fix` that is the difference between reporting the error and letting an unvalidated file authorize rewriting sources. For the same reason a setting is validated even when the entry point reading it will not apply it — an option belonging to a check it does not run, say — so one shared file is accepted or rejected identically by both hooks. This supersedes ADR-0012's contract, under which an unknown `--select` name exited 1: with configuration now arriving from two places, "the run never happened" has to be distinguishable from "the run found problems", and the same typo must not report a different exit code depending on which source it came from.

An explicitly empty selection and a malformed one are different. `select = []` is a legitimate instruction to run nothing and exits 0; `--select=,,,`, which names no check at all once blank tokens are dropped, is malformed input and exits 2. `ruff` draws the same line, accepting `select = []` while rejecting `--select=` as an unknown rule selector.

The cache directory moves to the discovered project root, replacing the working-directory-relative convention ADR-0019 chose on the grounds that no project root was ever discovered. Once a root is known, keeping the cache relative to the working directory violates chapter 28's requirement that the same invocation produce predictable results from different working directories when the selected project is the same. Runs with no discovered configuration, and `--isolated` runs, still resolve it against the working directory.

## Considered Options

- **`ruff`'s per-file hierarchical resolution**: rejected. Different files in one run could then need different check instances and different cache identities, which is a substantial change to the orchestrator for a capability a pre-commit hook — always invoked with the working directory at the repository root — does not need.
- **Honor the configuration file but keep check selection unavailable on the published hooks**: rejected. It gives two different answers to "can this hook select checks?" depending on whether you ask the command line or the configuration file.
- **Reject a `select` naming a check the running entry point cannot run**: rejected. It breaks the shared-configuration case outright, which is the normal case for a repository running both hooks.
- **Keep configuration errors at exit 1**: rejected. It leaves an invalid configuration that checked nothing indistinguishable from a clean run that found violations, which matters most in CI.

## Consequences

- ADR-0019 is superseded on both points it settled: configuration now comes from a file as well as the CLI, and the cache directory is anchored at the project root rather than the working directory.
- ADR-0012's exit-code contract gains a third value; existing tooling that treats any non-zero exit as failure is unaffected, but anything distinguishing 1 from 2 sees unknown-check-name errors move.
- Every distinct project root keeps its own cache directory. Existing caches under a working directory that is not the project root are orphaned rather than migrated — a one-time miss, the same cost every prior cache-key change carried.
- This repository configures itself in its own `pyproject.toml`, so the test suite must opt out of discovery or inherit `fix = true` and rewrite files no test asked it to.
- A malformed `pyproject.toml` anywhere on the search path fails the run, even when the table would have been found further up. Reporting it is deliberate: silently walking past a file that cannot be parsed would hide a real problem the user needs to fix.
