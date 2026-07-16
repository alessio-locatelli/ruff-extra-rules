# Contributing to Pre-Commit Extra Hooks

Thank you for contributing to this project! This guide will help you add new checks, update existing ones, and maintain the repository.

## Table of Contents

- [Getting Started](#getting-started)
- [Adding a New Check](#adding-a-new-check)
- [Updating Existing Checks](#updating-existing-checks)
- [Semantic Versioning](#semantic-versioning)
- [Backward Compatibility](#backward-compatibility)
- [Performance Testing](#performance-testing)
- [Release Process](#release-process)
- [CI/CD Configuration](#cicd-configuration)
- [Code Quality Standards](#code-quality-standards)

## Getting Started

### Prerequisites

- Python 3.13 or later
- [uv](https://docs.astral.sh/uv/)
- Git
- pre-commit framework, or [prek](https://github.com/j178/prek) as a drop-in alternative

### Setup Development Environment

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/pre-commit-extra-hooks.git
cd pre-commit-extra-hooks

# Install dependencies (creates .venv automatically)
uv sync

# Install pre-commit hooks (dogfooding!)
uv run pre-commit install
```

### Run Tests

```bash
# Run all tests
uv run pytest

# Run tests for a specific check
uv run pytest tests/test_forbid_vars.py -v

# Run with coverage
uv run coverage run -m pytest
uv run coverage report
```

### Run Linters

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy src/ tests/
```

## Adding a New Check

Checks live under `src/pre_commit_hooks/ast_checks/` and plug into the grouped `ast-checks` hook — there is no per-check `.pre-commit-hooks.yaml` entry or console script to add. Follow these steps:

### 1. Design Phase

Before writing code:

- Define the check's purpose (single responsibility)
- Decide on a check id (kebab-case, e.g. `no-bare-except`) and an error code (`TRI00N`, next unused number)
- Plan the violation message format and whether it needs an autofix mode
- **Choose the right implementation approach** (see below)

#### Choosing Between Bash/Grep and Python/AST

Use this decision tree to choose the right tool:

##### ✅ Use Bash/Grep When:

The check is **pattern-based** and **context-independent**:

```bash
# ✓ Check for trailing whitespace
grep -n ' $' "$@"

# ✓ Check for TODO/FIXME comments
grep -n 'TODO\|FIXME' "$@"

# ✓ Check for hardcoded IPs
grep -E '\b([0-9]{1,3}\.){3}[0-9]{1,3}\b' "$@"
```

**Characteristics of bash-appropriate checks:**

- Simple string/regex matching
- No need to understand syntax context
- Pattern means the same thing everywhere
- No false positives from strings/comments/etc.
- No complex suppression logic needed

##### ⚠️ Use Python/AST When:

The check requires **syntax awareness** or **semantic understanding**:

```python
# ✗ CANNOT use bash reliably:

# Forbidden variable names (forbid-vars check)
# - Must distinguish: data = 1  vs  obj.data = 1  vs  "data = 1"
# - Must detect function parameters: def foo(data):
# - Needs inline suppression logic

# Unused imports
# - Must parse import statements
# - Must track variable usage in scope
```

**Characteristics of Python-appropriate checks:**

- Requires parsing language syntax
- Context-dependent (same text means different things)
- Risk of false positives with simple grep
- Needs suppression via inline comments
- Requires accurate line number tracking

##### Why forbid-vars Uses AST, Not Grep

**Bash/grep approach (WRONG):**

```bash
grep -E "\bdata =|\bresult =" *.py
```

**Problems:**

```python
# Test file
obj.data = 1                    # ❌ False positive (attribute, not variable)
data = fetch()                  # ✓ Correct detection
def process(data):              # ❌ MISSED (function parameter not detected!)
    result = transform(data)    # ✓ Correct detection
    "data = 1"                  # ❌ False positive (inside string)
    # data = 1                  # ❌ False positive (inside comment)
```

**Python/AST approach (correct):** `forbid_vars.py` uses `ast.NodeVisitor` to check `ast.Assign`, `ast.AnnAssign`, and `ast.FunctionDef` parameters, filtering out attributes, strings, and comments automatically.

**When in doubt:** start with a `git grep`/`ripgrep` pre-filter to cheaply skip files that can't contain a violation (see `get_prefilter_pattern()` below), then confirm with AST analysis. This hybrid pipeline is what every check in this repo already does.

### 2. Implement the Check

Every check implements the `ASTCheck` protocol defined in `src/pre_commit_hooks/ast_checks/_base.py`:

```python
class ASTCheck(Protocol):
    @property
    def check_id(self) -> str: ...          # e.g. "forbid-vars"

    @property
    def error_code(self) -> str: ...        # e.g. "TRI001"

    def get_prefilter_pattern(self) -> list[str] | None: ...  # git-grep fast path

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]: ...

    def fix(self, filepath: Path, violations: list[Violation], source: str, tree: ast.Module, encoding: str = "utf-8") -> bool: ...
```

`CheckOrchestrator` parses each file's AST **once** and hands the same `tree`/`source` to every enabled check, so `check()` must not re-parse the file.

Create `src/pre_commit_hooks/ast_checks/your_check.py` (or a package with `__init__.py` if the check needs multiple modules — see `validate_function_name/` for an example):

```python
"""your-check - one-line description.

TRI00N: what this detects.

Inline ignore: # pytriage: ignore=TRI00N
"""

from __future__ import annotations

import ast
from pathlib import Path

from ._base import Violation

ERROR_CODE = "TRI00N"


class YourCheck:
    """One-line description."""

    @property
    def check_id(self) -> str:
        return "your-check"

    @property
    def error_code(self) -> str:
        return ERROR_CODE

    def get_prefilter_pattern(self) -> list[str] | None:
        # Fixed strings passed to `git grep` to skip files that can't match.
        # Return None to check every file (e.g. a check with no cheap prefilter).
        return ["some_fixed_string"]

    def check(self, filepath: Path, tree: ast.Module, source: str) -> list[Violation]:
        violations = []
        for node in ast.walk(tree):
            if ...:  # your detection logic
                violations.append(
                    Violation(
                        check_id=self.check_id,
                        error_code=self.error_code,
                        line=node.lineno,
                        col=node.col_offset,
                        message="...",
                        fixable=False,
                    )
                )
        return violations

    def fix(
        self,
        filepath: Path,
        violations: list[Violation],
        source: str,
        tree: ast.Module,
        encoding: str = "utf-8",
    ) -> bool:
        return False  # implement if the check supports --fix
```

Register the check by adding its class to `ALL_CHECKS` in `src/pre_commit_hooks/ast_checks/__init__.py`:

```python
from .excessive_blank_lines import ExcessiveBlankLinesCheck
from .forbid_vars import ForbidVarsCheck
from .misplaced_comment import MisplacedCommentCheck
from .redundant_assignment import RedundantAssignmentCheck
from .redundant_super_init import RedundantSuperInitCheck
from .validate_function_name import ValidateFunctionNameCheck
from .your_check import YourCheck  # add this import

ALL_CHECKS: list[type[ASTCheck]] = [
    ForbidVarsCheck,
    ExcessiveBlankLinesCheck,
    RedundantSuperInitCheck,
    ValidateFunctionNameCheck,
    RedundantAssignmentCheck,
    MisplacedCommentCheck,
    YourCheck,  # add this
]
```

That's it — no `.pre-commit-hooks.yaml` entry and no `[project.scripts]` entry needed. The check is now selectable via `--enable=your-check`/`--disable=your-check` on the existing `ast-checks` hook, and shows up in `python -m pre_commit_hooks.ast_checks --list-checks`.

**Key requirements:**

- Use only the Python standard library (no external runtime dependencies)
- Never touch text inside string/byte literals or comments when writing an autofix — locate targets via AST node positions (`node.lineno`/`node.col_offset`/`node.end_lineno`/`node.end_col_offset`), not blind regex substitution over the whole file. See `validate_function_name/autofix.py` for a worked example of AST-scoped renaming.
- Support inline suppression: `# pytriage: ignore=TRI00N`
- If the check is experimental or prone to false positives, keep it out of the default enabled set by adding `args: [--disable=your-check-id]` to `.pre-commit-hooks.yaml`

### 3. Write Tests

Create `tests/test_your_check.py` using `tmp_path` and `pytest.mark.parametrize` (see `tests/test_misplaced_comment.py` for the idiomatic pattern used in this repo):

```python
"""Tests for your-check (TRI00N)."""

from __future__ import annotations

from pathlib import Path

from pre_commit_hooks.ast_checks.your_check import YourCheck


def test_detects_violation(tmp_path: Path) -> None:
    test_file = tmp_path / "module.py"
    test_file.write_text("...")

    import ast

    source = test_file.read_text()
    tree = ast.parse(source)
    violations = YourCheck().check(test_file, tree, source)

    assert len(violations) == 1
    assert violations[0].error_code == "TRI00N"
```

**Required test coverage:**

- Detection: true positives across the patterns the check targets
- No false positives on the idiomatic code the check should leave alone
- Inline suppression (`# pytriage: ignore=TRI00N`)
- Autofix, if implemented — including that it never mutates unrelated text (string literals, comments, or identically-named symbols in unrelated scopes)

### 4. Add Fixtures (optional)

For larger example files, add them under `tests/fixtures/your_check/` (see `tests/fixtures/validate_function_name/` for the `good/`/`bad/`/`ignore/` convention used elsewhere).

### 5. Update Documentation

Add a subsection under "Available Checks" → "ast-checks (grouped)" in `README.md`, following the format used by the existing checks (why it exists, an example, features, suppression syntax).

### 6. Test and Validate

```bash
# Run tests
uv run pytest tests/test_your_check.py -v

# Confirm it's registered
uv run python -m pre_commit_hooks.ast_checks --list-checks

# Test the check independently (no git/pre-commit required)
uv run python -m pre_commit_hooks.ast_checks --enable=your-check tests/fixtures/invalid.py

# Run the full suite
uv run ruff check --fix .
uv run ruff format .
uv run mypy src/ tests/
uv run coverage run -m pytest
uv run coverage report
```

## Updating Existing Checks

When updating an existing check:

### 1. Maintain Backward Compatibility

- Don't remove check ids or CLI arguments (deprecate with warnings instead)
- Don't change default behavior in breaking ways
- Add new features as opt-in (via flags)
- Document migration path for breaking changes

### 2. Update Tests

- Add tests for new functionality
- Keep existing tests passing
- Update test fixtures if needed

### 3. Update Documentation

- Update `README.md` with new features
- Add migration notes if applicable

### 4. Validate Changes

```bash
uv run pytest
uv run python -m pre_commit_hooks.ast_checks --enable=check-id path/to/file.py
uv run ruff check .
```

## Semantic Versioning

This repository follows [Semantic Versioning 2.0.0](https://semver.org/).

### Version Format: MAJOR.MINOR.PATCH

- **MAJOR**: Incompatible API changes (breaking changes)
- **MINOR**: New functionality in a backward-compatible manner
- **PATCH**: Backward-compatible bug fixes

### Examples

**PATCH (1.0.0 → 1.0.1):**

- Fix bug in error message formatting
- Fix crash on edge case
- Performance improvement with no API changes

**MINOR (1.0.0 → 1.1.0):**

- Add new check to the `ast-checks` registry
- Add new CLI argument to an existing check (opt-in)

**MAJOR (1.0.0 → 2.0.0):**

- Remove a check id or CLI argument
- Change default forbidden names in `forbid-vars`
- Change error message format in a breaking way
- Remove the `ast-checks` or `misplaced-comment` hook entirely

### Deprecation Process

Before removing features (MAJOR version bump):

1. Mark feature as deprecated in a MINOR version
2. Add a deprecation warning to output
3. Document migration path in `README.md`
4. Wait at least one MINOR version before removal
5. Remove in next MAJOR version

## Backward Compatibility

### Guidelines

1. **CLI Interface Stability:**
   - The `ast-checks` and `misplaced-comment` hook ids are permanent
   - Check ids passed via `--enable`/`--disable` never change once shipped
   - New arguments are optional with sensible defaults
   - Deprecated arguments show warnings before removal

2. **Error Format Stability:**
   - Maintain `filepath:line: TRI00N: message` format
   - Tools may parse this format, don't break it

3. **Behavior Stability:**
   - Default enabled/disabled checks remain constant
   - New checks are opt-in via `--enable` until proven stable
   - Exit codes remain: 0 (success), 1 (failure)

4. **Configuration Compatibility:**
   - `.pre-commit-hooks.yaml` schema remains stable
   - `pyproject.toml` config tables (e.g. `[tool.forbid-vars.autofix]`) remain stable
   - New fields are optional

## Performance Testing

All checks must meet performance requirements.

### Performance Target

**Requirement:** Process <1000 files in <5 seconds (warm cache).

### Benchmarking

```bash
uv run python scripts/benchmark.py --iterations=3
```

`scripts/benchmark.py` invokes the real, currently-registered `ast_checks` package against this repo's own `src/`+`tests/` and reports per-check cold/warm timings — see the "Performance" section in `README.md` for the latest numbers.

### Profiling

```bash
# Profile a single check
python -m cProfile -o profile.stats -m pre_commit_hooks.ast_checks --enable=forbid-vars src/

# View results
python -c "import pstats; p = pstats.Stats('profile.stats'); p.sort_stats('cumulative'); p.print_stats(20)"
```

### Optimization Guidelines

1. **Minimize I/O:**
   - Let `CheckOrchestrator` read/parse each file once; don't re-read in `check()`/`fix()`
   - Use the shared `_cache.py` disk cache and `_prefilter.py` git-grep filtering rather than reinventing them

2. **Efficient Algorithms:**
   - Prefer O(n) over O(n²)
   - Use set lookups instead of list searches

3. **Parallel Processing:**
   - `prek`/`pre-commit` may run hooks in parallel across files; avoid shared mutable state without locking (see `_cache.py`)

## Release Process

### 1. Update Version

Update version in `pyproject.toml`:

```toml
[project]
version = "1.1.0"  # Increment according to semver
```

### 2. Run Full Test Suite

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy src/ tests/
uv run coverage run -m pytest
uv run coverage report
```

### 3. Create Git Tag

```bash
git add pyproject.toml
git commit -m "chore: bump version to v1.1.0"
git tag -a v1.1.0 -m "Release v1.1.0"
git push origin main
git push origin v1.1.0
```

### 4. Verify Release

```bash
pre-commit autoupdate
```

## CI/CD Configuration

This repository is developed locally by a single maintainer and intentionally ships no CI workflow — the full command sequence below is run locally (via `pre-commit`/`prek` and by hand) before every commit, not in a hosted pipeline. This is a deliberate choice, not a gap to fill: don't add a badge or other claim implying automated CI checks run on this repo unless a workflow is actually added.

If that changes and a CI workflow is added (GitHub Actions is the natural choice given the repo is hosted there), run the full command sequence from this project's own `CLAUDE.md`/README on every PR:

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy src/ tests/
uv run coverage run -m pytest
uv run coverage report
```

Target Python 3.13+ only (`requires-python = ">=3.13"` in `pyproject.toml`) — do not add older interpreters to a version matrix.

## Code Quality Standards

### Python Style

- Follow PEP 8 (enforced by ruff)
- Use type hints for all function signatures (enforced by mypy)
- Use f-strings for string formatting

### Docstrings

Use Google-style docstrings:

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

### Testing

- `pyproject.toml` sets `fail_under` for `coverage report` — keep it passing
- Test success cases, failure cases, and edge cases
- Use descriptive test names: `test_what_when_expected`
- Prefer `pytest.mark.parametrize` over near-duplicate test functions

### Error Messages

Format: `filepath:line: TRI00N: clear message`

**Good:**

```
src/app.py:42: TRI001: Forbidden variable name 'data' found. Use a more descriptive name.
```

**Bad:**

```
Error in file (line 42)
```

## Questions?

If you have questions about contributing:

- Open an issue with the `question` label
- Check existing issues for similar questions
- Review `README.md` for usage examples

Thank you for contributing!
