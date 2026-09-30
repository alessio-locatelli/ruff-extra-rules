# defined-far-from-use (TR11)

Reports a local variable that is assigned long before the code that needs it: either before an early `return`, `raise`, `continue`, or `break` that skips its use, or several unrelated statements before its first use.

## Why?

An assignment before an early exit does work that is thrown away whenever the exit is taken. An assignment far from its first use makes the reader keep the value in mind across unrelated code. Moving the assignment down to where the value is needed fixes both.

## Example

Reported at the `aggressive` level, because moving a call changes when it runs:

```python
def notify(user):
    message = render_template("welcome", user)

    if not user.email:
        return

    send(user.email, message)
```

Move the assignment below the early exit instead:

```python
def notify(user):
    if not user.email:
        return

    message = render_template("welcome", user)
    send(user.email, message)
```

The same applies without an early exit when many unrelated statements separate the assignment from its first use:

```python
def count_rows(rows):
    count = 0
    log_start()
    reset_counters()
    open_report()
    warm_cache()
    flush_logs()
    load_settings()
    for row in rows:
        count += 1
    return count
```

Statements that compute other values used by the same statement don't count toward the distance.

## Options

- `level`:
  - `conservative` (default) only reports values that are safe to move, such as literals, empty containers, and other local variables that are already set and that the code in between doesn't reassign.
  - `aggressive` also reports calls and attribute reads. Moving those changes when they run, so review each report. The check still leaves alone values that share state with the code in between, and calls that look like validation.
- `max-distance` (default `5`): the most unrelated statements allowed between an assignment and its first use.

```toml
[tool.ruff-extra-rules.defined-far-from-use]
level = "aggressive"
max-distance = 8
```

TR11 only looks inside functions. It skips variables that are read anywhere else, such as in an `except` or `finally` block, after a loop, in the next loop iteration, or in a closure. It also skips variables declared `global` or `nonlocal`, and functions that call `locals()`, `vars()`, `exec()`, or `eval()`.

## Suppression

```python
message = render_template("welcome", user)  # pytriage: TR11
```

TR11 is report-only.
