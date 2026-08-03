# FAQ

## General

### Is this an official Astral project?

No.

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

## Configuration and technical questions

### Cache Location

Check results are cached under `.cache/pre_commit_hooks/` relative to the process's current working directory, not a project root discovered independently of it — the same convention `mypy` (`.mypy_cache`) uses. `prek`/`pre-commit` always invoke this hook with the working directory set to the repository root, so the cache location is consistent there; running the CLI directly from elsewhere creates a separate `.cache/pre_commit_hooks/` under that directory instead. The cache itself is safe to delete at any time (see the `CACHEDIR.TAG` file it writes).

### Why is `--fix` slower than a plain check, even when nothing needs fixing?

`--fix` doesn't use the cache described above, on any file, even one that turns out to have nothing to fix. A plain check-only run gets noticeably faster on an unchanged file once the cache is warm; `--fix` always re-analyzes every file from scratch, every run. If you're running `--fix` over a large, already-clean tree, expect it to cost about as long as the very first (cold) check-only run — every single time, not just once.

### Why `# pytriage` instead of `# noqa` for inline ignore comments?

It will be possible to switch to `# noqa` once this is a Ruff plugin. Until then, unregistered codes will trigger "Invalid rule code in `# noqa`" warnings.

### Running without prek/pre-commit

Try the checks directly, with no persistent install:

```bash
uvx --from git+https://github.com/alessio-locatelli/ruff-extra-rules python -m pre_commit_hooks.ast_checks src/
```

There are no other installable hook ids and no console-script entry point (`[project.scripts]` in `pyproject.toml` is intentionally empty) — every check runs via `python -m pre_commit_hooks.ast_checks`.
