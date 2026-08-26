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

It also recognizes direct membership guards:

```python
if key not in config:
    raise KeyError(key)

port = config.get(key)
```

TR9 is conservative. It does not follow aliases, calls, mutations, loops, or complex control flow, and it never rewrites source automatically.

## Hook

TR9 runs with the dedicated type-aware hook:

```yaml
- id: ruff-extra-rules-ty
```

## Suppression

```python
port = config.get("port")  # pytriage: TR9
```
