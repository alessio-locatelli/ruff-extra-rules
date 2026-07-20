"""Command-line interface: argument parsing, wiring the discovery,
orchestrator, and diagnostics layers together, and the process exit code.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import ALL_CHECKS
from ._diagnostics import report
from ._discovery import expand_directories, filter_excluded_files
from ._orchestrator import CheckOrchestrator, load_checks


def main(argv: list[str] | None = None) -> int:
    """Main entry point for grouped AST checks.

    Args:
        argv: Command-line arguments

    Returns:
        0: no violations, and every requested file was read, parsed, and
            checked without error (this includes a `--fix` run that
            resolved every violation — matching the pre-commit convention
            that a hook only reports success when the working tree needs
            no further review, not `ruff check --fix`'s own bare-CLI
            default of exit 0 on a fully-fixed run).
        1: any of — a violation is present in the report (fixed, fixable,
            rejected, errored, or non-fixable; see the tags in each printed
            line); a file couldn't be read, decoded, or parsed
            (`--list-checks` and `--exclude`d files, so also `orchestrator.
            unprocessable_files`); a check raised while analyzing a file
            (`orchestrator.rule_failures`); or invalid CLI input (unknown
            `--select`/`--ignore` check id, or every check disabled).
            `--list-checks` and no-files-to-check return 0 unconditionally,
            before any of the above can apply.

        `argparse` itself calls `sys.exit(2)` directly (bypassing this
        function's own return) for malformed CLI arguments themselves (e.g.
        an unknown flag) — a third, separate value from this function's
        0/1 contract above.
    """
    parser = argparse.ArgumentParser(
        prog="ruff-extra-rules",
        description="Run multiple AST-based checks in a single pass",
    )
    parser.add_argument("filenames", nargs="*", help="Python files to check")
    parser.add_argument(
        "--select",
        help="Comma-separated list of checks to restrict to (default: all)",
    )
    parser.add_argument(
        "--ignore",
        help="Comma-separated list of checks to exclude",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Auto-fix violations where possible",
    )
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="List available checks and exit",
    )
    parser.add_argument(
        "--exclude",
        help="Glob pattern(s) to exclude files/directories (comma-separated)",
    )

    for check_class in ALL_CHECKS:
        check_class.add_cli_arguments(parser)

    args = parser.parse_args(argv)

    if args.list_checks:
        print("Available checks:")
        instances = sorted((cls() for cls in ALL_CHECKS), key=lambda c: c.check_id)
        for check in instances:
            print(f"  - {check.check_id}: {check.error_code}")
        return 0

    if not args.filenames:
        return 0

    # A directory argument (only reachable via direct CLI use — see
    # expand_directories()'s own docstring) must be expanded before
    # anything else touches it, or it silently checks nothing at all.
    filenames = expand_directories(args.filenames)
    if not filenames:
        return 0

    exclude_patterns = []
    if args.exclude:
        exclude_patterns = [p.strip() for p in args.exclude.split(",") if p.strip()]

    filenames = filter_excluded_files(filenames, exclude_patterns)
    if not filenames:
        return 0

    select = {c.strip() for c in args.select.split(",")} if args.select else None
    ignore = {c.strip() for c in args.ignore.split(",")} if args.ignore else None

    all_check_ids = {cls().check_id for cls in ALL_CHECKS}
    if select:
        invalid = select - all_check_ids
        if invalid:
            checks_str = ", ".join(sorted(invalid))
            print(f"Error: Unknown checks: {checks_str}", file=sys.stderr)
            return 1
    if ignore:
        invalid = ignore - all_check_ids
        if invalid:
            checks_str = ", ".join(sorted(invalid))
            print(f"Error: Unknown checks: {checks_str}", file=sys.stderr)
            return 1

    # Each check translates its own parsed CLI args into its own __init__ kwargs, if any.
    check_args: dict[str, dict[str, Any]] = {}
    for check_class in ALL_CHECKS:
        kwargs = check_class.cli_kwargs_from_args(args)
        if kwargs:
            check_args[check_class().check_id] = kwargs

    checks = load_checks(select=select, ignore=ignore, check_args=check_args)

    if not checks:
        print("Error: No checks enabled", file=sys.stderr)
        return 1

    orchestrator = CheckOrchestrator(checks=checks, fix_mode=args.fix)
    all_violations = orchestrator.process_files(filenames)

    return report(orchestrator, all_violations)
