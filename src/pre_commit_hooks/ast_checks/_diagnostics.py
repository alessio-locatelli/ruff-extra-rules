"""Diagnostic formatting: turning a completed run's violations and failures into the printed report and exit code."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from ._base import is_fix_errored, is_fix_failed, is_fix_rejected, is_fixed

if TYPE_CHECKING:
    from ._base import Violation
    from ._orchestrator import CheckOrchestrator


def report(orchestrator: CheckOrchestrator, all_violations: dict[str, list[Violation]]) -> int:
    """Prints every unprocessable file, rule failure, and violation from a
    completed run. `all_violations` is `orchestrator.process_files()`'s own
    return value; `orchestrator` itself is also consulted directly for its
    `unprocessable_files`/`rule_failures` bookkeeping.

    Returns 0 if nothing was printed, 1 otherwise.
    """
    exit_code = 0

    # A file that couldn't be read or parsed must never look identical to a
    # clean file: report it and fail the run, rather than letting it vanish
    # from all_violations with only a debug log line as evidence.
    for filepath in sorted(orchestrator.unprocessable_files):
        print(f"{filepath}: error: could not be read or parsed; file skipped", file=sys.stderr)
        exit_code = 1

    # A check that crashes on every file it sees must not look like a clean
    # run merely because no other check reported anything for the same
    # files — report the specific check and file, and fail the run.
    for filepath, check_id in sorted(orchestrator.rule_failures):
        print(
            f"{filepath}: error: check '{check_id}' raised an unexpected exception; "
            "its results for this file may be incomplete",
            file=sys.stderr,
        )
        exit_code = 1

    for filepath, violations in sorted(all_violations.items()):
        for v in violations:
            fixed = is_fixed(v)
            rejected = is_fix_rejected(v)
            errored = is_fix_errored(v)
            failed = is_fix_failed(v)
            if fixed:
                tag = "[FIXED] "
            elif rejected:
                tag = "[FIX REJECTED] "
            elif errored:
                tag = "[FIX ERRORED] "
            elif failed:
                tag = "[FIX FAILED] "
            elif v.fixable:
                tag = "[FIXABLE] "
            else:
                tag = ""
            if rejected:
                hint = (
                    " --fix produced invalid syntax, so the change was discarded — this is a bug, "
                    "please report it: https://github.com/alessio-locatelli/ruff-extra-rules/issues"
                )
            elif errored:
                hint = (
                    " --fix raised an unexpected internal error and was not applied — this is a bug, "
                    "please report it: https://github.com/alessio-locatelli/ruff-extra-rules/issues"
                )
            elif failed:
                hint = (
                    " --fix could not write the file — check file permissions and available disk "
                    "space, then run with --fix again."
                )
            elif v.fixable and not fixed:
                hint = " Run with --fix to inline automatically."
            else:
                hint = ""
            # Violation.col is a 0-based character offset (matching Python's
            # own ast.lineno being 1-based but ast.col_offset being
            # 0-based); +1 here reports the conventional 1-based column
            # most editors and other diagnostic tools (including ruff
            # itself) use, so "the first character of the line" reads as
            # column 1, not 0.
            print(
                f"{filepath}:{v.line}:{v.col + 1}: {v.error_code}: {tag}{v.message}{hint}",
                file=sys.stderr,
            )
            exit_code = 1

    return exit_code
