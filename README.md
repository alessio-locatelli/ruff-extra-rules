# Ruff Extra Rules

Extra Python rule checks and fixups that run as a pre-commit/prek hook alongside ruff — not a replacement for it.

[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![codecov](https://codecov.io/github/alessio-locatelli/ruff-extra-rules/graph/badge.svg?token=TMZ7VAVVUL)](https://codecov.io/github/alessio-locatelli/ruff-extra-rules)

## Project Status

- Stopgap until ruff adds plugin support ([astral-sh/ruff#283](https://github.com/astral-sh/ruff/issues/283)); see the [FAQ](docs/faq.md#what-will-happen-to-this-project-when-ruff-has-a-plugin-system) for what will happen then.
- This project is in the alpha stage, but it is already being used in a commercial project. Please open an issue if you see anything that can be improved.

## Available Checks

When invoking `python -m pre_commit_hooks.ast_checks` directly, individual checks are toggled with `--select`/`--ignore`, and `--fix` applies whatever each check's own fix logic considers safe — mirroring `ruff check`'s own `--select`/`--ignore`/`--fix` flags:

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

`ruff-extra-rules-ty` runs [redundant-type-conversion](docs/rules/redundant-type-conversion.md) by itself, as a single serial process, instead of alongside every other check. That check shares one persistent `ty` session across files to catch cross-file cases the rest of `ruff-extra-rules` can't — so it's kept out of the main, parallel-batched hook to avoid several parallel workers contending over that one shared session. It's optional and requires [`ty`](https://github.com/astral-sh/ty) on `PATH`.

`ruff-extra-rules` always excludes `redundant-type-conversion`, and `ruff-extra-rules-ty` always runs only that check. Both accept shared options such as `--fix`; check-selection options are not supported by these hooks.

## Configuration

### Inline Suppression

Use `# pytriage: <code>` (e.g., `# pytriage: TR1`), or a comma-separated list to suppress more than one check on the same line (`# pytriage: TR1,TR5`).

---

See [FAQ](docs/faq.md) for more information.
