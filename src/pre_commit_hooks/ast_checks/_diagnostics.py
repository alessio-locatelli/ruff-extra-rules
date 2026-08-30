from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from ._base import FixOutcome

if TYPE_CHECKING:
    from ._base import Violation
    from ._orchestrator import CheckOrchestrator


def report(orchestrator: CheckOrchestrator, all_violations: dict[str, list[Violation]]) -> int:
    exit_code = 0

    for _check_id, message in sorted(orchestrator.unavailable_checks):
        print(f"error: {message}", file=sys.stderr)
        exit_code = 1

    for filepath in sorted(orchestrator.unprocessable_files):
        print(f"{filepath}: error: could not be read or parsed; file skipped", file=sys.stderr)
        exit_code = 1

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
            print(
                f"{filepath}:{v.line}:{v.col + 1}: {v.error_code}: {tag}{v.message}{hint}",
                file=sys.stderr,
            )
            exit_code = 1

    return exit_code
