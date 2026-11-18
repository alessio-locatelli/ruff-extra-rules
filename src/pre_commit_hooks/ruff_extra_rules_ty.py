from __future__ import annotations

import sys

from .ast_checks.__main__ import run
from .ast_checks._cli import main as run_checks
from .ast_checks._hooks import HOOK_CHECKS, TY_HOOK_ID


def main(argv: list[str] | None = None) -> int:
    return run_checks(argv, check_classes=HOOK_CHECKS[TY_HOOK_ID])


if __name__ == "__main__":
    sys.exit(run(entrypoint=main))
