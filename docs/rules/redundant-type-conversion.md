# redundant-type-conversion (TR6)

Flags a builtin type/collection conversion call — `str(...)`, `list(...)`, and similar — that `ty` considers redundant given the real, statically-known type of the value it wraps, including when that value's type is declared in a different file.

## Why?

A conversion call like `str(x)` or `list(bar)` sometimes wraps a value that's already exactly the type being converted to. That adds indirection without changing anything. This check catches that case even when it spans a call site and an imported function's own parameter type — the same value passed into a function whose parameter is already the right type.

## Prerequisite: `ty`

This check delegates detection to [Astral's `ty`](https://github.com/astral-sh/ty) type checker, run as a background language-server process. It needs `ty` available on `PATH`:

```bash
uv tool install ty
```

or as your own project's dev dependency. If `ty` isn't found, or an installed `ty` doesn't behave the way this check expects, the check fails as soon as a file actually gives it something to flag, with a message explaining which of the two happened and what to do about it — never a silent, empty result. A run over files with nothing for this check to report never needs `ty` at all.

Run this check with your project's own virtual environment active. `ty` resolves whichever version is first on `PATH` at that moment, and needs your project's own dependencies importable to correctly infer their types — an inactive shell can silently pick up a different, unrelated `ty` install (with different diagnostics, since `ty` is pre-1.0) instead of the one pinned as your project's own dev dependency.

Also run it from inside the project being checked. `ty` resolves its own workspace root from the current working directory, so invoking this check's CLI directly (rather than through prek/pre-commit, which always runs a hook from the repo it's checking) against a file outside the current directory points `ty` at the wrong project — it won't see that project's own dependencies or configuration, which can change its diagnostics.

This check keeps `ty` running in a small background process, stored under your project's own `.cache/pre_commit_hooks/` directory (see "Cross-file coverage" below for why). If your environment can't run a background process that outlives the current commit — some sandboxed CI runners, for example — this check falls back to a private, per-commit `ty` session automatically; you don't need to configure anything either way.

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

- **`conservative`** (default): flags only the lowest-risk conversions — `str`, `int`, `float`, `bool`, `bytes`, `frozenset`, and `tuple` — and only when the wrapped value is already exactly that type. These seven avoid mutable-copy semantics: none of them produce a mutable result, so removing the call can't turn a distinct copy into a shared, mutable reference. (`tuple` and `frozenset` do return the same object as their argument when it's already exactly that type, but since both are immutable, that shared identity is harmless.) "already exactly that type" is a static/declared type, not a runtime guarantee — a value declared `int` that's actually holding a `bool` (or another subclass) at runtime is a case this level can't distinguish, and removing the conversion there would change the runtime value.
- **`permissive`**: also flags `list`, `dict`, `set`, and `bytearray` conversions, and a broader class of matches where the wrapped value merely satisfies what the surrounding code expects rather than matching it exactly (e.g. passing an already-`list[str]` value somewhere only an `Iterable[str]` is required). These four constructors normally produce an independent copy of their argument — flagging them by default risks reporting a conversion that's redundant to a type checker but not to code that relies on that copy being distinct from the original (e.g. mutating one without affecting the other, or relying on it to deduplicate). `permissive` also broadens matching enough that it can occasionally flag a conversion whose wrapped value isn't really compatible with the constructor at all, when the surrounding code accepts a very wide range of types (e.g. assigning to something typed `object`) and so doesn't distinguish one from the other either way — review a `permissive`-only report before removing the call.

This check has its own dedicated `ruff-extra-rules-ty` hook (see the main [README](../../README.md#installation)) rather than running through `ruff-extra-rules` itself: it shares one persistent `ty` session across files (see "Cross-file coverage" below), so it runs as a single serial process instead of alongside pre-commit/prek's own parallel-batched workers.

```yaml
- id: ruff-extra-rules-ty
```

```yaml
# Also catch copy-producing and structural-match conversions:
- id: ruff-extra-rules-ty
  args: [--redundant-type-conversion-level=permissive]
```

This check does not support `--fix` — it only reports.

## Cross-file coverage

Catching a redundant conversion at a call site whose parameter type is declared in another file needs both files examined together — but not necessarily in the same commit. This check keeps a small background process running between commits specifically for this: once it has seen both the call site and the file whose signature changed at least once, a later commit that only touches the signature still re-examines every call site that depends on it, with no extra configuration needed. The background process is scoped to your project, stops itself automatically after a period of inactivity, and restarts transparently if `ty` itself gets upgraded.

The very first time this check runs in a project, or after the background process has been idle long enough to stop itself, it hasn't seen every file yet — a conversion depending on a file it's never examined won't be caught until that file is checked at least once. Running this check over the whole project once, for example `pre-commit run --all-files`, gives it full cross-file coverage immediately rather than waiting for normal commits to build it up.

## Suppression

```python
def echo(value: str) -> str:
    return str(value)  # pytriage: TR6
```

A line already carrying a third-party type-checker suppression comment — `# type: ignore`, `# pyright: ignore`, `# ty: ignore` — is never flagged either, so a suppression you've already placed for an unrelated reason can't get misreported as a redundant conversion.

## Known false positive

A conversion whose argument is a binary expression can be reported using the type of the expression's **rightmost operand** instead of the expression's own type. The clearest case is `str(some_path / name)`, where `some_path` is a `Path` and `name` is a `str`: the conversion is required, but it gets reported as though the argument were already a `str`. Suppress it with `# pytriage: TR6` for now. Tracked in [issue #155](https://github.com/alessio-locatelli/ruff-extra-rules/issues/155).
