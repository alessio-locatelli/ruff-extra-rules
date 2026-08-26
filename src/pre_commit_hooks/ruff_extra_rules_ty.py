from __future__ import annotations

import sys

from .ast_checks import ALL_CHECKS, UnusedPytriageCheck
from .ast_checks.__main__ import run
from .ast_checks._cli import main as run_checks
from .ast_checks._ty_hook import belongs_to_ty_hook

_CHECKS = [*(check_class for check_class in ALL_CHECKS if belongs_to_ty_hook(check_class)), UnusedPytriageCheck]


def main(argv: list[str] | None = None) -> int:
    return run_checks(argv, check_classes=_CHECKS)


if __name__ == "__main__":
    sys.exit(run(entrypoint=main))
