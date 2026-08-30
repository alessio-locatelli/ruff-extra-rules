from __future__ import annotations

import sys
from pathlib import Path

from .daemon import _serve


def _main(argv: list[str]) -> int:
    _serve(Path(argv[0]).resolve())
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
