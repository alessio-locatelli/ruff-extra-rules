# Adding a new AST check

Checks live under `src/pre_commit_hooks/ast_checks/` and plug into the grouped `ruff-extra-rules` hook — there is no per-check `.pre-commit-hooks.yaml` entry or console script to add.

## 1. Design

- Purpose: one check, one responsibility.
- Check id: kebab-case (e.g. `no-bare-except`).
- Error code: `TRI00N` (next unused number), or `STYLE-00N` for a purely stylistic, always-safe-to-autofix check like `misplaced-comment`.
- Violation message format and whether the check needs an autofix mode.

For the general prefilter-then-parse pipeline shape, see CLAUDE.md's "Suggested Check Architecture". Concretely for this repo: almost nothing qualifies for a grep-only check, because every existing check needs to distinguish syntax context that only an AST gives you — e.g. `forbid-vars` must tell `data = 1` (violation) apart from `obj.data = 1` (attribute, fine) and `"data = 1"` (inside a string, fine), and must catch `def foo(data):` (a parameter, not an assignment) that grep would miss entirely. Use `get_prefilter_pattern()` for a cheap `git grep` pass to skip files that can't possibly match, then do the real detection with `ast`.

## 2. Implement

Every check implements the `ASTCheck` protocol (`src/pre_commit_hooks/ast_checks/_base.py`) and should inherit `BaseCheck` from the same module:

```python
class ASTCheck(Protocol):
    @property
    def check_id(self) -> str: ...  # e.g. "forbid-vars"

    @property
    def error_code(self) -> str: ...  # e.g. "TRI001"

    def get_prefilter_pattern(self) -> list[str] | None: ...  # git-grep fast path, None = check every file

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]: ...

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> bool: ...

    # Optional: register/parse check-specific CLI arguments, e.g. --your-check-option
    def add_cli_arguments(cls, parser: argparse.ArgumentParser) -> None: ...
    def cli_kwargs_from_args(cls, args: argparse.Namespace) -> dict[str, Any]: ...
```

`CheckOrchestrator` parses each file's AST **once** and hands the same `tree`/`source` to every enabled check — `check()` must not re-parse the file.

`add_cli_arguments`/`cli_kwargs_from_args` are part of the protocol, so `type[ASTCheck]` (as used by `ALL_CHECKS`) requires both. `BaseCheck` provides a no-op default for each — inherit it (`class YourCheck(BaseCheck):`) unless your check actually needs its own CLI option, in which case override both.

Create `src/pre_commit_hooks/ast_checks/your_check.py` (or a package with `__init__.py` if the check needs multiple modules — see `validate_function_name/` for an example). Register the class in `ALL_CHECKS` in `src/pre_commit_hooks/ast_checks/__init__.py`. That's the whole registration step — no `.pre-commit-hooks.yaml` entry and no `[project.scripts]` entry. The check becomes selectable via `--select=your-check`/`--ignore=your-check` on the `ruff-extra-rules` hook and shows up in `python -m pre_commit_hooks.ast_checks --list-checks`.

**Requirements:**

- Standard library only, no external runtime dependencies.
- Never touch text inside string/byte literals or comments when writing an autofix — locate targets via AST node positions (`node.lineno`/`node.col_offset`/`node.end_lineno`/`node.end_col_offset`), not blind regex substitution over the whole file. See `validate_function_name/autofix.py` for a worked example of AST-scoped renaming.
- Support inline suppression: `# pytriage: ignore=TRI00N`.
- If the check is experimental or prone to false positives, keep it out of the default-enabled set via `args: [--ignore=your-check-id]` in `.pre-commit-hooks.yaml`.

## 3. Write tests

Create `tests/test_your_check.py` using `tmp_path` and `pytest.mark.parametrize` (see `tests/test_misplaced_comment.py` for the idiomatic pattern used here). Required coverage:

- Detection: true positives across the patterns the check targets.
- No false positives on idiomatic code the check should leave alone.
- Inline suppression (`# pytriage: ignore=TRI00N`).
- Autofix, if implemented — including that it never mutates unrelated text (string literals, comments, identically-named symbols in unrelated scopes).

For larger example files, add fixtures under `tests/fixtures/your_check/`, following the `good/`/`bad/`/`ignore/` (and `autofix/`, if relevant) convention used by `tests/fixtures/validate_function_name/`.

## 4. Update README.md

Add a subsection under "Available Checks" → "ruff-extra-rules (grouped)", following the format used by the existing checks (why it exists, a short example, suppression syntax).

## 5. Validate

```bash
uv run pytest tests/test_your_check.py -v
uv run python -m pre_commit_hooks.ast_checks --list-checks
uv run python -m pre_commit_hooks.ast_checks --select=your-check path/to/file.py
uv run ruff check --fix .
uv run ruff format .
uv run mypy src/ tests/
uv run coverage run -m pytest
uv run coverage report
```

## Conventions

**Docstrings**: Google style.

```python
def check_file(filepath: str, forbidden_names: set[str]) -> list[str]:
    """Check a Python file for forbidden variable names.

    Args:
        filepath: Path to the Python file to check
        forbidden_names: Set of forbidden variable names

    Returns:
        List of error messages (empty if no violations)
    """
```

**Error messages**: `filepath:line: TRI00N: clear message` — e.g. `src/app.py:42: TRI001: Forbidden variable name 'data' found. Use a more descriptive name.` Not `Error in file (line 42)`.

## Performance

The existing optimizations a new check should reuse rather than reimplement:

- **`_cache.py`**: a SHA-1 + mtime disk cache (like mypy/ruff's), keyed per file, so an unchanged file isn't re-analyzed on the next run.
- **`_prefilter.py`**: a `git grep`-based pass that skips files that can't possibly match before any Python parsing happens.
- **`CheckOrchestrator`**: parses each file's AST once per run and hands the same `tree`/`source` to every enabled check.

Guidelines:

- Let `CheckOrchestrator` read/parse each file once; don't re-read in `check()`/`fix()`.
- Prefer O(n) over O(n²); use set lookups instead of list searches.
- `prek`/`pre-commit` may run hooks in parallel across files — avoid shared mutable state without locking (see `_cache.py`).

```bash
uv run python scripts/benchmark.py --iterations=3 --clear-cache  # measures this repo's own src/+tests/
python -m cProfile -o profile.stats -m pre_commit_hooks.ast_checks --select=your-check src/
python -c "import pstats; p = pstats.Stats('profile.stats'); p.sort_stats('cumulative'); p.print_stats(20)"
```

Each check invocation pays Python interpreter startup once per subprocess, which tends to dominate over per-file analysis cost at this repo's current size — don't trust a single run's cold-vs-warm percentage as a stable signal, the sign can flip between runs. Re-run `scripts/benchmark.py` yourself to get current numbers rather than relying on a hardcoded figure in docs. Cache location: `.cache/pre_commit_hooks/` (safe to delete).
