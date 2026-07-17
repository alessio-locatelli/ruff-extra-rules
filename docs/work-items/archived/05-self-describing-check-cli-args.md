# Self-describing per-check CLI arguments

Status: Open
Kind: Coupling / extensibility

## Problem

`--forbid-vars-names` is hardcoded into the shared orchestrator's `main()`
argparse setup (`ast_checks/__init__.py:492-495`) — the only per-check config
surface not expressed through the `ASTCheck` Protocol. Every other
check-specific concern (`check_id`, `error_code`, `get_prefilter_pattern`) is
self-described by the check class itself; this one option instead requires
editing the shared, check-agnostic `main()` function directly, and there's no
general mechanism for a future check to declare its own CLI arguments.

## Proposed Fix

Add an optional `add_cli_arguments(parser)` classmethod to the `ASTCheck`
Protocol. `main()` calls it generically for every class in `ALL_CHECKS`
before `parse_args()`. Migrate `--forbid-vars-names` onto it as the first
(and currently only) real usage.

## Priority

Medium.
