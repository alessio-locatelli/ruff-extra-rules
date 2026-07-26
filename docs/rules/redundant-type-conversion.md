# redundant-type-conversion (TRI006)

Flags a builtin type/collection conversion call — `str(...)`, `list(...)`, and similar — that's a no-op given the real, statically-known type of the value it wraps, including when that value's type is declared in a different file.

## Why?

A conversion call like `str(x)` or `list(bar)` sometimes wraps a value that's already exactly the type being converted to. That adds indirection without changing anything. This check catches that case even when it spans a call site and an imported function's own parameter type — the same value passed into a function whose parameter is already the right type.

## Prerequisite: `ty`

This check delegates detection to [Astral's `ty`](https://github.com/astral-sh/ty) type checker, run as a background language-server process. It needs `ty` available on `PATH`:

```bash
uv tool install ty
```

or as your own project's dev dependency. If `ty` isn't found, or an installed `ty` doesn't behave the way this check expects, the check fails immediately with a message explaining which of the two happened and what to do about it — never a silent, empty result.

## Example

```python
# pkg/callee.py
def takes_list(items: list[int]) -> int: ...


# caller.py
from pkg.callee import takes_list


def process(bar: list[int]) -> None:
    takes_list(list(bar))  # Redundant: bar is already list[int]
```

```python
def echo(value: str) -> str:
    return str(value)  # Redundant: value is already str
```

## Reporting level

`--redundant-type-conversion-level={conservative,permissive}` (default `conservative`) controls how broadly a conversion is flagged:

- **`conservative`** (default): flags only the conversions that can never be a behavior change even when removed — `str`, `int`, `float`, `bool`, `bytes`, `frozenset`, and `tuple` — and only when the wrapped value is already exactly that type.
- **`permissive`**: also flags `list`, `dict`, `set`, and `bytearray` conversions, and a broader class of matches where the wrapped value merely satisfies what the surrounding code expects rather than matching it exactly (e.g. passing an already-`list[str]` value somewhere only an `Iterable[str]` is required). These four constructors normally produce an independent copy of their argument — flagging them by default risks reporting a conversion that's redundant to a type checker but not to code that relies on that copy being distinct from the original (e.g. mutating one without affecting the other).

```yaml
- id: ruff-extra-rules
  args: [--select=redundant-type-conversion]
```

```yaml
# Also catch copy-producing and structural-match conversions:
- id: ruff-extra-rules
  args:
    [
      --select=redundant-type-conversion,
      --redundant-type-conversion-level=permissive,
    ]
```

This check does not support `--fix` — it only reports.

## Cross-file coverage

Catching a redundant conversion at a call site whose parameter type is declared in another file needs both files examined together. A normal commit that only touches the file whose signature changed won't catch a conversion that's now redundant at a call site elsewhere, since pre-commit/prek only pass this hook the files that changed. Run this check over the whole project periodically — for example `pre-commit run --all-files`, or as part of CI — to get its full cross-file coverage rather than relying on incremental per-commit runs alone.

## Suppression

```python
def echo(value: str) -> str:
    return str(value)  # pytriage: ignore=TRI006
```

A line already carrying a third-party type-checker suppression comment — `# type: ignore`, `# pyright: ignore`, `# ty: ignore` — is never flagged either, so a suppression you've already placed for an unrelated reason can't get misreported as a redundant conversion.
