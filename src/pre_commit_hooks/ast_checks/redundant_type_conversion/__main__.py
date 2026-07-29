"""Entry point for `python -m pre_commit_hooks.ast_checks.redundant_type_conversion`: runs the persistent
`ty` daemon server (see `daemon.py` and ADR-0041). `_spawn_daemon()` is this module's own, sole caller.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .daemon import _serve


def _main(argv: list[str]) -> int:
    _serve(Path(argv[0]).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
