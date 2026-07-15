# Pre-Commit Extra Hooks

Custom pre-commit hooks for code quality enforcement.

[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)
[![Python 3.13+](https://img.shields.io/badge/python-3.13+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Registered Hooks

This repository registers exactly two hooks in `.pre-commit-hooks.yaml`:

| Hook id                  | What it runs                                                                                                                                                                   |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `ast-checks`             | A grouped orchestrator that runs several AST-based checks (TRI001–TRI005) against each file in a single parse pass. Individual checks are toggled with `--enable`/`--disable`. |
| `fix-misplaced-comments` | STYLE-001: moves trailing comments on closing brackets to the expression line.                                                                                                 |

There are no other installable hook ids and no console-script entry points (`[project.scripts]` in `pyproject.toml` is intentionally empty) — every check runs via `python -m pre_commit_hooks.<package>`.

## Performance

All checks are optimized for speed with:

- **File content caching**: SHA-1 hash-based cache with mtime optimization (similar to mypy/ruff)
- **Batch pre-filtering**: Fast git grep pre-filtering before Python processing
- **Code-level optimizations**: Single AST parse, scope caching, pre-compiled regex patterns

**Benchmark results (92 Python files, this repo's own `src/`+`tests/`, 3 iterations):**

| Metric                   | Time    | Change     |
| ------------------------ | ------- | ---------- |
| Cold cache (first run)   | ~4.80 s | Baseline   |
| Warm cache (incremental) | ~4.69 s | ~2% faster |

**Per-check performance (cold cache averages):**

| Check                               | Time    |
| ----------------------------------- | ------- |
| `ast-checks` (all checks, one pass) | ~1.55 s |
| `forbid-vars`                       | ~1.34 s |
| `redundant-assignment`              | ~1.43 s |
| `validate-function-name`            | ~0.21 s |
| `excessive-blank-lines`             | ~0.12 s |
| `redundant-super-init`              | ~0.06 s |
| `fix-misplaced-comments`            | ~0.10 s |

Each measurement pays Python interpreter startup once per subprocess invocation, so the cache mainly saves the per-file re-analysis cost, not process startup — the warm-cache improvement is modest for that reason. These numbers come from actually running the current `ast_checks`/`fix_misplaced_comments` packages against this repo, not a stand-in.

**Cache location**: `.cache/pre_commit_hooks/` (automatically managed, safe to delete)

Run your own benchmarks:

```bash
uv run python scripts/benchmark.py --iterations=3
```

## Available Checks

---

### ast-checks (grouped)

The `ast-checks` hook runs the checks below in a single AST parse pass per file. Select which ones run with `--enable=<id>,<id>` or `--disable=<id>,<id>` (comma-separated check ids); by default all checks run except `redundant-assignment` (see `.pre-commit-hooks.yaml`).

```bash
# List available check ids
uv run python -m pre_commit_hooks.ast_checks --list-checks

# Run only forbid-vars and validate-function-name
uv run python -m pre_commit_hooks.ast_checks --enable=forbid-vars,validate-function-name src/

# Run everything except redundant-assignment, with autofix
uv run python -m pre_commit_hooks.ast_checks --disable=redundant-assignment --fix src/
```

---

#### forbid-vars

**TRI001**: Prevents use of meaningless variable names like `data` and `result`.

**Why?** Meaningless variable names reduce code clarity and maintainability. See [Peter Hilton's article on meaningless variable names](https://hilton.org.uk/blog/meaningless-variable-names) for more context.

**Default forbidden names:**

- `data`
- `result`

**Features:**

- Detects forbidden names in assignments, function parameters, and async functions
- **Autofixing**: suggests and optionally applies meaningful names based on context (`--fix`). The rename is scope-aware — it replaces only the AST `Name` nodes for that specific binding within its scope, not every textual occurrence in the file.
- Supports a custom blacklist via `--forbid-vars-names`
- Inline suppression with `# pytriage: ignore=TRI001`
- Clear error messages with line numbers and helpful links

**Suggest mode (default):**

```
src/process.py:2: TRI001: Forbidden variable name 'data' found. Use a more descriptive name. Or add '# pytriage: ignore=TRI001' to suppress.
```

**Fix mode:**

```yaml
- id: ast-checks
  args: [--enable=forbid-vars, --fix]
```

#### Autofix Configuration (`pyproject.toml`)

You can configure the `forbid-vars` autofix behavior in your `pyproject.toml` file.

**Enabling/Disabling Categories:**

The autofix patterns are grouped into categories (`http`, `file`, `database`, `data-science`, `semantic`). By default, only the `http` category is enabled. You can enable more categories like this:

```toml
[tool.forbid-vars.autofix]
enabled = ["http", "file", "database"]
```

**Custom Patterns:**

You can also add your own custom patterns. This is useful for project-specific conventions.

```toml
[tool.forbid-vars.autofix]
enabled = ["custom"]

[[tool.forbid-vars.autofix.patterns]]
category = "custom"
regex = "get_user_profile"
name = "user_profile"
```

---

#### excessive-blank-lines

**TRI002**: Collapses multiple consecutive blank lines after module headers (copyright, docstrings, or comments) to a single blank line.

**Why?** Excessive blank lines after module headers create visual clutter and violate PEP 8 conventions.

**Example:**

```python
"""Module docstring."""



import os  # Bad - 3 blank lines

# Fixed:
"""Module docstring."""

import os  # Good - 1 blank line
```

**Features:**

- Detects 2+ blank lines after module header
- Preserves copyright comment spacing (1 blank line after copyright)
- Only affects module-level blank lines, preserves function/class spacing
- Maintains file encoding and handles different line ending styles

---

#### redundant-super-init

**TRI003**: Detects when a class forwards `**kwargs` to a parent `__init__` that accepts no arguments.

**Why?** Forwarding kwargs to parents that don't accept them is a logic error that creates misleading inheritance patterns.

**Example:**

```python
# Bad - redundant kwargs forwarding:
class Base:
    def __init__(self):
        pass

class Child(Base):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)  # VIOLATION: Base doesn't accept kwargs

# Fixed - matching signatures:
class Child(Base):
    def __init__(self):
        super().__init__()
```

**Features:**

- Detects redundant `**kwargs` forwarding using AST analysis
- Analyzes class hierarchies and method signatures
- Limited to same-file parent classes (safe, zero false positives)
- Handles multiple inheritance correctly
- Gracefully skips unresolvable parent classes (imports, stdlib)

---

#### validate-function-name

**TRI004**: Detects functions with `get_` prefix and suggests better names based on their behavior patterns.

**Why?** The `get_` prefix is overused and often masks the true intent of a function. Specific verbs like `load_`, `fetch_`, `calculate_`, `is_`, or `iter_` make code more readable and self-documenting.

**Example:**

```python
# Bad - vague naming:
def get_users() -> list[User]:
    with open("users.json") as f:
        return json.load(f)

def get_active(user: User) -> bool:
    return user.status == "active"

# Good - specific naming:
def load_users() -> list[User]:
    with open("users.json") as f:
        return json.load(f)

def is_active(user: User) -> bool:
    return user.status == "active"
```

**Detection Patterns:**

| Pattern             | Suggested Prefix | Example                                 |
| ------------------- | ---------------- | --------------------------------------- |
| Boolean return type | `is_*`           | `get_valid()` → `is_valid()`            |
| Disk I/O (read)     | `load_*`         | `get_config()` → `load_config()`        |
| Disk I/O (write)    | `save_to_*`      | `get_saved()` → `save_to_saved()`       |
| Network (read)      | `fetch_*`        | `get_data()` → `fetch_data()`           |
| Network (write)     | `send_*`         | `get_posted()` → `send_posted()`        |
| Generator/yield     | `iter_*`         | `get_items()` → `iter_items()`          |
| Aggregation         | `calculate_*`    | `get_total()` → `calculate_total()`     |
| JSON/YAML parsing   | `parse_*`        | `get_json()` → `parse_json()`           |
| Searching           | `find_*`         | `get_root()` → `find_root()`            |
| Validation          | `validate_*`     | `get_errors()` → `validate_input()`     |
| Collection building | `extract_*`      | `get_names()` → `extract_names()`       |
| Object creation     | `create_*`       | `get_instance()` → `create_instance()`  |
| Mutation            | `update_*`       | `get_modified()` → `update_record()`    |
| @property           | Remove `get_`    | `@property get_name` → `@property name` |

Applies equally to `async def get_*` functions.

**Features:**

- Detects 15+ behavioral patterns using AST analysis
- Suggests appropriate function names based on what the function actually does
- **Safe autofix mode**: automatically renames small, simple functions (< 20 lines, single return, simple control flow). The rename is AST-scoped — it renames the definition plus true call-site references (`self.x`/`cls.x` within the same class for methods, or `Name` references across the module for free functions), and never touches string/byte literals, comments, or identically-named symbols in unrelated scopes (e.g. a same-named method on a different class).
- Inline suppression with `# pytriage: ignore=TRI004`
- Automatically skips:
  - `@property` decorators
  - `@override` / `@abstractmethod` decorators
  - Simple accessors: `return self.attr`, `return obj[key]`
  - Functions that only call other `get_*` functions
  - Test functions: `test_get_*`

**Safe autofix criteria (ALL must be met):**

- Function is small (< 20 lines, excluding docstring)
- Simple control flow (nesting depth ≤ 1)
- Single return statement
- High confidence suggestion

```yaml
- id: ast-checks
  args: [--enable=validate-function-name, --fix]
```

**Suppression:**

```python
def get_user(id: int) -> User:  # pytriage: ignore=TRI004
    """Legacy API - name cannot be changed."""
    return User.objects.get(id=id)
```

---

#### redundant-assignment

> **⚠️ Opt-in (Experimental)**: This check is disabled by default in `.pre-commit-hooks.yaml` as it's in early development and may have false positives. To enable it, override the default args in your `.pre-commit-config.yaml`:
>
> ```yaml
> - repo: https://github.com/YOUR_USERNAME/pre-commit-extra-hooks
>   rev: v1.0.0
>   hooks:
>     - id: ast-checks
>       args: [] # Remove --disable=redundant-assignment to enable all checks
>       # Or explicitly enable only this check:
>       # args: [--enable=redundant-assignment]
> ```

**TRI005**: Detects and optionally auto-fixes redundant variable assignments where the variable doesn't add meaningful clarity or simplification to the code.

**Why?** Unnecessary intermediate variables add cognitive load without providing value. However, variables that add semantic meaning (transformative verbs like "formatted", "validated") or break down complex expressions are preserved.

**Patterns Detected:**

1. **Immediate single use**: Variable assigned and used in the very next statement
2. **Single-use variables**: Variable assigned but used only once anywhere in its scope
3. **Literal identity**: Variable name matches its literal value (e.g., `foo = "foo"`)

**Example:**

```python
# ❌ Redundant - adds no value:
x = "foo"
func(x=x)

# ❌ Redundant - simple pass-through:
result = get_value()
return result

# ✅ Adds clarity - transformative verb indicates processing:
formatted_timestamp = format_iso8601(raw_ts)
return formatted_timestamp

# ✅ Adds clarity - breaks down complex chained expression:
collection_places = singleton_factory(mongo_client)[DATABASE_NAME]["places"]
return collection_places.find_one({"_id": place_id})

# ✅ Not flagged - conditional assignment with subsequent use:
if condition:
    msg = "foo"
else:
    msg = "bar"
msg += " suffix"  # Uses the conditional value
print(msg)
```

**Features:**

- **Smart semantic analysis**: Preserves variables that add meaning through:
  - Transformative verbs ("formatted", "validated", "parsed", etc.)
  - Long expressions (60+ characters)
  - Chained operations (`obj[x][y]`, `foo.bar.baz`)
  - Complex expressions (comprehensions, ternary operators, lambdas)
  - Multi-part descriptive names (`user_email_address`)
  - Type annotations
- **Safe autofix mode**: automatically inlines simple, low-value assignments when safe
- Inline suppression with `# pytriage: ignore=TRI005`
- Gracefully handles:
  - Augmented assignments (`x += 1`)
  - Conditional assignments in if/else blocks
  - Global and nonlocal variables (skipped)
  - Tuple unpacking (skipped)
  - Class attributes (skipped)

**Autofix criteria (ALL must be met):**

- Semantic value score ≤ 20 (very low value)
- Pattern is IMMEDIATE_SINGLE_USE or LITERAL_IDENTITY
- RHS is simple: literal, name, attribute, or simple call
- Inlining won't exceed 88 characters (Black's default)

```yaml
- id: ast-checks
  args: [--enable=redundant-assignment, --fix]
```

**Example autofix:**

```python
# Before:
x = "foo"
func(x=x)

# After (auto-fixed):
func(x="foo")
```

**Suppression:**

```python
result = expensive_calculation()  # pytriage: ignore=TRI005
return result
```

---

### fix-misplaced-comments

**STYLE-001**: Automatically fixes trailing comments on closing brackets by moving them to the expression line.

**Why?** When auto-formatters move closing brackets to new lines, comments on those lines become orphaned and lose context.

**Example:**

```python
# Bad - comment is on bracket line:
result = func(
    arg
)  # Comment about the function call

# Fixed - comment moves to expression line:
result = func(
    arg  # Comment about the function call
)
```

**Features:**

- Automatically moves comments from closing bracket lines to expression lines
- Places comments inline if they fit within 88 characters
- Otherwise places them as preceding comments on their own line
- Preserves file encoding and line endings
- Gracefully handles syntax errors in source files

---

## Installation

### Using pre-commit

Add to your `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/YOUR_USERNAME/pre-commit-extra-hooks
    rev: v1.0.0 # Use the latest version tag
    hooks:
      - id: ast-checks
      - id: fix-misplaced-comments
```

Then install the pre-commit hooks:

```bash
pre-commit install
```

### Manual Installation

```bash
pip install git+https://github.com/YOUR_USERNAME/pre-commit-extra-hooks.git
```

## Usage

### Automatic (via pre-commit)

Once installed, the hooks run automatically on `git commit`:

```bash
git add .
git commit -m "Add new feature"
```

**Example output when violations are found:**

```
Python AST checks (grouped)......................................Failed
- hook id: ast-checks
- exit code: 1

src/process.py:2: TRI001: Forbidden variable name 'data' found. Use a more descriptive name. Or add '# pytriage: ignore=TRI001' to suppress.
```

### Manual Execution

Run a hook manually via pre-commit on all files:

```bash
pre-commit run ast-checks --all-files
```

Run a check directly (independent of pre-commit, no console script is installed):

```bash
uv run python -m pre_commit_hooks.ast_checks --enable=forbid-vars src/main.py src/utils.py
```

## Configuration

### Custom Forbidden Names

Override the `forbid-vars` default blacklist with your own:

```yaml
- repo: https://github.com/YOUR_USERNAME/pre-commit-extra-hooks
  rev: v1.0.0
  hooks:
    - id: ast-checks
      args: [--forbid-vars-names=data, result, info, temp, obj, value]
```

### Inline Suppression

Suppress violations on specific lines:

```python
# This will trigger a violation:
data = load_from_database()

# This will be ignored:
data = load_from_database()  # pytriage: ignore=TRI001
```

**Note:** The ignore comment must be on the same line as the violation.

## Examples

### ❌ Code that Fails

```python
def process():
    """Process data."""
    data = fetch()  # Violation: 'data' is forbidden
    result = transform(data)  # Violation: 'result' is forbidden
    return result


def calculate(data):  # Violation: parameter 'data'
    """Calculate something."""
    return data * 2
```

### ✅ Code that Passes

```python
def process_user_records():
    """Process user records."""
    user_records = fetch_users()
    transformed_output = transform(user_records)
    return transformed_output


def calculate_total(invoice_items):
    """Calculate total from invoice items."""
    return sum(item.price * item.quantity for item in invoice_items)
```

### ✅ Code with Suppression

```python
def legacy_code():
    """Legacy code with necessary suppressions."""
    # New code - descriptive names
    user_records = fetch_users()

    # Legacy code - suppressed (refactoring is risky)
    data = transform(user_records)  # pytriage: ignore=TRI001
    result = validate(data)  # pytriage: ignore=TRI001

    return result
```

## Adding New Checks

Want to contribute a new check to this repository? See [CONTRIBUTING.md](CONTRIBUTING.md) for the full walkthrough of the `register_check`/`ASTCheck` protocol used by the `ast_checks` package.

## Testing

### Run the full test suite

```bash
uv run coverage run -m pytest
uv run coverage report
```

### Test a specific check

```bash
uv run pytest tests/test_forbid_vars.py -v
```

### Run a check independently

Every check works without git or pre-commit — just point it at files:

```bash
uv run python -m pre_commit_hooks.ast_checks --enable=forbid-vars tests/fixtures/invalid_code.py
```

## Development

### Setup

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/pre-commit-extra-hooks.git
cd pre-commit-extra-hooks

# Install development dependencies (this project uses uv)
uv sync

# Install pre-commit hooks (dogfooding!)
uv run pre-commit install
```

### Run Linter

```bash
uv run ruff check --fix .
uv run ruff format .
uv run mypy src/ tests/
```

### Run Tests

```bash
uv run coverage run -m pytest
uv run coverage report
```

## Project Structure

```text
pre_commit_python_extra_hooks/
├── .pre-commit-hooks.yaml     # Hook definitions (ast-checks, fix-misplaced-comments)
├── .pre-commit-config.yaml    # Self-dogfooding configuration
├── README.md                  # This file
├── CONTRIBUTING.md            # Guide for adding new checks
├── LICENSE                    # MIT license
├── pyproject.toml             # Python project metadata
│
├── src/pre_commit_hooks/
│   ├── _cache.py               # Shared disk cache (SHA-1 + mtime)
│   ├── _prefilter.py           # git-grep based candidate-file filtering
│   ├── ast_checks/             # Grouped orchestrator + individual checks
│   │   ├── __init__.py          # CheckOrchestrator, register_check, CLI
│   │   ├── _base.py             # ASTCheck protocol, Violation dataclass
│   │   ├── forbid_vars.py       # TRI001
│   │   ├── excessive_blank_lines.py  # TRI002
│   │   ├── redundant_super_init.py   # TRI003
│   │   ├── validate_function_name/   # TRI004
│   │   └── redundant_assignment/     # TRI005 (opt-in)
│   └── fix_misplaced_comments/  # STYLE-001
│
└── tests/                     # Test suite
    ├── fixtures/               # Test data per check
    └── test_*.py
```

## Troubleshooting

### Hook not running

**Problem:** Hook doesn't run on commit.

**Solution:** Make sure you've installed the git hooks:

```bash
pre-commit install
```

### No violations reported despite bad code

**Problem:** Code with `data` variable passes the hook.

**Solution:** Check if:

1. File is a Python file (hooks only run on `*.py` files)
2. Inline ignore comment is present
3. The variable is actually being assigned (not an attribute like `obj.data`)

### Syntax errors in code

**Problem:** Hook fails on syntactically invalid Python.

**Solution:** The checks require valid Python syntax to parse the AST. Fix syntax errors first:

```bash
python -m py_compile src/file.py
```

## License

MIT License. See [LICENSE](LICENSE) file for details.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on adding checks and maintaining this repository.

## Resources

- [Pre-commit Framework](https://pre-commit.com/)
- [Meaningless Variable Names](https://hilton.org.uk/blog/meaningless-variable-names)
- [Python AST Documentation](https://docs.python.org/3/library/ast.html)
