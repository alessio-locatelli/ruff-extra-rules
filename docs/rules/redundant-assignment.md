# redundant-assignment (TR5)

Detects and optionally auto-fixes redundant variable assignments where the variable doesn't add meaningful clarity or simplification to the code.

## Why?

Unnecessary intermediate variables add cognitive load without providing value. However, variables that add semantic meaning (transformative verbs like "formatted", "validated") or break down complex expressions are preserved. This includes a variable introduced purely to keep a line under the length limit: a multiline call/expression is preferable to a redundant intermediate variable invented just to shorten a line — the length problem belongs to the expression's own formatting, not to naming a value that doesn't otherwise need a name.

## Patterns detected

1. **Immediate single use**: Variable assigned and used in the very next statement
2. **Single-use variables**: Variable assigned but used only once anywhere in its scope
3. **Literal identity**: Variable name matches its literal value (e.g., `foo = "foo"`)

## Example

```python
# Redundant - adds no value:
x = "foo"
func(x=x)

# Redundant - same idea, positional: only recognized when `func` is defined
# exactly once, undecorated, in this file, and its parameter at that
# position is also named `x`:
func(x)

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

## Features

- **Smart semantic analysis**: Preserves variables that add meaning through:
  - Transformative verbs ("formatted", "validated", "parsed", etc.)
  - Long expressions (60+ characters)
  - Chained operations (`obj[x][y]`, `foo.bar.baz`)
  - Complex expressions (comprehensions, ternary operators, lambdas)
  - Multi-part descriptive names (`user_email_address`)
  - Type annotations
- **Safe autofix mode**: automatically inlines simple, low-value assignments when mechanically safe — see [ADR-0032](../adr/0032-redundant-assignment-autofix-safety-criteria.md) for the exact safety criteria
- Inline suppression with `# pytriage: TR5`
- Gracefully handles:
  - Augmented assignments (`x += 1`)
  - Conditional assignments in if/else blocks
  - Global and nonlocal variables (skipped)
  - Tuple unpacking (skipped)
  - Class attributes (skipped)

## Aggressiveness level

`--redundant-assignment-level={conservative,aggressive}` (default `conservative`) controls how eagerly a violation is flagged:

- **`conservative`** (default): flags only the clearest cases, and leaves alone a variable name that looks like it's documenting a non-obvious value rather than just restating it — e.g. `warning = conn.recv()` is not flagged, since `warning` adds real information `recv()` alone doesn't convey.
- **`aggressive`**: flags a broader range of low-value assignments, at the cost of more false positives on descriptively-named variables.

Defaulting to `conservative` is deliberate: someone confronted with a flood of suggestions on unfamiliar code is more likely to disable the check outright than to go discover a stricter flag, so the out-of-the-box experience undersells rather than oversells what gets flagged.

`--fix` applies identically at either reporting level, to whatever is mechanically safe to inline.

```yaml
- id: ruff-extra-rules
  args: [--select=redundant-assignment, --fix]
```

```yaml
# Report (and fix) the broader, aggressive set of violations:
- id: ruff-extra-rules
  args:
    [
      --select=redundant-assignment,
      --redundant-assignment-level=aggressive,
      --fix,
    ]
```

## Example autofix

```python
# Before:
x = "foo"
func(x=x)

# After (auto-fixed):
func(x="foo")
```

## Suppression

```python
result = expensive_calculation()  # pytriage: TR5
return result
```
