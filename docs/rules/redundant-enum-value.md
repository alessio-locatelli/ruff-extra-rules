# redundant-enum-value (TR10)

Reports `.value` on a directly declared member of a same-file `StrEnum` or `IntEnum`. Those members already
behave as `str` or `int` values, so passing the member preserves its enum identity while satisfying ordinary
string or integer APIs.

## Example

```python
from enum import StrEnum


class Status(StrEnum):
    READY = "ready"


send_status(Status.READY.value)
```

Write the member directly instead:

```python
send_status(Status.READY)
```

TR10 only reports direct local declarations and members. It leaves imported enum bases, indirect inheritance,
aliases, rebindings, unknown members, and other `.value` attributes unchanged.

## Suppression

Use an inline suppression when an API requires the exact built-in value rather than the enum member:

```python
send_exact_string(Status.READY.value)  # pytriage: TR10
```

TR10 is report-only.
