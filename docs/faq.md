# FAQ

## General

### Why not a Flake8 plugin?

Some rules and opt-in auto-fixing require more functionality.

### What happens to rules that will be added to Ruff?

These rules will be deleted here in favor of the official Ruff implementation.

### What will happen to this project when Ruff has a plugin system?

This project will be converted into a Ruff plugin to support rules that are not yet available in Ruff.

### I'd like to implement a new rule that is not available in Ruff. Can we add it here?

Given that this project exists for rules that are missing in Ruff, the preferred order of actions is:

1. If it does not exist yet, open a discussion in Ruff with your proposal. The rationale is that if Ruff implements it soon, duplicating the rule here is a waste of effort.
2. Open an issue here with the rule proposal.

### I got a false positive

- Default settings favor low-risk reports, but rules can still have documented false positives. For example, `redundant-type-conversion` can report `int(value)` when `value` is annotated as `int` but holds `True` or `False` at runtime; that conversion changes the result to `1` or `0`. Check the [rule documentation](rules/redundant-type-conversion.md#reporting-level) for known limitations. If a report is not documented, please open an issue.
- If you are using the aggressive detection level, it typically provides better detection at the cost of rare false positives. If this false positive is not already documented in `docs/rules/` as a known limitation of the aggressive level, please open an issue.

## Configuration and technical questions

### Cache Location

Check results are cached under `.cache/pre_commit_hooks/` in your project root — the directory holding the `pyproject.toml` whose `[tool.ruff-extra-rules]` table configured the run. Running the CLI from a subdirectory therefore reuses the same cache rather than starting a new one. With no configuration file (or under `--isolated`), it falls back to the working directory. Deleting it clears cached results (see the `CACHEDIR.TAG` file it writes). One copy of this directory is special: the one at the root of your git repository, which may be above your project root, also holds the socket of the background `ty` process that `redundant-type-conversion` keeps per repository. Deleting that copy while the process runs leaves the process unreachable but alive until it has been idle for 15 minutes. The next run starts a new one, which has not seen your other files yet, so until they are checked again it can miss conversions that depend on them (see [redundant-type-conversion](rules/redundant-type-conversion.md)).

### Why `# pytriage` instead of `# noqa` for inline ignore comments?

It will be possible to switch to `# noqa` once this is a Ruff plugin. Until then, unregistered codes will trigger "Invalid rule code in `# noqa`" warnings.

### Does this catch syntax errors?

Not reliably — treat that as out of scope. Run `check-ast` (from [`pre-commit/pre-commit-hooks`](https://github.com/pre-commit/pre-commit-hooks)) or `ruff check` in the same `.pre-commit-config.yaml`; either always reports a syntax error, which this tool doesn't guarantee.

### Running without prek/pre-commit

Try the checks directly, with no persistent install:

```bash
uvx --from git+https://github.com/alessio-locatelli/ruff-extra-rules python -m pre_commit_hooks.ast_checks src/
```

To run every check at its `aggressive` level, pass each check's level option. `--isolated` ignores your project's configuration, so its `select`, `ignore`, or exclusions can't skip anything. `redundant-type-conversion` needs [`ty`](https://github.com/astral-sh/ty) on `PATH`, which `--with ty` provides:

```bash
uvx --from git+https://github.com/alessio-locatelli/ruff-extra-rules --with ty python -m pre_commit_hooks.ast_checks \
  --isolated \
  --defined-far-from-use-level=aggressive \
  --meaningless-vars-level=aggressive \
  --overly-broad-return-type-level=aggressive \
  --redundant-assignment-level=aggressive \
  --redundant-dict-get-level=aggressive \
  --redundant-type-conversion-level=aggressive \
  src/
```

There are no other installable hook ids and no console-script entry point (`[project.scripts]` in `pyproject.toml` is intentionally empty) — every check runs via `python -m pre_commit_hooks.ast_checks`.

Run `ruff check` (or `check-ast`) yourself alongside this, the same way `.pre-commit-config.yaml` would — see [Does this catch syntax errors?](#does-this-catch-syntax-errors).
