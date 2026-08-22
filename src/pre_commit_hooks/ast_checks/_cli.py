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
        "--extend-select",
        action="append",
        help="Comma-separated list of checks to add to the selected checks; may be repeated",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        help="Comma-separated list of checks to exclude; may be repeated",
    )
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

    for check_class in ALL_CHECKS:
        add_check_arguments(parser, check_class().check_id, check_class.OPTIONS)

    args = parser.parse_args(argv)

    if args.verbose:
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

    filenames = expand_directories(args.filenames)
    if not filenames:
        return 0

    filenames = filter_excluded_files(filenames, config.exclude)
    if not filenames:
        return 0

    checks = load_checks(
        select=config.select,
        extend_select=config.extend_select,
        ignore=config.ignore,
        check_args=config.check_kwargs,
        check_classes=enabled_check_classes,
    )

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
