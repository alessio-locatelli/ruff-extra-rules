"""Command-line interface: argument parsing, wiring the configuration,
discovery, orchestrator, and diagnostics layers together, and the process
exit code.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import TYPE_CHECKING

from pre_commit_hooks._cache import CacheManager

from . import ALL_CHECKS
from ._config import resolve
from ._diagnostics import report
from ._discovery import expand_directories, filter_excluded_files
from ._options import ConfigError, add_check_arguments
from ._orchestrator import CheckOrchestrator, load_checks

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ._base import ASTCheck


def main(
    argv: list[str] | None = None,
    *,
    check_classes: Sequence[type[ASTCheck]] | None = None,
) -> int:
    """Main entry point for grouped AST checks.

    Args:
        argv: Command-line arguments

    Returns:
        0: no violations, and every requested file was read, parsed, and
            checked without error (this includes a `--fix` run that
            resolved every violation — matching the pre-commit convention
            that a hook only reports success when the working tree needs
            no further review, not `ruff check --fix`'s own bare-CLI
            default of exit 0 on a fully-fixed run). A selection that
            enables no checks at all also returns 0, having checked
            nothing, the same way `ruff check` does with `select = []`.
        1: any of — a violation is present in the report (fixed, fixable,
            rejected, errored, or non-fixable; see the tags in each printed
            line); a file couldn't be read, decoded, or parsed
            (`--list-checks` and `--exclude`d files, so also `orchestrator.
            unprocessable_files`); a check raised while analyzing a file
            (`orchestrator.rule_failures`); or a check raised
            `CheckUnavailableError` (printed once here, not once per file —
            every other check's own results are still reported normally).
            `--list-checks` and no-files-to-check return 0 unconditionally,
            before any of the above can apply.
        2: the configuration itself is invalid, so nothing was checked —
            malformed TOML, an unknown field or value in
            `[tool.ruff-extra-rules]`, an unknown check id in
            `--select`/`--ignore`/`--per-file-ignores`, a file pattern that
            doesn't compile, or an unreadable `--config` path. This is
            the same code `argparse` itself exits with (bypassing this
            function's own return) for a malformed argument such as an
            unknown flag. See `docs/adr/0045-pyproject-toml-configuration.md`.
    """
    parser = argparse.ArgumentParser(
        prog="ruff-extra-rules",
        description="Run multiple AST-based checks in a single pass",
    )
    parser.add_argument("filenames", nargs="*", help="Python files to check")
    parser.add_argument(
        "--select",
        action="append",
        help="Comma-separated list of checks to restrict to (default: all); may be repeated",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        help="Comma-separated list of checks to exclude; may be repeated",
    )
    # default=None throughout, so an unset flag stays distinguishable from
    # one explicitly set to its default — otherwise argparse's own default
    # would outrank the pyproject.toml value it must lose to.
    parser.add_argument(
        "--fix",
        action="store_true",
        default=None,
        help="Auto-fix violations where possible",
    )
    parser.add_argument(
        "--no-fix",
        action="store_false",
        dest="fix",
        help="Report violations without fixing them, overriding `fix` in the configuration file",
    )
    parser.add_argument(
        "--list-checks",
        action="store_true",
        help="List available checks and exit",
    )
    parser.add_argument(
        "--exclude",
        help="Glob pattern(s) to exclude files/directories (comma-separated), relative to the working directory",
    )
    parser.add_argument(
        "--per-file-ignores",
        action="append",
        help=(
            "Comma-separated `<file pattern>:<check>` pairs switching a check off in the files it matches, "
            "relative to the working directory; an initial `!` negates the pattern; may be repeated"
        ),
    )
    parser.add_argument(
        "--config",
        help="Path to a pyproject.toml to use, instead of searching for one",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Ignore any configuration file and use defaults plus these arguments",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help=(
            "Enable debug logging to stderr, e.g. the underlying exception "
            "behind a reported check/read/cache failure. Never changes "
            "which violations are reported or how --fix behaves."
        ),
    )

    enabled_check_classes = ALL_CHECKS if check_classes is None else check_classes

    # Every check's options, not just the ones this entry point runs; see ADR-0045.
    for check_class in ALL_CHECKS:
        add_check_arguments(parser, check_class().check_id, check_class.OPTIONS)

    args = parser.parse_args(argv)

    if args.verbose:
        # Every debug-level logger.debug(..., exc_info=True) call in this
        # codebase is deliberately silent by default (see e.g.
        # _orchestrator.py's _read_source docstring) so a self-healing
        # fallback or a cleanly-reported failure doesn't also dump a raw
        # traceback onto stderr — this is the opt-in that surfaces them.
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)

    if args.list_checks:
        print("Available checks:")
        instances = sorted((cls() for cls in enabled_check_classes), key=lambda c: c.check_id)
        for check in instances:
            print(f"  - {check.check_id}: {check.error_code}")
        return 0

    try:
        config = resolve(args, enabled_check_classes=enabled_check_classes, all_check_classes=ALL_CHECKS)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if not args.filenames:
        return 0

    # A directory argument (only reachable via direct CLI use — see
    # expand_directories()'s own docstring) must be expanded before
    # anything else touches it, or it silently checks nothing at all.
    filenames = expand_directories(args.filenames)
    if not filenames:
        return 0

    filenames = filter_excluded_files(filenames, config.exclude)
    if not filenames:
        return 0

    checks = load_checks(
        select=config.select,
        ignore=config.ignore,
        check_args=config.check_kwargs,
        check_classes=enabled_check_classes,
    )

    # A selection that leaves this entry point with nothing to run is a
    # legitimate outcome, not an error: one project-wide `select` is shared
    # by both hooks, each of which can only run its own subset.
    if not checks:
        return 0

    orchestrator = CheckOrchestrator(
        checks=checks,
        fix_mode=config.fix,
        cache_dir=config.root / CacheManager.DEFAULT_CACHE_DIR,
        per_file_ignores=config.per_file_ignores,
    )
    all_violations = orchestrator.process_files(filenames)

    return report(orchestrator, all_violations)
