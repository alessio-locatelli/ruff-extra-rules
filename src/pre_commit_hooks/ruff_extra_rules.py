from __future__ import annotations

import sys

from .ast_checks import ALL_CHECKS, RedundantTypeConversionCheck
from .ast_checks.__main__ import run
from .ast_checks._cli import main as run_checks

_CHECKS = [check_class for check_class in ALL_CHECKS if check_class is not RedundantTypeConversionCheck]


def main(argv: list[str] | None = None) -> int:
    return run_checks(argv, check_classes=_CHECKS, allow_check_selection=False)


if __name__ == "__main__":
    sys.exit(run(entrypoint=main))
