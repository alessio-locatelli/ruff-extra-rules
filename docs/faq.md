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

Check results are cached under `.cache/pre_commit_hooks/` in your project root — the directory holding the `pyproject.toml` whose `[tool.ruff-extra-rules]` table configured the run. Running the CLI from a subdirectory therefore reuses the same cache rather than starting a new one. With no configuration file (or under `--isolated`), it falls back to the working directory. The cache is safe to delete at any time (see the `CACHEDIR.TAG` file it writes).

### Why `# pytriage` instead of `# noqa` for inline ignore comments?

It will be possible to switch to `# noqa` once this is a Ruff plugin. Until then, unregistered codes will trigger "Invalid rule code in `# noqa`" warnings.

### Running without prek/pre-commit

Try the checks directly, with no persistent install:

```bash
uvx --from git+https://github.com/alessio-locatelli/ruff-extra-rules python -m pre_commit_hooks.ast_checks src/
```

There are no other installable hook ids and no console-script entry point (`[project.scripts]` in `pyproject.toml` is intentionally empty) — every check runs via `python -m pre_commit_hooks.ast_checks`.
