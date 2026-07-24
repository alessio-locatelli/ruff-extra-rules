# validate-function-name (TRI004)

Detects functions with `get_` prefix and suggests better names based on their behavior patterns.

## Why?

The `get_` prefix is overused and often masks the true intent of a function. Specific verbs like `load_`, `fetch_`, `calculate_`, `is_`, or `iter_` make code more readable and self-documenting.

## Example

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

## Detection patterns

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

## Features

- Detects 15+ behavioral patterns using AST analysis
- Suggests appropriate function names based on what the function actually does
- **Safe autofix mode**: automatically renames small, simple functions. The rename is AST-scoped — it renames the definition plus true call-site references (`self.x`/`cls.x` within the same class for methods, or `Name` references across the module for free functions), and never touches string/byte literals, comments, or identically-named symbols in unrelated scopes (e.g. a same-named method on a different class). See [ADR-0033](../adr/0033-validate-function-name-safe-autofix-criteria.md) for the exact safety criteria.
- Inline suppression with `# pytriage: ignore=TRI004`
- Automatically skips:
  - `@property` decorators
  - `@override` / `@abstractmethod` decorators
  - Simple accessors: `return self.attr`, `return obj[key]`
  - Functions that only call other `get_*` functions
  - Test functions: `test_get_*`

```yaml
- id: ruff-extra-rules
  args: [--select=validate-function-name, --fix]
```

## Suppression

```python
def get_user(id: int) -> User:  # pytriage: ignore=TRI004
    """Legacy API - name cannot be changed."""
    return User.objects.get(id=id)
```
