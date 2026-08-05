from __future__ import annotations

import sys

from .ast_checks import RedundantTypeConversionCheck
from .ast_checks.__main__ import run
from .ast_checks._cli import main as run_checks

_CHECKS = [RedundantTypeConversionCheck]


def main(argv: list[str] | None = None) -> int:
    return run_checks(argv, check_classes=_CHECKS)


if __name__ == "__main__":
    sys.exit(run(entrypoint=main))
