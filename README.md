# Ruff Extra Rules

Extra Python rule checks and fixups that run as a pre-commit/prek hook alongside ruff — not a replacement for it.

[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![codecov](https://codecov.io/github/alessio-locatelli/ruff-extra-rules/graph/badge.svg?token=TMZ7VAVVUL)](https://codecov.io/github/alessio-locatelli/ruff-extra-rules)

## Project Status

- Stopgap until ruff adds plugin support ([astral-sh/ruff#283](https://github.com/astral-sh/ruff/issues/283)); see the [FAQ](docs/faq.md#what-will-happen-to-this-project-when-ruff-has-a-plugin-system) for what will happen then.
- This project is in the alpha stage, but it is already being used in a commercial project. Please open an issue if you see anything that can be improved.

## Available Checks

Individual checks are toggled with `--select`/`--ignore`, and `--fix` applies whatever each check's own fix logic considers safe — mirroring `ruff check`'s own `--select`/`--ignore`/`--fix` flags:

- `--select=<id>,<id>` restricts the hook to **only** the listed check(s).
- `--ignore=<id>,<id>` excludes the listed check(s) — it composes with `--select` rather than replacing it, just like `ruff check --select`/`--ignore`.

| Rule                                                           | Code      | Description                                                                                  |
| -------------------------------------------------------------- | --------- | -------------------------------------------------------------------------------------------- |
| [forbid-vars](docs/rules/forbid-vars.md)                       | TRI001    | Prevents meaningless variable names like `data` and `result`.                                |
| [excessive-blank-lines](docs/rules/excessive-blank-lines.md)   | TRI002    | Collapses multiple blank lines after a module header to a single one.                        |
| [redundant-super-init](docs/rules/redundant-super-init.md)     | TRI003    | Flags `**kwargs` forwarded to a parent `__init__` that accepts none.                         |
| [validate-function-name](docs/rules/validate-function-name.md) | TRI004    | Flags functions where `get_*` is misused or inappropriate and suggests a more specific verb. |
| [redundant-assignment](docs/rules/redundant-assignment.md)     | TRI005    | Flags (and optionally inlines) variable assignments that add no clarity.                     |
| [misplaced-comment](docs/rules/misplaced-comment.md)           | STYLE-001 | Moves a trailing comment off a closing bracket onto the expression line.                     |

## Installation

Add to your `.pre-commit-config.yaml` — the same file [prek](https://github.com/j178/prek) and pre-commit both read:

```yaml
repos:
  - repo: https://github.com/alessio-locatelli/ruff-extra-rules
    rev: <tag-or-commit-sha> # pin a specific tag or commit; see the repo's tags for available versions
    hooks:
      - id: ruff-extra-rules
```

## Configuration

### Inline Suppression

Use `# pytriage: ignore=<code>` (e.g., `# pytriage: ignore=TRI001`).

---

See [FAQ](docs/faq.md) for more information.
