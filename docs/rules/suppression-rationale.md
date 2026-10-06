# suppression-rationale (TR12)

Requires an explanation for Ruff and `# pytriage` suppression comments. This check is opt-in and only reports diagnostics; it never changes files.

Enable it with `--extend-select=suppression-rationale`, or in `pyproject.toml`:

```toml
[tool.ruff-extra-rules]
extend-select = ["suppression-rationale"]
```

A suppression without an explanation is reported:

```python
import plugin  # noqa: F401
```

Plain trailing text is enough. The optional `#` and `--` separators also work:

```python
import plugin  # noqa: F401 imported for side effects
import plugin  # noqa: F401  # imported for side effects
import plugin  # noqa: F401 -- imported for side effects
```

A separator without text does not count. An immediately preceding contiguous comment block also counts, including for a suppression on its own line:

```python
# Importing this module registers the plugin.
# Registration happens during module import.
import plugin  # noqa: F401
```

A blank line breaks the association. The check uses adjacency alone: it does not judge an explanation's quality, length, relevance, or truthfulness. Ambiguous and malformed cases are accepted to avoid false positives, including trailing content that might be another pragma.

Supported comments include legacy `noqa`, file-level `ruff: noqa` and `flake8: noqa`, and Ruff's `ignore[...]`, `file-ignore[...]`, and `disable[...]` directives. Ruff rule names are supported as well as codes. For a clearly matched `ruff: disable[...]` / `ruff: enable[...]` range, only `disable` needs an explanation; `enable` does not. Unmatched or unusual ranges are accepted.

Ruff's import-sorting suppressions (`isort: skip`, `isort: skip_file`, and clearly paired `isort: off` / `isort: on`, including `ruff: isort:` variants) are also covered. `on` and `split` do not need explanations. See [Ruff's suppression documentation](https://docs.astral.sh/ruff/linter/#error-suppression) for recognized syntax.

Unrelated suppression systems, such as `type: ignore`, NOSONAR, pylint, mypy, pyright, and Bandit pragmas, are outside this check's scope.

The check follows the normal `# pytriage: TR12` suppression and usage-tracking rules. An explanation on that directive makes it unnecessary for TR12, so `unused-pytriage`, when enabled, can report the redundant TR12 entry.
