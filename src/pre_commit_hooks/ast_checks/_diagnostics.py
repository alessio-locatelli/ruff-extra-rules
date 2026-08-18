"""Diagnostic formatting: turning a completed run's violations and failures into the printed report and exit code."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from ._base import FixOutcome

if TYPE_CHECKING:
    from ._base import Violation
    from ._orchestrator import CheckOrchestrator


def report(orchestrator: CheckOrchestrator, all_violations: dict[str, list[Violation]]) -> int:
    """Prints every unavailable check, unprocessable file, rule failure, and
    violation from a completed run. `all_violations` is `orchestrator.
    process_files()`'s own return value; `orchestrator` itself is also
    consulted directly for its `unavailable_checks`/`unprocessable_files`/
    `rule_failures` bookkeeping.

    Returns 0 if nothing was printed, 1 otherwise.
    """
    exit_code = 0

    # A check that couldn't run at all (missing/misbehaving prerequisite)
    # is reported once here, not once per file — see CheckUnavailableError's
    # own docstring. Every other check's own violations are still reported
    # normally below; this doesn't discard them.
    for _check_id, message in sorted(orchestrator.unavailable_checks):
        print(f"error: {message}", file=sys.stderr)
        exit_code = 1

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
            if v.fix_outcome is FixOutcome.APPLIED:
                tag = "[FIXED] "
            elif v.fix_outcome is FixOutcome.RESOLVED_INDIRECTLY:
                tag = "[RESOLVED INDIRECTLY] "
            elif v.fix_outcome is FixOutcome.REJECTED:
                tag = "[FIX REJECTED] "
            elif v.fix_outcome is FixOutcome.ERRORED:
                tag = "[FIX ERRORED] "
            elif v.fix_outcome is FixOutcome.FAILED:
                tag = "[FIX FAILED] "
            elif v.fix_outcome is FixOutcome.ABORTED:
                tag = "[FIX ABORTED] "
            elif v.fix_outcome is FixOutcome.DECLINED:
                tag = "[FIX DECLINED] "
            elif v.fixable:
                tag = "[FIXABLE] "
            else:
                tag = ""
            if v.fix_outcome is FixOutcome.RESOLVED_INDIRECTLY:
                hint = " it disappeared as a side effect of another fix in this run; nothing was applied for it."
            elif v.fix_outcome is FixOutcome.APPLIED:
                hint = ""
            elif v.fix_outcome is FixOutcome.REJECTED:
                hint = (
                    " --fix produced invalid syntax, so the change was discarded — this is a bug, "
                    "please report it: https://github.com/alessio-locatelli/ruff-extra-rules/issues"
                )
            elif v.fix_outcome is FixOutcome.ERRORED:
                hint = (
                    " --fix raised an unexpected internal error and was not applied — this is a bug, "
                    "please report it: https://github.com/alessio-locatelli/ruff-extra-rules/issues"
                )
            elif v.fix_outcome is FixOutcome.FAILED:
                hint = (
                    " --fix could not write the file — check file permissions and available disk "
                    "space, then run with --fix again."
                )
            elif v.fix_outcome is FixOutcome.ABORTED:
                hint = (
                    " the file changed on disk while --fix was running, so the change was discarded — "
                    "run with --fix again."
                )
            elif v.fix_outcome is FixOutcome.DECLINED:
                hint = " --fix left the source unchanged because this change is not safe to apply automatically."
            elif v.fixable:
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
