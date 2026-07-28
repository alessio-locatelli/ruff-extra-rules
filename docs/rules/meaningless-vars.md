# meaningless-vars (TR1)

Flags meaningless variable names like `data` and `result`.

## Why?

Meaningless variable names reduce code clarity and maintainability. See [Peter Hilton's article on meaningless variable names](https://hilton.org.uk/blog/meaningless-variable-names) for more context.

**Default meaningless names:**

- `data`
- `result`
- `results`

## Features

- Detects meaningless names in assignments, function parameters, and async functions
- **Autofixing**: derives names from file-local semantics such as concrete annotations, imported standard APIs, producers, and consumers (`--fix`). The rename is scope-aware — it replaces only the AST `Name` nodes for that specific binding within its scope, not every textual occurrence in the file. `--fix` applies only high-confidence local renames; weaker evidence is reported as a suggestion without changing the file.
- Inline suppression with `# pytriage: TR1`

## Reporting level

`--meaningless-vars-level={conservative,permissive}` (default `conservative`) controls whether a meaningless name with no suggested replacement gets reported at all:

- **`conservative`** (default): reports a meaningless name only when a replacement can be suggested. On a codebase with a large, pre-existing backlog of `data`/`result` variables, this keeps the output focused on the cases you can actually act on.
- **`permissive`**: reports every meaningless name, whether or not a replacement can be suggested.

`--fix` behaves identically at both levels: it only ever applies a high-confidence suggestion, never a weaker one and never a name with no suggestion at all. See [ADR-0031](../adr/0031-meaningless-vars-conservative-reporting-default.md) for why `conservative` is the default.

## Suggest mode (default)

```
src/process.py:2: TR1: 'data' is a meaningless variable name — 'user' is more descriptive. Or add '# pytriage: TR1' to suppress.
```

**Permissive mode**, reporting a `result` binding the conservative default left out because no replacement could be suggested for it:

```
src/process.py:3: TR1: Meaningless variable name 'result' found. Use a more descriptive name. Or add '# pytriage: TR1' to suppress.
```

## Fix mode

```yaml
- id: ruff-extra-rules
  args: [--select=meaningless-vars, --fix]
```

```yaml
# Report (and fix) every meaningless name, not just the ones with a suggestion:
- id: ruff-extra-rules
  args: [--select=meaningless-vars, --meaningless-vars-level=permissive, --fix]
```

## Suppression

```python
def process():
    data = get_user()  # pytriage: TR1
    return data
```
