from __future__ import annotations

import logging
import signal
import sys
from typing import TYPE_CHECKING

from ._cli import main

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

logger = logging.getLogger("ast_checks")


def _raise_keyboard_interrupt(_signum: int, _frame: FrameType | None) -> None:
    raise KeyboardInterrupt


def _install_sigterm_handler() -> None:
    try:
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    except ValueError, OSError:
        logger.debug("Could not install a SIGTERM handler; continuing without one.")


def run(argv: list[str] | None = None, *, entrypoint: Callable[[list[str] | None], int] | None = None) -> int:
    _install_sigterm_handler()
    try:
        return (main if entrypoint is None else entrypoint)(argv)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
