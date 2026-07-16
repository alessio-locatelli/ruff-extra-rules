# Ruff Extra Rules

Extra Python rule checks and fixups for pre-commit/prek, meant to run alongside ruff rather than replace it.

[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit&logoColor=white)](https://github.com/pre-commit/pre-commit)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

## Disclaimer

- This is not a standalone linter and not a `ruff` competitor. It's a small set of rules/fixups `ruff` doesn't (yet) have, run as an extra pre-commit/prek hook alongside `ruff` — not instead of it.
- This project is a stopgap until plugin support is implemented in `ruff` ([astral-sh/ruff#283](https://github.com/astral-sh/ruff/issues/283)), and will be archived thereafter.
- This is a best-effort proof-of-concept implemented using coding agents.

## Registered Hooks

This repository registers two hooks in `.pre-commit-hooks.yaml`, both backed by the same `ast_checks` implementation (there's one orchestrator, one cache, one prefilter — not a duplicated pipeline):

| Hook id             | What it runs                                                                                                                                                                                                   |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ast-checks`        | A grouped orchestrator that runs every AST-based check (TRI001–TRI005, STYLE-001) against each file in a single parse pass, report-only by default. Individual checks are toggled with `--enable`/`--disable`. |
| `misplaced-comment` | The same orchestrator restricted to STYLE-001 with `--fix` on by default — moving a comment off a closing-bracket-only line never changes semantics, so it's safe to auto-apply without a human review step.   |

There are no other installable hook ids and no console-script entry points (`[project.scripts]` in `pyproject.toml` is intentionally empty) — every check runs via `python -m pre_commit_hooks.ast_checks`.

## Available Checks

---

### ast-checks (grouped)

The `ast-checks` hook runs the checks below in a single AST parse pass per file, with every check enabled by default. Select which ones run with `--enable=<id>,<id>` or `--disable=<id>,<id>` (comma-separated check ids) passed via `args:` in your `.pre-commit-config.yaml`.

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

**TRI005**: Detects and optionally auto-fixes redundant variable assignments where the variable doesn't add meaningful clarity or simplification to the code.

**Why?** Unnecessary intermediate variables add cognitive load without providing value. However, variables that add semantic meaning (transformative verbs like "formatted", "validated") or break down complex expressions are preserved.

**Patterns Detected:**

1. **Immediate single use**: Variable assigned and used in the very next statement
2. **Single-use variables**: Variable assigned but used only once anywhere in its scope
3. **Literal identity**: Variable name matches its literal value (e.g., `foo = "foo"`)

**Example:**

```python
# Redundant - adds no value:
x = "foo"
func(x=x)

# Redundant - simple pass-through:
result = get_value()
return result

# Adds clarity - transformative verb indicates processing:
formatted_timestamp = format_iso8601(raw_ts)
return formatted_timestamp

# Adds clarity - breaks down complex chained expression:
collection_places = singleton_factory(mongo_client)[DATABASE_NAME]["places"]
return collection_places.find_one({"_id": place_id})

# Not flagged - conditional assignment with subsequent use:
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

#### misplaced-comment

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
- Never moves linter pragma comments (`noqa`, `type: ignore`, `pragma:`, etc.)
- Inline suppression with `# pytriage: ignore=STYLE-001`
- Preserves the source file's PEP 263 declared encoding; lines untouched by a fix also keep their original newline style (CRLF/LF) — a line a fix rewrites gets a plain `\n`

Registered as its own hook id with `--fix` on by default (see [Registered Hooks](#registered-hooks)):

```yaml
- id: misplaced-comment
```

---

## Installation

### Using prek or pre-commit

Add to your `.pre-commit-config.yaml` — the same file [prek](https://github.com/j178/prek) and pre-commit both read:

```yaml
repos:
  - repo: https://github.com/alessio-locatelli/ruff-extra-rules
    rev: <tag-or-commit-sha> # pin a specific tag or commit; see the repo's tags for available versions
    hooks:
      - id: ast-checks
      - id: misplaced-comment
```

Then install the hooks:

```bash
prek install
# or: pre-commit install
```

### Manual Installation

```bash
pip install git+https://github.com/alessio-locatelli/ruff-extra-rules.git
```

## Usage

### Automatic (on commit)

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

Run a hook manually via prek (or pre-commit) on all files:

```bash
prek run ast-checks --all-files
```

## Configuration

### Custom Forbidden Names

Override the `forbid-vars` default blacklist with your own:

```yaml
- repo: https://github.com/alessio-locatelli/ruff-extra-rules
  rev: <tag-or-commit-sha>
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

## License

MIT License. See [LICENSE](LICENSE) file for details.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Resources

- [Pre-commit Framework](https://pre-commit.com/)
- [prek](https://github.com/j178/prek)
- [Meaningless Variable Names](https://hilton.org.uk/blog/meaningless-variable-names)
- [Python AST Documentation](https://docs.python.org/3/library/ast.html)
