# misplaced-comment (TR7)

Automatically fixes trailing comments on closing brackets by moving them to the expression line.

## Why?

When auto-formatters move closing brackets to new lines, comments on those lines become orphaned and lose context.

## Example

```python
# Bad - comment is on bracket line:
result = func(
    arg,
)  # Comment about the function call

# Fixed - comment moves to expression line:
result = func(
    arg,  # Comment about the function call
)
```

## Features

- Automatically moves comments from closing bracket lines to expression lines, except `#:` documentation comments
- Places comments inline if they fit within 88 characters, matching this project's own line-length convention; otherwise places them as preceding comments on their own line
- Never moves linter pragma comments (`noqa`, `type: ignore`, `pragma:`, etc.)
- Inline suppression with `# pytriage: TR7`
- Preserves the source file's PEP 263 declared encoding and newline style

Its fix is a purely mechanical text move that never changes semantics, so it's always safe to include in a `--fix` run alongside every other check:

```yaml
- id: ruff-extra-rules
  args: [--fix]
```
