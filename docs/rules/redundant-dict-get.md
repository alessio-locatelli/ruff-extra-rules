# redundant-dict-get (TR9)

Reports `dict.get()` where the current local code already proves the requested key exists. TR9 supports one
positional key argument on a direct local-name receiver only. It does not report calls with a default value,
such as `config.get("port", 0)`, or calls on non-local receivers.

## Example

```python
config = {"host": "localhost", "port": 5432}

port = config.get("port")
```

TR9 reports the access because the local dict display includes `"port"`. Write `config["port"]` to express the invariant directly.

It recognizes key-presence facts through direct aliases, branch joins, short-circuit Boolean guards, and structural exits:

```python
if key not in config:
    raise KeyError(key)

port = config.get(key)
```

TR9 also recognizes validated key collections when both the collection and dictionary are local built-ins:

```python
required = {"host", "port"}
config = {"host": "localhost", "port": 5432}

if required <= config.keys():
    for key in required:
        value = config.get(key)
```

Required `TypedDict` fields are key-presence facts, including fields whose values may be `None`:

```python
from typing import TypedDict


class Settings(TypedDict):
    port: int | None


def read(settings: Settings) -> int | None:
    return settings.get("port")
```

TR9 is report-only. It discards a proof after mutation, an unknown call, reassignment, or suspension, and it does not infer facts across comprehension scopes.

## Reporting level

`--redundant-dict-get-level={conservative,aggressive}` defaults to `conservative`.

- `conservative` recognizes local built-in dictionaries and collections, local `TypedDict` declarations, and control-flow proofs.
- `aggressive` additionally trusts a direct `dict[...]` parameter annotation. A subclass can customize mapping operations, so review these reports before changing code.

## Hook

TR9 runs with the dedicated type-aware hook:

```yaml
- id: ruff-extra-rules-ty
```

## Suppression

```python
port = config.get("port")  # pytriage: TR9
```
