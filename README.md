# Ruff Extra Rules

Extra Python rule checks and fixups for pre-commit/prek, meant to run alongside ruff rather than replace it.

[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)

## Disclaimer

- This is not a standalone linter and not a `ruff` competitor. It's a small set of rules/fixups `ruff` doesn't (yet) have, run as an extra pre-commit/prek hook alongside `ruff` — not instead of it.
- This project is a stopgap until plugin support is implemented in `ruff` ([astral-sh/ruff#283](https://github.com/astral-sh/ruff/issues/283)), and will be archived thereafter.
- This is a best-effort proof-of-concept implemented using coding agents.

## Available Checks

Individual checks are toggled with `--select`/`--ignore`, and `--fix` applies whatever each check's own fix logic considers safe — mirroring `ruff check`'s own `--select`/`--ignore`/`--fix` flags:

- `--select=<id>,<id>` restricts the hook to **only** the listed check(s).
- `--ignore=<id>,<id>` excludes the listed check(s) — it composes with `--select` rather than replacing it, just like `ruff check --select`/`--ignore`.

| Rule                                                           | Code      | Description                                                              |
| -------------------------------------------------------------- | --------- | ------------------------------------------------------------------------ |
| [forbid-vars](docs/rules/forbid-vars.md)                       | TRI001    | Prevents meaningless variable names like `data` and `result`.            |
| [excessive-blank-lines](docs/rules/excessive-blank-lines.md)   | TRI002    | Collapses multiple blank lines after a module header to a single one.    |
| [redundant-super-init](docs/rules/redundant-super-init.md)     | TRI003    | Flags `**kwargs` forwarded to a parent `__init__` that accepts none.     |
| [validate-function-name](docs/rules/validate-function-name.md) | TRI004    | Flags `get_*` functions and suggests a more specific verb.               |
| [redundant-assignment](docs/rules/redundant-assignment.md)     | TRI005    | Flags (and optionally inlines) variable assignments that add no clarity. |
| [misplaced-comment](docs/rules/misplaced-comment.md)           | STYLE-001 | Moves a trailing comment off a closing bracket onto the expression line. |

## Installation

Add to your `.pre-commit-config.yaml` — the same file [prek](https://github.com/j178/prek) and pre-commit both read:

```yaml
repos:
  - repo: https://github.com/alessio-locatelli/ruff-extra-rules
    rev: <tag-or-commit-sha> # pin a specific tag or commit; see the repo's tags for available versions
    hooks:
      - id: ruff-extra-rules
```

### Running without prek/pre-commit

Try the checks directly, with no persistent install:

```bash
uvx --from git+https://github.com/alessio-locatelli/ruff-extra-rules python -m pre_commit_hooks.ast_checks src/
```

There are no other installable hook ids and no console-script entry point (`[project.scripts]` in `pyproject.toml` is intentionally empty) — every check runs via `python -m pre_commit_hooks.ast_checks`.

## Configuration

### Inline Suppression

Suppress violations on specific lines:

```python
# This will trigger a violation:
def process():
    data = get_user()
    return data

# This will be ignored:
def process():
    data = get_user()  # pytriage: ignore=TRI001
    return data
```

**Note:** The ignore comment must be on the same line as the violation.

### Cache Location

Check results are cached under `.cache/pre_commit_hooks/` relative to the process's current working directory, not a project root discovered independently of it — the same convention `mypy` (`.mypy_cache`) uses. `prek`/`pre-commit` always invoke this hook with the working directory set to the repository root, so the cache location is consistent there; running the CLI directly from elsewhere (see `AGENTS.md`) creates a separate `.cache/pre_commit_hooks/` under that directory instead. The cache itself is safe to delete at any time (see the `CACHEDIR.TAG` file it writes).
