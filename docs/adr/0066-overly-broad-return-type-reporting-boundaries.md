# overly-broad-return-type reports only returns that syntax proves concrete

## Context

The typing documentation's [best practices](https://typing.python.org/en/latest/reference/best_practices.html#arguments-and-return-types) recommend concrete return types (`list`, `dict`) for concrete implementations, and leave the return types of protocols and abstract base classes to case-by-case judgement. ty, mypy, Pyright, and Pyrefly only check that a returned value is compatible with the annotation, so `def f() -> Mapping[str, int]: return MappingProxyType(...)` passes all of them even though the annotation throws away what callers could know.

Two things make the rule hard to apply mechanically. Proving what a function returns needs type inference, which this repository's AST checks don't have. And a narrower return annotation is a contract: on a method that subclasses override, it constrains every override, and syntax alone can't tell a concrete class from a base class that other modules extend.

## Decision

TR13 (`overly-broad-return-type`) is a default-enabled, report-only check in the main hook with a `level` option.

- **Proof.** A function is reported only when its return annotation is one abstract collection type from `collections.abc`, `typing`, or `typing_extensions` (optionally `| None`), and every `return` produces the same concrete type: a display or comprehension, a call to a known constructor or a known producer such as `sorted()`, a conditional expression whose branches agree, or a local variable whose every binding in the function is one of those. Names are resolved through the file's own imports and scopes; any ambiguous binding (including one a nested function rebinds through `nonlocal`), shadowing, wildcard import, possible builtins rebinding, or assignment to the module attribute a name refers to leaves the function unreported. A possible implicit `return None` counts as a `None` return. Generators are never reported.
- **Which functions.**
  - Both levels skip overload implementations, abstract methods, conditionally redefined functions, dunder methods, and every method of a `Protocol` or ABC.
  - `conservative` (default) checks functions and the methods no subclass can override: `@final` methods, methods of `@final` classes, and name-mangled methods.
  - `aggressive` also checks other methods, but only when every class in the file has bases the check can resolve, the class's local inheritance hierarchy is fully known (its ancestors, its descendants, and those descendants' other bases are all local or trivial: `object`, `Generic`), no other class in that hierarchy binds the same name, and the method isn't a placeholder that only returns an empty container. A sibling base that binds the name counts because narrowing one of two inherited definitions can make them incompatible.
- **Report only.** The message names the concrete type and, when the type arguments carry over one-to-one, the full annotation to use (`Sequence[int]` becomes `list[int]` or `tuple[int, ...]`). It leaves the type arguments out when a return goes through a local variable, because the variable's inferred element type may be narrower than the annotation's. There is no autofix.

## Considered Options

- **ty-assisted inference**: deferred. Asking ty for each return expression's type would also cover attribute and call results, but it would move the check into the serial, uncached ty hook, and turning ty's displayed types back into valid annotations and imports is a separate problem. It is tracked as a follow-up rather than blocking the syntactic version.
- **Functions only**: rejected. It misses `@final` and name-mangled methods, where narrowing is as safe as for a function.
- **All methods, default-enabled**: rejected. Base-class defaults meant to be overridden elsewhere would be reported, and narrowing them constrains every subclass.
- **Autofix**: rejected. The check fires rarely, and each report takes a minute to fix by hand, while a correct rewrite must spell the concrete type so that it resolves in the annotation's scope wherever the original did, invent type arguments when the abstract type had none (`Sized` → `list[...]`), and avoid creating a type error where a local variable's inferred element type is narrower than the annotation's. Each of those needs analysis the check doesn't otherwise do.
- **A separate rule for overly restrictive parameter types**: out of scope. It needs usage analysis rather than return analysis, and is a different check.

## Consequences

- The check misses returns it can't prove syntactically, such as `return self._items` or the result of an arbitrary call.
- `aggressive` remains a heuristic for methods: a class extended only from other modules still looks concrete.
