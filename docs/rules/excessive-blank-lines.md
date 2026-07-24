# excessive-blank-lines (TRI002)

Collapses multiple consecutive blank lines after module headers (copyright, docstrings, or comments) to a single blank line.

## Why?

Excessive blank lines after module headers create visual clutter and violate PEP 8 conventions.

## Example

```python
# Copyright (c) 2025 Example Corp
# All rights reserved.
#
# Licensed under the MIT License



import os  # Bad - 3 blank lines
```

Fixed:
```py
# Copyright (c) 2025 Example Corp
# All rights reserved.
#
# Licensed under the MIT License

import os  # Good - 1 blank line
```

## Features

- Detects 2+ blank lines after module header
- Preserves copyright comment spacing (1 blank line after copyright)
- Only affects module-level blank lines, preserves function/class spacing
- Maintains file encoding and handles different line ending styles
- Inline suppression with `# pytriage: ignore=TRI002`, placed on the first code line after the blank run (the violation's own line is blank and can't carry a trailing comment)
