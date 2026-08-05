# Ruff Extra Rules

Extra Python rule checks and fixups that run as a pre-commit/prek hook alongside ruff — not a replacement for it.

[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![codecov](https://codecov.io/github/alessio-locatelli/ruff-extra-rules/graph/badge.svg?token=TMZ7VAVVUL)](https://codecov.io/github/alessio-locatelli/ruff-extra-rules)

## Project Status

- Stopgap until ruff adds plugin support ([astral-sh/ruff#283](https://github.com/astral-sh/ruff/issues/283)); see the [FAQ](docs/faq.md#what-will-happen-to-this-project-when-ruff-has-a-plugin-system) for what will happen then.
- This project is in the alpha stage, but it is already being used in a commercial project. Please open an issue if you see anything that can be improved.

## Available Checks

Individual checks are toggled with `--select`/`--ignore` (or the matching `pyproject.toml` keys), and `--fix` applies whatever each check's own fix logic considers safe — mirroring `ruff check`'s own `--select`/`--ignore`/`--fix` flags:

- `--select=<id>,<id>` restricts the hook to **only** the listed check(s).
- `--ignore=<id>,<id>` excludes the listed check(s) — it composes with `--select` rather than replacing it, just like `ruff check --select`/`--ignore`.

| Rule                                                                 | Code | Description                                                                                                                                                                       |
| -------------------------------------------------------------------- | ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [meaningless-vars](docs/rules/meaningless-vars.md)                   | TR1  | Flags meaningless variable names like `data`, `result`, and `results`.                                                                                                            |
| [excessive-blank-lines](docs/rules/excessive-blank-lines.md)         | TR2  | Collapses multiple blank lines after a module header to a single one.                                                                                                             |
| [redundant-super-init](docs/rules/redundant-super-init.md)           | TR3  | Flags `**kwargs` forwarded to a parent `__init__` that accepts none.                                                                                                              |
| [validate-function-name](docs/rules/validate-function-name.md)       | TR4  | Flags functions where `get_*` is misused or inappropriate and suggests a more specific verb.                                                                                      |
| [redundant-assignment](docs/rules/redundant-assignment.md)           | TR5  | Flags (and optionally inlines) variable assignments that add no clarity.                                                                                                          |
| [redundant-type-conversion](docs/rules/redundant-type-conversion.md) | TR6  | Flags a builtin type conversion that `ty` considers redundant given the argument's real type, including across files. Requires [`ty`](https://github.com/astral-sh/ty) on `PATH`. |
| [misplaced-comment](docs/rules/misplaced-comment.md)                 | TR7  | Moves a trailing comment off a closing bracket onto the expression line.                                                                                                          |

## Installation

Add to your `.pre-commit-config.yaml` — the same file [prek](https://github.com/j178/prek) and pre-commit both read:

```yaml
repos:
  - repo: https://github.com/alessio-locatelli/ruff-extra-rules
    rev: <tag-or-commit-sha> # pin a specific tag or commit; see the repo's tags for available versions
    hooks:
      - id: ruff-extra-rules
      - id: ruff-extra-rules-ty # optional: adds redundant-type-conversion (TR6), see below
```

`ruff-extra-rules-ty` runs [redundant-type-conversion](docs/rules/redundant-type-conversion.md) by itself. It's optional and requires [`ty`](https://github.com/astral-sh/ty) on `PATH`.

`ruff-extra-rules` always excludes `redundant-type-conversion`, and `ruff-extra-rules-ty` always runs only that check. Both read the same configuration and accept the same options; a check one hook can't run is simply left out rather than rejected, so a single configuration works for both.

## Configuration

Settings go in your `pyproject.toml`, under `[tool.ruff-extra-rules]`:

```toml
[tool.ruff-extra-rules]
fix = true                          # apply safe fixes without passing --fix
exclude = ["vendor/**"]             # glob patterns, relative to this file
ignore = ["misplaced-comment"]      # or select = [...] to run only certain checks

[tool.ruff-extra-rules.meaningless-vars]
level = "permissive"                # each check's own settings live in its own table
```

The nearest `pyproject.toml` with a `[tool.ruff-extra-rules]` table, searching upward from where the command runs, is the one used — so a monorepo can configure everything from its root. The search stops at your git repository. Command-line arguments win over the file, `--config` points at a specific file, and `--isolated` ignores configuration files entirely.

### Inline Suppression

Use `# pytriage: <code>` (e.g., `# pytriage: TR1`), or a comma-separated list to suppress more than one check on the same line (`# pytriage: TR1,TR5`).

---

See [FAQ](docs/faq.md) for more information.
