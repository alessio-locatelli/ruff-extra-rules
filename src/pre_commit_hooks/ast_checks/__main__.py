"""Entry point for running ast_checks as a module.

Usage: python -m pre_commit_hooks.ast_checks
"""

from __future__ import annotations

import logging
import signal
import sys
from typing import TYPE_CHECKING

from ._cli import main

if TYPE_CHECKING:
    from types import FrameType

logger = logging.getLogger("ast_checks")


def _raise_keyboard_interrupt(_signum: int, _frame: FrameType | None) -> None:
    """SIGTERM handler that reuses Python's own SIGINT->KeyboardInterrupt
    translation path, so a SIGTERM-based cancellation (e.g. `prek`'s own
    timeout, or a CI job killing this process) unwinds through the same
    already-`try`/`finally`-guarded cleanup as Ctrl-C (`atomic_write_text()`,
    `CacheManager._write_cache()`, `CacheManager._locked()`) instead of the
    OS's default action for SIGTERM, which terminates the process
    immediately and runs no Python cleanup at all.
    """
    raise KeyboardInterrupt


def _install_sigterm_handler() -> None:
    """Best-effort: `signal.signal()` raises `ValueError` when called
    outside the main thread, and can raise `OSError` in some restricted or
    sandboxed environments. Either way, Ctrl-C still works via SIGINT's own
    handler (installed by Python itself, independent of this call) — this
    only extends the same graceful shutdown path to SIGTERM, so a failure
    to install it just means one fewer signal is handled gracefully, not a
    reason to abort the run.
    """
    try:
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    except ValueError, OSError:
        logger.debug("Could not install a SIGTERM handler; continuing without one.")


def run(argv: list[str] | None = None) -> int:
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
        return main(argv)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
