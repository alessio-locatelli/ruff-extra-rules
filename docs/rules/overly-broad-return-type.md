# overly-broad-return-type (TR13)

Reports a return annotation that names an abstract collection type, such as `Mapping`, `Sequence`, or `Iterable`, when every `return` in the function produces the same concrete type.

## Why?

The typing documentation [recommends](https://typing.python.org/en/latest/reference/best_practices.html#arguments-and-return-types) concrete return types for concrete implementations. Type checkers accept the broader annotation, but callers lose what they could otherwise rely on, such as appending to a `list` or the read-only view a `MappingProxyType` gives them.

## Example

```python
from collections.abc import Mapping
from types import MappingProxyType


def defaults() -> Mapping[str, int]:
    return MappingProxyType({"timeout": 30})
```

Annotate the type the function actually returns:

```python
def defaults() -> MappingProxyType[str, int]:
    return MappingProxyType({"timeout": 30})
```

TR13 recognizes literals, comprehensions, calls such as `dict(...)`, `tuple(...)`, `sorted(...)`, `MappingProxyType(...)`, `defaultdict(...)`, or `deque(...)`, and local variables assigned only those. It leaves a function alone when any `return` produces something else, such as an attribute, a parameter, or another function's result, and it never reports generators, overloads, abstract methods, or methods of a `Protocol` or abstract base class.

## Options

- `level`:
  - `conservative` (default) checks functions, `@final` methods, methods of `@final` classes, and name-mangled (`__name`) methods: the code no subclass can override.
  - `aggressive` also checks other methods, as long as the class's bases are defined in the same file and no related class there overrides the method or inherits another method with the same name alongside it. A class that other modules extend can still be reported, so review each report.

```toml
[tool.ruff-extra-rules.overly-broad-return-type]
level = "aggressive"
```

TR13 is report-only. When the abstract type's type arguments carry over, the message gives the full replacement annotation, such as `list[int]` for `Sequence[int]`. When the returned value goes through a local variable, as in `out = []` … `return out`, the message names only the concrete type: a type checker may have given the variable a narrower element type than the annotation, so the right type arguments, and whether the variable needs an annotation too, are up to you.

## Suppression

```python
def defaults() -> Mapping[str, int]:  # pytriage: TR13
    return MappingProxyType({"timeout": 30})
```
