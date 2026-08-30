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
    """Process-level wrapper around `main()`.

    Installs graceful SIGTERM handling and turns a cancellation (Ctrl-C or
    SIGTERM) into a short stderr message and exit code 1 — the same
    non-success code every other incomplete-run outcome already returns
    (see `main()`'s own docstring) — instead of letting a raw
    `KeyboardInterrupt` traceback reach the user.

    Cancellation stops at the next safe opportunity, not instantly: an
    in-flight `atomic_write_text()`/`_write_cache()` call either finishes
    (its temp-file-then-`replace()` rename already committed) or rolls back
    completely (its `finally` clause removes the temp file, leaving the
    real file untouched) before the interrupt can propagate further — never
    partway through a single file's own replacement. Once that safe point
    is reached, no further files are processed.
    """
    _install_sigterm_handler()
    try:
        return (main if entrypoint is None else entrypoint)(argv)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
