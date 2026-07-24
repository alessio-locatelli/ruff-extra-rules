# redundant-super-init (TRI003)

Detects when a class forwards `**kwargs` to a parent `__init__` that accepts no arguments.

## Why?

Forwarding kwargs to parents that don't accept them is a logic error that creates misleading inheritance patterns.

## Example

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

## Features

- Detects redundant `**kwargs` forwarding using AST analysis
- Analyzes class hierarchies and method signatures
- Handles multiple inheritance correctly
- Limited to same-file parent classes — an imported or stdlib parent is skipped rather than guessed at
- Inline suppression with `# pytriage: ignore=TRI003`, placed on the `__init__` definition line
- No autofix — this check flags a design decision the caller has to make
